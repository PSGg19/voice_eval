"""P5 acceptance against the LIVE local backends (compose profiles).

One synthetic run must land as one trace in Phoenix AND Langfuse, and one log
line must be queryable in Loki by `run_id` as a field. Skips (with the start
command) when a backend is not reachable, so `pytest tests/` stays green on a
machine without the containers.
"""
from __future__ import annotations

import json
import logging
import time
import uuid

import httpx
import pytest

PHOENIX = "http://localhost:6006"
LANGFUSE = "http://localhost:3200"
LOKI = "http://localhost:3100"
LF_AUTH = ("pk-lf-opkura-local", "sk-lf-opkura-local")


def _reachable(url: str) -> bool:
    try:
        httpx.get(url, timeout=3)
        return True
    except Exception:
        return False


def _skip_unless(url: str, profile: str) -> None:
    if not _reachable(url):
        pytest.skip(f"{url} not reachable — docker compose --profile {profile} up -d")


def test_one_run_is_one_trace_in_phoenix_and_langfuse(monkeypatch):
    _skip_unless(PHOENIX, "tracing")
    _skip_unless(LANGFUSE, "tracing")
    monkeypatch.setenv("TRACING_PROVIDER", "both")
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", f"{PHOENIX}/v1/traces")
    monkeypatch.setenv("LANGFUSE_HOST", LANGFUSE)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", LF_AUTH[0])
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", LF_AUTH[1])
    monkeypatch.delenv("TRACE_SAMPLE_RATE", raising=False)

    from opkura.observers import build_observers, emit, run_dispatcher
    from opkura.observers.events import (
        BotTurn, CallerTurn, Generation, RunFinished, RunStarted,
    )

    marker = f"p5-live-{uuid.uuid4().hex[:10]}"

    def phoenix_trace_count() -> int:
        r = httpx.post(
            f"{PHOENIX}/graphql",
            json={"query": "query{projects{edges{node{name traceCount}}}}"},
            timeout=10,
        )
        edges = r.json()["data"]["projects"]["edges"]
        return sum(e["node"]["traceCount"] for e in edges)

    phoenix_before = phoenix_trace_count()

    observers = build_observers()
    assert observers, "TRACING_PROVIDER=both must build the tracing observer"
    with run_dispatcher(observers):
        emit(RunStarted(run_id=marker, scenario_name=marker, target="+15550001111",
                        region="us", transport="text"))
        now = time.time_ns()
        emit(BotTurn(turn_index=1, text="Hello.", module="greeting",
                     latency_ms=500.0, at_ns=now))
        emit(CallerTurn(turn_index=1, text="Hi.", at_ns=now + 10**9))
        emit(Generation(role="caller", model="gemini-2.5-flash", provider="gemini",
                        prompt_tokens=100, completion_tokens=10,
                        started_ns=now + 2 * 10**8, ended_ns=now + 8 * 10**8))
        emit(RunFinished(status="completed", passed=True))

    # Langfuse ingests via worker + ClickHouse — poll for the named trace.
    trace = None
    for _ in range(30):
        r = httpx.get(f"{LANGFUSE}/api/public/traces",
                      params={"name": f"run: {marker}"}, auth=LF_AUTH, timeout=10)
        data = r.json().get("data", [])
        if data:
            trace = data[0]
            break
        time.sleep(2)
    assert trace is not None, f"trace 'run: {marker}' never appeared in Langfuse"
    # root + bot turn + caller turn + generation = 4 observations
    assert len(trace["observations"]) >= 4

    # Phoenix ingests synchronously on flush — count must have grown.
    assert phoenix_trace_count() > phoenix_before


def test_log_line_lands_in_loki_with_run_id_as_field(monkeypatch):
    _skip_unless(f"{LOKI}/ready", "logs")
    monkeypatch.setenv("LOG_SINK", "loki")
    monkeypatch.setenv("LOKI_URL", f"{LOKI}/loki/api/v1/push")

    from opkura.connectors.logs import build
    from opkura.core.log_context import call_id_var

    marker = f"p5-loki-{uuid.uuid4().hex[:10]}"
    handler = build()
    assert type(handler).__name__ == "LokiQueueHandler"
    lg = logging.getLogger("opkura.p5test")
    lg.setLevel(logging.INFO)
    lg.addHandler(handler)
    token = call_id_var.set(marker)
    try:
        lg.info("p5 live loki check")
    finally:
        call_id_var.reset(token)
        lg.removeHandler(handler)

    found = None
    query = {"query": '{service="opkura"}',
             "start": str((time.time_ns()) - 120 * 10**9)}
    for _ in range(15):
        r = httpx.get(f"{LOKI}/loki/api/v1/query_range", params=query, timeout=10)
        for stream in r.json().get("data", {}).get("result", []):
            for _, line in stream.get("values", []):
                if marker in line:
                    found = (stream["stream"], json.loads(line))
                    break
        if found:
            break
        time.sleep(1)
    assert found, "log line never appeared in Loki"
    labels, payload = found
    assert payload["run_id"] == marker          # a FIELD in the line…
    assert "run_id" not in labels               # …never a stream label
