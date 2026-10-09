"""P4.1 — dispatch semantics against the real migrated schema.

The Redis half is fakeredis (streams behave; no container needed beyond the
Postgres the DB fixtures already require). What these tests pin is the PRD's
admission asymmetry: the Postgres row is the record and is written first; the
queue is dispatch and may be down without a run being lost or refused.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeaioredis
from sqlalchemy import select
from sqlalchemy.orm import Session

from judge_it.db.models import Run, RunBatch
from judge_it.queue.dispatch import QueueExecutor, redispatch_stuck
from judge_it.queue.streams import StageQueue


@pytest_asyncio.fixture
async def queue():
    redis = fakeaioredis.FakeRedis(decode_responses=True)
    q = StageQueue(redis)
    await q.ensure_groups()
    yield q
    await redis.aclose()


def make_run(engine, *, transport="text", batch_id=None,
             created_at=None, status="created") -> tuple[uuid.UUID, datetime]:
    with Session(engine) as session:
        run = Run(
            scenario_key=f"disp-{uuid.uuid4().hex[:8]}",
            region="us",
            transport=transport,
            status=status,
            vars_snapshot={},
            batch_id=batch_id,
        )
        if created_at is not None:
            run.created_at = created_at
        session.add(run)
        session.commit()
        return run.run_uuid, run.created_at


def get_run(engine, run_uuid) -> Run:
    with Session(engine) as session:
        return session.execute(
            select(Run).where(Run.run_uuid == run_uuid)
        ).scalar_one()


@pytest.mark.asyncio
async def test_execute_marks_row_queued_then_enqueues(migrated_engine, queue):
    run_uuid, _ = make_run(migrated_engine, transport="text")
    await QueueExecutor(engine=migrated_engine, queue=queue).execute(run_uuid)

    assert get_run(migrated_engine, run_uuid).status == "queued"
    [entry] = await queue.claim("converse-text", consumer="w1", block_ms=1)
    assert entry.run_uuid == run_uuid
    assert entry.lane == "interactive"       # single run = a person waiting


@pytest.mark.asyncio
async def test_voice_routes_to_the_voice_stage(migrated_engine, queue):
    run_uuid, _ = make_run(migrated_engine, transport="sip")
    await QueueExecutor(engine=migrated_engine, queue=queue).execute(run_uuid)

    assert await queue.depth("converse-text") == 0
    [entry] = await queue.claim("converse-voice", consumer="w1", block_ms=1)
    assert entry.run_uuid == run_uuid


@pytest.mark.asyncio
async def test_batch_members_ride_the_batch_lane(migrated_engine, queue):
    with Session(migrated_engine) as session:
        batch = RunBatch(name="b", config={}, status="created")
        session.add(batch)
        session.commit()
        batch_id = batch.id
    run_uuid, _ = make_run(migrated_engine, transport="text", batch_id=batch_id)

    await QueueExecutor(engine=migrated_engine, queue=queue).execute(run_uuid)

    [entry] = await queue.claim("converse-text", consumer="w1", block_ms=1)
    assert entry.lane == "batch"


@pytest.mark.asyncio
async def test_redis_down_defers_dispatch_but_keeps_the_row(migrated_engine, queue):
    """The PRD acceptance: with Redis down at admission, the API still
    accepts — row written, dispatch deferred."""
    run_uuid, _ = make_run(migrated_engine)

    async def broken_enqueue(*a, **kw):
        raise ConnectionError("redis is down")

    queue.enqueue = broken_enqueue  # type: ignore[method-assign]
    await QueueExecutor(engine=migrated_engine, queue=queue).execute(run_uuid)

    # No exception escaped, and the row tells the truth.
    assert get_run(migrated_engine, run_uuid).status == "queued"


@pytest.mark.asyncio
async def test_redispatch_resends_stuck_queued_rows(migrated_engine, queue):
    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
    run_uuid, _ = make_run(migrated_engine, status="queued", created_at=stale)
    fresh_uuid, _ = make_run(migrated_engine, status="queued")  # inside grace

    sent = await redispatch_stuck(migrated_engine, queue)

    assert sent == 1
    [entry] = await queue.claim("converse-text", consumer="w1", block_ms=1)
    assert entry.run_uuid == run_uuid          # only the stale one re-sent
    await queue.ack(entry)
    # the fresh row (inside the grace window) was NOT re-sent
    assert await queue.claim("converse-text", consumer="w1", block_ms=1) == []
