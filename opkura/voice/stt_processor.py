"""P6.1 — the STT processor: inbound audio → speech frames.

Wraps a DeepgramSTT-shaped client (start/stop/send_audio + on_final /
on_interim / on_speech_started callbacks) as one chain link:

    InboundAudio  → stt.send_audio (and onward: the audio keeps flowing to
                    taps behind this link — recording, VAD)
    on_final      → BotFinalSpeech        (a fragment; turn detection joins)
    on_interim    → BotPartialSpeech      (the barge processor's food)
    on_speech_started → SpeechStarted
    UtteranceEnd (via on_utterance_end when the client exposes it)
                  → UtteranceBoundary

The Deepgram client fires callbacks from ITS receive task; frames are pushed
onto the chain via call_soon_threadsafe-free same-loop puts — the client's
receive loop runs on the chain's loop, registered through spawn() so P6.5's
reaping owns it.
"""
from __future__ import annotations

import asyncio
import logging

from opkura.frames import BotFinalSpeech, BotPartialSpeech
from opkura.voice.frames import CallEnded, InboundAudio, SpeechStarted, UtteranceBoundary
from opkura.voice.processor import Processor

logger = logging.getLogger(__name__)


class STTProcessor(Processor):
    """`stt_factory(on_final, on_interim, on_speech_started, on_utterance_end)`
    builds the client — a seam so tests inject a fake and production binds
    DeepgramSTT with its usual config."""

    def __init__(self, stt_factory, name: str = "stt", *,
                 emit_utterance_boundary: bool = True, **kw):
        super().__init__(name, **kw)
        self._factory = stt_factory
        self._stt = None
        # False in Silero mode: the VAD is the SOLE turn-end authority, so
        # Deepgram's UtteranceEnd must not produce a second opinion.
        self._emit_boundary = emit_utterance_boundary

    # Callbacks may fire while handle() awaits; queue and flush in-order.
    def _cb_final(self, text: str) -> None:
        if text:
            self._out(BotFinalSpeech(text=text))

    def _cb_interim(self, text: str) -> None:
        if text:
            self._out(BotPartialSpeech(text=text))

    def _cb_speech_started(self) -> None:
        self._out(SpeechStarted())

    def _cb_utterance_end(self) -> None:
        if self._emit_boundary:
            self._out(UtteranceBoundary())

    async def restart(self) -> None:
        """Reattach support: the monolith rebuilds its STT connection per WS
        attach ('STT is rebuilt per attach'); the chain session asks for the
        same here."""
        if self._stt is not None:
            try:
                await self._stt.stop()
            except Exception as exc:
                logger.warning("stt stop during restart failed: %s", exc)
            await self._stt.start()

    def _out(self, frame) -> None:
        # Callbacks run on the chain's loop (the client's receive loop is a
        # spawned side task) — emit directly onto our own inbox so ordering
        # with in-flight audio stays queue-defined, not interleave-defined.
        self.inbox.put_nowait(_Emit(frame))

    async def start(self) -> None:
        self._stt = self._factory(
            on_final=self._cb_final,
            on_interim=self._cb_interim,
            on_speech_started=self._cb_speech_started,
            on_utterance_end=self._cb_utterance_end,
        )
        await self._stt.start()
        # The client's receive loop (if it exposes one to own) is its
        # affair; clients that need a task register it via spawn() from the
        # factory using the processor handle we pass in production wiring.

    async def handle(self, frame) -> None:
        if isinstance(frame, _Emit):
            await self.emit(frame.frame)
            return
        if isinstance(frame, InboundAudio):
            try:
                await self._stt.send_audio(frame.payload)
            except Exception as exc:
                logger.warning("stt send_audio failed: %s", exc)
            # CONSUMED. Audio must not travel past this link: 50 frames/sec
            # through every downstream inbox floods the chain the moment
            # audio-out paces a long utterance — the 2026-09-01 live smoke
            # lost turn detection exactly that way. Taps that need audio
            # (VAD, recording) sit UPSTREAM of, or beside, this link.
            return
        await self.emit(frame)

    async def on_call_ended(self, frame: CallEnded) -> None:
        try:
            await self._stt.stop()
        except Exception as exc:
            logger.warning("stt stop failed: %s", exc)
        # A callback firing during the last send_audio queues its frame
        # BEHIND an already-enqueued CallEnded. Flush those stragglers —
        # the bot's final line must not die in the inbox. Post-call audio
        # is dropped; speech frames go out.
        while not self.inbox.empty():
            leftover = self.inbox.get_nowait()
            if isinstance(leftover, _Emit):
                await self.emit(leftover.frame)

    async def cleanup(self) -> None:
        # The cancel path never reaches on_call_ended — close the client's
        # connection here or it leaks an FD per cancelled call (P6.5).
        if self._stt is not None:
            try:
                await self._stt.stop()
            except Exception:
                pass


class _Emit:
    """Internal wrapper: a callback-produced frame riding our own inbox so
    emission order is the queue's order."""

    __slots__ = ("frame",)

    def __init__(self, frame):
        self.frame = frame
