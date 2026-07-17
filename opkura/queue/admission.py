"""P4.4 — admission control: the no-op gate becomes real.

Three budgets and a kill switch, split across the two places they can
actually be enforced:

* **At admission** (API, sync): the kill switch, the fleet concurrency cap,
  and the per-client concurrency cap — read against live gauges. A refused
  admission is an explicit 429/503, never a silent queue.
* **At execution** (worker, async): the per-assistant concurrent-slot budget
  and the per-assistant turns/sec token bucket. Enforced where the calls are
  actually placed, so the cap holds ACROSS workers (the PRD's "budget of 2
  concurrent, peak measured at the target = 2, never 3").

**Env-gated existence**: `AUTOCALLS_ADMISSION_ENABLED=false` (the default)
makes the gate the historical no-op — no Redis reads at all on the admission
path. **Live tuning**: every knob lives in the Redis hash
`opkura:admission:config` and is re-read each cycle (a short cache), so the
kill switch halts new admissions within one cycle with nothing restarted.

**Asymmetric failure**, the PRD's exact rule:
* config read fails → DEFAULTS (the tuned values are lost, the gate stays);
* gauge/depth read fails → UNTHROTTLED (admit) — a broken Redis must degrade
  to "no limits", never to "no service".

Gauge keys (all plain counters, adjusted by the worker):
    opkura:gauge:inflight                      — fleet
    opkura:gauge:inflight:client:{client}      — per client
    opkura:gauge:inflight:assistant:{assistant}— per assistant (the budget)
"""
from __future__ import annotations

import logging
import time
import uuid as uuid_mod
from dataclasses import dataclass
from typing import Optional

import redis as sync_redis
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

CONFIG_HASH = "opkura:admission:config"
GAUGE_FLEET = "opkura:gauge:inflight"
CONFIG_CACHE_S = 1.0  # "within one cycle"


def gauge_client(client: str) -> str:
    return f"opkura:gauge:inflight:client:{client}"


def gauge_assistant(assistant: str) -> str:
    return f"opkura:gauge:inflight:assistant:{assistant}"


def bucket_assistant(assistant: str) -> str:
    return f"opkura:bucket:assistant:{assistant}"


def budget_keys(body: dict) -> tuple[str, str]:
    """(client, assistant) identity for budget purposes, derived one way
    everywhere. Assistant: the bot actually receiving traffic — assistant_uuid
    when the scenario names one, else the dialed target. Client: an explicit
    `client` field, else the first path segment of the scenario's identity."""
    assistant = str(body.get("assistant_uuid") or body.get("target") or "unknown")
    client = str(
        body.get("client")
        or str(body.get("name", "")).split("/", 1)[0].split(" ", 1)[0]
        or "unknown"
    )
    return client, assistant


@dataclass(frozen=True)
class AdmissionConfig:
    kill_switch: bool = False
    fleet_max_concurrency: int = 100
    per_client_max_concurrency: int = 20
    per_assistant_max_concurrency: int = 2
    per_assistant_tps: float = 2.0


DEFAULTS = AdmissionConfig()


def _parse_config(raw: dict) -> AdmissionConfig:
    def _int(key: str, default: int) -> int:
        try:
            return int(raw.get(key, default))
        except (TypeError, ValueError):
            return default

    def _float(key: str, default: float) -> float:
        try:
            return float(raw.get(key, default))
        except (TypeError, ValueError):
            return default

    return AdmissionConfig(
        kill_switch=str(raw.get("kill_switch", "0")).strip().lower() in ("1", "true", "on"),
        fleet_max_concurrency=_int("fleet_max_concurrency", DEFAULTS.fleet_max_concurrency),
        per_client_max_concurrency=_int(
            "per_client_max_concurrency", DEFAULTS.per_client_max_concurrency
        ),
        per_assistant_max_concurrency=_int(
            "per_assistant_max_concurrency", DEFAULTS.per_assistant_max_concurrency
        ),
        per_assistant_tps=_float("per_assistant_tps", DEFAULTS.per_assistant_tps),
    )


@dataclass(frozen=True)
class Decision:
    admitted: bool
    reason: str = ""
    # Refusals a caller can simply retry (budget pressure) vs the kill switch
    # (operator intent). The API maps these to 429 vs 503.
    retryable: bool = True


