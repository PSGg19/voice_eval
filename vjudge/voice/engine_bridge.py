"""P6.1 — the engine link: the ConversationEngine hosted inside the chain.

On a worker (P6.4) the whole chain is ONE loop, so the two-thread bridge the
wrapped monolith needs (transport/voice_adapter.py) disappears: the engine
runs as this processor's spawned side task, pulls bot frames from an internal
queue `handle()` feeds, and its delivered caller frames go straight
downstream — CallerSpeech to TTS, CallerDTMF to the DTMF strategy,
CallerHangup to whoever ends the call.

The FrameTransport the engine drives is a small inner object rather than the
processor itself: both protocols define `start()`, and the engine calling the
processor's would re-spawn the drive task.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Sequence

from vjudge.engine.conversation import ConversationEngine, ConversationOutcome
from vjudge.frames import (
    BotFinalSpeech,
    BotTurnEnded,
    CallerFrame,
    CallerHangup,
    ControlEnded,
)
from vjudge.voice.frames import CallEnded
from vjudge.voice.processor import Processor

logger = logging.getLogger(__name__)


class _EngineTransport:
    """The FrameTransport surface the engine drives, over the bridge."""

    nudge_on_silence = False  # D4: voice — the abandonment counter decides

    def __init__(self, bridge: "EngineBridge"):
        self._bridge = bridge

    async def start(self) -> None:
        return None

    async def receive(self):
        return await self._bridge._engine_inbox.get()

    async def deliver(self, frames: Sequence[CallerFrame]) -> None:
        for frame in frames:
            await self._bridge.emit(frame)
        if any(isinstance(f, CallerHangup) for f in frames):
            self._bridge.hangup_delivered = True

    async def finish(self, *, graceful: bool) -> None:
        return None


class EngineBridge(Processor):
    def __init__(self, conversation: ConversationEngine,
                 name: str = "engine", dispatcher=None, **kw):
        super().__init__(name, **kw)
        self.conversation = conversation
        # The runner's observer dispatcher. The chain runs under the SERVER's
        # websocket task tree, which never inherited the runner's ContextVar —
        # without re-installing it here, every turn/generation event the
        # engine emits during a voice call is a silent no-op (text runs kept
        # their spans; voice traces were bare roots).
        self.dispatcher = dispatcher
        self.outcome: Optional[ConversationOutcome] = None
        self.hangup_delivered = False
        self._engine_inbox: asyncio.Queue = asyncio.Queue()
        self._drive_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._drive_task = self.spawn(self._drive(), name="conversation")

    async def _drive(self) -> None:
        if self.dispatcher is not None:
            from vjudge.observers.dispatcher import install_dispatcher

            install_dispatcher(self.dispatcher)
        self.outcome = await self.conversation.run(_EngineTransport(self))
        logger.info("engine settled: termination=%s",
                    self.outcome.termination)

    async def handle(self, frame) -> None:
        # The engine's diet: final speech and turn ends. Partials feed the
        # barge processor UPSTREAM of this link; audio never reaches here
        # in a well-ordered chain but is forwarded harmlessly if it does.
        if isinstance(frame, (BotFinalSpeech, BotTurnEnded)):
            self._engine_inbox.put_nowait(frame)
        await self.emit(frame)

    async def on_call_ended(self, frame: CallEnded) -> None:
        # The wire is gone: settle the engine (REMOTE_ENDED unless it
        # already terminated) and wait for the outcome, bounded.
        self._engine_inbox.put_nowait(ControlEnded(reason=frame.reason))
        if self._drive_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._drive_task), 10)
            except asyncio.TimeoutError:
                logger.error("engine did not settle after call end")
