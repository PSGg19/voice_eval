"""P6.1 — turn detection as a processor (Deepgram mode).

The monolith's most delicate behaviour, extracted verbatim in semantics:
Deepgram fires UtteranceEnd after `utterance_end_ms` of silence, but bots
with multi-sentence turns naturally pause that long BETWEEN sentences (the
Mini-Miranda's "…used for this purpose." pause). Acting on the first
UtteranceEnd would have us speak over the bot mid-turn. So:

    SpeechStarted / BotPartialSpeech  → the bot is talking (grace aborts)
    UtteranceBoundary                 → open a GRACE window
    grace expires with no resumption  → the turn is over: emit BotTurnEnded
    nothing at all for `turn_timeout` → the turn is over anyway (dead air —
                                        the engine's silence policy decides)

Emission is timer-driven, so the ticker rides a spawned side task (reaped by
the chain) and marks decisions back through the processor's own inbox — the
queue defines ordering, never task interleaving.

Latency (D3): the transport half of the bot-response time is anchored to the
last `SpeakEnded` (our utterance done) and stamped onto the next
BotTurnEnded's `transport_ms` at first speech.

Silero mode lands with P6.3 (VAD off-thread); this processor is the
`use_silero_vad=False` path.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from opkura.frames import BotPartialSpeech, BotTurnEnded
from opkura.voice.frames import CallEnded, SpeakEnded, SpeechStarted, UtteranceBoundary
from opkura.voice.processor import Processor

logger = logging.getLogger(__name__)

GRACE_PERIOD_S = 1.5     # the monolith's post-UtteranceEnd resumption window
TICK_S = 0.05


class _Tick:
    __slots__ = ()


class TurnDetector(Processor):
    def __init__(
        self,
        *,
        turn_timeout_s: float = 15.0,
        grace_s: float = GRACE_PERIOD_S,
        name: str = "turn-detect",
        **kw,
    ):
        super().__init__(name, **kw)
        self.turn_timeout_s = turn_timeout_s
        self.grace_s = grace_s
        self._speaking = False
        self._grace_deadline: Optional[float] = None
        self._turn_deadline: float = 0.0
        self._speak_ended_at: Optional[float] = None
        self._first_speech_latency_ms: Optional[float] = None

    async def start(self) -> None:
        self._arm_turn()
        self.spawn(self._ticker(), name="ticker")

    def _arm_turn(self) -> None:
        self._speaking = False
        self._grace_deadline = None
        self._turn_deadline = time.monotonic() + self.turn_timeout_s

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self.inbox.put_nowait(_Tick())
            except asyncio.QueueFull:
                # A full inbox means the chain is momentarily backed up; a
                # lost tick is fine — the next one comes in 50ms. Raising
                # here would kill the ticker and with it ALL future turn
                # detection (the 2026-09-01 live-smoke failure mode).
                continue

    def _on_speech_signal(self) -> None:
        if not self._speaking and self._speak_ended_at is not None:
            self._first_speech_latency_ms = (
                (time.monotonic() - self._speak_ended_at) * 1000.0
            )
            self._speak_ended_at = None
        self._speaking = True
        self._grace_deadline = None            # bot resumed — keep listening

    async def _end_turn(self, *, reason: str) -> None:
        latency = self._first_speech_latency_ms
        self._first_speech_latency_ms = None
        logger.debug("turn end (%s)", reason)
        await self.emit(BotTurnEnded(transport_ms=latency))
        self._arm_turn()

    async def handle(self, frame) -> None:
        if isinstance(frame, _Tick):
            now = time.monotonic()
            if self._grace_deadline is not None and now >= self._grace_deadline:
                await self._end_turn(reason="grace expired")
            elif not self._speaking and now >= self._turn_deadline:
                # Dead air for a whole turn window: the turn is over with
                # whatever (nothing) accumulated — the engine's silence
                # policy owns what happens next.
                await self._end_turn(reason="turn timeout")
            return
        if isinstance(frame, SpeechStarted):
            self._on_speech_signal()
            await self.emit(frame)
            return
        if isinstance(frame, BotPartialSpeech):
            self._on_speech_signal()
            await self.emit(frame)
            return
        if isinstance(frame, UtteranceBoundary):
            if self._speaking:
                self._speaking = False
                self._grace_deadline = time.monotonic() + self.grace_s
            await self.emit(frame)
            return
        if isinstance(frame, SpeakEnded):
            # CONSUMED, not forwarded: SpeakEnded originates at the TTS link
            # (downstream) and reaches us via the session's feedback edge —
            # forwarding it would circulate it forever.
            self._speak_ended_at = time.monotonic()
            return
        await self.emit(frame)

    async def on_call_ended(self, frame: CallEnded) -> None:
        self._grace_deadline = None
