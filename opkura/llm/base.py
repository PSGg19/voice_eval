"""LLMClient Protocol + provider factory."""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

_OPENAI_PREFIXES = ("gpt-", "o1-", "o3-", "o4-")


@runtime_checkable
class LLMClient(Protocol):
    provider: str
    model: str

    async def complete_json(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 500,
        temperature: float = 0.3,
        thinking_budget: Optional[int] = None,
    ) -> dict: ...


def detect_provider(model: str) -> str:
    if model.startswith("gemini-"):
        return "gemini"
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith(_OPENAI_PREFIXES):
        return "openai"
    raise ValueError(f"unknown provider prefix: {model}")


def get_client(model: str, api_key: str, provider: Optional[str] = None) -> LLMClient:
    provider = provider or detect_provider(model)
    if provider == "gemini":
        from .gemini import GeminiClient

        return GeminiClient(model, api_key)
    if provider == "anthropic":
        raise NotImplementedError("anthropic provider not implemented in Phase 1")
    if provider == "openai":
        raise NotImplementedError("openai provider not implemented in Phase 1")
    raise ValueError(f"unknown provider: {provider}")
