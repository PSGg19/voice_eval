"""P4.4 — admission control: the gate, the budgets, the asymmetric failures."""
from __future__ import annotations

import asyncio
import time

import fakeredis
import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeaioredis

from opkura.queue.admission import (
    CONFIG_HASH,
    DEFAULTS,
    GAUGE_FLEET,
    AdmissionGate,
    ExecutionBudget,
    budget_keys,
    gauge_client,
    load_config_async,
)


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest_asyncio.fixture
async def aredis():
    r = fakeaioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class BrokenRedis:
    """Every operation fails — the asymmetric-failure tests' Redis."""

    def __getattr__(self, name):
        def boom(*a, **kw):
            raise ConnectionError("redis is down")
        return boom


# --------------------------------------------------------------------- gate

def test_disabled_gate_is_the_historical_noop():
    gate = AdmissionGate(BrokenRedis(), enabled=False)   # never touches Redis
    assert gate.check(client="c", assistant="a").admitted is True


def test_kill_switch_refuses_non_retryably(redis):
    redis.hset(CONFIG_HASH, "kill_switch", "1")
    gate = AdmissionGate(redis, enabled=True, cache_s=0)
    decision = gate.check(client="c", assistant="a")
    assert decision.admitted is False
    assert decision.retryable is False
    assert "kill switch" in decision.reason


def test_kill_switch_halts_within_one_cycle(redis):
    """Flipped in the hash mid-flight — no restart, next cycle refuses."""
    gate = AdmissionGate(redis, enabled=True, cache_s=0.02)
    assert gate.check(client="c", assistant="a").admitted is True

    redis.hset(CONFIG_HASH, "kill_switch", "true")
    time.sleep(0.03)                                     # one cache cycle
    assert gate.check(client="c", assistant="a").admitted is False


def test_config_read_failure_means_defaults_not_closed(redis):
    """Asymmetric rule #1: config unreadable → the gate runs on DEFAULTS."""
    gate = AdmissionGate(BrokenRedis(), enabled=True, cache_s=0)
    # DEFAULTS has no kill switch… but gauges are also unreadable here, which
    # trips rule #2 → unthrottled admit. Both rules together: never refuse
    # because Redis broke.
    decision = gate.check(client="c", assistant="a")
    assert decision.admitted is True
    assert gate.config() == DEFAULTS


def test_gauge_read_failure_means_unthrottled(redis):
    class ConfigOnlyRedis:
        def hgetall(self, key):
            return {"fleet_max_concurrency": "1"}
        def get(self, key):
            raise ConnectionError("gauge store down")

    gate = AdmissionGate(ConfigOnlyRedis(), enabled=True, cache_s=0)
    decision = gate.check(client="c", assistant="a")
    assert decision.admitted is True
    assert "unthrottled" in decision.reason


def test_fleet_cap_refuses_retryably(redis):
    redis.hset(CONFIG_HASH, "fleet_max_concurrency", "2")
    redis.set(GAUGE_FLEET, 2)
    gate = AdmissionGate(redis, enabled=True, cache_s=0)
    decision = gate.check(client="c", assistant="a")
    assert decision.admitted is False and decision.retryable is True
    assert "fleet at capacity" in decision.reason


def test_per_client_cap(redis):
    redis.hset(CONFIG_HASH, "per_client_max_concurrency", "3")
    redis.set(gauge_client("veros"), 3)
    gate = AdmissionGate(redis, enabled=True, cache_s=0)
    assert gate.check(client="veros", assistant="a").admitted is False
    assert gate.check(client="acme", assistant="a").admitted is True


# ------------------------------------------------------------------ budgets

@pytest.mark.asyncio
async def test_per_assistant_slots_hold_across_budget_instances(aredis):
    """Two workers, one Redis: the limit binds them jointly — the unit-level
    shape of the PRD's 'peak concurrency = 2, never 3'."""
    w1, w2 = ExecutionBudget(aredis), ExecutionBudget(aredis)

    assert await w1.acquire(client="c", assistant="bot", limit=2) is True
    assert await w2.acquire(client="c", assistant="bot", limit=2) is True
    assert await w1.acquire(client="c", assistant="bot", limit=2) is False
    assert await w2.acquire(client="c", assistant="bot", limit=2) is False

    await w1.release(client="c", assistant="bot")
    assert await w2.acquire(client="c", assistant="bot", limit=2) is True


@pytest.mark.asyncio
async def test_release_never_goes_negative(aredis):
    budget = ExecutionBudget(aredis)
    await budget.release(client="c", assistant="bot")     # nothing held
    await budget.release(client="c", assistant="bot")
    # a limit-1 slot is still exactly one slot
    assert await budget.acquire(client="c", assistant="bot", limit=1) is True
    assert await budget.acquire(client="c", assistant="bot", limit=1) is False


@pytest.mark.asyncio
async def test_failed_acquire_changes_no_gauges(aredis):
    budget = ExecutionBudget(aredis)
    await budget.acquire(client="c", assistant="bot", limit=1)
    await budget.acquire(client="c", assistant="bot", limit=1)   # refused
    snap = await budget.snapshot()
    assert snap["fleet_inflight"] == 1
    assert snap["assistants"] == {"bot": 1}
    assert snap["clients"] == {"c": 1}


@pytest.mark.asyncio
async def test_token_bucket_rate_limits_then_refills(aredis):
    budget = ExecutionBudget(aredis)
    assert await budget.take_token(assistant="bot", rate=100.0, burst=2) is True
    assert await budget.take_token(assistant="bot", rate=100.0, burst=2) is True
    assert await budget.take_token(assistant="bot", rate=100.0, burst=2) is False
    await asyncio.sleep(0.03)                             # 100/s → refilled
    assert await budget.take_token(assistant="bot", rate=100.0, burst=2) is True


@pytest.mark.asyncio
async def test_async_config_defaults_on_failure():
    class Broken:
        async def hgetall(self, key):
            raise ConnectionError("down")

    assert await load_config_async(Broken()) == DEFAULTS


# --------------------------------------------------------------------- keys

def test_budget_keys_prefer_explicit_identity():
    assert budget_keys({"assistant_uuid": "abc", "client": "veros"}) == ("veros", "abc")
    assert budget_keys({"target": "+15550001111", "name": "veros/IB2 test"}) == (
        "veros", "+15550001111",
    )
    assert budget_keys({}) == ("unknown", "unknown")
