"""P5.5 — the canary: exit-code contract + gauge updates, no network."""
from __future__ import annotations

import asyncio

from vjudge.canary import run_canary
from vjudge.core.models import CallResult, CallStatus, EvaluationResult
from vjudge.observers.metrics_observer import CANARY_PASSED


class FakeRunner:
    def __init__(self, *, passed=True, explode=False):
        self._passed = passed
        self._explode = explode

    async def run_scenario_file(self, path):
        if self._explode:
            raise RuntimeError("OSV 401: token expired")
        return CallResult(
            flow_name="canary", target="+1",
            status=CallStatus.COMPLETED if self._passed else CallStatus.ERROR,
            evaluation=EvaluationResult(overall_passed=self._passed,
                                        overall_reasoning="x"),
            error="" if self._passed else "boom",
        )


def test_passing_canary():
    outcome = asyncio.run(run_canary("x.yaml", FakeRunner(passed=True)))
    assert outcome.passed and outcome.status == "completed"
    assert CANARY_PASSED._value.get() == 1


def test_failing_canary():
    outcome = asyncio.run(run_canary("x.yaml", FakeRunner(passed=False)))
    assert not outcome.passed
    assert CANARY_PASSED._value.get() == 0


def test_exploding_canary_is_a_failure_not_a_crash():
    """The canary exists to catch exactly this class (expired OSV token) —
    an exception is a FAIL with the reason, never an unhandled traceback."""
    outcome = asyncio.run(run_canary("x.yaml", FakeRunner(explode=True)))
    assert not outcome.passed
    assert "OSV 401" in outcome.error
    assert outcome.status == "error"
