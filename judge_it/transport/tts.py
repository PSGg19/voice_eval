"""Text-to-speech via Deepgram. Returns mulaw 8kHz audio ready for Twilio.

Two transports live side-by-side:

* `DeepgramTTS` — HTTP `/v1/speak`. `synthesize` (blocking) and
  `synthesize_stream` (chunked HTTP). TTFB ~1.1s in production.
* `DeepgramWebSocketTTS` — WebSocket `/v1/speak`. Long-lived per
  call. TTFB ~250ms because frames flush as the synthesizer produces
  them. Default in production.

Both expose the same `synthesize_stream(text) -> AsyncIterator[bytes]`
shape so `_speak_*` in `stream.py` can swap with a single conditional.
"""

from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator

import httpx
import websockets

logger = logging.getLogger(__name__)

DEEPGRAM_TTS_URL = "https://api.deepgram.com/v1/speak"
DEEPGRAM_TTS_WS_URL = "wss://api.deepgram.com/v1/speak"


class DeepgramTTS:
    """Converts text to mulaw 8kHz audio bytes using Deepgram Aura."""

    def __init__(self, api_key: str, model: str = "aura-2-thalia-en"):
        self.api_key = api_key
        self.model = model
        # Longer read timeout for streaming — the response body stays open
        # while Deepgram trickles audio. 30s is generous; even a 30-word
        # utterance finishes well within this.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0)
        )

    def _params(self) -> dict:
        return {
            "model": self.model,
            "encoding": "mulaw",
            "sample_rate": "8000",
            "container": "none",
        }

    def _headers(self) -> dict:
        return {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
        }

    async def synthesize(self, text: str) -> bytes:
        """Blocking — return raw mulaw 8kHz bytes for the entire utterance.

        Kept for backward compatibility and as fallback when
        JUDGE_IT_STREAMING_TTS=false. Prefer `synthesize_stream` in the
        hot path; it's ~1.5s faster to first byte.
        """
        resp = await self._client.post(
            DEEPGRAM_TTS_URL,
            params=self._params(),
            headers=self._headers(),
            json={"text": text},
        )
        resp.raise_for_status()
        logger.debug("TTS generated %d bytes for: %s", len(resp.content), text[:60])
        return resp.content

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield mulaw 8kHz audio chunks AS THEY ARRIVE from Deepgram.

        Uses httpx's streaming response so bytes land in our hands as
        Deepgram's server flushes them, instead of waiting for the full
        body. Typical first-chunk latency: 200-400ms vs ~1.8s for the
        blocking `synthesize`.

        Each yielded chunk is variable-size (Deepgram doesn't align to
        20ms Twilio frame boundaries). The caller (`_speak_streaming`)
        buffers chunks and repackages into 160-byte Twilio media events.

        Important: callers must consume the generator inside the same
        async context that opened it — httpx closes the underlying
        connection when the generator exits.
        """
        async with self._client.stream(
            "POST",
            DEEPGRAM_TTS_URL,
            params=self._params(),
            headers=self._headers(),
            json={"text": text},
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes(chunk_size=4096):
                if chunk:  # skip empty keepalive frames
                    yield chunk

    async def close(self):
        await self._client.aclose()


class DeepgramWebSocketTTS:
    """Per-utterance Deepgram WebSocket TTS with optional prewarm.

    Deepgram's WS TTS endpoint parses every text frame as text to
    synthesize, so STT-style `{"type":"KeepAlive"}` heartbeats trigger
    a `1008 policy violation DATA-0000` close. Long-lived persistent
    connections therefore aren't viable; the right pattern is
    open-on-demand, close-after-Flushed.

    To avoid paying the ~150-350ms WS handshake on the critical path,
    `prewarm()` opens a connection ahead of time — in judge_it's flow,
    this fires the moment Silero detects bot-turn-end, in parallel
    with the Gemini caller LLM call (~1.3s). By the time the LLM
    returns text, the WS is already hot and `synthesize_stream`
    consumes it directly.

    `synthesize_stream(text)`:
      * Uses the prewarmed WS if available; otherwise opens fresh.
      * Sends `Speak` + `Flush`, yields binary mulaw frames as they
        arrive, returns on `Flushed`.
      * Closes the WS when the generator exits.

    `close()` cleans up any leaked prewarm at call end.
    """

    def __init__(self, api_key: str, model: str = "aura-2-thalia-en"):
        self.api_key = api_key
        self.model = model
        # Prewarmed WS waiting to be consumed by the next synthesize_stream.
        # None if no prewarm has happened, prewarm failed, or the
        # previous prewarm was already consumed.
        self._prewarmed_ws: websockets.WebSocketClientProtocol | None = None

    def _build_url(self) -> str:
        params = (
            f"model={self.model}&encoding=mulaw"
            f"&sample_rate=8000&container=none"
        )
        return f"{DEEPGRAM_TTS_WS_URL}?{params}"

    def _headers(self) -> dict:
        return {"Authorization": f"Token {self.api_key}"}

    async def prewarm(self) -> None:
        """Open a WS in advance so the next synthesize_stream skips
        the handshake. Idempotent: a second prewarm with an existing
        prewarmed WS is a no-op. Failures are logged and swallowed —
        synthesize_stream will fall back to opening fresh.
        """
        if self._prewarmed_ws is not None:
            return
        try:
            t0 = time.monotonic()
            # close_timeout=0.5: the websockets lib's default close
            # handshake waits 10s for the peer's close-ack. Deepgram's
            # WS TTS endpoint doesn't ack close frames promptly (it
            # stays in "waiting for next Speak" state), so the default
            # added 10s of dead time AFTER each Flushed before our
            # finally `await ws.close()` returned. 0.5s gives the peer
            # a moment to ack cleanly without blocking the call path.
            ws = await websockets.connect(
                self._build_url(),
                additional_headers=self._headers(),
                close_timeout=0.5,
            )
            elapsed_ms = (time.monotonic() - t0) * 1000
            self._prewarmed_ws = ws
            logger.info(
                "Deepgram WS TTS prewarm connected in %.0fms",
                elapsed_ms,
            )
        except Exception as exc:
            logger.warning("Deepgram WS TTS prewarm failed: %s", exc)
            self._prewarmed_ws = None

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield mulaw 8kHz audio frames for `text`.

        Consumes the prewarmed WS if present; otherwise opens fresh.
        Sends `Speak` + `Flush`, then yields binary frames until
        `Flushed`. Closes the WS on exit (normal or exception).
        """
        if self._prewarmed_ws is not None:
            ws = self._prewarmed_ws
            self._prewarmed_ws = None
            prewarmed = True
        else:
            # close_timeout=0.5: see prewarm() comment — Deepgram's
            # WS TTS close-ack is slow, default 10s would block.
            ws = await websockets.connect(
                self._build_url(),
                additional_headers=self._headers(),
                close_timeout=0.5,
            )
            prewarmed = False
            logger.info("Deepgram WS TTS opened fresh (no prewarm)")

        try:
            await ws.send(json.dumps({"type": "Speak", "text": text}))
            await ws.send(json.dumps({"type": "Flush"}))

            try:
                async for raw in ws:
                    if isinstance(raw, bytes):
                        if raw:
                            yield raw
                    else:
                        try:
                            data = json.loads(raw)
                        except Exception as exc:
                            logger.warning(
                                "WS TTS recv parse error: %s — raw=%s",
                                exc, str(raw)[:200],
                            )
                            continue
                        msg_type = data.get("type", "")
                        # Non-terminal messages: keep iterating.
                        if msg_type == "Metadata":
                            logger.info(
                                "Deepgram WS TTS metadata (prewarmed=%s): %s",
                                prewarmed, data,
                            )
                            continue
                        if msg_type == "Warning":
                            logger.warning("Deepgram WS TTS warning: %s", data)
                            continue
                        # Anything else (Flushed / Cleared / Closed /
                        # lowercase / unexpected) is treated as
                        # end-of-utterance. Logged at INFO so we can
                        # confirm what Deepgram actually sent — a
                        # silent DEBUG bucket previously caused us to
                        # hang for ~10s per turn while the WS idled
                        # toward its policy timeout.
                        logger.info(
                            "Deepgram WS TTS end-of-utterance: type=%r data=%s",
                            msg_type, data,
                        )
                        return
                # `async for` ended without exception — newer
                # `websockets` releases exit cleanly on a peer-initiated
                # close (no ConnectionClosed raised). Treat this as
                # end-of-utterance so the caller doesn't think more
                # audio is coming.
                logger.info("Deepgram WS TTS connection ended cleanly")
            except websockets.exceptions.ConnectionClosed as exc:
                # Closed before any end marker — return whatever we
                # already yielded. Caller's audio may be truncated.
                logger.warning("Deepgram WS TTS closed mid-stream: %s", exc)
                return
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    async def close(self) -> None:
        """Close any leaked prewarmed WS — call at end of call.

        If a prewarm happened but the caller fell back to HTTP (or the
        scenario ended before consuming it), this closes the orphaned
        connection so it doesn't sit until Deepgram times it out.
        """
        if self._prewarmed_ws is not None:
            try:
                await self._prewarmed_ws.close()
            except Exception:
                pass
            self._prewarmed_ws = None
