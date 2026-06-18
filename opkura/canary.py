"""Canary (P5.5): one synthetic run through the WHOLE path, on a schedule.

What it catches, from the PRD: expired OSV tokens, region misroutes, broken
flow configs — the failures that otherwise wait for a human's run to surface.
Point it at a TEXT scenario against a zero-account assistant; schedule it with
cron / launchd / a CI job:

    AUTOCALLS_CANARY_SCENARIO=clients/_examples/happy-path.yaml opkura canary

Exit code is the contract (0 pass, 1 fail) so any scheduler alerts for free;
when METRICS_ENABLED is on the canary gauges update too, so Grafana can alert
on `time() - opkura_canary_last_success_timestamp_seconds`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CanaryOutcome:
    passed: bool
    duration_s: float
    status: str
    error: str = ""


async def run_canary(scenario_path: str, runner: "object") -> CanaryOutcome:
    """Run the scenario once and report. `runner` is anything with
    `run_scenario_file` (the real TestRunner, or a fake in tests)."""
    started = time.monotonic()
    try:
        result = await runner.run_scenario_file(scenario_path)
        outcome = CanaryOutcome(
            passed=bool(result.passed),
            duration_s=time.monotonic() - started,
            status=getattr(getattr(result, "status", None), "value", "unknown"),
            error=getattr(result, "error", "") or "",
        )
    except Exception as exc:
        outcome = CanaryOutcome(
            passed=False,
            duration_s=time.monotonic() - started,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
        )
    _record(outcome)
    return outcome


def _record(outcome: CanaryOutcome) -> None:
    """Update the canary gauges when prometheus is available; silence when not
    — the exit code is the primary signal either way."""
    try:
        from opkura.observers.metrics_observer import (
            CANARY_DURATION,
            CANARY_LAST_SUCCESS,
            CANARY_PASSED,
        )
    except Exception:
        return
    CANARY_PASSED.set(1 if outcome.passed else 0)
    CANARY_DURATION.set(outcome.duration_s)
    if outcome.passed:
        CANARY_LAST_SUCCESS.set(time.time())
