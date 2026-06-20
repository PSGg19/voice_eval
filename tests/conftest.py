"""Shared pytest fixtures for the opkura test suite."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest


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
