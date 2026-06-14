"""Shared pytest fixtures for the opkura test suite."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def mock_llm_client():
    """Async-mocked LLMClient with the same Protocol surface as real providers.

    Each test overrides `.complete_json.return_value` (or `.side_effect`) with
    the response shape it expects. `last_usage` is pre-seeded to None to
    prevent AsyncMock's auto-generated MagicMock from leaking into recorded
    token fields (real GeminiClient seeds None at module init).
    """
    c = AsyncMock()
    c.provider = "mock"
    c.model = "mock-model"
    c.last_usage = None
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
