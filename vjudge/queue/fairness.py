"""P4.5 — fair split + spread: water-filling across suites, paced dispatch.

The problem this solves: three suites all target one assistant whose budget
is K concurrent calls. Naive FIFO dispatch lets the 500-run suite starve the
5-run smoke test for an hour, and admitting a suite all at once slams the
assistant at minute boundaries.

Mechanism — a staging pool between admission and the stage streams:

* `submit()` parks a batch member in its suite's pool
  (`vjudge:pool:{assistant}:{batch}`) instead of the stage stream.
* `pump()` (a periodic cycle on the API process) tops the stage stream up to
  the **standing-buffer setpoint** — `max(1, per_assistant_max_concurrency)`,
  capacity-derived so a tuned budget moves it, NEVER throughput-derived (a
  slow assistant must not talk the buffer down to zero and stall itself) and
  never zero (a worker should always find the next run staged).
* Within one top-up, entries are taken **water-filling**: one per suite per
  round, so every suite's demand is met equally until the small suites drain
  and the remainder goes to the big one.
* Every move spends one token from the assistant's turns/sec bucket, so a
  minute's admissions are **spread across the minute** by the bucket's
  continuous refill instead of front-loading at the boundary.

The staged gauge (`vjudge:gauge:staged:{assistant}`) counts entries moved to
the stream but not yet picked up by a converse worker; the worker decrements
it (clamped at zero) when it starts the conversation.
"""
from __future__ import annotations

import logging
import uuid as uuid_mod
from dataclasses import dataclass

import redis.asyncio as aioredis

from vjudge.queue.admission import ExecutionBudget, load_config_async
from vjudge.queue.streams import StageQueue

logger = logging.getLogger(__name__)

POOLS_INDEX = "vjudge:pools"

# Clamped decrement — an entry that never went through a pool (interactive
# lane, direct dispatch) must not drive the gauge negative.
_DECR_CLAMPED_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current > 0 then
  return redis.call('DECR', KEYS[1])
end
return 0
"""


def pool_key(assistant: str, batch: str) -> str:
    return f"vjudge:pool:{assistant}:{batch}"


def staged_gauge(assistant: str) -> str:
    return f"vjudge:gauge:staged:{assistant}"


def setpoint_for(per_assistant_cap: int) -> int:
    """Capacity-derived, never zero, never throughput-derived."""
    return max(1, int(per_assistant_cap))


@dataclass(frozen=True)
class PoolRef:
    assistant: str
    batch: str

    @property
    def key(self) -> str:
        return pool_key(self.assistant, self.batch)

    def encode(self) -> str:
        return f"{self.assistant}|{self.batch}"

    @staticmethod
    def decode(raw: str) -> "PoolRef":
        assistant, _, batch = raw.partition("|")
        return PoolRef(assistant=assistant, batch=batch)


class FairDispatcher:
    def __init__(self, redis: aioredis.Redis, queue: StageQueue):
        self.redis = redis
        self.queue = queue
        self.budget = ExecutionBudget(redis)

    async def submit(
        self,
        *,
        assistant: str,
        batch: str,
        stage: str,
        run_uuid: uuid_mod.UUID,
        created_at: str,
        lane: str = "batch",
    ) -> None:
        """Park one batch member in its suite's pool. FIFO within a suite."""
        ref = PoolRef(assistant=assistant, batch=batch)
        await self.redis.rpush(
            ref.key, f"{stage}|{lane}|{run_uuid}|{created_at}"
        )
        await self.redis.sadd(POOLS_INDEX, ref.encode())

    async def worker_started(self, assistant: str) -> None:
        """Called by the converse worker when it picks a run up — one staged
        entry became an in-flight conversation."""
        await self.redis.eval(_DECR_CLAMPED_LUA, 1, staged_gauge(assistant))

    async def pump(self) -> int:
        """One dispatch cycle. Returns how many entries moved to streams."""
        cfg = await load_config_async(self.redis)
        setpoint = setpoint_for(cfg.per_assistant_max_concurrency)

        refs = [PoolRef.decode(raw) for raw in await self.redis.smembers(POOLS_INDEX)]
        by_assistant: dict[str, list[PoolRef]] = {}
        for ref in refs:
            by_assistant.setdefault(ref.assistant, []).append(ref)

        moved = 0
        for assistant, pools in by_assistant.items():
            staged = int(await self.redis.get(staged_gauge(assistant)) or 0)
            room = setpoint - staged
            # Deterministic suite order inside a round; the ROUND-robin is
            # what makes it water-filling, not the order itself.
            pools = sorted(pools, key=lambda r: r.batch)
            while room > 0:
                progressed = False
                for ref in pools:
                    if room <= 0:
                        break
                    # Spread: each move costs one turns/sec token — the
                    # bucket's continuous refill paces the minute.
                    if not await self.budget.take_token(
                        assistant=assistant, rate=cfg.per_assistant_tps
                    ):
                        room = 0
                        break
                    raw = await self.redis.lpop(ref.key)
                    if raw is None:
                        await self.redis.srem(POOLS_INDEX, ref.encode())
                        continue
                    stage, lane, run_uuid, created_at = raw.split("|", 3)
                    await self.queue.enqueue(
                        stage, run_uuid=uuid_mod.UUID(run_uuid),
                        created_at=created_at, lane=lane,
                    )
                    await self.redis.incr(staged_gauge(assistant))
                    moved += 1
                    room -= 1
                    progressed = True
                if not progressed:
                    break
        if moved:
            logger.debug("fair dispatcher moved %d entr(y/ies)", moved)
        return moved
