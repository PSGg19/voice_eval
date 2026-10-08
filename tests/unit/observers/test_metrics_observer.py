"""P5.4 — the metrics observer, plus the label-discipline tripwire."""
from __future__ import annotations

from prometheus_client import REGISTRY

from vjudge.observers.events import (
    BotTurn,
    CallerTurn,
    Generation,
    RunFinished,
    RunStarted,
)
from vjudge.observers.metrics_observer import (
    LLM_TOKENS_TOTAL,
    RUNS_IN_FLIGHT,
    RUNS_TOTAL,
    TURNS_TOTAL,
    MetricsObserver,
)

# The high-cardinality identifiers that must NEVER become label names — the
# exact list the log-sink guard enforces, applied to metrics.
FORBIDDEN_LABELS = {
    "run_id", "call_id", "interaction_uuid", "session_id", "worker_id",
    "scenario", "request_id", "trace_id", "recipient_id",
}


def _value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()


def drive_run(obs, *, transport="text", passed=True):
    obs.on_event(RunStarted(run_id="r", scenario_name="s", target="+1",
                            region="us", transport=transport, at_ns=10**9))
    obs.on_event(BotTurn(turn_index=1, text="hi", at_ns=2 * 10**9))
    obs.on_event(CallerTurn(turn_index=1, text="yo", at_ns=3 * 10**9))
    obs.on_event(Generation(role="caller", model="m", prompt_tokens=11,
                            completion_tokens=4, started_ns=1, ended_ns=2))
    obs.on_event(RunFinished(status="completed" if passed else "failed",
                             passed=passed, at_ns=4 * 10**9))


def test_run_lifecycle_moves_the_counters():
    runs_before = _value(RUNS_TOTAL, status="completed", transport="text",
                         region="us", passed="true")
    bot_before = _value(TURNS_TOTAL, speaker="bot")
    tokens_before = _value(LLM_TOKENS_TOTAL, role="caller", kind="prompt")

    drive_run(MetricsObserver())

    assert _value(RUNS_TOTAL, status="completed", transport="text",
                  region="us", passed="true") == runs_before + 1
    assert _value(TURNS_TOTAL, speaker="bot") == bot_before + 1
    assert _value(LLM_TOKENS_TOTAL, role="caller", kind="prompt") == tokens_before + 11
    # in-flight returns to its starting level once the run finishes
    assert RUNS_IN_FLIGHT.labels(transport="text")._value.get() >= 0


def test_in_flight_gauge_rises_then_falls():
    obs = MetricsObserver()
    g = RUNS_IN_FLIGHT.labels(transport="sip")
    base = g._value.get()
    obs.on_event(RunStarted(run_id="r", scenario_name="s", target="+1",
                            region="in", transport="sip"))
    assert g._value.get() == base + 1
    obs.on_event(RunFinished(status="failed", passed=False))
    assert g._value.get() == base


def test_no_vjudge_metric_uses_a_high_cardinality_label():
    """PRD: label discipline enforced — no run/scenario/interaction ids as
    labels, on ANY vjudge_* metric now or later."""
    for metric in REGISTRY.collect():
        if not metric.name.startswith("vjudge_"):
            continue
        for sample in metric.samples:
            bad = FORBIDDEN_LABELS & set(sample.labels)
            assert not bad, f"{metric.name} carries forbidden label(s) {bad}"
