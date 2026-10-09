"""The two-worker proof (P4 acceptance) — real Redis, real Postgres, real
worker subprocesses. Run with:  pytest -m distributed tests/distributed/

Each test: seed rows (the record), enqueue entries (the dispatch), spawn
`_worker_proc` subprocesses, and measure through the probe keys the fake
runner writes into Redis.
"""
from __future__ import annotations

import asyncio
import signal
import time
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from judge_it.db.models import Run
from judge_it.queue.leases import LEASE_HISTORY
from judge_it.queue.streams import StageQueue, connect

from tests.distributed.conftest import (
    REDIS_URL,
    SCENARIO_BODY,
    _test_url,
    seed_runs,
    spawn_worker,
)

pytestmark = pytest.mark.distributed


def dispatch_all(rows, *, lane: str = "batch") -> None:
    async def _go():
        redis = connect(REDIS_URL)
        try:
            q = StageQueue(redis)
            await q.ensure_groups()
            for run_uuid, created_at, _name in rows:
                await q.enqueue("converse-text", run_uuid=run_uuid,
                                created_at=created_at, lane=lane)
        finally:
            await redis.aclose()

    asyncio.run(_go())


def wait_all_terminal(engine, expected: int, *, timeout_s: float) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with Session(engine) as session:
            rows = session.execute(select(Run.status)).scalars().all()
        counts: dict = {}
        for status in rows:
            counts[status] = counts.get(status, 0) + 1
        done = counts.get("completed", 0) + counts.get("failed", 0)
        if done >= expected:
            return counts
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {expected} terminal runs; "
                         f"last counts: {counts}")


def converse_acquisitions(redis) -> dict[str, list[str]]:
    """run_uuid → [consumers] for converse-stage lease acquisitions."""
    out: dict[str, list[str]] = {}
    for _id, fields in redis.xrange(LEASE_HISTORY):
        if fields.get("stage") == "converse-text":
            out.setdefault(fields["run_uuid"], []).append(fields["consumer"])
    return out


def stop(*procs, sig=signal.SIGTERM, timeout=15):
    for p in procs:
        if p.poll() is None:
            p.send_signal(sig)
    for p in procs:
        try:
            p.wait(timeout=timeout)
        except Exception:
            p.kill()
            p.wait(timeout=5)


# --------------------------------------------------------------- proof no. 1

def test_200_runs_complete_exactly_once_across_two_workers(
    dist_engine, dist_redis, clean_runs
):
    dist_redis.hset("judge_it:admission:config",
                    "per_assistant_max_concurrency", "64")
    rows = seed_runs(dist_engine, 200, name_prefix="p1")
    dispatch_all(rows)

    w1 = spawn_worker(consumer="w1", slots=8, sleep=0.03, lease=2.0)
    w2 = spawn_worker(consumer="w2", slots=8, sleep=0.03, lease=2.0)
    try:
        counts = wait_all_terminal(dist_engine, 200, timeout_s=120)
    finally:
        stop(w1, w2)

    assert counts.get("completed") == 200 and not counts.get("failed")
    # No run picked up by both workers — asserted from lease history.
    acquisitions = converse_acquisitions(dist_redis)
    assert len(acquisitions) == 200
    doubled = {r: c for r, c in acquisitions.items() if len(c) != 1}
    assert not doubled, f"runs claimed more than once: {doubled}"
    # And no conversation ran twice.
    for _u, _c, name in rows:
        assert dist_redis.get(f"probe:completions:{name}") == "1"


# --------------------------------------------------------------- proof no. 2

def test_kill9_mid_run_is_recovered_by_the_survivor(
    dist_engine, dist_redis, clean_runs
):
    dist_redis.hset("judge_it:admission:config",
                    "per_assistant_max_concurrency", "64")
    rows = seed_runs(dist_engine, 80, name_prefix="p2")
    dispatch_all(rows)

    w1 = spawn_worker(consumer="w1", slots=6, sleep=0.25, lease=2.0)
    w2 = spawn_worker(consumer="w2", slots=6, sleep=0.25, lease=2.0)
    try:
        time.sleep(2.0)                       # both mid-flight
        w1.kill()                             # kill -9, no drain
        w1.wait(timeout=5)
        counts = wait_all_terminal(dist_engine, 80, timeout_s=120)
    finally:
        stop(w2)

    assert counts.get("completed") == 80 and not counts.get("failed")
    # A run whose worker died may legitimately converse again (recovery);
    # only the small set in flight at the kill can — never the rest.
    doubled = [name for _u, _c, name in rows
               if int(dist_redis.get(f"probe:completions:{name}") or 0) > 1]
    assert len(doubled) <= 6, f"too many re-conversed runs: {doubled}"
    for _u, _c, name in rows:
        n = int(dist_redis.get(f"probe:completions:{name}") or 0)
        assert 1 <= n <= 2


# --------------------------------------------------------------- proof no. 3

