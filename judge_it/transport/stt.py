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
        on_speech_started: Callable[[], None] | None = None,
        *,
        endpointing_ms: int = 300,
        utterance_end_ms: int = 2500,
        vad_events: bool = False,
    ):
        self.api_key = api_key
        self.on_final = on_final
        self.on_interim = on_interim
        # Fires when Deepgram's server-side VAD detects start of voice
        # activity (audio-domain, faster than STT-derived interim signals).
        # Used by stream._wait_for_bot_turn to catch bot resumption mid-grace
        # period — including "uhh", breaths, and other non-word sounds that
        # STT alone misses.
        self.on_speech_started = on_speech_started
        self.endpointing_ms = endpointing_ms
        self.utterance_end_ms = utterance_end_ms
        self.vad_events = vad_events
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._recv_task: asyncio.Task | None = None

    async def start(self):
        # vad_events requires a model that supports it (nova-2+). The
        # account-default model silently dropped all transcripts when we
        # enabled vad_events without specifying a model — so when the flag
        # is on we ALSO pin model=nova-2 to guarantee support.
        # nova-3 + language=multi for bilingual EN/ES bots (Veros, etc.) — without
        # multi, English-only STT returns empty transcripts when bot speaks
        # Spanish, causing judge_it to think the bot stopped responding and hang up.
        # nova-3 is the Deepgram model that supports language=multi.
        params = (
            f"encoding=mulaw&sample_rate=8000&channels=1"
            f"&punctuate=true&endpointing={self.endpointing_ms}"
            f"&interim_results=true&utterance_end_ms={self.utterance_end_ms}"
            f"&model=nova-3&language=multi"
        )
        if self.vad_events:
            params += "&vad_events=true"
            logger.info("Deepgram STT: enabling vad_events with model=nova-3 + language=multi")
        else:
            logger.info("Deepgram STT: using model=nova-3 + language=multi")
        url = f"{DEEPGRAM_WS_URL}?{params}"
        headers = {"Authorization": f"Token {self.api_key}"}
        self._ws = await websockets.connect(url, additional_headers=headers)
        self._recv_task = asyncio.create_task(self._receive_loop())
        logger.info("Deepgram STT connected")

    async def _receive_loop(self):
        # Per-message try/except — a single malformed message (unexpected
        # schema, unknown event type with weird fields) must NOT kill the
        # whole recv task. Previously a stray KeyError would silently end
        # this task and the rest of the call would receive zero transcripts
        # while audio kept flowing IN — exact symptom of the vad_events
        # regression we just diagnosed.
        try:
            async for raw in self._ws:  # type: ignore[union-attr]
                try:
                    data = json.loads(raw)
                    msg_type = data.get("type", "")

                    if msg_type == "Results":
                        channel = data.get("channel") or {}
                        alternatives = channel.get("alternatives") or []
                        alt = alternatives[0] if alternatives else {}
                        transcript = alt.get("transcript", "")
                        is_final = data.get("is_final", False)
                        if transcript:
                            if is_final:
                                logger.info("STT final: %s", transcript)
                                self.on_final(transcript)
                            elif self.on_interim:
                                self.on_interim(transcript)

                    elif msg_type == "UtteranceEnd":
                        # Deepgram detected end of utterance (silence). Used
                        # by the legacy Deepgram-based _wait_for_bot_turn
                        # path; ignored when Silero VAD is enabled (Silero
                        # is the sole turn-end authority in that mode).
                        # Logged at DEBUG to keep INFO output clean when
                        # Silero is on — set log level to DEBUG to see.
                        logger.debug("STT utterance end")
                        self.on_final("")  # empty signal = utterance boundary

                    elif msg_type == "SpeechStarted":
                        # vad_events=true: Deepgram's server-side VAD fired.
                        # Audio-domain detection — fires for ANY voice activity
                        # (breaths, "uhh", music), not just recognized words.
                        logger.info("STT speech started (VAD)")
                        if self.on_speech_started:
                            self.on_speech_started()

                    elif msg_type == "Metadata":
                        # Connection metadata — log so we know we're talking
                        # to the right model when debugging.
                        logger.info("STT metadata: %s", data)

                    else:
                        logger.info("STT unknown msg type=%r raw=%s", msg_type, raw[:200])

                except Exception as exc:
                    # Don't let one bad message kill the receive loop —
                    # log and continue. Bumped to INFO so debug.log captures
                    # it without enabling DEBUG globally.
                    logger.warning("STT message parse error: %s — raw=%s", exc, raw[:200])

        except websockets.exceptions.ConnectionClosed as exc:
            logger.warning("Deepgram STT connection closed: %s", exc)
        except Exception as exc:
            logger.exception("Deepgram STT recv loop crashed: %s", exc)

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
