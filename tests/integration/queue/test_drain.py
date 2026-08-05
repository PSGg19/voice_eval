"""P4.7 — graceful drain: stop claiming, finish in-flight, lose nothing.

The cross-process SIGTERM/kill -9 versions live in the `-m distributed`
two-worker proof; here the drain contract is pinned in-process: after
request_stop() (what the signal handler calls), in-flight runs FINISH, no
new entries are claimed, and everything unclaimed stays claimable — a
restart loses zero runs.
"""
from __future__ import annotations

import asyncio
import signal
import uuid

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeaioredis
from sqlalchemy.orm import Session

from opkura.db.models import Run, Scenario as ScenarioRow
from opkura.queue.streams import StageQueue

from tests.integration.queue.test_two_lanes import SlowRunner, seed
from tests.integration.queue.test_worker_stages import get_run, make_worker


@pytest_asyncio.fixture
async def queue():
    redis = fakeaioredis.FakeRedis(decode_responses=True)
    q = StageQueue(redis)
    await q.ensure_groups()
    yield q
    await redis.aclose()


@pytest.mark.asyncio
async def test_drain_finishes_in_flight_and_abandons_nothing(migrated_engine, queue):
    await queue.redis.hset("opkura:admission:config",
                           "per_assistant_max_concurrency", "50")
    run_uuids = []
    for i in range(6):
        run_uuid, created = seed(migrated_engine, name=f"drain-{i}")
        run_uuids.append(run_uuid)
        await queue.enqueue("converse-text", run_uuid=run_uuid,
                            created_at=created, lane="interactive")

    runner = SlowRunner(conversation_s=0.25)
    worker = make_worker(migrated_engine, queue, runner)
    loop_task = asyncio.create_task(
        worker.run_forever(slots=2, interactive_reserve=0, idle_sleep_s=0.02)
    )

    # Let two conversations get in flight, then the "SIGTERM" arrives.
    await asyncio.sleep(0.15)
    assert runner.in_flight >= 1
    worker.request_stop()
    await asyncio.wait_for(loop_task, 10)      # exits, within bounds

    statuses = {str(r): get_run(migrated_engine, r).status for r in run_uuids}
    finished = [r for r, s in statuses.items() if s == "evaluating"]
    untouched = [r for r, s in statuses.items() if s == "queued"]
    # In-flight runs FINISHED their conversation stage; nothing half-done.
    assert len(finished) == runner.peak
    assert len(finished) + len(untouched) == 6
    # Zero lost: every untouched run is still claimable by the next worker.
    remaining = await queue.claim("converse-text", consumer="w2", count=10,
                                  block_ms=1)
    assert {str(e.run_uuid) for e in remaining} == set(untouched)


@pytest.mark.asyncio
async def test_sigterm_handler_triggers_the_drain(migrated_engine, queue):
    """The signal wiring itself: SIGTERM → request_stop → loop exits."""
    import os

    from opkura.cli.worker_cmd import install_signal_handlers

    worker = make_worker(migrated_engine, queue, SlowRunner(conversation_s=0.01))
    install_signal_handlers(asyncio.get_running_loop(), worker)
    try:
        loop_task = asyncio.create_task(
            worker.run_forever(slots=1, idle_sleep_s=0.02)
        )
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(loop_task, 5)   # drained and returned
        assert worker._stopping is True
    finally:
        loop = asyncio.get_running_loop()
        loop.remove_signal_handler(signal.SIGTERM)
        loop.remove_signal_handler(signal.SIGINT)
