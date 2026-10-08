"""The two-worker proof's worker SUBPROCESS (run with `python -m`).

A real `worker_main` — real StageWorker, real leases, real housekeeping,
real SIGTERM handling — with the conversation faked: the ProbeRunner sleeps
instead of dialing, and writes its observations into Redis so the parent
test can measure across processes:

    probe:inflight:{target}   current concurrency at the fake target
    probe:peak:{target}       high-water mark (Lua max)
    probe:completions:{name}  how many times this scenario's conversation
                              finished (duplicate detector)
    probe:started:{name}      monotonic-ish wall time the conversation began
"""
from __future__ import annotations

import argparse
import asyncio
import time

_PEAK_LUA = """
local v = tonumber(redis.call('GET', KEYS[1]) or '0')
local n = tonumber(ARGV[1])
if n > v then redis.call('SET', KEYS[1], n) end
return math.max(v, n)
"""


class ProbeRunner:
    def __init__(self, redis, sleep_s: float):
        self.redis = redis
        self.sleep_s = sleep_s

    async def run_scenario(self, scenario):
        from vjudge.core.models import CallResult, CallStatus, TranscriptEntry

        target = scenario.target
        name = scenario.name
        await self.redis.set(f"probe:started:{name}", f"{time.time():.6f}", nx=True)
        n = await self.redis.incr(f"probe:inflight:{target}")
        await self.redis.eval(_PEAK_LUA, 1, f"probe:peak:{target}", str(n))
        try:
            await asyncio.sleep(self.sleep_s)
        finally:
            await self.redis.decr(f"probe:inflight:{target}")
        await self.redis.incr(f"probe:completions:{name}")
        result = CallResult(id=name, session_id=name, flow_name=name,
                            target=target, status=CallStatus.COMPLETED)
        result.transcript = [TranscriptEntry(speaker="bot", text="hi")]
        return result

    def _run_assertions(self, scenario, result):
        result.assertion_outcomes = []

    async def _run_evaluator(self, scenario, result):
        from vjudge.core.models import EvaluationResult

        return EvaluationResult(overall_passed=True, overall_reasoning="probe")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--stages", default="converse-text,evaluate,settle")
    parser.add_argument("--slots", type=int, default=8)
    parser.add_argument("--reserve", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--lease", type=float, default=2.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--drain-deadline", type=float, default=10.0)
    args = parser.parse_args()

    import logging

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from vjudge.cli.worker_cmd import worker_main
    from vjudge.core.config import Settings
    from vjudge.queue.streams import connect

    settings = Settings(
        _env_file=None,
        twilio_account_sid="ACproof", twilio_auth_token="t",
        twilio_phone_number="+15550001111", deepgram_api_key="dg",
        database_url=args.db_url,
        redis_url=args.redis_url,
        queue_lease_s=args.lease,
        queue_max_attempts=args.max_attempts,
        queue_drain_deadline_s=args.drain_deadline,
    )
    probe_redis = connect(args.redis_url)
    runner = ProbeRunner(probe_redis, sleep_s=args.sleep)

    asyncio.run(worker_main(
        settings,
        stages=tuple(s.strip() for s in args.stages.split(",")),
        consumer=args.consumer,
        slots=args.slots,
        interactive_reserve=args.reserve,
        runner_factory=lambda: runner,
    ))


if __name__ == "__main__":
    main()