def test_per_assistant_budget_of_2_holds_across_workers(
    dist_engine, dist_redis, clean_runs
):
    dist_redis.hset("judge_it:admission:config",
                    "per_assistant_max_concurrency", "2")
    target = "+15550002222"
    rows = seed_runs(dist_engine, 24, target=target, name_prefix="p3")
    dispatch_all(rows)

    w1 = spawn_worker(consumer="w1", slots=6, sleep=0.1, lease=2.0)
    w2 = spawn_worker(consumer="w2", slots=6, sleep=0.1, lease=2.0)
    try:
        counts = wait_all_terminal(dist_engine, 24, timeout_s=120)
    finally:
        stop(w1, w2)

    assert counts.get("completed") == 24
    peak = int(dist_redis.get(f"probe:peak:{target}") or 0)
    assert peak == 2, f"peak concurrency at the target was {peak}, wanted 2"


# --------------------------------------------------------------- proof no. 4

def test_sigterm_drains_within_deadline_zero_lost_zero_duplicated(
    dist_engine, dist_redis, clean_runs
):
    dist_redis.hset("judge_it:admission:config",
                    "per_assistant_max_concurrency", "64")
    rows = seed_runs(dist_engine, 50, name_prefix="p4")
    dispatch_all(rows)

    w1 = spawn_worker(consumer="w1", slots=6, sleep=0.3, lease=2.0,
                      drain_deadline=10.0)
    w2 = spawn_worker(consumer="w2", slots=6, sleep=0.3, lease=2.0)
    try:
        time.sleep(1.5)                       # both mid-flight
        w1.send_signal(signal.SIGTERM)
        t0 = time.monotonic()
        w1.wait(timeout=15)                   # drain deadline 10s + slack
        drain_s = time.monotonic() - t0
        assert drain_s < 12, f"worker took {drain_s:.1f}s to drain"

        counts = wait_all_terminal(dist_engine, 50, timeout_s=120)
    finally:
        stop(w2)

    assert counts.get("completed") == 50 and not counts.get("failed")
    # Graceful drain finished (and acked) its in-flight work: NOTHING reruns.
    for _u, _c, name in rows:
        assert dist_redis.get(f"probe:completions:{name}") == "1"


# --------------------------------------------------------------- proof no. 5

def test_interactive_run_starts_within_5s_during_a_batch_flood(
    dist_engine, dist_redis, clean_runs
):
    dist_redis.hset("judge_it:admission:config",
                    "per_assistant_max_concurrency", "50")
    flood = seed_runs(dist_engine, 60, target="+15550003333", name_prefix="p5b")
    dispatch_all(flood, lane="batch")

    w1 = spawn_worker(consumer="w1", slots=3, reserve=1, sleep=1.0, lease=3.0)
    w2 = spawn_worker(consumer="w2", slots=3, reserve=1, sleep=1.0, lease=3.0)
    try:
        time.sleep(2.0)                       # the flood is in full swing
        [(run_uuid, created_at, name)] = seed_runs(
            dist_engine, 1, target="+15550004444", name_prefix="p5i"
        )
        submitted = time.time()
        dispatch_all([(run_uuid, created_at, name)], lane="interactive")

        deadline = time.monotonic() + 20
        started = None
        while time.monotonic() < deadline:
            raw = dist_redis.get(f"probe:started:{name}")
            if raw:
                started = float(raw)
                break
            time.sleep(0.1)
        assert started is not None, "interactive run never started"
        wait_s = started - submitted
        assert wait_s < 5.0, f"interactive run waited {wait_s:.1f}s (PRD bound: 5s)"
    finally:
        stop(w1, w2)


# --------------------------------------------------------------- proof no. 6

def test_kill_switch_halts_admissions_within_one_cycle(
    dist_engine, dist_redis, clean_runs
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from judge_it.api import install_v3
    from tests.integration.api.conftest import make_settings

    settings = make_settings(
        admission_enabled=True, redis_url=REDIS_URL, queue_enabled=False,
    )
    app = FastAPI()

    class NullExecutor:
        async def execute(self, run_uuid):
            return None

    install_v3(app, settings=settings, engine=dist_engine,
               executor=NullExecutor())
    client = TestClient(app)

    scenario = client.post("/v1/scenarios", json={
        "key": f"ks-{uuid.uuid4().hex[:6]}", "body": SCENARIO_BODY,
    })
    assert scenario.status_code == 201, scenario.text
    scenario_id = scenario.json()["id"]

    ok = client.post("/v1/runs", json={"scenario_id": scenario_id, "region": "us"})
    assert ok.status_code == 201, ok.text            # gate open → admitted

    dist_redis.hset("judge_it:admission:config", "kill_switch", "1")
    time.sleep(1.1)                                   # one config cycle

    refused = client.post("/v1/runs", json={"scenario_id": scenario_id, "region": "us"})
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"]["error_type"] == "admission_refused"

    # …and flips back off within a cycle too — no restart anywhere.
    dist_redis.hset("judge_it:admission:config", "kill_switch", "0")
    time.sleep(1.1)
    again = client.post("/v1/runs", json={"scenario_id": scenario_id, "region": "us"})
    assert again.status_code == 201, again.text
