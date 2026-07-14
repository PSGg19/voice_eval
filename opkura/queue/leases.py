"""P4.2 — leases and the reaper: a dead worker's runs are someone else's soon.

Ownership model, two layers with different jobs:

* **The lease key** (`opkura:lease:{run_uuid}`, value = consumer name,
  PX = lease window) is the EXECUTION arbiter: a worker runs a claimed entry
  only while it holds the lease, and heartbeats extend it (compare-and-expire,
  so a hijacked key is never extended by the old owner). Exactly-once
  execution reduces to SET NX on this key — the deliberate duplicate slack in
  dispatch (redispatch) collapses here.
* **The stream's pending-entries list** is the RECOVERY ledger: an entry
  claimed by a worker that stopped heartbeating goes idle; the reaper
  XAUTOCLAIMs entries idle past the lease window, checks the lease is truly
  dead, and re-queues with `attempt+1` — or, past `max_attempts`, dead-letters
  with the failure attached and fails the row.

`opkura:lease-history` records every acquisition (run, consumer, ts) — the
two-worker proof asserts "no run picked up by both workers" from it.
"""
from __future__ import annotations

import logging
import time
import uuid as uuid_mod
from dataclasses import dataclass, field
from typing import Optional

import redis.asyncio as aioredis

from opkura.queue.streams import GROUP, LANES, STAGES, Entry, StageQueue, stream_key

logger = logging.getLogger(__name__)

LEASE_PREFIX = "opkura:lease:"
LEASE_HISTORY = "opkura:lease-history"

# Owner-checked extend / delete. GET-compare-then-act must be atomic or a
# reaped-and-reassigned lease could be extended by its previous owner.
_EXTEND_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def lease_key(run_uuid: uuid_mod.UUID | str) -> str:
    return f"{LEASE_PREFIX}{run_uuid}"


class LeaseManager:
    def __init__(self, redis: aioredis.Redis, *, consumer: str, lease_s: float):
        self.redis = redis
        self.consumer = consumer
        self.lease_ms = int(lease_s * 1000)

    async def acquire(self, run_uuid: uuid_mod.UUID | str, *, stage: str = "") -> bool:
        ok = await self.redis.set(
            lease_key(run_uuid), self.consumer, nx=True, px=self.lease_ms
        )
        if ok:
            await self.redis.xadd(
                LEASE_HISTORY,
                {"run_uuid": str(run_uuid), "consumer": self.consumer,
                 "stage": stage, "ts": f"{time.time():.6f}"},
                maxlen=100_000, approximate=True,
            )
        return bool(ok)

    async def heartbeat(self, run_uuid: uuid_mod.UUID | str) -> bool:
        """Extend the lease iff this consumer still owns it."""
        extended = await self.redis.eval(
            _EXTEND_LUA, 1, lease_key(run_uuid), self.consumer, str(self.lease_ms)
        )
        return bool(extended)

    async def release(self, run_uuid: uuid_mod.UUID | str) -> bool:
        released = await self.redis.eval(
            _RELEASE_LUA, 1, lease_key(run_uuid), self.consumer
        )
        return bool(released)

    async def holder(self, run_uuid: uuid_mod.UUID | str) -> Optional[str]:
        return await self.redis.get(lease_key(run_uuid))


async def retry_or_dead(
    queue: StageQueue,
    entry: Entry,
    *,
    max_attempts: int,
    error: str,
) -> str:
    """One policy for every failure path: re-queue with attempt+1, or — at
    `max_attempts` — dead-letter with the failure attached. Returns
    'requeued' or 'dead'. The attempt counter travels ON the entry, so the
    Nth failure is the Nth regardless of which worker or sweep saw it."""
    if entry.attempt >= max_attempts:
        await queue.dead_letter(entry, error=error)
        return "dead"
    await queue.enqueue(
        entry.stage,
        run_uuid=entry.run_uuid,
        created_at=entry.created_at,
        lane=entry.lane,
        attempt=entry.attempt + 1,
    )
    await queue.ack(entry)
    return "requeued"


@dataclass
class ReapStats:
    requeued: int = 0
    dead: int = 0
    skipped_live: int = 0
    dead_runs: list[uuid_mod.UUID] = field(default_factory=list)


class Reaper:
    """Reclaims expired claims via each stream's pending-entries list.

    Runs periodically (on workers and/or the API process — sweeps are
    idempotent and safe to race: XAUTOCLAIM hands each pending entry to
    exactly one caller)."""

    def __init__(
        self,
        queue: StageQueue,
        *,
        lease_s: float,
        max_attempts: int,
        consumer: str = "reaper",
    ):
        self.queue = queue
        self.lease_ms = int(lease_s * 1000)
        self.max_attempts = max_attempts
        self.consumer = consumer

    async def sweep(self) -> ReapStats:
        stats = ReapStats()
        for stage in STAGES:
            for lane in LANES:
                await self._sweep_stream(stage, lane, stats)
        return stats

    async def _sweep_stream(self, stage: str, lane: str, stats: ReapStats) -> None:
        key = stream_key(stage, lane)
        start = "0-0"
        while True:
            resp = await self.queue.redis.xautoclaim(
                key, GROUP, self.consumer,
                min_idle_time=self.lease_ms, start_id=start, count=50,
            )
            # redis-py: [next_start, [(id, fields), …]] (+ deleted ids on 7.x)
            next_start, claimed = resp[0], resp[1]
            for entry_id, fields in claimed:
                entry = Entry(
                    entry_id=entry_id, stage=stage, lane=lane,
                    run_uuid=uuid_mod.UUID(fields["run_uuid"]),
                    created_at=fields["created_at"],
                    attempt=int(fields.get("attempt", "1")),
                )
                holder = await self.queue.redis.get(lease_key(entry.run_uuid))
                if holder is not None:
                    # The owner is alive and heartbeating — a long run, not a
                    # dead worker. Leave it pending; a later sweep rechecks.
                    stats.skipped_live += 1
                    continue
                outcome = await retry_or_dead(
                    self.queue, entry,
                    max_attempts=self.max_attempts,
                    error=f"lease expired on attempt {entry.attempt} "
                          f"(worker died or stopped heartbeating)",
                )
                if outcome == "dead":
                    stats.dead += 1
                    stats.dead_runs.append(entry.run_uuid)
                    logger.error(
                        "reaper: run %s dead-lettered after %d attempts",
                        entry.run_uuid, entry.attempt,
                    )
                else:
                    stats.requeued += 1
                    logger.warning(
                        "reaper: reclaimed run %s (attempt %d → %d)",
                        entry.run_uuid, entry.attempt, entry.attempt + 1,
                    )
            if not claimed or next_start == "0-0":
                break
            start = next_start
