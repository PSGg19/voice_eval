"""P6.1 — the TTS link: caller speech → outbound audio.

Consumes `CallerSpeech`, streams synthesis through a factory-injected client
(`synthesize_stream(text) -> async iterator of μ-law bytes`, the DeepgramTTS
surface), emits `OutboundAudio` chunks as they arrive — the audio-out link
does the pacing — and closes each utterance with `SpeakEnded`, the D3
transport-latency anchor turn detection reads.

A synthesis failure drops the utterance, not the call: the engine already
recorded the line in the transcript, and the bot hears silence for one turn —
same degradation the monolith's WS-TTS fallback chain bottomed out at.
"""
from __future__ import annotations

import logging

from vjudge.frames import CallerSpeech
from vjudge.voice.frames import OutboundAudio, SpeakEnded
from vjudge.voice.processor import Processor

logger = logging.getLogger(__name__)


class TTSProcessor(Processor):
    """`tts_factory()` builds the synthesis client lazily at chain start —
    tests inject a fake; production binds DeepgramTTS (per-scenario voice
    override included, since the factory closes over the scenario)."""

    def __init__(self, tts_factory, name: str = "tts", **kw):
        super().__init__(name, **kw)
        self._factory = tts_factory
        self._tts = None

    async def start(self) -> None:
        self._tts = self._factory()

    async def handle(self, frame) -> None:
        if not isinstance(frame, CallerSpeech):
            await self.emit(frame)
            return
        if not frame.text:
            return
        try:
            async for chunk in self._tts.synthesize_stream(frame.text):
                if chunk:
                    await self.emit(OutboundAudio(payload=bytes(chunk)))
        except Exception as exc:
            logger.error("TTS synthesis failed — utterance dropped: %s", exc)
        finally:
            await self.emit(SpeakEnded())
