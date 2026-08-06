"""P2.4 — the voice frame adapter: MediaStreamSession wrapped, not decomposed.

`MediaStreamSession` keeps driving everything real-time — turn detection,
barge-in, VAD, DTMF, TTS — exactly as before. What changes is WHO decides the
conversation: the session's `flow` slot now holds a `StreamFlowBridge`, which
translates the session's engine-facing calls into frames for the
`ConversationEngine` and the engine's caller frames back into the
`CallerTurn` the session expects. Decomposing stream.py is Phase 6; this file
is deliberately a shim at its boundary.

The rendezvous: the stream parks inside `bridge.end_bot_turn()` awaiting this
turn's answer; the engine, running concurrently over the adapter, receives
the queued frames and answers every `BotTurnEnded` with exactly one
`deliver()` — possibly frameless, meaning "keep listening" (the engine
guarantees the ack; see `_deliver_caller`).

Divergence decisions this adapter declares (P2.5):

* **D4 — `nudge_on_silence = False`.** Voice silence is dead air the stream
  keeps listening through; the persona engine's abandonment counter
  (`MAX_EMPTY_BOT_TURNS`) decides when it becomes a hangup. Nudging is a
  text-only behaviour.
* **D1 — the final line is spoken.** The engine hands speech and hangup back
  in one turn; the stream speaks the speech first, then its loop observes
  `is_complete` and ends the call — the goodbye is on the wire.
* **D3 — voice measures transport latency only.** The stream's measured
  bot-response time (carrier + STT + bot + TTS) is stamped onto the next
  `BotTurnEnded.transport_ms`; `reasoning_ms` stays None — voice cannot
  observe the bot's own thinking time.
* **D5 — drain vs drop.** `finish(graceful=True)` leaves the stream's own
  teardown to drain in-flight TTS/barge work as it always has;
  `finish(graceful=False)` latches the bridge complete and unblocks a parked
  `end_bot_turn` with None, so the stream loop exits at the next check
  without speaking anything further.

Barge-in stays INSIDE: `maybe_barge` / `record_barge` / the barge counters
delegate straight to the same `AICallerEngine` the conversation engine
drives, so mid-turn barge decisions and end-of-turn replies share one
history, exactly as before the wrap.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Sequence

from opkura.engine.caller import AICallerEngine, CallerTurn
from opkura.frames import (
    BotFinalSpeech,
    BotTurnEnded,
    CallerDTMF,
    CallerFrame,
    CallerHangup,
    CallerSpeech,
    ControlEnded,
    Frame,
)

logger = logging.getLogger(__name__)


class VoiceFrameAdapter:
    """FrameTransport over a wrapped MediaStreamSession.

    Construct with the run's `AICallerEngine`; hand `adapter.bridge` to
    `MediaStreamSession(engine=…)` and run `ConversationEngine.run(adapter)`
    concurrently with the session.
    """

    nudge_on_silence = False  # D4 — see module docstring

    def __init__(self, caller: AICallerEngine):
        self._caller = caller
        self._inbound: asyncio.Queue[Frame] = asyncio.Queue()
        # (future, owning loop) for the currently parked end_bot_turn. TWO
        # loops meet here in production: the ConversationEngine task runs on
        # the RUNNER's loop, while MediaStreamSession drives the bridge from
        # the FastAPI server's thread/loop (cli/main.py runs uvicorn in a
        # thread). asyncio primitives are not thread-safe, so every crossing
        # goes through call_soon_threadsafe — proven live by the 2026-08-31
        # voice smoke, where same-loop put_nowait never woke the engine and
        # the caller's reply arrived 23s after the bot gave up and hung up.
        self._reply: Optional[tuple[asyncio.Future, asyncio.AbstractEventLoop]] = None
        self._engine_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ended = False
        self.bridge = StreamFlowBridge(self, caller)

    def _push(self, frame: Frame) -> None:
        """Enqueue a frame onto the ENGINE's loop, from any thread."""
        target = self._engine_loop
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover — non-async caller
            here = None
        if target is None or target is here:
            self._inbound.put_nowait(frame)
        else:
            target.call_soon_threadsafe(self._inbound.put_nowait, frame)

    # ---- FrameTransport --------------------------------------------------

    async def start(self) -> None:
        # Dialing/answer is the runner's affair (Twilio + the FastAPI WS);
        # frames simply begin arriving once the stream attaches. Capturing
        # the loop here is what makes cross-thread pushes deliverable — the
        # runner creates the engine task before it dials, so this runs
        # before any WS can land.
        self._engine_loop = asyncio.get_running_loop()

    async def receive(self) -> Frame:
        return await self._inbound.get()

    async def deliver(self, frames: Sequence[CallerFrame]) -> None:
        speech, dtmf, hangup = "", "", False
        for f in frames:
            if isinstance(f, CallerSpeech):
                speech = f.text
            elif isinstance(f, CallerDTMF):
                dtmf = f.digits
            elif isinstance(f, CallerHangup):
                hangup = True
        if hangup:
            self._ended = True
        parked = self._reply
        if parked is not None:
            # D1: speech travels in the same turn as the hangup — the stream
            # speaks it before its loop sees is_complete. The future belongs
            # to the BRIDGE's loop; resolve it there.
            turn = CallerTurn(speech=speech, dtmf=dtmf, hangup=hangup)
            self._resolve(parked, turn)
        elif speech or dtmf or hangup:
            logger.warning(
                "voice adapter had no parked turn for delivery "
                "(speech=%r dtmf=%r hangup=%s) — dropped",
                speech[:60], dtmf, hangup,
            )

    @staticmethod
    def _resolve(parked: tuple, value) -> None:
        fut, fut_loop = parked

        def _set() -> None:
            if not fut.done():
                fut.set_result(value)

        try:
            here = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover — non-async caller
            here = None
        if here is fut_loop:
            _set()
        else:
            fut_loop.call_soon_threadsafe(_set)

    async def finish(self, *, graceful: bool) -> None:
        self._ended = True
        parked = self._reply
        if parked is not None:
            # D5, the cancel half: unblock the parked stream with "nothing" —
            # its loop exits at the next is_complete check without speaking.
            self._resolve(parked, None)
        # graceful=True changes nothing here: the stream's own teardown
        # already drains in-flight TTS and barge tasks.

    # ---- runner-facing ---------------------------------------------------

    def transport_closed(self, reason: str = "call ended") -> None:
        """Tell the engine the call is over (remote hangup, session teardown).

        Safe to call late or twice: if the engine already returned, the queue
        is simply never read again.
        """
        self._ended = True
        self._push(ControlEnded(reason=reason))


