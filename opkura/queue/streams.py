"""P4.1 — queue stages: Redis Streams with consumer groups, one per stage.

Four stages, two lanes (P4.6), one bounded stream per (stage, lane):

    opkura:stage:converse-text:interactive
    opkura:stage:converse-text:batch
    opkura:stage:converse-voice:…   (consumed by the API process this phase)
    opkura:stage:evaluate:…
    opkura:stage:settle:…

plus `opkura:dead`, the dead-letter stream (P4.2 writes there).

Contracts:

* **The row first.** Enqueue happens only after the Postgres row exists; an
  entry is (run_uuid, created_at) — the partitioned PK — plus lane and
  attempt. Nothing in Redis is the truth about a run.
* **Every stream bounded.** XADD uses MAXLEN ~ so a stuck consumer cannot
  grow Redis without limit; the cap is generous because entries are tiny and
  eviction of an unconsumed entry is detectable (the row stays 'queued' and
  the dispatch sweeper re-enqueues it).
* **Lanes are claim priority, not partitions.** A claim drains interactive
  first, then batch — the reserved-slice policy lives in the worker (P4.6);
  this module only orders the reads.
"""
from __future__ import annotations

import logging
import uuid as uuid_mod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

STAGES = ("converse-text", "converse-voice", "evaluate", "settle")
LANES = ("interactive", "batch")
GROUP = "workers"
DEAD_STREAM = "opkura:dead"
DEFAULT_MAXLEN = 100_000


def stream_key(stage: str, lane: str) -> str:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; stages are {STAGES}")
    if lane not in LANES:
        raise ValueError(f"unknown lane {lane!r}; lanes are {LANES}")
    return f"opkura:stage:{stage}:{lane}"


@dataclass(frozen=True)
class Entry:
    """One claimed queue entry. `entry_id` + (stage, lane) identify it for
    ack/heartbeat; `run_uuid` + `created_at` are the row's partitioned PK."""

    entry_id: str
    stage: str
    lane: str
    run_uuid: uuid_mod.UUID
    created_at: str          # ISO string, parsed by the DB layer
    attempt: int


def connect(redis_url: str) -> aioredis.Redis:
    """One place to build the client so decode_responses stays consistent."""
    return aioredis.from_url(redis_url, decode_responses=True)


class StageQueue:
    def __init__(self, redis: aioredis.Redis, *, maxlen: int = DEFAULT_MAXLEN):
        self.redis = redis
        self.maxlen = maxlen

    async def ensure_groups(self) -> None:
        """Idempotently create every (stage, lane) stream + consumer group."""
        for stage in STAGES:
            for lane in LANES:
                await self._ensure_group(stream_key(stage, lane))
        await self._ensure_group(DEAD_STREAM)

    async def _ensure_group(self, key: str) -> None:
        try:
            await self.redis.xgroup_create(key, GROUP, id="0", mkstream=True)
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(
        self,
        stage: str,
        *,
        run_uuid: uuid_mod.UUID,
        created_at: datetime | str,
        lane: str = "batch",
        attempt: int = 1,
    ) -> str:
        """Dispatch one run into a stage. The row MUST already exist."""
        created = created_at.isoformat() if isinstance(created_at, datetime) else created_at
        entry_id = await self.redis.xadd(
            stream_key(stage, lane),
            {
                "run_uuid": str(run_uuid),
                "created_at": created,
                "attempt": str(attempt),
            },
            maxlen=self.maxlen,
            approximate=True,      # bounded, cheaply (P4.1)
        )
        return entry_id

    async def claim(
        self,
        stage: str,
        *,
        consumer: str,
        lanes: Sequence[str] = LANES,   # interactive first by default
        count: int = 1,
        block_ms: int = 1000,
    ) -> list[Entry]:
        """Claim up to `count` new entries, draining lanes in the given order.

        Blocks up to `block_ms` only on the LAST lane, so an empty interactive
        lane costs one non-blocking read, not a full block interval.
        """
        out: list[Entry] = []
        for i, lane in enumerate(lanes):
            want = count - len(out)
            if want <= 0:
                break
            is_last = i == len(lanes) - 1
            resp = await self.redis.xreadgroup(
                GROUP, consumer,
                {stream_key(stage, lane): ">"},
                count=want,
                block=(block_ms if (is_last and not out) else None),
            )
            for _key, entries in resp or []:
                for entry_id, fields in entries:
                    out.append(self._entry(stage, lane, entry_id, fields))
        return out

    def _entry(self, stage: str, lane: str, entry_id: str, fields: dict) -> Entry:
        return Entry(
            entry_id=entry_id,
            stage=stage,
            lane=lane,
            run_uuid=uuid_mod.UUID(fields["run_uuid"]),
            created_at=fields["created_at"],
            attempt=int(fields.get("attempt", "1")),
        )

    async def ack(self, entry: Entry) -> None:
        await self.redis.xack(stream_key(entry.stage, entry.lane), GROUP, entry.entry_id)
        # Acked entries have served their purpose; deleting keeps XLEN an
        # honest backlog gauge instead of a history that MAXLEN trims blindly.
        await self.redis.xdel(stream_key(entry.stage, entry.lane), entry.entry_id)

    async def dead_letter(self, entry: Entry, *, error: str) -> None:
        """Move a poisoned entry out of circulation with its failure attached."""
        await self.redis.xadd(
            DEAD_STREAM,
            {
                "run_uuid": str(entry.run_uuid),
                "created_at": entry.created_at,
                "stage": entry.stage,
                "lane": entry.lane,
                "attempt": str(entry.attempt),
                "error": error[:2000],
            },
            maxlen=self.maxlen,
            approximate=True,
        )
        await self.ack(entry)

    async def depth(self, stage: str, lane: Optional[str] = None) -> int:
        """Backlog size — the admission gate's asymmetric read (P4.4): callers
        treat a failure here as 'unthrottled', never as 'refuse'."""
        lanes = [lane] if lane else list(LANES)
        total = 0
        for ln in lanes:
            total += await self.redis.xlen(stream_key(stage, ln))
        return total
