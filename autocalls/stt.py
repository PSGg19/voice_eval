"""Streaming speech-to-text via Deepgram WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import websockets

logger = logging.getLogger(__name__)

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"


class DeepgramSTT:
    """Streams mulaw audio to Deepgram, fires callbacks on transcripts."""

    def __init__(
        self,
        api_key: str,
        on_final: Callable[[str], None],
        on_interim: Callable[[str], None] | None = None,
        *,
        endpointing_ms: int = 300,
    ):
        self.api_key = api_key
        self.on_final = on_final
        self.on_interim = on_interim
        self.endpointing_ms = endpointing_ms
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._recv_task: asyncio.Task | None = None

    async def start(self):
        params = (
            f"encoding=mulaw&sample_rate=8000&channels=1"
            f"&punctuate=true&endpointing={self.endpointing_ms}"
            f"&interim_results=true&utterance_end_ms=1500"
        )
        url = f"{DEEPGRAM_WS_URL}?{params}"
        headers = {"Authorization": f"Token {self.api_key}"}
        self._ws = await websockets.connect(url, additional_headers=headers)
        self._recv_task = asyncio.create_task(self._receive_loop())
        logger.info("Deepgram STT connected")

    async def _receive_loop(self):
        try:
            async for raw in self._ws:  # type: ignore[union-attr]
                data = json.loads(raw)
                msg_type = data.get("type", "")

                if msg_type == "Results":
                    alt = data["channel"]["alternatives"][0]
                    transcript = alt.get("transcript", "")
                    is_final = data.get("is_final", False)
                    if transcript:
                        if is_final:
                            logger.debug("STT final: %s", transcript)
                            self.on_final(transcript)
                        elif self.on_interim:
                            self.on_interim(transcript)

                elif msg_type == "UtteranceEnd":
                    # Deepgram detected end of utterance (silence)
                    logger.debug("STT utterance end")
                    self.on_final("")  # empty signal = utterance boundary

        except websockets.exceptions.ConnectionClosed:
            logger.debug("Deepgram STT connection closed")

    async def send_audio(self, audio: bytes):
        if self._ws:
            await self._ws.send(audio)

    async def stop(self):
        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:
                pass
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        logger.info("Deepgram STT stopped")