class StreamFlowBridge:
    """The `flow` surface MediaStreamSession drives, expressed as frames.

    Everything barge- and counter-shaped delegates untouched to the run's
    AICallerEngine (barge stays inside the stream, per the phase contract);
    the turn cycle — accumulate, end, reply — is what gets framed.
    """

    def __init__(self, adapter: VoiceFrameAdapter, caller: AICallerEngine):
        self._adapter = adapter
        self._caller = caller
        self._parts: list[str] = []
        self._transport_ms: Optional[float] = None
        # Stream reads this after end_bot_turn() for its transcript entry.
        self.last_bot_utterance: str = ""

    # ---- the framed turn cycle -------------------------------------------

    def accumulate_bot_speech(self, text: str) -> None:
        if text:
            self._parts.append(text)
            self._adapter._push(BotFinalSpeech(text=text))

    async def end_bot_turn(self) -> Optional[CallerTurn]:
        self.last_bot_utterance = " ".join(self._parts).strip()
        self._parts.clear()
        if self._adapter._ended:
            return None
        # The future lives on THIS loop (the server's); the engine resolves
        # it from its own loop via call_soon_threadsafe (see _resolve).
        loop = asyncio.get_running_loop()
        reply: asyncio.Future = loop.create_future()
        self._adapter._reply = (reply, loop)
        self._adapter._push(BotTurnEnded(transport_ms=self._transport_ms))
        self._transport_ms = None
        try:
            turn = await reply
        finally:
            self._adapter._reply = None
        if turn is None or not (turn.speech or turn.dtmf or turn.hangup):
            return None  # "keep listening" — the pre-wrap empty-turn contract
        return turn

    def record_bot_response_latency(self, latency_ms: float) -> None:
        # D3: what the stream measures is TRANSPORT latency (carrier + STT +
        # bot + TTS). Stamped on the next BotTurnEnded; also forwarded so the
        # logwriter keeps its per-run latency stats exactly as before.
        self._transport_ms = latency_ms
        self._caller.record_bot_response_latency(latency_ms)

    # ---- lifecycle the stream polls ----------------------------------------

    @property
    def hangup(self) -> bool:
        return self._adapter._ended or self._caller.hangup

    @property
    def is_complete(self) -> bool:
        return self._adapter._ended or self._caller.is_complete

    @property
    def current_flow_step(self):
        if self.is_complete:
            return None
        return self._caller.current_flow_step

    @property
    def step_results(self) -> list:
        return []

    # ---- straight delegation: barge and counters stay inside ---------------

    @property
    def scenario(self):
        return self._caller.scenario

    @property
    def barge_mode(self) -> str:
        return self._caller.barge_mode

    @property
    def turn_count(self) -> int:
        return self._caller.turn_count

    @property
    def bot_word_count_in_current_turn(self) -> int:
        return self._caller.bot_word_count_in_current_turn

    @bot_word_count_in_current_turn.setter
    def bot_word_count_in_current_turn(self, value: int) -> None:
        self._caller.bot_word_count_in_current_turn = value

    @property
    def triggers_fired(self) -> set:
        return self._caller.triggers_fired

    def reset_bot_turn_counters(self) -> None:
        self._caller.reset_bot_turn_counters()

    async def maybe_barge(self, partial_bot_transcript: str):
        return await self._caller.maybe_barge(partial_bot_transcript)

    def record_barge(self, speech: str, dtmf: str, bot_partial: str) -> None:
        self._caller.record_barge(speech=speech, dtmf=dtmf, bot_partial=bot_partial)
