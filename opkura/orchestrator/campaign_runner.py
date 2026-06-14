"""Phase 2 Task 3 — worker-pool campaign runner.

Runs every scenario in a `Campaign` through `N = campaign.max_concurrency`
parallel asyncio workers, each pulling from a shared queue. Workers
are isolated: one scenario's exception does not affect the others.

Design notes
------------
* `asyncio.Queue` as the work queue (FIFO; preserves YAML ordering).
* Exactly `N` worker tasks; no over- or under-allocation.
* Workers stop when the queue is empty (`get_nowait` raises
  `QueueEmpty`).
* `fail_fast` short-circuits the queue via a shared `asyncio.Event`
  set on the first failure. In-flight scenarios still complete; only
  un-pulled work is skipped.
* `worker_id_var` ContextVar is set per-worker so log records carry a
  stable worker identifier for triage when 10 calls run in parallel.
* Result aggregation is guarded by an `asyncio.Lock` even though
  `list.append` is GIL-safe in CPython — keeps semantics explicit and
  makes it trivial to swap in non-list aggregation later (e.g.,
  per-worker totals).

Each scenario is run by delegating to `TestRunner.run_scenario_file`,
which already handles V1/V2 dispatch, retries, evaluator invocation,
and per-call log folder finalization. The campaign runner adds nothing
to the single-call path — it only schedules.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Union

from opkura.core.log_context import worker_id_var
from opkura.core.models import (
    Campaign,
    CampaignResult,
    CallResult,
    CallStatus,
)

# Optional callback fired after each scenario completes. Receives
# (call_result, completed_so_far, total). Sync or async — runner
# awaits if it's a coroutine. Used by the CLI for live Rich progress
# output; tests don't need it.
ScenarioCompleteHook = Callable[
    [CallResult, int, int], Union[None, Awaitable[None]]
]

logger = logging.getLogger(__name__)


class CampaignRunner:
    """Worker-pool scheduler around `TestRunner.run_scenario_file`."""

    def __init__(self, runner) -> None:  # avoid circular import via type-hint
        # `runner` must be a `TestRunner` (or test fake providing the
        # same `run_scenario_file(path) -> CallResult` coroutine).
        self.runner = runner

    async def run(
        self,
        campaign: Campaign,
        on_scenario_complete: Optional[ScenarioCompleteHook] = None,
    ) -> CampaignResult:
        """Execute the campaign and return an aggregated result.

        Spawns `campaign.max_concurrency` worker coroutines, each
        pulling scenarios from a FIFO queue. Workers exit cleanly
        when the queue is empty OR fail-fast tripped.

        `on_scenario_complete` (optional) fires after each scenario
        finishes. Used by the CLI for live progress output; tests
        don't pass it. Coroutines are awaited; sync callables are
        called directly. Exceptions in the hook are logged but never
        propagate (a UI bug shouldn't break the campaign).
        """
        queue: asyncio.Queue[str] = asyncio.Queue()
        for path in campaign.resolved_scenarios:
            queue.put_nowait(path)

        results: list[CallResult] = []
        results_lock = asyncio.Lock()
        stop_event = asyncio.Event()

        started_at = datetime.now(timezone.utc)
        logger.info(
            "Campaign %r starting: %d scenarios, %d workers (fail_fast=%s)",
            campaign.name,
            len(campaign.resolved_scenarios),
            campaign.max_concurrency,
            campaign.fail_fast,
        )

        total = len(campaign.resolved_scenarios)
        worker_tasks = [
            asyncio.create_task(
                self._worker(
                    worker_id=i,
                    queue=queue,
                    results=results,
                    results_lock=results_lock,
                    stop_event=stop_event,
                    fail_fast=campaign.fail_fast,
                    on_scenario_complete=on_scenario_complete,
                    total=total,
                ),
                name=f"campaign-worker-{i}",
            )
            for i in range(campaign.max_concurrency)
        ]

        # gather() so any unexpected worker exception still propagates
        # to the caller — workers internally catch scenario errors, so
        # an actual raise here would indicate an opkura bug.
        await asyncio.gather(*worker_tasks)

        ended_at = datetime.now(timezone.utc)

        # Whatever's still in the queue was skipped — only possible under
        # fail_fast. Drain it for the report (so the reporter can list
        # "10 of 50 scenarios skipped after failure at scenario 5").
        skipped_paths: list[str] = []
        while not queue.empty():
            try:
                skipped_paths.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        result = self._aggregate(
            campaign=campaign,
            results=results,
            started_at=started_at,
            ended_at=ended_at,
            stop_event_set=stop_event.is_set(),
            skipped_paths=skipped_paths,
        )

        logger.info(
            "Campaign %r done: %d/%d passed (%.1f%%) in %.1fs",
            campaign.name,
            result.passed,
            result.total_scenarios,
            result.pass_rate * 100,
            result.duration_s,
        )
        return result

    async def _worker(
        self,
        worker_id: int,
        queue: "asyncio.Queue[str]",
        results: list[CallResult],
        results_lock: asyncio.Lock,
        stop_event: asyncio.Event,
        fail_fast: bool,
        on_scenario_complete: Optional[ScenarioCompleteHook] = None,
        total: int = 0,
    ) -> None:
        """One worker. Loops until queue empty or fail-fast tripped."""
        token = worker_id_var.set(worker_id)
        try:
            while True:
                if stop_event.is_set():
                    return
                try:
                    path = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                logger.info("worker %d picked up %s", worker_id, path)
                t0 = datetime.now(timezone.utc)

                try:
                    call_result = await self.runner.run_scenario_file(path)
                except Exception as exc:
                    # Worker isolation: one scenario's crash must not
                    # take down the rest of the campaign. Surface as a
                    # synthetic ERROR result so the reporter still lists
                    # the path with a failure reason.
                    logger.exception(
                        "worker %d: scenario %s raised — recording as ERROR",
                        worker_id, path,
                    )
                    call_result = self._synthetic_failure(path, exc)

                elapsed_s = (datetime.now(timezone.utc) - t0).total_seconds()
                logger.info(
                    "worker %d finished %s in %.1fs (passed=%s)",
                    worker_id, path, elapsed_s, call_result.passed,
                )

                async with results_lock:
                    results.append(call_result)
                    completed = len(results)

                # Fire the CLI progress hook outside the lock so a slow
                # hook (e.g., Rich screen refresh) doesn't serialize
                # other workers' result appends. Hook errors are caught
                # and logged — a UI bug must not break a campaign.
                if on_scenario_complete is not None:
                    try:
                        hook_result = on_scenario_complete(
                            call_result, completed, total
                        )
                        if asyncio.iscoroutine(hook_result):
                            await hook_result
                    except Exception as hook_exc:
                        logger.warning(
                            "on_scenario_complete hook raised: %s — ignored",
                            hook_exc,
                        )

                # fail_fast: set the stop flag so the next iteration of
                # this AND every other worker exits without pulling more
                # work. In-flight scenarios on other workers still complete.
                if fail_fast and not call_result.passed:
                    if not stop_event.is_set():
                        logger.warning(
                            "fail_fast: failure in %s — draining queue",
                            path,
                        )
                        stop_event.set()

                queue.task_done()
        finally:
            worker_id_var.reset(token)

    @staticmethod
    def _synthetic_failure(path: str, exc: BaseException) -> CallResult:
        """Build a minimal `CallResult` representing a scenario the
        worker couldn't even start (or one that raised mid-flight).

        Used only when the runner's own try/except chain leaks an
        exception — normal pass/fail outcomes go through the
        evaluator and CallResult is fully populated by the runner.
        """
        cr = CallResult(
            flow_name=str(path),
            target="",
            started_at=datetime.now(timezone.utc),
        )
        cr.finish(CallStatus.ERROR, f"worker caught exception: {exc!r}")
        return cr

    @staticmethod
    def _aggregate(
        campaign: Campaign,
        results: list[CallResult],
        started_at: datetime,
        ended_at: datetime,
        stop_event_set: bool,
        skipped_paths: list[str],
    ) -> CampaignResult:
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        # `failed` = ran to completion but evaluator said no
        # `errored` = setup error, timeout, exception (no evaluator verdict)
        failed = sum(
            1 for r in results
            if not r.passed and r.status == CallStatus.COMPLETED
        )
        errored = sum(
            1 for r in results
            if r.status in (CallStatus.ERROR, CallStatus.FAILED)
        )
        pass_rate = (passed / total) if total else 0.0
        duration_s = (ended_at - started_at).total_seconds()

        return CampaignResult(
            campaign_name=campaign.name,
            source_path=campaign.source_path,
            started_at=started_at,
            ended_at=ended_at,
            duration_s=duration_s,
            total_scenarios=len(campaign.resolved_scenarios),
            passed=passed,
            failed=failed,
            errored=errored,
            pass_rate=pass_rate,
            call_results=results,
            fail_fast_triggered=stop_event_set,
            skipped_paths=skipped_paths,
        )
