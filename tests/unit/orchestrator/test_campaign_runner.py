"""Unit tests for the Phase 2 worker-pool campaign runner.

All tests use a `_FakeRunner` instead of a real `TestRunner` — no
Twilio, no Deepgram, no Gemini. The fake records which scenario each
worker picked up and how long the work took, so we can verify queue
draining, concurrency caps, isolation, and fail-fast semantics
without any external calls.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import pytest

from opkura.core.log_context import worker_id_var
from opkura.core.models import (
    Campaign,
    CallResult,
    CallStatus,
    EvaluationResult,
)
from opkura.orchestrator.campaign_runner import CampaignRunner


def _make_campaign(
    paths: list[str],
    max_concurrency: int = 2,
    fail_fast: bool = False,
) -> Campaign:
    return Campaign(
        name="test campaign",
        scenarios=paths,
        resolved_scenarios=paths,
        max_concurrency=max_concurrency,
        fail_fast=fail_fast,
    )


class _FakeRunner:
    """Mock of TestRunner for campaign-runner tests.

    Records every (path, worker_id, t_start, t_end) tuple so tests can
    assert FIFO order, max-in-flight, and per-worker assignment.
    Optionally simulates a sleep so concurrency overlap is observable.
    """

    def __init__(
        self,
        per_scenario_sleep: float = 0.0,
        fail_on_paths: Optional[set[str]] = None,
        raise_on_paths: Optional[set[str]] = None,
    ) -> None:
        self.per_scenario_sleep = per_scenario_sleep
        self.fail_on_paths = fail_on_paths or set()
        self.raise_on_paths = raise_on_paths or set()
        # (path, worker_id_at_pickup, start_time, end_time)
        self.calls: list[tuple] = []
        self._lock = asyncio.Lock()
        # Track max-in-flight to verify max_concurrency is honored.
        self._in_flight = 0
        self.max_in_flight = 0

    async def run_scenario_file(self, path: str) -> CallResult:
        worker_id = worker_id_var.get()
        async with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)

        t_start = asyncio.get_event_loop().time()
        try:
            if path in self.raise_on_paths:
                raise RuntimeError(f"simulated raise for {path}")
            if self.per_scenario_sleep > 0:
                await asyncio.sleep(self.per_scenario_sleep)
            t_end = asyncio.get_event_loop().time()

            # Build a synthetic CallResult based on whether this path
            # should "fail" (completed but the evaluator said no).
            # `CallResult.passed` is computed: V2 → evaluation.overall_passed,
            # V1 → all steps PASSED. We attach an evaluation so the V2
            # path is exercised (matches real campaign runs).
            cr = CallResult(
                flow_name=path,
                target="+15555550100",
                started_at=datetime.now(timezone.utc),
            )
            cr.finish(CallStatus.COMPLETED)
            cr.evaluation = EvaluationResult(
                overall_passed=path not in self.fail_on_paths,
                overall_reasoning=(
                    "simulated failure" if path in self.fail_on_paths
                    else "simulated pass"
                ),
            )

            self.calls.append((path, worker_id, t_start, t_end))
            return cr
        finally:
            async with self._lock:
                self._in_flight -= 1


# ----- Basic queue draining -----


@pytest.mark.asyncio
async def test_single_worker_runs_scenarios_in_order():
    paths = [f"s-{i}.yaml" for i in range(4)]
    runner = _FakeRunner(per_scenario_sleep=0.01)
    campaign = _make_campaign(paths, max_concurrency=1)

    result = await CampaignRunner(runner).run(campaign)

    assert result.total_scenarios == 4
    assert result.passed == 4
    assert result.failed == 0
    assert result.errored == 0
    assert len(result.call_results) == 4
    # With one worker, the order picked up must equal FIFO insertion.
    picked_order = [c[0] for c in runner.calls]
    assert picked_order == paths


@pytest.mark.asyncio
async def test_two_workers_run_in_parallel():
    paths = [f"s-{i}.yaml" for i in range(6)]
    runner = _FakeRunner(per_scenario_sleep=0.05)
    campaign = _make_campaign(paths, max_concurrency=2)

    result = await CampaignRunner(runner).run(campaign)

    assert result.total_scenarios == 6
    assert result.passed == 6
    # Must have observed 2 in-flight at peak — otherwise we ran sequentially.
    assert runner.max_in_flight == 2


@pytest.mark.asyncio
async def test_max_concurrency_capped_when_workers_exceed_queue():
    """4 workers but only 2 scenarios — extras should exit cleanly via
    QueueEmpty without errors."""
    paths = ["s-1.yaml", "s-2.yaml"]
    runner = _FakeRunner(per_scenario_sleep=0.01)
    campaign = _make_campaign(paths, max_concurrency=4)

    result = await CampaignRunner(runner).run(campaign)

    assert result.passed == 2
    assert runner.max_in_flight <= 2  # can't exceed queue size


@pytest.mark.asyncio
async def test_concurrency_respected_at_10_workers():
    """Stress: 10 workers + 50 scenarios. Verify peak in-flight is exactly 10."""
    paths = [f"s-{i}.yaml" for i in range(50)]
    runner = _FakeRunner(per_scenario_sleep=0.02)
    campaign = _make_campaign(paths, max_concurrency=10)

    result = await CampaignRunner(runner).run(campaign)

    assert result.total_scenarios == 50
    assert result.passed == 50
    assert runner.max_in_flight == 10


# ----- Worker isolation -----


@pytest.mark.asyncio
async def test_one_scenario_raises_others_still_complete():
    paths = [f"s-{i}.yaml" for i in range(5)]
    runner = _FakeRunner(
        per_scenario_sleep=0.01,
        raise_on_paths={"s-2.yaml"},
    )
    campaign = _make_campaign(paths, max_concurrency=2)

    result = await CampaignRunner(runner).run(campaign)

    # All 5 paths processed (the raised one becomes a synthetic error).
    assert result.total_scenarios == 5
    assert len(result.call_results) == 5
    # 4 passed, 1 errored
    assert result.passed == 4
    assert result.errored == 1


# ----- fail_fast -----


@pytest.mark.asyncio
async def test_fail_fast_stops_pulling_after_first_failure():
    """6 scenarios, scenario at index 1 fails. With fail_fast=True and
    max_concurrency=1, only the first 2 should run before the queue is
    drained. The remaining 4 should be in `skipped_paths`."""
    paths = [f"s-{i}.yaml" for i in range(6)]
    runner = _FakeRunner(
        per_scenario_sleep=0.01,
        fail_on_paths={"s-1.yaml"},
    )
    campaign = _make_campaign(paths, max_concurrency=1, fail_fast=True)

    result = await CampaignRunner(runner).run(campaign)

    # With 1 worker FIFO: s-0 passes, s-1 fails → fail_fast trips →
    # no more pulled. 4 paths should be skipped.
    assert result.fail_fast_triggered is True
    assert len(result.call_results) == 2
    assert len(result.skipped_paths) == 4
    assert result.skipped_paths == ["s-2.yaml", "s-3.yaml", "s-4.yaml", "s-5.yaml"]


@pytest.mark.asyncio
async def test_fail_fast_off_runs_full_sweep():
    """Same setup as above but fail_fast=False — all 6 should run."""
    paths = [f"s-{i}.yaml" for i in range(6)]
    runner = _FakeRunner(
        per_scenario_sleep=0.01,
        fail_on_paths={"s-1.yaml"},
    )
    campaign = _make_campaign(paths, max_concurrency=1, fail_fast=False)

    result = await CampaignRunner(runner).run(campaign)

    assert result.fail_fast_triggered is False
    assert len(result.call_results) == 6
    assert len(result.skipped_paths) == 0


# ----- worker_id propagation -----


@pytest.mark.asyncio
async def test_worker_id_var_is_set_during_scenario():
    """The fake runner reads worker_id_var.get() at scenario start.
    With max_concurrency=2, we should see both 0 and 1 as worker ids."""
    paths = [f"s-{i}.yaml" for i in range(4)]
    runner = _FakeRunner(per_scenario_sleep=0.02)
    campaign = _make_campaign(paths, max_concurrency=2)

    await CampaignRunner(runner).run(campaign)

    observed_worker_ids = {c[1] for c in runner.calls}
    assert observed_worker_ids == {0, 1}


@pytest.mark.asyncio
async def test_worker_id_var_resets_after_campaign():
    paths = ["s-0.yaml"]
    runner = _FakeRunner()
    campaign = _make_campaign(paths, max_concurrency=1)

    # Var should be None before, during the campaign workers it's
    # set, and back to None after.
    assert worker_id_var.get() is None
    await CampaignRunner(runner).run(campaign)
    assert worker_id_var.get() is None


# ----- Empty campaign -----


@pytest.mark.asyncio
async def test_empty_scenarios_list_returns_zero_results():
    """Edge case: campaign with zero scenarios. Should not crash."""
    campaign = _make_campaign([], max_concurrency=2)
    runner = _FakeRunner()

    result = await CampaignRunner(runner).run(campaign)
    assert result.total_scenarios == 0
    assert result.passed == 0
    assert result.failed == 0
    assert result.errored == 0
    assert result.pass_rate == 0.0
    assert result.duration_s >= 0


# ----- Pass rate calculation -----


@pytest.mark.asyncio
async def test_pass_rate_is_fraction():
    """3 passes, 1 fail → pass_rate = 0.75."""
    paths = [f"s-{i}.yaml" for i in range(4)]
    runner = _FakeRunner(
        per_scenario_sleep=0.01,
        fail_on_paths={"s-3.yaml"},
    )
    campaign = _make_campaign(paths, max_concurrency=2)

    result = await CampaignRunner(runner).run(campaign)
    assert result.passed == 3
    assert result.pass_rate == pytest.approx(0.75)
