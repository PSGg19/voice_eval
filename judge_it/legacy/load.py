"""Load test orchestrator — runs N concurrent calls with CPS rate limiting."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from judge_it.core.models import CallResult, CallStatus, Flow, LoadTestConfig, LoadTestResult
from judge_it.orchestrator.runner import TestRunner

logger = logging.getLogger(__name__)


class LoadTestRunner:
    """Runs multiple concurrent calls against a bot and aggregates results.

    Uses the existing TestRunner.run_flow() for each call — no changes to the
    single-call pipeline. Concurrency is controlled by CPS pacing and an
    optional max_concurrent semaphore.
    """

    def __init__(self, runner: TestRunner, config: LoadTestConfig):
        self.runner = runner
        self.config = config

    async def run(
        self,
        flow: Flow,
        on_call_complete: callable | None = None,
    ) -> LoadTestResult:
        """Execute the load test.

        Args:
            flow: The conversation flow to test.
            on_call_complete: Optional callback(index, total, CallResult) for progress.
        """
        result = LoadTestResult(
            flow_name=flow.name,
            target=flow.target,
            config=self.config,
            started_at=datetime.now(timezone.utc),
        )

        call_results: list[CallResult] = []
        lock = asyncio.Lock()
        completed = 0

        semaphore = (
            asyncio.Semaphore(self.config.max_concurrent)
            if self.config.max_concurrent > 0
            else None
        )

        async def run_single(index: int):
            nonlocal completed

            if semaphore:
                await semaphore.acquire()

            try:
                cr = await self.runner.run_flow(flow)
            except Exception as exc:
                logger.error("Call %d failed with exception: %s", index + 1, exc)
                cr = CallResult(
                    flow_name=flow.name,
                    target=flow.target,
                    started_at=datetime.now(timezone.utc),
                )
                cr.finish(CallStatus.ERROR, str(exc))
            finally:
                if semaphore:
                    semaphore.release()

            async with lock:
                call_results.append(cr)
                completed += 1

            if on_call_complete:
                on_call_complete(completed, self.config.calls, cr)

        # Launch calls at the configured CPS rate
        interval = 1.0 / self.config.cps if self.config.cps > 0 else 0
        tasks: list[asyncio.Task] = []

        logger.info(
            "Starting load test: %d calls at %.1f CPS → %s",
            self.config.calls,
            self.config.cps,
            flow.target,
        )

        for i in range(self.config.calls):
            task = asyncio.create_task(run_single(i))
            tasks.append(task)
            # Pace launches (skip sleep after the last one)
            if i < self.config.calls - 1 and interval > 0:
                await asyncio.sleep(interval)

        # Wait for all calls to finish
        await asyncio.gather(*tasks, return_exceptions=True)

        result.ended_at = datetime.now(timezone.utc)
        result.call_results = call_results
        _compute_stats(result)

        logger.info(
            "Load test complete: %d/%d passed (%.1f%%) in %.1fs",
            result.passed_calls,
            result.total_calls,
            result.pass_rate,
            (result.total_duration_ms or 0) / 1000,
        )

        return result


def _compute_stats(result: LoadTestResult):
    """Fill in aggregate stats from individual call results."""
    calls = result.call_results
    result.total_calls = len(calls)
    result.passed_calls = sum(1 for c in calls if c.passed)
    result.failed_calls = sum(
        1
        for c in calls
        if not c.passed and c.status == CallStatus.COMPLETED
    )
    result.error_calls = sum(
        1
        for c in calls
        if c.status in (CallStatus.ERROR, CallStatus.FAILED)
    )

    durations = sorted(c.duration_ms for c in calls if c.duration_ms is not None)
    if durations:
        result.avg_duration_ms = sum(durations) / len(durations)
        result.min_duration_ms = durations[0]
        result.max_duration_ms = durations[-1]
        result.p50_duration_ms = _percentile(durations, 50)
        result.p95_duration_ms = _percentile(durations, 95)


def _percentile(sorted_data: list[float], p: int) -> float:
    """Linear interpolation percentile on pre-sorted data."""
    n = len(sorted_data)
    if n == 1:
        return sorted_data[0]
    k = (n - 1) * p / 100
    f = int(k)
    c = min(f + 1, n - 1)
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])
