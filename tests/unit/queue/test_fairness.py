"""P4.5 — water-filling across suites, token-paced spread, capacity setpoint."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeaioredis

from opkura.queue.admission import CONFIG_HASH
from opkura.queue.fairness import (
    FairDispatcher,
    setpoint_for,
    staged_gauge,
)
from opkura.queue.streams import StageQueue

NOW = "2026-08-31T12:00:00+00:00"


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


async def submit_suite(dispatcher, *, assistant, batch, count) -> list[uuid.UUID]:
    runs = []
    for _ in range(count):
        run = uuid.uuid4()
        runs.append(run)
        await dispatcher.submit(
            assistant=assistant, batch=batch, stage="converse-text",
            run_uuid=run, created_at=NOW,
        )
    return runs


async def set_config(redis, **kv):
    await redis.hset(CONFIG_HASH, mapping={k: str(v) for k, v in kv.items()})


async def staged_runs(queue, *, count=50) -> list[uuid.UUID]:
    entries = await queue.claim("converse-text", consumer="probe", count=count,
                                block_ms=1)
    return [e.run_uuid for e in entries]


# ------------------------------------------------------------- water-filling

@pytest.mark.asyncio
async def test_small_suite_is_not_starved_by_the_big_one(redis, queue):
    """Suite A has 10 runs, suite B has 2, setpoint 4: one pump stages
    A,B,A,B — B's whole demand lands in the FIRST top-up."""
    await set_config(redis, per_assistant_max_concurrency=4,
                     per_assistant_tps=1000)
    d = FairDispatcher(redis, queue)
    a_runs = await submit_suite(d, assistant="bot", batch="suite-a", count=10)
    b_runs = await submit_suite(d, assistant="bot", batch="suite-b", count=2)

    moved = await d.pump()

    assert moved == 4
    staged = await staged_runs(queue)
    # round-robin: a, b, a, b — both of B's runs staged despite A's backlog
    assert staged == [a_runs[0], b_runs[0], a_runs[1], b_runs[1]]


@pytest.mark.asyncio
async def test_drained_suite_leaves_the_rest_to_the_big_one(redis, queue):
    await set_config(redis, per_assistant_max_concurrency=5,
                     per_assistant_tps=1000)
    d = FairDispatcher(redis, queue)
    a_runs = await submit_suite(d, assistant="bot", batch="suite-a", count=10)
    b_runs = await submit_suite(d, assistant="bot", batch="suite-b", count=1)

    await d.pump()

    staged = await staged_runs(queue)
    assert len(staged) == 5
    assert b_runs[0] in staged                 # the smoke test got through
    assert staged.count(b_runs[0]) == 1
    assert sum(1 for r in staged if r in a_runs) == 4   # remainder water-fills


@pytest.mark.asyncio
async def test_assistants_are_independent(redis, queue):
    await set_config(redis, per_assistant_max_concurrency=2,
                     per_assistant_tps=1000)
    d = FairDispatcher(redis, queue)
    await submit_suite(d, assistant="bot-1", batch="a", count=5)
    await submit_suite(d, assistant="bot-2", batch="b", count=5)

    moved = await d.pump()

    assert moved == 4                          # 2 per assistant
    assert int(await redis.get(staged_gauge("bot-1"))) == 2
    assert int(await redis.get(staged_gauge("bot-2"))) == 2


# ------------------------------------------------------------------- spread

@pytest.mark.asyncio
async def test_moves_are_token_paced_not_front_loaded(redis, queue):
    """Setpoint 10 but only ~2 tokens in the bucket: one pump moves 2, not
    10 — the bucket's refill spreads the minute's admissions."""
    await set_config(redis, per_assistant_max_concurrency=10,
                     per_assistant_tps=2)     # burst == max(rate,1) == 2
    d = FairDispatcher(redis, queue)
    await submit_suite(d, assistant="bot", batch="a", count=10)

    assert await d.pump() == 2
    assert await d.pump() == 0                # bucket empty; nothing forced


# ------------------------------------------------------- the standing buffer

@pytest.mark.asyncio
async def test_pump_tops_up_only_to_the_setpoint(redis, queue):
    await set_config(redis, per_assistant_max_concurrency=3,
                     per_assistant_tps=1000)
    d = FairDispatcher(redis, queue)
    await submit_suite(d, assistant="bot", batch="a", count=10)

    assert await d.pump() == 3                # to the setpoint
    assert await d.pump() == 0                # already full — no over-stage

    # A worker picked one up → the buffer refills by exactly one.
    await d.worker_started("bot")
    assert await d.pump() == 1


def test_setpoint_is_capacity_derived_and_never_zero():
    assert setpoint_for(4) == 4
    assert setpoint_for(1) == 1
    assert setpoint_for(0) == 1               # never zero
    assert setpoint_for(-5) == 1
