"""P2.4 — the voice frame adapter and its stream-facing bridge.

The fake here plays MediaStreamSession's part: it drives the bridge exactly
the way stream.py's `_conversation_loop` does (accumulate STT finals, call
`end_bot_turn`, speak whatever comes back, stop at `is_complete`). Barge-in,
VAD and DTMF are not simulated — they stay inside the real session and
delegate straight to AICallerEngine, which the delegation tests pin.

Also holds the PRD cross-transport acceptance test: the same scenario driven
through the text adapter and through the (fake-driven) voice adapter produces
identical transcripts and identical termination.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from opkura.core.models import Scenario
from opkura.engine.caller import AICallerEngine, MAX_EMPTY_BOT_TURNS
from opkura.engine.conversation import ConversationEngine, Termination
from opkura.frames import ControlCancelled
from opkura.llm.base import LLMResult
from opkura.transport.text import BotTurn
from opkura.transport.text_adapter import TextFrameAdapter
from opkura.transport.voice_adapter import VoiceFrameAdapter


def make_scenario(max_turns: int = 10) -> Scenario:
    return Scenario(
        name="voice-adapter-test", target="+15550009999", vars={},
        persona_rendered="You are the caller.",
        evaluation_prompt_rendered="Judge.",
        max_turns=max_turns,
    )


class ScriptedLLM:
    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.calls = 0

    async def complete_json(self, system, messages, **kw) -> LLMResult:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LLMResult(dict(reply), {"input_tokens": 1, "output_tokens": 1})


def build(scenario, llm):
    caller = AICallerEngine(scenario, llm)
    adapter = VoiceFrameAdapter(caller)
    engine = ConversationEngine(scenario, caller)
    return caller, adapter, engine


async def drive_stream(bridge, bot_lines: list[str]):
    """What stream.py's conversation loop does, minus the audio."""
    spoken: list[tuple[str, str, bool]] = []   # (speech, dtmf, hangup)
    for line in bot_lines:
        if bridge.is_complete:
            break
        if line:
            bridge.accumulate_bot_speech(line)
        turn = await bridge.end_bot_turn()
        if turn is not None:
            spoken.append((turn.speech, turn.dtmf, turn.hangup))
    return spoken


# ----------------------------------------------------------- the turn cycle

@pytest.mark.asyncio
async def test_stream_turn_cycle_round_trips_through_the_engine():
    scenario = make_scenario()
    llm = ScriptedLLM([
        {"speech": "Hi, who is this?", "hangup": False},
        {"speech": "Alright, goodbye.", "hangup": True},
    ])
    caller, adapter, engine = build(scenario, llm)
    run_task = asyncio.create_task(engine.run(adapter))

    spoken = await drive_stream(
        adapter.bridge, ["Hello, this is the bank.", "How can I help?"]
    )
    outcome = await asyncio.wait_for(run_task, 5)

    assert spoken == [
        ("Hi, who is this?", "", False),
        ("Alright, goodbye.", "", True),   # D1: goodbye handed BACK to speak
    ]
    assert outcome.termination == Termination.CALLER_HANGUP
    assert outcome.caller_turns == 2 and outcome.bot_turns == 2
    assert adapter.bridge.is_complete
    # The bridge exposed each turn's utterance for the stream's transcript.
    assert adapter.bridge.last_bot_utterance == "How can I help?"


@pytest.mark.asyncio
async def test_empty_turn_means_keep_listening():
    """The pre-wrap contract: an empty bot turn returns None and the stream
    keeps waiting for audio — nothing is spoken, nothing ends."""
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "unused", "hangup": False}])
    caller, adapter, engine = build(scenario, llm)
    run_task = asyncio.create_task(engine.run(adapter))

    turn = await adapter.bridge.end_bot_turn()   # no accumulate → empty turn

    assert turn is None
    assert not adapter.bridge.is_complete
    assert llm.calls == 0
    adapter.transport_closed("test over")
    await asyncio.wait_for(run_task, 5)


