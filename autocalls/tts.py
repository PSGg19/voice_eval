"""Text-to-speech via Deepgram. Returns mulaw 8kHz audio ready for Twilio."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

DEEPGRAM_TTS_URL = "https://api.deepgram.com/v1/speak"


class DeepgramTTS:
    """Converts text to mulaw 8kHz audio bytes using Deepgram Aura."""

    def __init__(self, api_key: str, model: str = "aura-asteria-en"):
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(timeout=15.0)

    async def synthesize(self, text: str) -> bytes:
        """Return raw mulaw 8kHz audio bytes for the given text."""
        resp = await self._client.post(
            DEEPGRAM_TTS_URL,
            params={
                "model": self.model,
                "encoding": "mulaw",
                "sample_rate": "8000",
                "container": "none",
            },
            headers={
                "Authorization": f"Token {self.api_key}",
                "Content-Type": "application/json",
            },
            json={"text": text},
        )
        resp.raise_for_status()
        logger.debug("TTS generated %d bytes for: %s", len(resp.content), text[:60])
        return resp.content

    async def close(self):
        await self._client.aclose()
