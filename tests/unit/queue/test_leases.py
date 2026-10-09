"""P4.2 — leases + reaper, hermetic on fakeredis.

Timing note: fakeredis honours PX expiry and XAUTOCLAIM idle times against
the real clock, so the tests use tens-of-milliseconds leases and short sleeps.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeaioredis

from judge_it.queue.leases import (
    LEASE_HISTORY,
    LeaseManager,
    Reaper,
    lease_key,
    retry_or_dead,
)
from judge_it.queue.streams import DEAD_STREAM, StageQueue

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def redis():
    r = fakeaioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def queue(redis):
    q = StageQueue(redis)
    await q.ensure_groups()
    return q


# ------------------------------------------------------------------- leases

@pytest.mark.asyncio
async def test_lease_is_exclusive_and_recorded(redis):
    run = uuid.uuid4()
    w1 = LeaseManager(redis, consumer="w1", lease_s=5)
    w2 = LeaseManager(redis, consumer="w2", lease_s=5)

    assert await w1.acquire(run) is True
    assert await w2.acquire(run) is False        # exclusive
    assert await w1.holder(run) == "w1"

    history = await redis.xrange(LEASE_HISTORY)
    assert len(history) == 1                      # only the successful acquire
    assert history[0][1]["consumer"] == "w1"


@pytest.mark.asyncio
async def test_heartbeat_extends_only_for_the_owner(redis):
    run = uuid.uuid4()
    w1 = LeaseManager(redis, consumer="w1", lease_s=0.05)
    w2 = LeaseManager(redis, consumer="w2", lease_s=0.05)
    await w1.acquire(run)

    assert await w2.heartbeat(run) is False       # not yours to extend
    assert await w1.heartbeat(run) is True

    await asyncio.sleep(0.08)                     # past the un-renewed window
    assert await w1.holder(run) is None           # expired despite heartbeat API
    # (the heartbeat above only bought one more window, which then lapsed)


@pytest.mark.asyncio
async def test_expired_lease_can_be_taken_by_the_survivor(redis):
    run = uuid.uuid4()
    dead = LeaseManager(redis, consumer="dead-worker", lease_s=0.03)
    survivor = LeaseManager(redis, consumer="survivor", lease_s=5)
    await dead.acquire(run)
    await asyncio.sleep(0.06)

    assert await survivor.acquire(run) is True
    assert await dead.heartbeat(run) is False     # old owner can't extend
    assert await dead.release(run) is False       # …or delete
    assert await survivor.holder(run) == "survivor"


# ------------------------------------------------------------------- reaper

@pytest.mark.asyncio
async def test_reaper_requeues_a_dead_workers_entry(redis, queue):
    run = uuid.uuid4()
    await queue.enqueue("converse-text", run_uuid=run, created_at=NOW)
    [entry] = await queue.claim("converse-text", consumer="w1", block_ms=1)
    # w1 acquires a tiny lease and dies (never acks, never heartbeats).
    await LeaseManager(redis, consumer="w1", lease_s=0.03).acquire(run)
    await asyncio.sleep(0.06)

    stats = await Reaper(queue, lease_s=0.03, max_attempts=3).sweep()

    assert stats.requeued == 1 and stats.dead == 0
    [reclaimed] = await queue.claim("converse-text", consumer="w2", block_ms=1)
    assert reclaimed.run_uuid == run
    assert reclaimed.attempt == 2                 # the counter travelled


@pytest.mark.asyncio
async def test_reaper_leaves_a_live_lease_alone(redis, queue):
    run = uuid.uuid4()
    await queue.enqueue("converse-text", run_uuid=run, created_at=NOW)
    [entry] = await queue.claim("converse-text", consumer="w1", block_ms=1)
    # Long lease: the worker is slow, not dead.
    await LeaseManager(redis, consumer="w1", lease_s=60).acquire(run)
    await asyncio.sleep(0.06)

    stats = await Reaper(queue, lease_s=0.03, max_attempts=3).sweep()

    assert stats.skipped_live == 1
    assert stats.requeued == 0 and stats.dead == 0
    # nothing re-queued for another worker
    assert await queue.claim("converse-text", consumer="w2", block_ms=1) == []


@pytest.mark.asyncio
async def test_always_crashing_run_dead_letters_after_exactly_n_attempts(redis, queue):
    """The PRD acceptance: N attempts, then the dead-letter, with the error —
    and the queue does not loop it."""
    max_attempts = 3
    run = uuid.uuid4()
    await queue.enqueue("converse-text", run_uuid=run, created_at=NOW)
    reaper = Reaper(queue, lease_s=0.02, max_attempts=max_attempts)

    deliveries = 0
    for _ in range(10):                            # would loop forever if broken
        claimed = await queue.claim("converse-text", consumer="w1", block_ms=1)
        if not claimed:
            break
        deliveries += 1
        # worker "crashes": lease acquired then never heartbeated/acked
        lm = LeaseManager(redis, consumer="w1", lease_s=0.02)
        await lm.acquire(claimed[0].run_uuid)
        await asyncio.sleep(0.05)
        await reaper.sweep()

    assert deliveries == max_attempts              # exactly N pickups
    dead = await redis.xrange(DEAD_STREAM)
    assert len(dead) == 1
    _id, fields = dead[0]
    assert fields["run_uuid"] == str(run)
    assert "lease expired" in fields["error"]
    # the stream is empty — no loop
    assert await queue.claim("converse-text", consumer="w1", block_ms=1) == []


@pytest.mark.asyncio
async def test_retry_or_dead_is_the_single_failure_policy(queue):
    run = uuid.uuid4()
    await queue.enqueue("evaluate", run_uuid=run, created_at=NOW, attempt=2)
    [entry] = await queue.claim("evaluate", consumer="w1", block_ms=1)

    outcome = await retry_or_dead(queue, entry, max_attempts=2, error="judge crashed")

    assert outcome == "dead"
    dead = await queue.redis.xrange(DEAD_STREAM)
    assert dead[0][1]["error"] == "judge crashed"
