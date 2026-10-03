"""P4.3 — the judge is a stage: converse → evaluate → settle over the row.

Real migrated Postgres, fakeredis streams, fake runner (no LLM, no HTTP).
What's pinned: the stage transitions on the row, the judge-policy matrix
(always / sampled / assertions-only, failures ALWAYS judged), duplicate
collapse via the lease, and the crash → retry_or_dead path.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeaioredis
from sqlalchemy import select
from sqlalchemy.orm import Session

from opkura.core.models import (
    CallResult,
    CallStatus,
    EvaluationResult,
    TranscriptEntry,
)
from opkura.db.models import Run, Scenario as ScenarioRow
from opkura.queue.leases import LeaseManager
from opkura.queue.streams import StageQueue
from opkura.queue.worker import StageWorker, sampled_in


SCENARIO_BODY = {
    "name": "judge-stage-test",
    "target": "+15550009999",
    "vars": {},
    "persona": "You are the caller.",
    "evaluation_prompt": "Judge it.",
    "transport": "text",
}


@pytest_asyncio.fixture
async def queue():
    redis = fakeaioredis.FakeRedis(decode_responses=True)
    q = StageQueue(redis)
    await q.ensure_groups()
    yield q
    await redis.aclose()


def seed_run(engine, *, judge_policy="always") -> uuid.UUID:
    with Session(engine) as session:
        scen = ScenarioRow(key=f"js-{uuid.uuid4().hex[:8]}", body=SCENARIO_BODY)
        session.add(scen)
        session.commit()
        run = Run(
            scenario_id=scen.id, scenario_key=scen.key, region="us",
            transport="text", status="queued", vars_snapshot={},
            judge_policy=judge_policy,
        )
        session.add(run)
        session.commit()
        return run.run_uuid


def get_run(engine, run_uuid) -> Run:
    with Session(engine) as session:
        return session.execute(
            select(Run).where(Run.run_uuid == run_uuid)
        ).scalar_one()


class FakeRunner:
    """Serves both worker roles: conversation and judge. No LLM, no HTTP."""

    def __init__(self, *, call_status=CallStatus.COMPLETED, crash=False,
                 judged: list | None = None):
        self.call_status = call_status
        self.crash = crash
        self.judged = judged if judged is not None else []

    async def run_scenario(self, scenario, *, run_id=None):
        if self.crash:
            raise RuntimeError("transport exploded")
        result = CallResult(
            id="x", session_id="x", flow_name=scenario.name,
            target=scenario.target, status=self.call_status,
            error="boom" if self.call_status != CallStatus.COMPLETED else None,
        )
        result.transcript = [
            TranscriptEntry(speaker="bot", text="Hello?"),
            TranscriptEntry(speaker="caller", text="Hi, goodbye."),
        ]
        result.modules_visited = ["GREETINGS"]
        result.tool_calls = [{"name": "check_rmn_temp"}]
        result.interaction_uuid = "int-stage-1"
        return result

    def _run_assertions(self, scenario, result):
        result.assertion_outcomes = []

    async def _run_evaluator(self, scenario, result):
        self.judged.append(result.id)
        return EvaluationResult(overall_passed=True, overall_reasoning="llm judged")


def make_worker(engine, queue, runner, **kw) -> StageWorker:
    from tests.integration.api.conftest import make_settings

    settings = make_settings(queue_lease_s=5.0, queue_max_attempts=3)
    return StageWorker(
        settings=settings, engine=engine, queue=queue, consumer="w1",
        runner_factory=lambda: runner, **kw,
    )


async def drive_all_stages(worker, *, rounds: int = 6):
    for _ in range(rounds):
        if await worker.run_once(block_ms=1) == 0:
            break


# ------------------------------------------------------------- the pipeline

@pytest.mark.asyncio
async def test_full_pipeline_converse_evaluate_settle(migrated_engine, queue):
    run_uuid = seed_run(migrated_engine, judge_policy="always")
    await queue.enqueue("converse-text", run_uuid=run_uuid,
                        created_at=get_run(migrated_engine, run_uuid).created_at)
    runner = FakeRunner()
    worker = make_worker(migrated_engine, queue, runner)

    await drive_all_stages(worker)

    run = get_run(migrated_engine, run_uuid)
    assert run.status == "completed"
    assert run.passed is True
    assert run.finished_at is not None
    assert run.interaction_uuid == "int-stage-1"
    assert [e["speaker"] for e in run.transcript] == ["bot", "caller"]
    assert run.observations["modules_visited"] == ["GREETINGS"]
    assert run.verdict["overall_reasoning"] == "llm judged"
    assert len(runner.judged) == 1             # policy=always → judged once


@pytest.mark.asyncio
async def test_assertions_only_skips_the_judge_on_a_passing_run(migrated_engine, queue):
    run_uuid = seed_run(migrated_engine, judge_policy="assertions-only")
    await queue.enqueue("converse-text", run_uuid=run_uuid,
                        created_at=get_run(migrated_engine, run_uuid).created_at)
    runner = FakeRunner()
    worker = make_worker(migrated_engine, queue, runner)

    await drive_all_stages(worker)

    run = get_run(migrated_engine, run_uuid)
    assert run.status == "completed" and run.passed is True
    assert runner.judged == []                 # never reached the LLM
    assert "skipped" in run.verdict["overall_reasoning"]


@pytest.mark.asyncio
async def test_failures_are_always_judged_whatever_the_policy(migrated_engine, queue):
    run_uuid = seed_run(migrated_engine, judge_policy="assertions-only")
    await queue.enqueue("converse-text", run_uuid=run_uuid,
                        created_at=get_run(migrated_engine, run_uuid).created_at)
    runner = FakeRunner(call_status=CallStatus.ERROR)
    worker = make_worker(migrated_engine, queue, runner)

    await drive_all_stages(worker)

    run = get_run(migrated_engine, run_uuid)
    assert run.status == "failed"              # call errored → run failed
    assert len(runner.judged) == 1             # …but it WAS judged
    assert run.verdict["overall_reasoning"] == "llm judged"


@pytest.mark.asyncio
async def test_duplicate_entry_is_collapsed_by_the_lease(migrated_engine, queue):
    run_uuid = seed_run(migrated_engine)
    created = get_run(migrated_engine, run_uuid).created_at
    await queue.enqueue("converse-text", run_uuid=run_uuid, created_at=created)
    worker = make_worker(migrated_engine, queue, FakeRunner())

    # Another worker holds the lease mid-flight.
    other = LeaseManager(queue.redis, consumer="other-worker", lease_s=60)
    assert await other.acquire(run_uuid)

    [entry] = await queue.claim("converse-text", consumer="w1", block_ms=1)
    disposition = await worker.process(entry)

    assert disposition == "duplicate"
    assert get_run(migrated_engine, run_uuid).status == "queued"  # untouched
    # entry acked — nothing left to claim
    assert await queue.claim("converse-text", consumer="w1", block_ms=1) == []


@pytest.mark.asyncio
async def test_crashing_stage_retries_then_dead_letters_and_fails_the_row(
    migrated_engine, queue
):
    run_uuid = seed_run(migrated_engine)
    created = get_run(migrated_engine, run_uuid).created_at
    await queue.enqueue("converse-text", run_uuid=run_uuid, created_at=created)
    worker = make_worker(migrated_engine, queue, FakeRunner(crash=True))
    worker.settings = worker.settings.model_copy(update={"queue_max_attempts": 2})

    dispositions = []
    for _ in range(5):
        entries = await queue.claim("converse-text", consumer="w1", block_ms=1)
        if not entries:
            break
        dispositions.append(await worker.process(entries[0]))

    assert dispositions == ["requeued", "dead"]     # exactly N=2 attempts
    run = get_run(migrated_engine, run_uuid)
    assert run.status == "failed"
    assert "dead-lettered after 2 attempts" in run.error


def test_sampling_is_deterministic_per_run():
    run = uuid.uuid4()
    first = sampled_in(run, 0.5)
    assert all(sampled_in(run, 0.5) == first for _ in range(20))
    assert sampled_in(run, 1.0) is True
    assert sampled_in(run, 0.0) is False
    # roughly the requested fraction across many runs
    hits = sum(sampled_in(uuid.uuid4(), 0.3) for _ in range(2000))
    assert 450 < hits < 750


@pytest.mark.asyncio
async def test_budget_exhausted_defers_without_burning_an_attempt(
    migrated_engine, queue
):
    """P4.4: no slot → the entry is re-queued with the SAME attempt after the
    lease is released, and the row is untouched."""
    from opkura.queue.admission import CONFIG_HASH, ExecutionBudget

    run_uuid = seed_run(migrated_engine)
    created = get_run(migrated_engine, run_uuid).created_at
    await queue.enqueue("converse-text", run_uuid=run_uuid, created_at=created)
    await queue.redis.hset(CONFIG_HASH, "per_assistant_max_concurrency", "1")
    # Someone else holds the only slot for this assistant (the target).
    other = ExecutionBudget(queue.redis)
    assert await other.acquire(
        client="judge-stage-test", assistant="+15550009999", limit=1
    )

    worker = make_worker(migrated_engine, queue, FakeRunner())
    worker.defer_backoff_s = 0.0
    [entry] = await queue.claim("converse-text", consumer="w1", block_ms=1)

    assert await worker.process(entry) == "deferred"
    assert get_run(migrated_engine, run_uuid).status == "queued"   # untouched
    [requeued] = await queue.claim("converse-text", consumer="w2", block_ms=1)
    assert requeued.run_uuid == run_uuid
    assert requeued.attempt == entry.attempt                        # not burned

    # Slot freed → the deferred entry now converses to completion.
    await other.release(client="judge-stage-test", assistant="+15550009999")
    assert await worker.process(requeued) == "done"
    assert get_run(migrated_engine, run_uuid).status == "evaluating"
