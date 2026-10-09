from __future__ import annotations

import pytest

from vjudge.llm import detect_provider, get_client
from vjudge.llm.gemini import GeminiClient


def test_detect_gemini():
    assert detect_provider("gemini-2.5-flash") == "gemini"


def test_detect_anthropic():
    assert detect_provider("claude-sonnet-4-6") == "anthropic"


@pytest.mark.parametrize("model_name", ["gpt-4o", "o3-mini", "o1-preview", "o4-mini"])
def test_detect_openai(model_name: str):
    assert detect_provider(model_name) == "openai"


def test_detect_unknown():
    with pytest.raises(ValueError, match="unknown provider prefix"):
        detect_provider("mistral-large")


def test_get_client_gemini():
    client = get_client("gemini-2.5-flash", "fake-key")
    assert isinstance(client, GeminiClient)
    assert client.provider == "gemini"
    assert client.model == "gemini-2.5-flash"


def test_get_client_anthropic_not_implemented():
    with pytest.raises(NotImplementedError):
        get_client("claude-sonnet-4-6", "fake-key")


def test_get_client_openai_not_implemented():
    with pytest.raises(NotImplementedError):
        get_client("gpt-4o", "fake-key")


def test_get_client_explicit_provider_override():
    client = get_client("custom-name", "fake-key", provider="gemini")
    assert client.provider == "gemini"
    assert client.model == "custom-name"


def test_get_client_unknown_explicit_provider_raises():
    with pytest.raises(ValueError, match="unknown provider"):
        get_client("anything", "fake-key", provider="bogus")
