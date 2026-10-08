"""Shared pytest fixtures for the opkura test suite."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(scope="session", autouse=True)
def _hermetic_environment():
    """Strip ambient deployment env before any test runs.

    pydantic-settings lets process env BEAT the .env file, so a developer
    shell that has `export`-ed AUTOCALLS_* (or sourced the observability env)
    silently changes what Settings() resolves inside tests — ~50 tests fail
    with region/preflight/queue mismatches that never reproduce in a clean
    shell. Tests that need these vars set their own via monkeypatch."""
    doomed = [
        k for k in os.environ
        if k.startswith(("AUTOCALLS_", "OTEL_", "LANGFUSE_", "PHOENIX_"))
        or k in ("TRACING_PROVIDER", "LOG_SINK", "LOKI_URL",
                 "TRACE_CAPTURE_LLM_IO", "TRACE_SAMPLE_RATE", "DEPLOY_ENV")
    ]
    for k in doomed:
        del os.environ[k]
    yield


@pytest.fixture
def mock_llm_client():
    """Async-mocked LLMClient with the same Protocol surface as real providers.

    Each test overrides `.complete_json.return_value` (or `.side_effect`) with
    the response shape it expects — a plain dict works (usage reads fall back
    to `{}`); return an `LLMResult(data, usage)` when a test asserts token
    accounting (P1.2: usage travels in the result, no client state).
    """
    c = AsyncMock()
    c.provider = "mock"
    c.model = "mock-model"
    return c


@pytest.fixture
def example_scenario_path() -> Path:
    """Path to the happy-path reference scenario in clients/_examples/."""
    return (
        Path(__file__).resolve().parent.parent
        / "clients"
        / "_examples"
        / "happy-path.yaml"
    )
