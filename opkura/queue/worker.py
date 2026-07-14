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
        return TestRunner(settings, per_run_debug_log=False)

    def _judge_runner(self):
        """A TestRunner used only for _run_assertions/_run_evaluator."""
        if self._runner_factory is not None:
            return self._runner_factory()
        from opkura.orchestrator.runner import TestRunner

        return TestRunner(self.settings, per_run_debug_log=False)

    # ---- the claim cycle ----------------------------------------------------

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
        try:
            await self._handlers[entry.stage](entry)
            await self.queue.ack(entry)
            return "done"
        except Exception as exc:
            logger.exception("stage %s failed for run %s", entry.stage, entry.run_uuid)
            outcome = await retry_or_dead(
                self.queue, entry,
                max_attempts=self.settings.queue_max_attempts,
                error=f"{entry.stage} attempt {entry.attempt}: {exc!r}",
            )
            if outcome == "dead":
                self._fail_row(entry, error=f"dead-lettered after "
                              f"{entry.attempt} attempts: {exc!r}")
            return outcome
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):
                pass
            await self.leases.release(entry.run_uuid)

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

    async def _converse(self, entry: Entry) -> None:
        with Session(self.engine) as session:
            run = self._load(session, entry)
            if run is None:
                return
            if run.status not in ("queued", "running"):
                # Already past this stage (a reclaimed duplicate) — no-op.
                return
            scenario = self._scenario_for(session, run)
            run.status = "running"
            run.started_at = run.started_at or datetime.now(timezone.utc)
            session.commit()

        runner = self._conversation_runner()
        result: CallResult = await runner.run_scenario(scenario)

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
