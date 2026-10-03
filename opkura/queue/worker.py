"""P4.3 — the stage worker: converse frees the slot, the judge is a stage.

One worker process consumes one or more stages. Per claimed entry:

    lease CAS (collapses dispatch's duplicate slack)
      → heartbeat task (the reaper's liveness signal)
        → stage handler
      → ack + release
    on handler failure → retry_or_dead (the single failure policy)

Stage handlers:

* **converse** — run the conversation WITHOUT the judge (skip_evaluation), so
  the expensive slot (a phone line, an OSV session) frees the moment the call
  ends. Persist transcript + observations on the row, flip to 'evaluating',
  enqueue the evaluate stage on the same lane.
* **evaluate** — rebuild the CallResult from the row, run assertions always,
  and the LLM judge per `judge_policy`: 'always'; 'sampled' (deterministic by
  run_uuid, so retries don't flip the coin); 'assertions-only' (never for
  passing runs). Whatever the policy, a FAILING run is always LLM-judged.
  Persist verdict + passed, enqueue settle.
* **settle** — the terminal write: completed/failed + finished_at. (Budget
  release lands here when P4.4 wires budgets.)

Graceful drain (P4.7) extends `run_forever`; the handlers themselves are
already restart-safe because every stage transition is row-first.
"""
from __future__ import annotations

import asyncio
import logging
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from opkura.core.config import Settings
from opkura.core.models import CallResult, CallStatus, TranscriptEntry
from opkura.db.models import Run, Scenario as ScenarioRow
from opkura.queue.admission import ExecutionBudget, budget_keys, load_config_async
from opkura.queue.leases import LeaseManager, retry_or_dead
from opkura.queue.streams import Entry, StageQueue

logger = logging.getLogger(__name__)

JUDGE_POLICIES = ("always", "sampled", "assertions-only")


def sampled_in(run_uuid: uuid_mod.UUID, rate: float) -> bool:
    """Deterministic per-run sampling: a retried run gets the same answer,
    so attempt N and attempt N+1 cannot disagree about whether to judge."""
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return (int(run_uuid.hex[:8], 16) % 10_000) < int(rate * 10_000)


class StageWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        engine: Engine,
        queue: StageQueue,
        consumer: str,
        stages: Sequence[str] = ("converse-text", "evaluate", "settle"),
        runner_factory: Optional[Callable[[], object]] = None,
        judge_sample_rate: float = 0.1,
    ):
        self.settings = settings
        self.engine = engine
        self.queue = queue
        self.consumer = consumer
        self.stages = tuple(stages)
        self.judge_sample_rate = judge_sample_rate
        self.leases = LeaseManager(
            queue.redis, consumer=consumer, lease_s=settings.queue_lease_s
        )
        # P4.4 — the per-assistant budget is enforced HERE, where the calls
        # are placed, so it holds across workers.
        self.budget = ExecutionBudget(queue.redis)
        self.defer_backoff_s = 0.05
        self._stopping = False
        self._runner_factory = runner_factory
        self._handlers = {
            "converse-text": self._converse,
            "converse-voice": self._converse,
            "evaluate": self._evaluate,
            "settle": self._settle,
        }

    # ---- runner construction ------------------------------------------------

    def _conversation_runner(self):
        """A TestRunner that converses but never judges — the judge is a stage."""
        if self._runner_factory is not None:
            return self._runner_factory()
        from opkura.orchestrator.runner import TestRunner

        settings = self.settings.model_copy(update={"skip_evaluation": True})
        # persist_result=False: the queue owns the canonical `runs` row; the
        # runner writing its own would double-account every call in the store
        # (one CLI-shaped duplicate per queue run, transcriptless).
        return TestRunner(settings, per_run_debug_log=False,
                          persist_result=False)

    def _judge_runner(self):
        """A TestRunner used only for _run_assertions/_run_evaluator."""
        if self._runner_factory is not None:
            return self._runner_factory()
        from opkura.orchestrator.runner import TestRunner

        return TestRunner(self.settings, per_run_debug_log=False)

    # ---- the claim cycle ----------------------------------------------------

    async def run_forever(
        self,
        *,
        slots: int = 4,
        interactive_reserve: int = 1,
        idle_sleep_s: float = 0.2,
    ) -> None:
        """The worker loop: `slots` concurrent entries, with P4.6's lane
        policy — batch work may occupy at most `slots - interactive_reserve`
        of them, so an interactive run submitted mid-batch always finds a
        slot within one cycle instead of queueing behind 500 batch entries.
        Interactive runs may use ANY slot (the reserve is a floor for them,
        not a ceiling).

        `request_stop()` (P4.7) flips the loop into drain: no new claims,
        in-flight entries finish, then the loop returns. A stop requested
        BEFORE the loop starts is honoured too (no reset here).
        """
        in_flight: dict[asyncio.Task, str] = {}   # task -> lane
        batch_cap = max(0, slots - max(0, interactive_reserve))

        try:
            while True:
                for task in [t for t in in_flight if t.done()]:
                    lane = in_flight.pop(task)
                    if task.exception() is not None:  # pragma: no cover — process() catches
                        logger.error("worker task (%s lane) crashed: %r",
                                     lane, task.exception())

                if self._stopping:
                    if not in_flight:
                        return
                    await asyncio.wait(set(in_flight),
                                       return_when=asyncio.FIRST_COMPLETED)
                    continue

                free = slots - len(in_flight)
                if free <= 0:
                    await asyncio.wait(set(in_flight),
                                       return_when=asyncio.FIRST_COMPLETED)
                    continue

                batch_busy = sum(1 for lane in in_flight.values() if lane == "batch")
                batch_room = max(0, batch_cap - batch_busy)
                claimed: list[Entry] = []
                for stage in self.stages:
                    if len(claimed) >= free:
                        break
                    # Interactive may take any free slot; batch only its
                    # remaining share of `slots - reserve` (the reserved
                    # slice is a floor for interactive, a ceiling for batch).
                    got = await self.queue.claim(
                        stage, consumer=self.consumer, lanes=("interactive",),
                        count=free - len(claimed), block_ms=20,
                    )
                    claimed.extend(got)
                    want_batch = min(free - len(claimed), batch_room)
                    if want_batch > 0:
                        got = await self.queue.claim(
                            stage, consumer=self.consumer, lanes=("batch",),
                            count=want_batch, block_ms=20,
                        )
                        batch_room -= len(got)
                        claimed.extend(got)
                if not claimed:
                    await asyncio.sleep(idle_sleep_s)
                    continue
                for entry in claimed:
                    task = asyncio.create_task(self.process(entry))
                    in_flight[task] = entry.lane
        finally:
            # However the loop exits, never abandon claimed work silently.
            if in_flight:
                await asyncio.wait(set(in_flight))

    def request_stop(self) -> None:
        """Stop claiming; drain in-flight; run_forever returns when empty."""
        self._stopping = True

    async def run_once(self, *, block_ms: int = 200) -> int:
        """Claim and process up to one entry per stage. Returns entries handled."""
        handled = 0
        for stage in self.stages:
            entries = await self.queue.claim(
                stage, consumer=self.consumer, count=1, block_ms=block_ms
            )
            for entry in entries:
                await self.process(entry)
                handled += 1
        return handled

    async def process(self, entry: Entry) -> str:
        """One entry, exactly-once by lease. Returns the disposition."""
        if not await self.leases.acquire(entry.run_uuid, stage=entry.stage):
            # A live lease means either a duplicate entry (dispatch slack) or
            # another worker mid-flight. Either way this ENTRY is redundant —
            # the row is the record and its owner will settle it.
            logger.info(
                "duplicate entry for run %s (%s) — dropped", entry.run_uuid, entry.stage
            )
            await self.queue.ack(entry)
            return "duplicate"

        heartbeat = asyncio.create_task(self._heartbeat_forever(entry.run_uuid))
        disposition = "done"
        try:
            outcome = await self._handlers[entry.stage](entry)
            await self.queue.ack(entry)
            if outcome == "defer":
                disposition = "deferred"
        except Exception as exc:
            logger.exception("stage %s failed for run %s", entry.stage, entry.run_uuid)
            disposition = await retry_or_dead(
                self.queue, entry,
                max_attempts=self.settings.queue_max_attempts,
                error=f"{entry.stage} attempt {entry.attempt}: {exc!r}",
            )
            if disposition == "dead":
                self._fail_row(entry, error=f"dead-lettered after "
                              f"{entry.attempt} attempts: {exc!r}")
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):
                pass
            await self.leases.release(entry.run_uuid)

        if disposition == "deferred":
            # Budget pressure is not a failure: same attempt count, and the
            # re-enqueue happens AFTER the lease release above — otherwise
            # another worker claiming the fresh entry would see OUR lease and
            # drop the run as a duplicate. (The staged gauge stays decremented
            # for the round trip — a one-entry undercount while the assistant
            # is saturated, which is exactly when extra staging is pointless.)
            await asyncio.sleep(self.defer_backoff_s)
            await self.queue.enqueue(
                entry.stage, run_uuid=entry.run_uuid,
                created_at=entry.created_at, lane=entry.lane,
                attempt=entry.attempt,
            )
        return disposition

    async def _heartbeat_forever(self, run_uuid: uuid_mod.UUID) -> None:
        interval = max(self.settings.queue_lease_s / 3.0, 0.01)
        while True:
            await asyncio.sleep(interval)
            if not await self.leases.heartbeat(run_uuid):
                logger.warning("lost lease on run %s mid-flight", run_uuid)
                return

    # ---- row helpers ---------------------------------------------------------

    def _load(self, session: Session, entry: Entry) -> Optional[Run]:
        return session.execute(
            select(Run).where(Run.run_uuid == entry.run_uuid)
        ).scalar_one_or_none()

    def _fail_row(self, entry: Entry, *, error: str) -> None:
        with Session(self.engine) as session:
            run = self._load(session, entry)
            if run is None or run.status in ("completed", "failed", "cancelled"):
                return
            run.status = "failed"
            run.error = error
            run.finished_at = datetime.now(timezone.utc)
            session.commit()

    def _scenario_for(self, session: Session, run: Run):
        from opkura.core.scenario_loader import parse_scenario_data

        row = session.get(ScenarioRow, run.scenario_id) if run.scenario_id else None
        if row is None:
            raise RuntimeError(f"scenario {run.scenario_id} not found for run")
        body = {**dict(row.body), "vars": run.vars_snapshot,
                "region": run.region, "transport": run.transport}
        return parse_scenario_data(body, source_path=f"<run {run.run_uuid}>")

    # ---- stage: converse -----------------------------------------------------

    async def _converse(self, entry: Entry) -> Optional[str]:
        with Session(self.engine) as session:
            run = self._load(session, entry)
            if run is None:
                return None
            if run.status not in ("queued", "running"):
                # Already past this stage (a reclaimed duplicate) — no-op.
                return None
            scenario = self._scenario_for(session, run)
            row = session.get(ScenarioRow, run.scenario_id)
            raw_body = dict(row.body) if row is not None else {}

        # P4.5 — this staged entry is becoming an in-flight conversation;
        # the fair dispatcher's standing buffer frees one slot to refill.
        client, assistant = budget_keys(raw_body)
        from opkura.queue.fairness import _DECR_CLAMPED_LUA, staged_gauge

        await self.queue.redis.eval(_DECR_CLAMPED_LUA, 1, staged_gauge(assistant))

        # P4.4 — the per-assistant budget, held in Redis so it binds every
        # worker at once. No slot → defer: same attempt, tiny backoff, the
        # entry goes back on the stream after our lease is released.
        cfg = await load_config_async(self.queue.redis)
        if not await self.budget.acquire(
            client=client, assistant=assistant,
            limit=cfg.per_assistant_max_concurrency,
        ):
            return "defer"

        try:
            with Session(self.engine) as session:
                run = self._load(session, entry)
                run.status = "running"
                run.started_at = run.started_at or datetime.now(timezone.utc)
                session.commit()

            runner = self._conversation_runner()
            # The queue run's uuid becomes the session/log/trace id — one
            # identifier across the console row, Loki lines, and traces.
            result: CallResult = await runner.run_scenario(
                scenario, run_id=str(entry.run_uuid)
            )
        finally:
            # Conversation end frees the slot (P4.3) — the judge queues
            # separately and holds no phone line.
            await self.budget.release(client=client, assistant=assistant)

        with Session(self.engine) as session:
            run = self._load(session, entry)
            run.transcript = [e.model_dump() for e in result.transcript]
            run.observations = {
                "call_status": result.status.value,
                "error": result.error or "",
                "modules_visited": result.modules_visited,
                "tool_calls": result.tool_calls,
                "tag_only_bot_turns": result.tag_only_bot_turns,
                "osv_enriched": result.osv_enriched,
            }
            run.interaction_uuid = result.interaction_uuid or run.interaction_uuid
            run.call_sid = getattr(result, "call_sid", None) or run.call_sid
            artifact_keys: dict = {}
            if getattr(result, "log_dir", None):
                artifact_keys["log_dir"] = result.log_dir
            if getattr(result, "recording_paths", None):
                artifact_keys["recordings"] = dict(result.recording_paths)
            if artifact_keys:
                run.artifact_keys = artifact_keys
            run.status = "evaluating"          # slot freed; the judge is next
            session.commit()

        await self.queue.enqueue(
            "evaluate", run_uuid=entry.run_uuid,
            created_at=entry.created_at, lane=entry.lane,
        )

    # ---- stage: evaluate -------------------------------------------------------

    def _rebuild_result(self, run: Run) -> CallResult:
        obs = run.observations or {}
        result = CallResult(
            id=str(run.run_uuid),
            session_id=str(run.run_uuid),
            flow_name=run.scenario_key,
            target="",
            status=CallStatus(obs.get("call_status", "error")),
            error=obs.get("error", "") or None,
        )
        result.transcript = [TranscriptEntry(**e) for e in (run.transcript or [])]
        result.modules_visited = list(obs.get("modules_visited") or [])
        result.tool_calls = list(obs.get("tool_calls") or [])
        result.tag_only_bot_turns = obs.get("tag_only_bot_turns") or []
        result.osv_enriched = bool(obs.get("osv_enriched"))
        result.interaction_uuid = run.interaction_uuid or ""
        return result

    def _should_llm_judge(self, run: Run, result: CallResult) -> bool:
        failing = (
            result.status != CallStatus.COMPLETED
            or any(o.get("status") == "failed" for o in result.assertion_outcomes)
        )
        if failing:
            return True                        # failures are ALWAYS judged
        policy = run.judge_policy or "always"
        if policy == "always":
            return True
        if policy == "sampled":
            return sampled_in(run.run_uuid, self.judge_sample_rate)
        return False                           # assertions-only, and it passed

    async def _evaluate(self, entry: Entry) -> None:
        with Session(self.engine) as session:
            run = self._load(session, entry)
            if run is None or run.status not in ("evaluating",):
                return
            scenario = self._scenario_for(session, run)
            judge_policy = run.judge_policy

        result = self._rebuild_result_from(entry)
        runner = self._judge_runner()
        runner._run_assertions(scenario, result)

        with Session(self.engine) as session:
            run = self._load(session, entry)
            if self._should_llm_judge(run, result):
                result.evaluation = await runner._run_evaluator(scenario, result)
            else:
                from opkura.core.models import EvaluationResult

                result.evaluation = EvaluationResult(
                    overall_passed=True,
                    overall_reasoning=(
                        f"LLM judge skipped (judge_policy={judge_policy}); "
                        "call completed and assertions passed"
                    ),
                )
            run.passed = result.passed
            run.verdict = _verdict_dict(result)
            session.commit()

        await self.queue.enqueue(
            "settle", run_uuid=entry.run_uuid,
            created_at=entry.created_at, lane=entry.lane,
        )

    def _rebuild_result_from(self, entry: Entry) -> CallResult:
        with Session(self.engine) as session:
            run = self._load(session, entry)
            return self._rebuild_result(run)

    # ---- stage: settle ----------------------------------------------------------

    async def _settle(self, entry: Entry) -> None:
        with Session(self.engine) as session:
            run = self._load(session, entry)
            if run is None or run.status in ("completed", "failed", "cancelled"):
                return
            obs = run.observations or {}
            ok = (obs.get("call_status") or "").upper().endswith("COMPLETED")
            run.status = "completed" if ok else "failed"
            run.error = run.error or obs.get("error", "")
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
        # Bot-side correlation happens HERE, not in converse: by settle the
        # call has been over for a full evaluate stage, so OSV's post-call
        # write has had time to land. Best-effort by design — a correlation
        # miss must never fail a settled run.
        try:
            await self._correlate_voice(entry)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "voice correlation failed for %s: %s", entry.run_uuid, exc
            )

    async def _correlate_voice(self, entry: Entry) -> None:
        """Resolve a voice run's opkura session id (== its run uuid, P6) to
        the bot-side OSV interaction, exactly like job mode does — the
        session id travels as the X-Opkura-Session-Id SIP header and comes
        back in the interaction's own metadata. Text runs already carry
        their interaction_uuid from the HTTP response; this is voice's
        equivalent. Needs a Target row for the dialed number to know the
        client_uuid; without one it logs and moves on."""
        from datetime import timedelta

        from opkura.db.models import Target

        with Session(self.engine) as session:
            run = self._load(session, entry)
            if run is None or run.interaction_uuid or run.transport not in (
                "sip", "pstn",
            ):
                return
            # Queue runs carry the dialed number in the scenario body, not in
            # config_snapshot (that "target" key is the CLI results shape).
            target_number = (run.config_snapshot or {}).get("target", "")
            if not target_number and run.scenario_id is not None:
                srow = session.get(ScenarioRow, run.scenario_id)
                if srow is not None:
                    target_number = (srow.body or {}).get("target", "")
            client_uuid = ""
            row = None
            if run.target_id is not None:
                row = session.get(Target, run.target_id)
            if row is None and target_number:
                row = session.execute(
                    select(Target).where(Target.phone_number == target_number)
                ).scalars().first()
            if row is not None:
                client_uuid = row.client_uuid or ""
            if not client_uuid:
                import logging

                logging.getLogger(__name__).info(
                    "voice correlation skipped for %s: no Target row with a "
                    "client_uuid for %r", run.run_uuid, target_number,
                )
                return
            started = run.started_at or run.created_at
            finished = run.finished_at or datetime.now(timezone.utc)
            run_uuid = str(run.run_uuid)

        from opkura.jobs.pipeline import find_interaction_by_session_id

        # OSV's post-call write can lag the hangup by seconds — one retry
        # inside the lease window covers the common case; a persistent miss
        # stays a miss (best-effort, never blocks settling).
        match = None
        for attempt in range(2):
            match = await find_interaction_by_session_id(
                client_uuid=client_uuid,
                session_id=run_uuid,
                assistant_id=None,
                window_start=started - timedelta(minutes=5),
                window_end=finished + timedelta(minutes=5),
                caller_id=self.settings.twilio_phone_number,
            )
            if match or attempt == 1:
                break
            await asyncio.sleep(10)
        if not match:
            return
        with Session(self.engine) as session:
            run = self._load(session, entry)
            if run is None:
                return
            run.interaction_uuid = str(match.get("uuid") or "") or None
            # The web console builds the llm-studio deep link from
            # config_snapshot.client_uuid — stamp it alongside.
            snap = dict(run.config_snapshot or {})
            snap["client_uuid"] = client_uuid
            if match.get("disposition"):
                obs = dict(run.observations or {})
                obs["disposition"] = match.get("disposition")
                run.observations = obs
            run.config_snapshot = snap
            session.commit()


