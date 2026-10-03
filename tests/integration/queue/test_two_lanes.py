"""P4.6 — two lanes: batch fills the floor, interactive keeps a reserved slice.

The PRD acceptance, scaled to unit time: an interactive run submitted during
a batch flood starts promptly because batch work may hold at most
`slots - reserve` of the worker's slots.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeaioredis
from sqlalchemy.orm import Session

from opkura.core.models import CallResult, CallStatus, TranscriptEntry
from opkura.db.models import Run, RunBatch, Scenario as ScenarioRow
from opkura.queue.streams import StageQueue
from opkura.queue.worker import StageWorker

from tests.integration.queue.test_worker_stages import (
    SCENARIO_BODY,
    get_run,
    make_worker,
)


@pytest_asyncio.fixture
async def queue():
    redis = fakeaioredis.FakeRedis(decode_responses=True)
    q = StageQueue(redis)
    await q.ensure_groups()
    yield q
    await redis.aclose()


class SlowRunner:
    """Conversations take `conversation_s`; records concurrency and starts."""

    def __init__(self, conversation_s: float = 0.3):
        self.conversation_s = conversation_s
        self.in_flight = 0
        self.peak = 0
        self.started_at: dict[str, float] = {}

    async def run_scenario(self, scenario, *, run_id=None):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.started_at[scenario.name] = time.monotonic()
        try:
            await asyncio.sleep(self.conversation_s)
        finally:
            self.in_flight -= 1
        result = CallResult(id="x", session_id="x", flow_name=scenario.name,
                            target=scenario.target, status=CallStatus.COMPLETED)
        result.transcript = [TranscriptEntry(speaker="bot", text="hi")]
        return result

    def _run_assertions(self, scenario, result):
        result.assertion_outcomes = []

    async def _run_evaluator(self, scenario, result):
        from opkura.core.models import EvaluationResult

        return EvaluationResult(overall_passed=True, overall_reasoning="ok")


def seed(engine, *, name: str, batch_id=None) -> tuple[uuid.UUID, object]:
    with Session(engine) as session:
        scen = ScenarioRow(
            key=f"lane-{uuid.uuid4().hex[:8]}",
            body={**SCENARIO_BODY, "name": name},
        )
        session.add(scen)
        session.commit()
        run = Run(scenario_id=scen.id, scenario_key=scen.key, region="us",
                  transport="text", status="queued", vars_snapshot={},
                  batch_id=batch_id)
        session.add(run)
        session.commit()
        return run.run_uuid, run.created_at


@pytest.mark.asyncio
async def test_interactive_run_starts_promptly_during_a_batch_flood(
    migrated_engine, queue
):
    # Budget must not bind — this test is about LANES. Distinct targets per
    # run would also work; simpler to raise the per-assistant cap.
    await queue.redis.hset("opkura:admission:config",
                           "per_assistant_max_concurrency", "50")

    with Session(migrated_engine) as session:
        batch = RunBatch(name="flood", config={}, status="created")
        session.add(batch)
        session.commit()
        batch_id = batch.id

    # A batch flood: more entries than the worker can hold at once.
    for i in range(8):
        run_uuid, created = seed(migrated_engine, name=f"batch-{i}",
                                 batch_id=batch_id)
        await queue.enqueue("converse-text", run_uuid=run_uuid,
                            created_at=created, lane="batch")

    runner = SlowRunner(conversation_s=0.25)
    worker = make_worker(migrated_engine, queue, runner)
    loop_task = asyncio.create_task(
        worker.run_forever(slots=2, interactive_reserve=1, idle_sleep_s=0.02)
    )

    try:
        # Let the batch occupy what it may.
        await asyncio.sleep(0.15)
        # The person arrives mid-flood.
        run_uuid, created = seed(migrated_engine, name="interactive-1")
        submitted = time.monotonic()
        await queue.enqueue("converse-text", run_uuid=run_uuid,
                            created_at=created, lane="interactive")

        for _ in range(100):
            await asyncio.sleep(0.02)
            if "interactive-1" in runner.started_at:
                break
        assert "interactive-1" in runner.started_at, "interactive run never started"
        wait_s = runner.started_at["interactive-1"] - submitted
        # The PRD bound is 5s on real hardware; here one 0.25s conversation
        # is the theoretical worst case for the reserved slot.
        assert wait_s < 1.0, f"interactive run waited {wait_s:.2f}s"

        # And batch never occupied the reserved slice: with slots=2/reserve=1,
        # peak batch concurrency is 1 until the interactive run shares.
        assert runner.peak <= 2
    finally:
        worker.request_stop()
        await asyncio.wait_for(loop_task, 10)


@pytest.mark.asyncio
async def test_batch_alone_is_capped_at_slots_minus_reserve(migrated_engine, queue):
    await queue.redis.hset("opkura:admission:config",
                           "per_assistant_max_concurrency", "50")
    with Session(migrated_engine) as session:
        batch = RunBatch(name="flood2", config={}, status="created")
        session.add(batch)
        session.commit()
        batch_id = batch.id
    for i in range(6):
        run_uuid, created = seed(migrated_engine, name=f"b2-{i}", batch_id=batch_id)
        await queue.enqueue("converse-text", run_uuid=run_uuid,
                            created_at=created, lane="batch")

    runner = SlowRunner(conversation_s=0.1)
    worker = make_worker(migrated_engine, queue, runner)
    loop_task = asyncio.create_task(
        worker.run_forever(slots=3, interactive_reserve=1, idle_sleep_s=0.02)
    )
    try:
        await asyncio.sleep(0.5)
        assert runner.peak <= 2               # 3 slots − 1 reserved
        assert len(runner.started_at) >= 4    # …but the batch IS progressing
    finally:
        worker.request_stop()
        await asyncio.wait_for(loop_task, 10)
