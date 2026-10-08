"""P6.4 — worker registration: each voice worker reports ITS OWN callback URL.

Lease-then-dial needs per-worker addressing: the worker that claims a voice
run dials Twilio with a TwiML/status/WS URL pointing at ITSELF, so the media
stream lands on the process that owns the session state. `_sessions` in
transport/server.py is a module-level dict — worker-local by construction,
which it always really was; this registry just makes the addressing explicit
and observable.

Registration lives in a per-worker Redis key with a heartbeat TTL, so
`list_workers` is also the fleet's liveness view (a dead worker's row simply
expires).
"""
from __future__ import annotations

import json
import time
from typing import Optional

import redis.asyncio as aioredis

WORKER_PREFIX = "vjudge:worker:"
DEFAULT_TTL_S = 30.0


def worker_key(consumer: str) -> str:
    return f"{WORKER_PREFIX}{consumer}"


class WorkerRegistry:
    def __init__(self, redis: aioredis.Redis, *, ttl_s: float = DEFAULT_TTL_S):
        self.redis = redis
        self.ttl_s = ttl_s

    async def register(self, *, consumer: str, public_url: str,
                       stages: tuple[str, ...]) -> None:
        await self.redis.set(
            worker_key(consumer),
            json.dumps({
                "consumer": consumer,
                "public_url": public_url,
                "stages": list(stages),
                "registered_at": time.time(),
            }),
            px=int(self.ttl_s * 1000),
        )

    async def heartbeat(self, consumer: str) -> bool:
        """Refresh the TTL. False means the row expired (re-register)."""
        extended = await self.redis.pexpire(
            worker_key(consumer), int(self.ttl_s * 1000)
        )
        return bool(extended)

    async def deregister(self, consumer: str) -> None:
        await self.redis.delete(worker_key(consumer))

    async def get(self, consumer: str) -> Optional[dict]:
        raw = await self.redis.get(worker_key(consumer))
        return json.loads(raw) if raw else None

    async def list_workers(self) -> list[dict]:
        out = []
        async for key in self.redis.scan_iter(match=f"{WORKER_PREFIX}*", count=100):
            raw = await self.redis.get(key)
            if raw:
                out.append(json.loads(raw))
        return out
