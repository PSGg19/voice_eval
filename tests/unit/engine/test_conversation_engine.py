"""P2.2 — the conversation engine, tested against fakes with no network.

The PRD acceptance test lives here: a scripted 5-turn conversation against a
fake transport, asserting transcript, turn count and termination reason. The
divergence decisions (P2.5) that live in the engine are each pinned too.
"""
from __future__ import annotations

import asyncio

import pytest

from opkura.core.models import Scenario
from opkura.engine.caller import AICallerEngine, MAX_EMPTY_BOT_TURNS
from opkura.engine.conversation import (
    EMPTY_TURN_NUDGE,
    EMPTY_TURN_NUDGE_LIMIT,
    ConversationEngine,
    Termination,
)
from opkura.frames import (
    BotFinalSpeech,
    BotTurnEnded,
    CallerDTMF,
    CallerHangup,
    CallerSpeech,
    ControlCancelled,
    ControlEnded,
    ControlStarted,
    ModuleEntered,
    ToolCalled,
)
from opkura.llm.base import LLMResult


def make_scenario(max_turns: int = 10) -> Scenario:
    return Scenario(
        name="engine-test", target="+15550009999", vars={},
        persona_rendered="You are the caller.",
        evaluation_prompt_rendered="Judge it.",
        transport="text", max_turns=max_turns,
    )


class ScriptedLLM:
    """Returns the scripted caller replies in order; repeats the last one."""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.calls = 0

    async def complete_json(self, system, messages, **kw) -> LLMResult:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LLMResult(dict(reply), {"input_tokens": 1, "output_tokens": 1})


def bot_turn(text: str, module: str = "", tools: list | None = None,
             reasoning_ms=None, transport_ms=None, tag_only: bool = False):
    """The frame sequence one complete bot turn is made of."""
    frames = []
    if module:
        frames.append(ModuleEntered(module=module))
    for t in tools or []:
        frames.append(ToolCalled(name=t.get("name", ""), payload=t))
    if text:
        frames.append(BotFinalSpeech(text=text))
    frames.append(BotTurnEnded(reasoning_ms=reasoning_ms,
                               transport_ms=transport_ms, tag_only=tag_only))
    return frames


class FakeTransport:
    """Scripted transport: receive() pops pre-laid frames; deliver() records.

    The engine strictly alternates (receive → BotTurnEnded → deliver →
    receive), so a flat list is enough — no queues, no network. When the
    script runs out, the far side 'hangs up' with ControlEnded.
    """

    def __init__(self, turns: list[list], *, nudge_on_silence: bool):
        self.nudge_on_silence = nudge_on_silence
        self._frames = [ControlStarted(interaction_uuid="int-0001")]
        for t in turns:
            self._frames.extend(t)
        self.delivered: list[tuple] = []     # each deliver() call's frames
        self.started = False
        self.finished_graceful: bool | None = None

    async def start(self) -> None:
        self.started = True

    async def receive(self):
        if self._frames:
            return self._frames.pop(0)
        return ControlEnded(reason="script exhausted")

    async def deliver(self, frames) -> None:
        self.delivered.append(tuple(frames))

    async def finish(self, *, graceful: bool) -> None:
        self.finished_graceful = graceful


def run_engine(scenario, llm, transport, **kw):
    engine = ConversationEngine(scenario, AICallerEngine(scenario, llm), **kw)
    outcome = asyncio.run(engine.run(transport))
    return engine, outcome


# ------------------------------------------------------- the acceptance test

def test_scripted_5_turn_conversation_no_network():
    """PRD acceptance: transcript, turn count, termination — no network."""
    scenario = make_scenario(max_turns=5)
    llm = ScriptedLLM([
        {"speech": "Hi, I got a call from you.", "hangup": False},
        {"speech": "My DOB is the fourth of August.", "hangup": False},
        {"speech": "Last four are seven five seven nine.", "hangup": False},
        {"speech": "No payment today.", "hangup": False},
        {"speech": "Goodbye.", "hangup": True},
    ])
    transport = FakeTransport(
        [bot_turn(f"Bot line {i}", module=f"m{i}") for i in range(1, 6)],
        nudge_on_silence=True,
    )

    engine, outcome = run_engine(scenario, llm, transport)

    assert outcome.termination == Termination.CALLER_HANGUP
    assert outcome.bot_turns == 5 and outcome.caller_turns == 5
    speakers = [e.speaker for e in outcome.transcript]
    assert speakers == ["bot", "caller"] * 5           # strict alternation
    assert outcome.transcript[-1].text == "Goodbye."   # D1: final line kept
    assert outcome.modules_visited == ["m1", "m2", "m3", "m4", "m5"]
    assert outcome.interaction_uuid == "int-0001"
    assert transport.finished_graceful is True         # D5: normal end drains


def test_final_line_travels_with_the_hangup_speech_first():
    """D1 regression: the parting line is DELIVERED, not just transcribed."""
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "Thanks, goodbye!", "hangup": True}])
    transport = FakeTransport([bot_turn("Anything else?")], nudge_on_silence=True)

    _, outcome = run_engine(scenario, llm, transport)

    last = transport.delivered[-1]
    assert isinstance(last[0], CallerSpeech) and last[0].text == "Thanks, goodbye!"
    assert isinstance(last[-1], CallerHangup)
    assert last[-1].reason == Termination.CALLER_HANGUP
    assert outcome.termination == Termination.CALLER_HANGUP


