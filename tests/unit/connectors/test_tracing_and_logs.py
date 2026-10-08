"""Tracing and log-sink connectors."""
from __future__ import annotations

import logging

import pytest

from vjudge.connectors.logs import _FORBIDDEN_LABELS, _labels_from_env
from vjudge.connectors.logs import build as build_sink
from vjudge.connectors.tracing import NoOpTracer
from vjudge.connectors.tracing import build as build_tracer


# ------------------------------------------------------------------- tracing

def test_tracing_defaults_to_noop_and_imports_nothing(monkeypatch):
    for var in ("TRACING_PROVIDER", "PHOENIX_ENABLED", "LANGFUSE_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    assert isinstance(build_tracer(), NoOpTracer)


def test_the_noop_tracer_satisfies_the_whole_protocol():
    """The run path calls these unconditionally, so they must always work."""
    t = NoOpTracer()
    with t.span("run", run_id="r1") as s:
        s.set_attribute("k", "v")
        with t.generation("turn", model="gemini-2.5-flash", turn=1) as g:
            g.set_attribute("llm.token_count.total", 91)
    t.flush()


def test_orchestrator_service_env_vars_are_honoured(monkeypatch, caplog):
    """A deployment already carrying OSV's variables needs no new ones."""
    monkeypatch.delenv("TRACING_PROVIDER", raising=False)
    monkeypatch.setenv("PHOENIX_ENABLED", "true")
    with caplog.at_level(logging.WARNING):
        tracer = build_tracer()
    # The point is that PHOENIX_ENABLED alone SELECTED a backend. What comes
    # back depends on the environment: a real tracer when opentelemetry is
    # installed (it is, since the P5 observability extra landed), or a NoOp
    # after a logged degradation when it is not. Either proves selection;
    # a silent NoOp with no warning would mean the flag was ignored.
    if isinstance(tracer, NoOpTracer):
        assert any("failed to build" in r.getMessage() for r in caplog.records)
    else:
        assert getattr(tracer, "is_noop", True) is False


@pytest.mark.parametrize("value", ["none", "nope", ""])
def test_disabled_or_bogus_tracing_yields_noop(monkeypatch, value):
    monkeypatch.delenv("PHOENIX_ENABLED", raising=False)
    monkeypatch.delenv("LANGFUSE_ENABLED", raising=False)
    monkeypatch.setenv("TRACING_PROVIDER", value)
    assert isinstance(build_tracer(), NoOpTracer)


# ------------------------------------------------------------------ log sink

def test_log_sink_defaults_to_readable_text(monkeypatch):
    monkeypatch.delenv("LOG_SINK", raising=False)
    handler = build_sink()
    rec = logging.LogRecord("vjudge.x", logging.INFO, "f.py", 1, "hello", (), None)
    assert "hello" in handler.format(rec)


def test_json_sink_puts_the_run_id_in_the_body(monkeypatch):
    """Not as a label — that is the Loki cardinality rule."""
    import json

    from vjudge.core.log_context import call_id_var

    handler = build_sink(override="json")
    token = call_id_var.set("run_abc")
    try:
        rec = logging.LogRecord("vjudge.x", logging.INFO, "f.py", 7, "turn done", (), None)
        payload = json.loads(handler.format(rec))
    finally:
        call_id_var.reset(token)

    assert payload["run_id"] == "run_abc"
    assert payload["msg"] == "turn done"
    assert payload["file"] == "f.py:7"


def test_json_sink_omits_run_id_outside_a_run(monkeypatch):
    import json

    handler = build_sink(override="json")
    rec = logging.LogRecord("vjudge.x", logging.INFO, "f.py", 1, "startup", (), None)
    assert "run_id" not in json.loads(handler.format(rec))


def test_loki_labels_stay_low_cardinality(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "vjudge")
    monkeypatch.setenv("DEPLOY_ENV", "prod")
    monkeypatch.setenv("LOG_LABEL_REGION", "us")
    labels = _labels_from_env()
    assert labels == {"service": "vjudge", "env": "prod", "region": "us"}
    assert not (_FORBIDDEN_LABELS & set(labels))


def test_the_forbidden_label_set_names_the_unbounded_ids():
    """Regression guard: these are the ones that would explode Loki's index."""
    for key in ("run_id", "interaction_uuid", "session_id", "call_id"):
        assert key in _FORBIDDEN_LABELS
