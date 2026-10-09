"""Metrics observer (P5.4): Prometheus counters/gauges from the event stream.

Label discipline is ENFORCED by shape: the label sets below are fixed and
low-cardinality (transport, region, status, role) — no run id, no scenario,
no interaction uuid can ever become a label because no metric accepts one.
The test suite additionally pins the forbidden names out of every label set.

Collectors are module-level singletons (Prometheus registries refuse
duplicate registration, and one process has one truth per metric). Exposed at
GET /metrics on `judge_it serve` when METRICS_ENABLED=true; queue-depth gauges
arrive with the P4 queue itself.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from prometheus_client import Counter, Gauge, Histogram

from judge_it.observers.events import (
    BotTurn,
    CallerTurn,
    Generation,
    RunFinished,
    RunStarted,
)

RUNS_IN_FLIGHT = Gauge(
    "judge_it_runs_in_flight", "Runs currently executing", ["transport"]
)
RUNS_TOTAL = Counter(
    "judge_it_runs_total", "Finished runs", ["status", "transport", "region", "passed"]
)
RUN_DURATION = Histogram(
    "judge_it_run_duration_seconds", "Wall-clock run duration", ["transport"],
    buckets=(5, 15, 30, 60, 120, 300, 600, 1200),
)
TURNS_TOTAL = Counter(
    "judge_it_turns_total", "Conversation turns observed", ["speaker"]
)
LLM_CALLS_TOTAL = Counter(
    "judge_it_llm_calls_total", "LLM calls", ["role", "outcome"]
)
LLM_TOKENS_TOTAL = Counter(
    "judge_it_llm_tokens_total", "LLM tokens", ["role", "kind"]
)

# Canary gauges (P5.5) — set by `judge_it canary`, scraped like everything else.
CANARY_PASSED = Gauge(
    "judge_it_canary_passed", "1 when the last canary run passed, else 0"
)
CANARY_LAST_SUCCESS = Gauge(
    "judge_it_canary_last_success_timestamp_seconds",
    "Unix time of the last passing canary run",
)
CANARY_DURATION = Gauge(
    "judge_it_canary_duration_seconds", "Duration of the last canary run"
)


class MetricsObserver:
    def __init__(self) -> None:
        self._starts: Dict[str, RunStarted] = {}

    def on_event(self, event: Any) -> None:
        if isinstance(event, RunStarted):
            self._starts[event.run_id] = event
            RUNS_IN_FLIGHT.labels(transport=event.transport).inc()
        elif isinstance(event, BotTurn):
            TURNS_TOTAL.labels(speaker="bot").inc()
        elif isinstance(event, CallerTurn):
            TURNS_TOTAL.labels(speaker="caller").inc()
        elif isinstance(event, Generation):
            LLM_CALLS_TOTAL.labels(
                role=event.role, outcome="error" if event.error else "ok"
            ).inc()
            if event.prompt_tokens:
                LLM_TOKENS_TOTAL.labels(role=event.role, kind="prompt").inc(
                    event.prompt_tokens
                )
            if event.completion_tokens:
                LLM_TOKENS_TOTAL.labels(role=event.role, kind="completion").inc(
                    event.completion_tokens
                )
        elif isinstance(event, RunFinished):
            start = self._pop_start()
            transport = start.transport if start else "unknown"
            region = start.region if start else "unknown"
            RUNS_IN_FLIGHT.labels(transport=transport).dec()
            RUNS_TOTAL.labels(
                status=event.status, transport=transport, region=region,
                passed=str(bool(event.passed)).lower(),
            ).inc()
            if start:
                RUN_DURATION.labels(transport=transport).observe(
                    max(0.0, (event.at_ns - start.at_ns) / 1e9)
                )

    def _pop_start(self) -> Optional[RunStarted]:
        # One observer instance serves one run (installed per run_dispatcher),
        # so there is at most one start to pop.
        if not self._starts:
            return None
        return self._starts.pop(next(iter(self._starts)))
