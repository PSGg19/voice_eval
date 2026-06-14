"""Gemini implementation of LLMClient."""
from __future__ import annotations

import json
import logging
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_ROLE_MAP = {"user": "user", "assistant": "model", "model": "model"}


class GeminiClient:
    provider = "gemini"

    def __init__(self, model: str, api_key: str):
        self.model = model
        self._client = genai.Client(api_key=api_key)
        # Populated after each complete_json call from response.usage_metadata.
        # Sidecar (not in Protocol) so non-Gemini providers don't have to expose it.
        # NOT safe when one client is shared across concurrent coroutines — the
        # caller in ai_caller reads it synchronously right after the await
        # returns, which is only race-free for single-coroutine ownership.
        self.last_usage: Optional[dict] = None

    async def complete_json(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 500,
        temperature: float = 0.3,
        thinking_budget: Optional[int] = None,
    ) -> dict:
        # Invalidate BEFORE any work so any raise (message-construction errors,
        # SDK exceptions, network blips) leaves last_usage definitively None.
        # The caller in ai_caller reads this synchronously after the await
        # returns and would otherwise attribute the previous call's tokens
        # to this failed attempt.
        self.last_usage = None

        contents = [
            types.Content(
                role=_ROLE_MAP.get(m["role"], m["role"]),
                parts=[types.Part.from_text(text=m["content"])],
            )
            for m in messages
        ]

        # Gemini 2.5 Pro spends `max_output_tokens` on internal "thinking" before
        # emitting the answer — callers using Pro must pass a larger budget
        # (~2000+) or set a thinking_config with budget=0.
        # thinking_budget=0 disables thinking entirely (faster, no token spend
        # on hidden reasoning). Used for the caller LLM where snappy JSON
        # replies matter more than deep reasoning. None = SDK default.
        config_kwargs = dict(
            system_instruction=system,
            response_mime_type="application/json",
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        if thinking_budget is not None:
            config_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=thinking_budget
            )
        config = types.GenerateContentConfig(**config_kwargs)

        response = await self._client.aio.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )

        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            self.last_usage = {
                "input_tokens": getattr(usage, "prompt_token_count", None),
                "output_tokens": getattr(usage, "candidates_token_count", None),
            }

        text = response.text or ""
        try:
            result = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Gemini returned non-JSON: {text!r}") from e
        if not isinstance(result, dict):
            raise ValueError(f"Gemini returned non-dict JSON: {result!r}")
        return result
