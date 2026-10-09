"""P2.2 — the conversation engine: one loop, any transport.

Consumes bot frames, drives the persona (via `AICallerEngine`), emits caller
frames. Owns the four things a conversation IS, regardless of how its bytes
move: the transcript, the turn count, the termination rules, and the deadline.
It has zero transport imports — no sockets, no HTTP, no audio — enforced by
`tests/unit/engine/test_engine_has_no_transport_imports.py`.

Divergence decisions (P2.5) implemented here, each previously an accidental
difference between the text and voice paths:

* **D1 — the caller's final line is delivered.** When the persona hangs up
  WITH parting speech, the speech frame and the hangup frame travel in the
  same `deliver()` call, speech first. (The old text loop broke out before
  sending; the line existed only in the transcript.)
* **D2 — empty bot turns are transcript entries on every transport.** An
  empty turn is evidence (it is usually the tag-only defect); recording it
  only on text made the transports' transcripts structurally different.
* **D3 — one latency was two.** `BotTurnEnded` carries `reasoning_ms` (the
  bot's own thinking time, text-observable) and `transport_ms` (round trip at
  the transport boundary) as separate named fields; the engine aggregates
  them separately and never sums or compares them.
* **D4 — nudge-on-silence is a transport-declared policy.** The adapter says
  `nudge_on_silence = True/False`. Text nudges (an HTTP turn is definitively
  over; a real caller says "hello?"), bounded by `EMPTY_TURN_NUDGE_LIMIT`.
  Voice declares False: silence there is dead air the stream keeps listening
  through, and the persona engine's abandonment counter
  (`MAX_EMPTY_BOT_TURNS`) decides when to give up.
* **D5 — graceful end and cancel are two operations.** Every normal
  termination calls `finish(graceful=True)`: the adapter drains queued work
  (in-flight sends complete) before closing. Cancellation (a
  `ControlCancelled` frame) and the engine's own deadline call
  `finish(graceful=False)`: pending work is dropped, nothing further is
  delivered, the run stops within its bound.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence

from judge_it.core.models import Scenario, TranscriptEntry
from judge_it.engine.caller import AICallerEngine
from judge_it.frames import (
    BotFinalSpeech,
    BotPartialSpeech,
    BotTurnEnded,
    CallerDTMF,
    CallerFrame,
    CallerHangup,
    CallerSpeech,
    ControlCancelled,
    ControlEnded,
    ControlStarted,
    Frame,
    ModuleEntered,
    ToolCalled,
)
from judge_it.observers import BotTurn as BotTurnEvent
from judge_it.observers import CallerTurn as CallerTurnEvent
from judge_it.observers import emit as emit_event

logger = logging.getLogger(__name__)

# D4 — the bounded "hello? are you still there?" a real caller gives a bot
# that has gone quiet, on transports that declare nudge_on_silence.
EMPTY_TURN_NUDGE = "Hello? Are you still there?"
EMPTY_TURN_NUDGE_LIMIT = 2


class Termination:
    """Why the conversation ended. String constants, not an enum, so results
    serialise without a codec and fakes can compare literally."""

    CALLER_HANGUP = "caller_hangup"        # the persona decided to hang up
    MAX_TURNS = "max_turns"                # scenario.max_turns reached
    BOT_SILENCE = "bot_silence"            # nudges/abandonment allowance spent
    EMPTY_CALLER_TURN = "empty_caller_turn"  # persona produced nothing sendable
    REMOTE_ENDED = "remote_ended"          # transport saw the far side end
    CANCELLED = "cancelled"                # ControlCancelled from outside
    DEADLINE = "deadline"                  # the engine's own wall clock


class FrameTransport(Protocol):
    """What the engine needs from any transport adapter.

    `nudge_on_silence` is divergence D4 as a declared attribute. `receive()`
    returns the next bot/control frame (blocking until one exists);
    `deliver()` takes ONE caller turn as an ordered frame sequence — speech
    before DTMF before hangup (D1) — and performs it; `finish()` closes with
    D5's drain semantics.
    """

    nudge_on_silence: bool

    async def start(self) -> None: ...
    async def receive(self) -> Frame: ...
    async def deliver(self, frames: Sequence[CallerFrame]) -> None: ...
    async def finish(self, *, graceful: bool) -> None: ...


@dataclass
class ConversationOutcome:
    termination: str
    caller_turns: int = 0
    bot_turns: int = 0
    transcript: list[TranscriptEntry] = field(default_factory=list)
    modules_visited: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    interaction_uuid: str = ""
    tag_only_bot_turns: int = 0
    nudges_used: int = 0
    # D3 — the two latencies, aggregated separately.
    reasoning_latencies_ms: list[float] = field(default_factory=list)
    transport_latencies_ms: list[float] = field(default_factory=list)


def _flat(text: str) -> str:
    """One log line per turn even when the flow config bakes newlines in."""
    return " ".join((text or "").split()) or "(no text)"


class ConversationEngine:
    """Drives one conversation over any FrameTransport.

    `caller` is the persona driver (AICallerEngine, or any object with its
    accumulate_bot_speech / end_bot_turn / is_complete / hangup surface).
    `deadline_s` is the wall-clock cap the engine OWNS — expiry terminates
    with `Termination.DEADLINE` and non-graceful finish, whatever the
    transport is doing.

    `transcript` is exposed as a live attribute (and shared into the
    outcome) so a caller that crashes mid-run still has the partial
    transcript for triage.
    """

    def __init__(
        self,
        scenario: Scenario,
        caller: AICallerEngine,
        *,
        deadline_s: Optional[float] = None,
    ):
        self.scenario = scenario
        self.caller = caller
        self.deadline_s = deadline_s
        self.transcript: list[TranscriptEntry] = []
        self.modules_visited: list[str] = []
        self.tool_calls: list[dict] = []
        self.interaction_uuid: str = ""
        # Nudge bookkeeping (D4): `_nudges_left` refills on any non-empty bot
        # turn — a later silence gets its own full allowance; `_nudges_total`
        # is the lifetime count reported on the outcome.
        self._nudges_left: int = EMPTY_TURN_NUDGE_LIMIT
        self._nudges_total: int = 0

    async def run(self, transport: FrameTransport) -> ConversationOutcome:
        outcome = ConversationOutcome(
            termination=Termination.REMOTE_ENDED,
            transcript=self.transcript,
            modules_visited=self.modules_visited,
            tool_calls=self.tool_calls,
        )
        started = time.monotonic()

        def elapsed_ms() -> float:
            return (time.monotonic() - started) * 1000.0

        graceful = True
        try:
            # The deadline covers the WHOLE drive — start, receive, the
            # persona LLM, and deliver. Wrapping only receive() would leave a
            # transport whose SEND hangs (a wedged HTTP POST, a stuck TTS
            # write) holding the slot forever, which is exactly what P1.6
            # exists to prevent. Expiry is a cancel, not a graceful end (D5).
            drive = self._drive(transport, outcome, elapsed_ms)
            if self.deadline_s is None:
                graceful = await drive
            else:
                try:
                    graceful = await asyncio.wait_for(drive, self.deadline_s)
                except asyncio.TimeoutError:
                    outcome.termination = Termination.DEADLINE
                    graceful = False
        finally:
            await transport.finish(graceful=graceful)

        outcome.nudges_used = self._nudges_total
        logger.info(
            "conversation finished: termination=%s caller_turns=%d bot_turns=%d "
            "modules=%s tools=%d",
            outcome.termination, outcome.caller_turns, outcome.bot_turns,
            " → ".join(self.modules_visited) or "-", len(self.tool_calls),
        )
        return outcome

    async def _drive(
        self,
        transport: FrameTransport,
        outcome: ConversationOutcome,
        elapsed_ms,
    ) -> bool:
        """The frame loop. Returns whether the ending was graceful (D5)."""
        await transport.start()

        # State of the bot turn currently being framed.
        utterance_parts: list[str] = []

        while True:
            frame = await transport.receive()

            if isinstance(frame, ControlStarted):
                if frame.interaction_uuid:
                    self.interaction_uuid = frame.interaction_uuid
                    outcome.interaction_uuid = frame.interaction_uuid
                continue
            if isinstance(frame, ControlEnded):
                outcome.termination = Termination.REMOTE_ENDED
                return True
            if isinstance(frame, ControlCancelled):
                outcome.termination = Termination.CANCELLED
                return False
            if isinstance(frame, BotPartialSpeech):
                continue  # informational; barge stays inside the voice stream
            if isinstance(frame, BotFinalSpeech):
                if frame.text:
                    utterance_parts.append(frame.text)
                continue
            if isinstance(frame, ModuleEntered):
                # Collapse consecutive duplicates: the signal is the PATH
                # through the flow, not how many turns a module held.
                if frame.module and (
                    not self.modules_visited
                    or self.modules_visited[-1] != frame.module
                ):
                    self.modules_visited.append(frame.module)
                continue
            if isinstance(frame, ToolCalled):
                self.tool_calls.append(frame.payload)
                continue
            if isinstance(frame, BotTurnEnded):
                done, graceful = await self._on_bot_turn_ended(
                    transport, outcome,
                    utterance=" ".join(utterance_parts).strip(),
                    meta=frame,
                    elapsed_ms=elapsed_ms,
                )
                utterance_parts.clear()
                if done:
                    return graceful
                continue
            logger.warning("engine ignoring unknown frame: %r", frame)

    async def _on_bot_turn_ended(
        self,
        transport: FrameTransport,
        outcome: ConversationOutcome,
        *,
        utterance: str,
        meta: BotTurnEnded,
        elapsed_ms,
    ) -> tuple[bool, bool]:
        """Handle one completed bot turn. Returns (done, graceful)."""
        # D2 — every bot turn is a transcript entry, empty or not.
        self.transcript.append(
            TranscriptEntry(speaker="bot", text=utterance, timestamp_ms=elapsed_ms())
        )
        outcome.bot_turns += 1
        module = self.modules_visited[-1] if self.modules_visited else ""
        emit_event(BotTurnEvent(
            turn_index=outcome.bot_turns,
            text=utterance,
            module=module,
            latency_ms=meta.reasoning_ms,
            tag_only=not utterance,
        ))
        # D3 — the two latencies, kept apart.
        if meta.reasoning_ms is not None:
            outcome.reasoning_latencies_ms.append(meta.reasoning_ms)
        if meta.transport_ms is not None:
            outcome.transport_latencies_ms.append(meta.transport_ms)
        logger.info("%-6s %s%s", "BOT:", _flat(utterance),
                    f"  [{module}]" if module else "")

        if not utterance:
            outcome.tag_only_bot_turns += 1 if meta.tag_only else 0
            # D4 — silence policy is the transport's declaration.
            if transport.nudge_on_silence:
                if self._nudges_left > 0:
                    self._nudges_left -= 1
                    self._nudges_total += 1
                    logger.warning(
                        "bot produced no speech (message_type=%s) — nudging as "
                        "a caller would (%d/%d). A tag-only reply looks "
                        "identical here: the tag is stripped and nothing is left.",
                        meta.message_type,
                        self._nudges_total, EMPTY_TURN_NUDGE_LIMIT,
                    )
                    await self._deliver_caller(
                        transport, outcome, elapsed_ms,
                        speech=EMPTY_TURN_NUDGE, dtmf="", hangup=False,
                    )
                    return False, True
                logger.error(
                    "bot produced no speech %d times in a row — giving up.",
                    EMPTY_TURN_NUDGE_LIMIT + 1,
                )
                await self._deliver_caller(
                    transport, outcome, elapsed_ms,
                    speech="", dtmf="", hangup=True,
                    reason=Termination.BOT_SILENCE,
                )
                outcome.termination = Termination.BOT_SILENCE
                return True, True
            # Non-nudging transport (voice): the persona engine's abandonment
            # counter owns the give-up decision. An empty utterance never
            # yields a reply, so the returned turn is ignored.
            self.caller.accumulate_bot_speech("")
            await self.caller.end_bot_turn()
            if self.caller.hangup:
                await self._deliver_caller(
                    transport, outcome, elapsed_ms,
                    speech="", dtmf="", hangup=True,
                    reason=Termination.BOT_SILENCE,
                )
                outcome.termination = Termination.BOT_SILENCE
                return True, True
            # Empty ack: every BotTurnEnded gets exactly one deliver(), even a
            # frameless one — a wrapped voice stream is parked inside its
            # end_bot_turn call waiting for THIS turn's answer (P2.4), and
            # "keep listening" is an answer.
            await self._deliver_caller(
                transport, outcome, elapsed_ms,
                speech="", dtmf="", hangup=False,
            )
            return False, True

        # Bot is speaking again — refill the silence allowance.
        self._nudges_left = EMPTY_TURN_NUDGE_LIMIT

        self.caller.accumulate_bot_speech(utterance)
        caller_turn = await self.caller.end_bot_turn()
        if caller_turn is None:  # defensive; utterance was non-empty
            outcome.termination = Termination.BOT_SILENCE
            return True, True

        hangup = bool(caller_turn.hangup)
        max_turns_hit = self.caller.is_complete and not hangup
        terminating = hangup or max_turns_hit or not (
            caller_turn.speech or caller_turn.dtmf
        )

        if terminating and not hangup and not max_turns_hit:
            # Nothing to send and not a hangup: sending an empty user message
            # would re-trigger the greeting path, so end here instead.
            logger.warning("caller produced an empty non-hangup turn — ending")
            outcome.termination = Termination.EMPTY_CALLER_TURN
        elif hangup:
            outcome.termination = Termination.CALLER_HANGUP
        elif max_turns_hit:
            outcome.termination = Termination.MAX_TURNS

        # D1 — the final line travels WITH the hangup, speech first.
        await self._deliver_caller(
            transport, outcome, elapsed_ms,
            speech=caller_turn.speech or "",
            dtmf=caller_turn.dtmf or "",
            hangup=terminating,
            reason=outcome.termination if terminating else "",
        )
        return terminating, True

    async def _deliver_caller(
        self,
        transport: FrameTransport,
        outcome: ConversationOutcome,
        elapsed_ms,
        *,
        speech: str,
        dtmf: str,
        hangup: bool,
        reason: str = "",
    ) -> None:
        """Record and deliver one caller turn as an ordered frame sequence."""
        if speech or dtmf:
            self.transcript.append(
                TranscriptEntry(
                    speaker="caller", text=speech,
                    dtmf=dtmf or None, timestamp_ms=elapsed_ms(),
                )
            )
            outcome.caller_turns += 1
            emit_event(CallerTurnEvent(
                turn_index=outcome.caller_turns, text=speech, dtmf=dtmf,
            ))
            logger.info(
                "%-6s %s%s%s", "CALLER:", _flat(speech),
                f"  [dtmf {dtmf}]" if dtmf else "",
                "  [hangup]" if hangup else "",
            )
        frames: list[CallerFrame] = []
        if speech:
            frames.append(CallerSpeech(text=speech, ts_ms=elapsed_ms()))
        if dtmf:
            frames.append(CallerDTMF(digits=dtmf, ts_ms=elapsed_ms()))
        if hangup:
            frames.append(CallerHangup(reason=reason, ts_ms=elapsed_ms()))
        # Delivered even when empty: the turn-level ack a wrapped voice
        # transport synchronises on (see the empty-utterance branch above).
        await transport.deliver(frames)
