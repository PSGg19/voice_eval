"""P4.1 — stage streams: bounded, grouped, lane-ordered. Hermetic (fakeredis)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeaioredis

from opkura.queue.streams import (
    DEAD_STREAM,
    GROUP,
    LANES,
    STAGES,
    Entry,
    StageQueue,
    stream_key,
)


@pytest_asyncio.fixture
async def queue():
    redis = fakeaioredis.FakeRedis(decode_responses=True)
    q = StageQueue(redis, maxlen=1000)
    await q.ensure_groups()
    yield q
    await redis.aclose()


def rid() -> uuid.UUID:
    return uuid.uuid4()


NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_enqueue_claim_ack_round_trip(queue):
    run = rid()
    await queue.enqueue("converse-text", run_uuid=run, created_at=NOW)

    [entry] = await queue.claim("converse-text", consumer="w1", block_ms=1)
    assert entry.run_uuid == run
    assert entry.created_at == NOW.isoformat()
    assert entry.attempt == 1 and entry.lane == "batch"

    await queue.ack(entry)
    assert await queue.depth("converse-text") == 0
    # nothing left pending either
    pending = await queue.redis.xpending(stream_key("converse-text", "batch"), GROUP)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_interactive_lane_is_claimed_before_batch(queue):
    batch_run, interactive_run = rid(), rid()
    await queue.enqueue("converse-text", run_uuid=batch_run, created_at=NOW, lane="batch")
    await queue.enqueue("converse-text", run_uuid=interactive_run, created_at=NOW,
                        lane="interactive")

    [first] = await queue.claim("converse-text", consumer="w1", count=1, block_ms=1)
    assert first.run_uuid == interactive_run     # P4.6: interactive first
    [second] = await queue.claim("converse-text", consumer="w1", count=1, block_ms=1)
    assert second.run_uuid == batch_run


@pytest.mark.asyncio
async def test_unknown_stage_and_lane_are_refused(queue):
    with pytest.raises(ValueError):
        stream_key("conversate", "batch")
    with pytest.raises(ValueError):
        stream_key("converse-text", "premium")


@pytest.mark.asyncio
async def test_every_stream_is_bounded(queue):
    """The PRD's 'every queue bounded': entries beyond maxlen are trimmed."""
    q = StageQueue(queue.redis, maxlen=5)
    for _ in range(50):
        await q.enqueue("settle", run_uuid=rid(), created_at=NOW)
    # MAXLEN ~ is approximate; the invariant is 'nowhere near 50'.
    assert await q.depth("settle") <= 20


@pytest.mark.asyncio
async def test_dead_letter_carries_the_failure_and_acks(queue):
    run = rid()
    await queue.enqueue("evaluate", run_uuid=run, created_at=NOW)
    [entry] = await queue.claim("evaluate", consumer="w1", block_ms=1)

    await queue.dead_letter(entry, error="LLM exploded: boom")

    assert await queue.depth("evaluate") == 0
    dead = await queue.redis.xrange(DEAD_STREAM)
    assert len(dead) == 1
    _id, fields = dead[0]
    assert fields["run_uuid"] == str(run)
    assert fields["stage"] == "evaluate"
    assert "boom" in fields["error"]


@pytest.mark.asyncio
async def test_two_consumers_never_receive_the_same_entry(queue):
    """Consumer groups partition delivery — the substrate of exactly-once."""
    runs = {rid() for _ in range(20)}
    for r in runs:
        await queue.enqueue("converse-text", run_uuid=r, created_at=NOW)

    seen_w1 = await queue.claim("converse-text", consumer="w1", count=20, block_ms=1)
    seen_w2 = await queue.claim("converse-text", consumer="w2", count=20, block_ms=1)

    ids1 = {e.run_uuid for e in seen_w1}
    ids2 = {e.run_uuid for e in seen_w2}
    assert ids1 | ids2 == runs
    assert not (ids1 & ids2)