def test_max_turns_terminates_with_its_own_reason():
    scenario = make_scenario(max_turns=2)
    llm = ScriptedLLM([{"speech": "Still here.", "hangup": False}])
    transport = FakeTransport(
        [bot_turn("One?"), bot_turn("Two?"), bot_turn("Three?")],
        nudge_on_silence=True,
    )
    _, outcome = run_engine(scenario, llm, transport)
    assert outcome.termination == Termination.MAX_TURNS
    assert outcome.caller_turns == 2
    assert isinstance(transport.delivered[-1][-1], CallerHangup)


def test_dtmf_is_framed_between_speech_and_hangup():
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "Sending it now.", "dtmf": "1234", "hangup": True}])
    transport = FakeTransport([bot_turn("Enter the code.")], nudge_on_silence=True)
    run_engine(scenario, llm, transport)
    kinds = [type(f) for f in transport.delivered[-1]]
    assert kinds == [CallerSpeech, CallerDTMF, CallerHangup]
    assert transport.delivered[-1][1].digits == "1234"


# --------------------------------------------------------------- D2 and D3

def test_empty_bot_turns_are_transcript_entries():
    """D2: silence is evidence — it appears in the transcript on EVERY
    transport, not just text."""
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "Hello?", "hangup": False}])
    transport = FakeTransport(
        [bot_turn(""), bot_turn("Sorry, hi.")],   # empty then recovered
        nudge_on_silence=True,
    )
    _, outcome = run_engine(scenario, llm, transport)
    bot_entries = [e for e in outcome.transcript if e.speaker == "bot"]
    assert bot_entries[0].text == "" and bot_entries[1].text == "Sorry, hi."


def test_latencies_are_two_named_streams():
    """D3: reasoning vs transport latency never merge."""
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "ok", "hangup": True}])
    transport = FakeTransport(
        [bot_turn("hi", reasoning_ms=111.0, transport_ms=222.0)],
        nudge_on_silence=True,
    )
    _, outcome = run_engine(scenario, llm, transport)
    assert outcome.reasoning_latencies_ms == [111.0]
    assert outcome.transport_latencies_ms == [222.0]


# --------------------------------------------------------------------- D4

def test_nudging_transport_gets_bounded_nudges_then_bot_silence():
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "unused", "hangup": False}])
    empties = [bot_turn("", tag_only=True)
               for _ in range(EMPTY_TURN_NUDGE_LIMIT + 1)]
    transport = FakeTransport(empties, nudge_on_silence=True)

    _, outcome = run_engine(scenario, llm, transport)

    assert outcome.termination == Termination.BOT_SILENCE
    assert outcome.nudges_used == EMPTY_TURN_NUDGE_LIMIT
    nudges = [f for turn in transport.delivered for f in turn
              if isinstance(f, CallerSpeech) and f.text == EMPTY_TURN_NUDGE]
    assert len(nudges) == EMPTY_TURN_NUDGE_LIMIT
    assert outcome.tag_only_bot_turns == EMPTY_TURN_NUDGE_LIMIT + 1
    assert llm.calls == 0            # silence never reached the persona LLM


def test_non_nudging_transport_defers_to_the_abandonment_counter():
    """D4, voice policy: no nudges; AICallerEngine's MAX_EMPTY_BOT_TURNS
    decides when dead air becomes a hangup."""
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "unused", "hangup": False}])
    transport = FakeTransport(
        [bot_turn("") for _ in range(MAX_EMPTY_BOT_TURNS)],
        nudge_on_silence=False,
    )
    _, outcome = run_engine(scenario, llm, transport)
    assert outcome.termination == Termination.BOT_SILENCE
    assert outcome.nudges_used == 0
    # no nudge speech was ever delivered; only the final hangup frame
    all_frames = [f for turn in transport.delivered for f in turn]
    assert [type(f) for f in all_frames] == [CallerHangup]


# --------------------------------------------------------------------- D5

def test_cancel_stops_without_draining():
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "hello", "hangup": False}])
    transport = FakeTransport(
        [bot_turn("Hi there."), [ControlCancelled(reason="operator stop")]],
        nudge_on_silence=True,
    )
    _, outcome = run_engine(scenario, llm, transport)
    assert outcome.termination == Termination.CANCELLED
    assert transport.finished_graceful is False


def test_remote_end_is_graceful():
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "hello", "hangup": False}])
    transport = FakeTransport([bot_turn("Hi.")], nudge_on_silence=True)
    _, outcome = run_engine(scenario, llm, transport)   # script exhausts
    assert outcome.termination == Termination.REMOTE_ENDED
    assert transport.finished_graceful is True


# ---------------------------------------------------------------- deadline

def test_engine_owns_the_deadline():
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "hello", "hangup": False}])

    class HangingTransport(FakeTransport):
        async def receive(self):
            if self._frames:
                return self._frames.pop(0)
            await asyncio.sleep(3600)

    transport = HangingTransport([bot_turn("Hi.")], nudge_on_silence=True)
    _, outcome = run_engine(scenario, llm, transport, deadline_s=0.2)
    assert outcome.termination == Termination.DEADLINE
    assert transport.finished_graceful is False        # deadline = cancel


# ------------------------------------------------------------ import purity

def test_engine_has_no_transport_imports():
    """Exit criterion: the engine imports no sockets, no HTTP, no audio."""
    import subprocess
    import sys

    code = (
        "import sys; import opkura.engine.conversation; "
        "transports = [m for m in sys.modules if m.startswith('opkura.transport')]; "
        "third = [m for m in sys.modules if m.split('.')[0] in "
        "('twilio', 'httpx', 'fastapi', 'websockets', 'deepgram', 'uvicorn', 'audioop')]; "
        "assert not transports, f'engine imports transports: {transports}'; "
        "assert not third, f'engine imports transport libs: {third}'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