@pytest.mark.asyncio
async def test_abandonment_counter_still_ends_dead_air():
    """D4 on voice: no nudges — MAX_EMPTY_BOT_TURNS empty turns become a
    hangup, exactly as before the wrap."""
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "unused", "hangup": False}])
    caller, adapter, engine = build(scenario, llm)
    run_task = asyncio.create_task(engine.run(adapter))

    turns = []
    for _ in range(MAX_EMPTY_BOT_TURNS):
        turns.append(await adapter.bridge.end_bot_turn())

    outcome = await asyncio.wait_for(run_task, 5)
    assert turns[:-1] == [None] * (MAX_EMPTY_BOT_TURNS - 1)
    final = turns[-1]
    assert final is not None and final.hangup
    assert outcome.termination == Termination.BOT_SILENCE
    assert llm.calls == 0                      # silence never reached the LLM


@pytest.mark.asyncio
async def test_transport_closed_settles_the_engine_as_remote_ended():
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "hello", "hangup": False}])
    caller, adapter, engine = build(scenario, llm)
    run_task = asyncio.create_task(engine.run(adapter))

    adapter.bridge.accumulate_bot_speech("Hi.")
    await adapter.bridge.end_bot_turn()
    adapter.transport_closed("far side hung up")

    outcome = await asyncio.wait_for(run_task, 5)
    assert outcome.termination == Termination.REMOTE_ENDED
    assert adapter.bridge.is_complete and adapter.bridge.hangup


@pytest.mark.asyncio
async def test_cancel_unblocks_a_parked_stream_without_speaking():
    """D5: ControlCancelled reaches the engine first; its non-graceful finish
    hands the parked end_bot_turn a None so the stream exits silently."""
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "should never be spoken", "hangup": False}])
    caller, adapter, engine = build(scenario, llm)

    adapter._inbound.put_nowait(ControlCancelled(reason="operator stop"))
    run_task = asyncio.create_task(engine.run(adapter))

    adapter.bridge.accumulate_bot_speech("Hello, this is the bank.")
    turn = await asyncio.wait_for(adapter.bridge.end_bot_turn(), 5)

    outcome = await asyncio.wait_for(run_task, 5)
    assert outcome.termination == Termination.CANCELLED
    assert turn is None                       # nothing spoken after cancel
    assert llm.calls == 0


# ---------------------------------------------------- barge stays delegated

def test_barge_surface_delegates_to_the_same_caller_engine():
    scenario = make_scenario()
    caller = AICallerEngine(scenario, ScriptedLLM([{"speech": "x"}]))
    adapter = VoiceFrameAdapter(caller)
    bridge = adapter.bridge

    assert bridge.barge_mode == caller.barge_mode
    assert bridge.scenario is scenario
    bridge.bot_word_count_in_current_turn = 7
    assert caller.bot_word_count_in_current_turn == 7
    bridge.reset_bot_turn_counters()
    assert caller.bot_word_count_in_current_turn == 0
    assert bridge.triggers_fired is caller.triggers_fired

    bridge.record_barge(speech="wait!", dtmf="", bot_partial="Your balance is")
    committed = json.loads(caller.history[-1]["content"])
    assert committed["speech"] == "wait!" and committed["barge"] is True


def test_transport_latency_is_stamped_not_merged():
    """D3: the stream's measured latency lands on BotTurnEnded.transport_ms;
    reasoning_ms stays None (voice cannot observe it)."""
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "ok", "hangup": True}])

    async def main():
        caller, adapter, engine = build(scenario, llm)
        run_task = asyncio.create_task(engine.run(adapter))
        adapter.bridge.record_bot_response_latency(842.0)
        adapter.bridge.accumulate_bot_speech("Hello.")
        await adapter.bridge.end_bot_turn()
        return await asyncio.wait_for(run_task, 5)

    outcome = asyncio.run(main())
    assert outcome.transport_latencies_ms == [842.0]
    assert outcome.reasoning_latencies_ms == []


# ------------------------------------- PRD acceptance: transport identity

