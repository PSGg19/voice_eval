"""Redis-backed job store.

Deliberately not a process dict. `transport/server.py:22` keeps `_sessions` as
one, and the consequence is documented in `docs/platform-evolution.md`: a
restart loses every in-flight call and it cannot scale past one pod. Job state
repeating that mistake would be worse, because a job owns a whole campaign.

Key layout (all namespaced, all TTL'd so a PoC cannot leak forever):

    vjudge:job:{job_id}                       STRING  the Job document as JSON
    vjudge:job:{job_id}:seed:{iuuid}          STRING  SeededCall as JSON
    vjudge:job:{job_id}:seeds                 SET     which iuuids have been seeded
    vjudge:prompt:{client}:{aid}:{ver}        STRING  prompt cache, shared across jobs

The prompt cache is keyed by client+assistant+version, **not** by job or call:
six faulty calls on one bot should cost one prompt fetch.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

import redis.asyncio as aioredis

from vjudge.jobs.models import Job, SeededCall

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("VJUDGE_REDIS_URL", "redis://localhost:6379/0")

# 7 days for job docs, 1 hour for prompts. Prompts change with a flow-config
# version, and the version is in the key, so a short TTL only costs a refetch.
JOB_TTL_S = int(os.getenv("VJUDGE_JOB_TTL_S", str(7 * 24 * 3600)))
PROMPT_TTL_S = int(os.getenv("VJUDGE_PROMPT_TTL_S", "3600"))

_NS = "vjudge"


def new_job_id() -> str:
    """Short, sortable-ish, greppable in a log line."""
    return f"job_{uuid.uuid4().hex[:12]}"


def _job_key(job_id: str) -> str:
    return f"{_NS}:job:{job_id}"


def _seed_key(job_id: str, interaction_uuid: str) -> str:
    return f"{_NS}:job:{job_id}:seed:{interaction_uuid}"


def _seeds_key(job_id: str) -> str:
    return f"{_NS}:job:{job_id}:seeds"


def prompt_key(client_uuid: str, assistant_id: int | str, version: str | int | None) -> str:
    return f"{_NS}:prompt:{client_uuid}:{assistant_id}:{version or 'prod'}"


class JobStore:
    """Async Redis wrapper. One instance per process is plenty."""

    def __init__(self, url: str | None = None):
        # Prefer Settings (which reads `.env`) over os.getenv: `.env` is never
        # exported into the process environment, so a value set there is
        # invisible to os.getenv. Same trap that made the OSV token look unset.
        if url is None:
            try:
                from vjudge.core.config import Settings

                url = Settings().redis_url
            except Exception:  # pragma: no cover - env-dependent
                url = None
        self.url = url or REDIS_URL
        self._redis: Optional[aioredis.Redis] = None

    async def redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(self.url, decode_responses=True)
        return self._redis

    async def ping(self) -> bool:
        try:
            return bool(await (await self.redis()).ping())
        except Exception as exc:  # pragma: no cover - depends on a live server
            logger.warning("Redis unreachable at %s: %s", self.url, exc)
            return False

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    # ------------------------------------------------------------------- jobs

    async def save(self, job: Job) -> Job:
        r = await self.redis()
        await r.set(_job_key(job.job_id), job.model_dump_json(), ex=JOB_TTL_S)
        return job

    async def get(self, job_id: str) -> Optional[Job]:
        raw = await (await self.redis()).get(_job_key(job_id))
        if raw is None:
            return None
        return Job.model_validate_json(raw)

    async def exists(self, job_id: str) -> bool:
        return bool(await (await self.redis()).exists(_job_key(job_id)))

    async def list_jobs(self) -> list[Job]:
        """Every live job doc, newest first. Seed and seed-set keys share the
        `vjudge:job:` prefix with extra `:seed…` segments, so match on the
        exact two-segment shape rather than a bare prefix."""
        r = await self.redis()
        jobs: list[Job] = []
        async for key in r.scan_iter(match=f"{_NS}:job:*", count=200):
            name = key.decode() if isinstance(key, bytes) else key
            if name.count(":") != 2:          # vjudge:job:<id> exactly
                continue
            raw = await r.get(name)
            if raw is None:
                continue
            try:
                jobs.append(Job.model_validate_json(raw))
            except Exception:                  # a foreign/corrupt doc never
                continue                       # breaks the listing
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    # ------------------------------------------------------------------ seeds

    async def save_seed(self, job_id: str, seed: SeededCall) -> None:
        r = await self.redis()
        pipe = r.pipeline()
        pipe.set(
            _seed_key(job_id, seed.interaction_uuid),
            seed.model_dump_json(),
            ex=JOB_TTL_S,
        )
        pipe.sadd(_seeds_key(job_id), seed.interaction_uuid)
        pipe.expire(_seeds_key(job_id), JOB_TTL_S)
        await pipe.execute()

    async def get_seed(self, job_id: str, interaction_uuid: str) -> Optional[SeededCall]:
        raw = await (await self.redis()).get(_seed_key(job_id, interaction_uuid))
        return SeededCall.model_validate_json(raw) if raw else None

    async def list_seeds(self, job_id: str) -> list[SeededCall]:
        r = await self.redis()
        iuuids = sorted(await r.smembers(_seeds_key(job_id)))
        if not iuuids:
            return []
        raws = await r.mget([_seed_key(job_id, i) for i in iuuids])
        return [SeededCall.model_validate_json(x) for x in raws if x]

    # ----------------------------------------------------------- prompt cache

    async def get_prompt(
        self, client_uuid: str, assistant_id: int | str, version: str | int | None
    ) -> Optional[str]:
        return await (await self.redis()).get(prompt_key(client_uuid, assistant_id, version))

    async def set_prompt(
        self,
        client_uuid: str,
        assistant_id: int | str,
        version: str | int | None,
        payload: str,
    ) -> None:
        await (await self.redis()).set(
            prompt_key(client_uuid, assistant_id, version), payload, ex=PROMPT_TTL_S
        )

    # ------------------------------------------------------------ callback sink

    async def push_hook(self, sink_id: str, payload: str) -> int:
        """Append a received callback. Returns the new count.

        Exists so a caller with no public HTTP server — Claude Code, a script,
        anyone on a laptop without a tunnel — can still use the callback pattern:
        point `callback_url` at vjudge's own sink and read the deliveries back.
        """
        r = await self.redis()
        key = f"{_NS}:hook:{sink_id}"
        pipe = r.pipeline()
        pipe.rpush(key, payload)
        pipe.expire(key, JOB_TTL_S)
        result = await pipe.execute()
        return int(result[0]) if result else 0

    async def list_hooks(self, sink_id: str) -> list[str]:
        return await (await self.redis()).lrange(f"{_NS}:hook:{sink_id}", 0, -1)

    async def clear_hooks(self, sink_id: str) -> None:
        await (await self.redis()).delete(f"{_NS}:hook:{sink_id}")


# Module-level singleton, same convention as services.osv.osv_client.
job_store = JobStore()