class AdmissionGate:
    """The API-side gate. Sync, because the admission path is sync."""

    def __init__(self, redis: sync_redis.Redis, *, enabled: bool,
                 cache_s: float = CONFIG_CACHE_S):
        self.redis = redis
        self.enabled = enabled
        self.cache_s = cache_s
        self._cached: Optional[AdmissionConfig] = None
        self._cached_at = 0.0

    def config(self) -> AdmissionConfig:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self.cache_s:
            return self._cached
        try:
            raw = self.redis.hgetall(CONFIG_HASH) or {}
            cfg = _parse_config(raw)
        except Exception as exc:
            # Asymmetric rule #1: config unreadable → defaults, gate stays up.
            logger.warning("admission: config read failed (%s) — using defaults", exc)
            cfg = DEFAULTS
        self._cached, self._cached_at = cfg, now
        return cfg

    def check(self, *, client: str, assistant: str) -> Decision:
        if not self.enabled:
            return Decision(admitted=True, reason="gate disabled")
        cfg = self.config()
        if cfg.kill_switch:
            return Decision(admitted=False, reason="kill switch engaged",
                            retryable=False)
        try:
            fleet = int(self.redis.get(GAUGE_FLEET) or 0)
            per_client = int(self.redis.get(gauge_client(client)) or 0)
        except Exception as exc:
            # Asymmetric rule #2: depth unreadable → unthrottled.
            logger.warning("admission: gauge read failed (%s) — unthrottled", exc)
            return Decision(admitted=True, reason="gauges unreadable; unthrottled")
        if fleet >= cfg.fleet_max_concurrency:
            return Decision(admitted=False,
                            reason=f"fleet at capacity ({fleet}/{cfg.fleet_max_concurrency})")
        if per_client >= cfg.per_client_max_concurrency:
            return Decision(admitted=False,
                            reason=f"client {client} at capacity "
                                   f"({per_client}/{cfg.per_client_max_concurrency})")
        return Decision(admitted=True)


async def load_config_async(redis: aioredis.Redis) -> AdmissionConfig:
    """The worker-side config read. Same asymmetric rule: unreadable → defaults."""
    try:
        raw = await redis.hgetall(CONFIG_HASH) or {}
    except Exception as exc:
        logger.warning("admission: async config read failed (%s) — defaults", exc)
        return DEFAULTS
    return _parse_config(raw)


# ---- worker-side budgets ----------------------------------------------------

# Acquire one slot iff the counter is below the limit. TTL guards a crashed
# worker from leaking a slot forever (the reaper's lease window is shorter, so
# a real recovery releases properly; the TTL is the belt for the belt).
_ACQUIRE_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current < tonumber(ARGV[1]) then
  local v = redis.call('INCR', KEYS[1])
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return v
end
return 0
"""
# Release without ever going negative (a slot released twice must not open
# capacity that was never held).
_RELEASE_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current > 0 then
  return redis.call('DECR', KEYS[1])
end
return 0
"""
# Token bucket: refill by elapsed time at `rate`/s up to `burst`, then take
# one. Returns 1 if a token was taken.
_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = burst ts = now end
tokens = math.min(burst, tokens + (now - ts) * rate)
local ok = 0
if tokens >= 1 then tokens = tokens - 1 ok = 1 end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 300)
return ok
"""

SLOT_TTL_S = 3600  # the leak-guard belt; releases are the real mechanism


class ExecutionBudget:
    """Worker-side budget: per-assistant slots + gauges, held across workers
    because they live in Redis, not in any process."""

    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    async def acquire(self, *, client: str, assistant: str, limit: int) -> bool:
        """Take one per-assistant slot; bump the fleet/client gauges with it.
        Returns False (and changes nothing) when the assistant is at limit."""
        got = await self.redis.eval(
            _ACQUIRE_LUA, 1, gauge_assistant(assistant), str(limit), str(SLOT_TTL_S)
        )
        if not got:
            return False
        await self.redis.incr(GAUGE_FLEET)
        await self.redis.incr(gauge_client(client))
        return True

    async def release(self, *, client: str, assistant: str) -> None:
        await self.redis.eval(_RELEASE_LUA, 1, gauge_assistant(assistant))
        await self.redis.eval(_RELEASE_LUA, 1, GAUGE_FLEET)
        await self.redis.eval(_RELEASE_LUA, 1, gauge_client(client))

    async def take_token(self, *, assistant: str, rate: float,
                         burst: Optional[float] = None) -> bool:
        """One turns/sec token for the assistant. Failure-open by policy:
        Redis trouble mid-run must not stall a conversation."""
        try:
            ok = await self.redis.eval(
                _BUCKET_LUA, 1, bucket_assistant(assistant),
                str(rate), str(burst if burst is not None else max(rate, 1.0)),
                f"{time.time():.6f}",
            )
            return bool(ok)
        except Exception as exc:
            logger.warning("token bucket read failed (%s) — unthrottled", exc)
            return True

    async def snapshot(self) -> dict:
        """The gauge endpoint's payload: every budget counter, best-effort."""
        out: dict = {"fleet_inflight": 0, "clients": {}, "assistants": {}}
        try:
            out["fleet_inflight"] = int(await self.redis.get(GAUGE_FLEET) or 0)
            for pattern, bucket in (
                ("opkura:gauge:inflight:client:*", "clients"),
                ("opkura:gauge:inflight:assistant:*", "assistants"),
            ):
                async for key in self.redis.scan_iter(match=pattern, count=100):
                    out[bucket][key.rsplit(":", 1)[-1]] = int(await self.redis.get(key) or 0)
        except Exception as exc:
            out["error"] = f"gauges unreadable: {exc}"
        return out