class FakeTextBackend:
    """Text side of the identity test: same bot lines, HTTP-shaped."""

    def __init__(self, lines: list[str]):
        self._lines = list(lines)
        self.interaction_uuid = "int-identity"

    def _next(self) -> BotTurn:
        text = self._lines.pop(0) if self._lines else "(exhausted)"
        return BotTurn(text=text, message_type="RECORD")

    async def start(self) -> BotTurn:
        return self._next()

    async def send(self, speech: str = "", dtmf: str = "") -> BotTurn:
        return self._next()

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_text_and_voice_adapters_produce_identical_conversations():
    """PRD acceptance: the same scenario through the text adapter and a fake
    voice adapter yields identical transcripts and identical termination."""
    scenario = make_scenario(max_turns=5)
    bot_lines = [
        "Hello, this is Riverline Bank.",
        "Can I get your date of birth?",
        "Thank you. Anything else today?",
    ]
    replies = [
        {"speech": "Hi, you called me earlier.", "hangup": False},
        {"speech": "Fourth of August, two thousand one.", "hangup": False},
        {"speech": "No, that's all. Goodbye.", "hangup": True},
    ]

    # -- text path
    text_engine = ConversationEngine(
        scenario, AICallerEngine(scenario, ScriptedLLM(replies))
    )
    text_outcome = await text_engine.run(TextFrameAdapter(FakeTextBackend(bot_lines)))

    # -- voice path (fake stream driving the real bridge)
    caller, adapter, voice_engine = build(scenario, ScriptedLLM(replies))
    run_task = asyncio.create_task(voice_engine.run(adapter))
    await drive_stream(adapter.bridge, bot_lines)
    voice_outcome = await asyncio.wait_for(run_task, 5)

    def shape(outcome):
        return [(e.speaker, e.text, e.dtmf) for e in outcome.transcript]

    assert shape(text_outcome) == shape(voice_outcome)
    assert text_outcome.termination == voice_outcome.termination == Termination.CALLER_HANGUP
    assert text_outcome.caller_turns == voice_outcome.caller_turns == 3
    assert text_outcome.bot_turns == voice_outcome.bot_turns == 3


# ------------------------------------- the two-loop production topology

def test_bridge_on_a_second_threads_loop_round_trips():
    """Regression (found by the 2026-08-31 live voice smoke): in production
    the ConversationEngine runs on the runner's loop while MediaStreamSession
    drives the bridge from the FastAPI server's THREAD. Same-loop asyncio
    primitives silently failed to wake the engine — the caller's reply
    arrived after the bot had given up and hung up. This test reproduces the
    two-thread, two-loop topology and requires a prompt round trip."""
    import threading

    scenario = make_scenario()
    llm = ScriptedLLM([
        {"speech": "Hi, who is this?", "hangup": False},
        {"speech": "Goodbye.", "hangup": True},
    ])
    caller = AICallerEngine(scenario, llm)
    adapter = VoiceFrameAdapter(caller)
    spoken: list[tuple] = []
    errors: list[BaseException] = []

    async def engine_side():
        engine = ConversationEngine(scenario, caller)
        return await engine.run(adapter)

    def server_thread():
        # The uvicorn-thread stand-in: its OWN event loop drives the bridge.
        async def drive():
            # wait until the engine captured its loop (start() ran)
            for _ in range(100):
                if adapter._engine_loop is not None:
                    break
                await asyncio.sleep(0.01)
            for line in ["Hello, this is the bank.", "How can I help?"]:
                adapter.bridge.accumulate_bot_speech(line)
                turn = await asyncio.wait_for(adapter.bridge.end_bot_turn(), 5)
                if turn is not None:
                    spoken.append((turn.speech, turn.hangup))
        try:
            asyncio.run(drive())
        except BaseException as exc:  # noqa: BLE001 — surfaced to the test
            errors.append(exc)

    async def main():
        run_task = asyncio.create_task(engine_side())
        thread = threading.Thread(target=server_thread)
        thread.start()
        while thread.is_alive():
            await asyncio.sleep(0.02)
        thread.join()
        return await asyncio.wait_for(run_task, 5)

    outcome = asyncio.run(main())

    assert not errors, f"server-thread side failed: {errors}"
    assert spoken == [("Hi, who is this?", False), ("Goodbye.", True)]
    assert outcome.termination == Termination.CALLER_HANGUP
