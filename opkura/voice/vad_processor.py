"""P6.3 — VAD as a processor, inference OFF the event loop.

Silero inference is sync torch (~5ms per 32ms frame). Inline on the loop —
what the monolith does in its receive path — that is 5ms of loop stall per
frame, multiplied by every concurrent call on the worker. Here each chunk is
fed through a dedicated single-thread executor: one thread keeps the model's
thread affinity and the frame ORDER (VAD state is sequential), `await
run_in_executor` keeps arrival order and hands backpressure to the bounded
inbox, and the loop stays free during inference — the 100-concurrent-calls
loop-lag acceptance depends on exactly this.

Signalling (the Silero turn-end contract, unchanged): voice onset →
`SpeechStarted` once per turn; silence ≥ threshold after voice →
`UtteranceBoundary` once per turn, then the VAD resets for the next turn.
Silero stays the SOLE authority on "the bot is done": pair this processor
with a TurnDetector configured with `grace_s=0`, so the boundary converts to
BotTurnEnded without a second opinion.

DTMF note (the other half of P6.3): the typed DTMF errors
(DTMFRedirectFailedError / DTMFReconnectTimeoutError /
DTMFRedirectAbortedError) and the reattach state machine stay owned by the
live session shell until the P6.6 DTMFStrategy port lands — nothing in the
chain touches them, so their labels and semantics are preserved by not being
rewritten.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from opkura.voice.frames import CallEnded, InboundAudio, SpeechStarted, UtteranceBoundary
from opkura.voice.processor import Processor

logger = logging.getLogger(__name__)

TICK_S = 0.05


class _Signal:
    __slots__ = ("frame",)

    def __init__(self, frame):
        self.frame = frame


class VADProcessor(Processor):
    """`vad_factory()` builds a SileroBotVAD-shaped object: feed_mulaw,
    has_seen_voice, time_since_voice_s, reset."""

    def __init__(self, vad_factory, *, silence_threshold_s: float = 1.2,
                 name: str = "vad", **kw):
        super().__init__(name, **kw)
        self._factory = vad_factory
        self.silence_threshold_s = silence_threshold_s
        self._vad = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._onset_announced = False

    async def start(self) -> None:
        self._vad = self._factory()
        # ONE thread: the model keeps its thread affinity and frames keep
        # their order — VAD state is sequential by nature.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="silero-vad"
        )
        self.spawn(self._watch(), name="watch")

    async def handle(self, frame) -> None:
        if isinstance(frame, _Signal):
            await self.emit(frame.frame)
            return
        if isinstance(frame, InboundAudio):
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    self._executor, self._vad.feed_mulaw, frame.payload
                )
            except Exception as exc:
                logger.warning("VAD inference failed — chunk skipped: %s", exc)
        await self.emit(frame)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            if self._vad is None:
                continue
            try:
                seen = self._vad.has_seen_voice()
                quiet_s = self._vad.time_since_voice_s()
            except Exception:  # pragma: no cover — defensive
                continue
            if seen and not self._onset_announced:
                self._onset_announced = True
                self.inbox.put_nowait(_Signal(SpeechStarted()))
            elif seen and quiet_s >= self.silence_threshold_s:
                # The turn is over — Silero's word is final. Reset for the
                # next turn before announcing, so a fast bot resumption
                # counts as a NEW turn, not a continuation.
                self._vad.reset()
                self._onset_announced = False
                self.inbox.put_nowait(_Signal(UtteranceBoundary()))

    async def on_call_ended(self, frame: CallEnded) -> None:
        await self.cleanup()

    async def cleanup(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