def _verdict_dict(result: CallResult) -> dict:
    out: dict = {}
    ev = result.evaluation
    if ev is not None:
        out["overall_passed"] = ev.overall_passed
        out["overall_reasoning"] = ev.overall_reasoning
        out["criteria"] = [
            c if isinstance(c, dict) else getattr(c, "__dict__", str(c))
            for c in (ev.criteria or [])
        ]
        out["metrics"] = ev.metrics or {}
    out["assertion_outcomes"] = result.assertion_outcomes
    return out


async def housekeeping(queue: StageQueue, engine: Engine, *, lease_s: float,
                       max_attempts: int, interval_s: float,
                       stop: asyncio.Event) -> None:
    """Reaper + stuck-dispatch sweep + fair pump, one shared cadence.

    Idempotent and safe to run on many processes at once: XAUTOCLAIM hands
    each pending entry to exactly one sweeper, redispatch duplicates collapse
    on the lease, and the pump's LPOP is atomic. Runs on every `opkura
    worker` and on the API process when queue_enabled."""
    from opkura.queue.dispatch import redispatch_stuck
    from opkura.queue.fairness import FairDispatcher
    from opkura.queue.leases import Reaper

    reaper = Reaper(queue, lease_s=lease_s, max_attempts=max_attempts)
    dispatcher = FairDispatcher(queue.redis, queue)
    while not stop.is_set():
        try:
            stats = await reaper.sweep()
            if stats.dead:
                # Dead-lettered runs must fail their rows even when the crash
                # happened on another (now dead) worker.
                with Session(engine) as session:
                    for run_uuid in stats.dead_runs:
                        run = session.execute(
                            select(Run).where(Run.run_uuid == run_uuid)
                        ).scalar_one_or_none()
                        if run is not None and run.status not in (
                            "completed", "failed", "cancelled"
                        ):
                            run.status = "failed"
                            run.error = run.error or (
                                "dead-lettered: max attempts exhausted "
                                "(lease expired repeatedly)"
                            )
                            run.finished_at = datetime.now(timezone.utc)
                    session.commit()
            await redispatch_stuck(engine, queue)
            await dispatcher.pump()
        except Exception:
            logger.exception("housekeeping cycle failed — retrying next cycle")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
