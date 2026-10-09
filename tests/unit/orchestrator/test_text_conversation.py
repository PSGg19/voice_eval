"""The text-transport turn loop — now ConversationEngine + TextFrameAdapter.

These tests pinned `TestRunner._run_text_conversation` until P2.3 deleted it;
the SAME termination scenarios now run through the frame path, so the
behaviour that would otherwise hang a run or spend turns after a hangup stays
pinned. One expectation flipped BY DESIGN: divergence D1 (P2.5) — the
caller's final line is now POSTed with the hangup instead of dying in the
transcript, so the fakes see one more send than they used to.

Hermetic: fake transport, fake persona engine, no HTTP and no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from judge_it.core.models import (
    CallResult,
    CallStatus,
    LLMConfig,
    STTConfig,
    Scenario,
    TTSConfig,
)
from judge_it.engine.caller import CallerTurn
from judge_it.engine.conversation import ConversationEngine, Termination
from judge_it.transport.text import BotTurn
from judge_it.transport.text_adapter import TextFrameAdapter


def _scenario(max_turns: int = 30) -> Scenario:
    return Scenario(
        name="loop-test",
        target="+18664530054",
        vars={},
        persona_rendered="caller",
        evaluation_prompt_rendered="judge",
        llm=LLMConfig(),
        stt=STTConfig(),
        tts=TTSConfig(),
        max_turns=max_turns,
    )


@dataclass
class FakeTransport:
    """Returns scripted bot turns; records what the loop sent.

    Unlike the pre-frame fake, an exhausted script returns a throwaway turn
    instead of raising: since D1 the goodbye line IS sent, and the real
    runtime always answers a POST — the reply is discarded by the adapter.
    """

    turns: list[BotTurn]
    interaction_uuid: str = "int-uuid-1"
    sent: list[tuple[str, str]] = field(default_factory=list)
    closed: bool = False
    _i: int = 0

    def _next(self) -> BotTurn:
        if self._i >= len(self.turns):
            return BotTurn(text="(post-goodbye reply)", message_type="RECORD")
        turn = self.turns[self._i]
        self._i += 1
        return turn

    async def start(self) -> BotTurn:
        return self._next()

    async def send(self, speech: str = "", dtmf: str = "") -> BotTurn:
        self.sent.append((speech, dtmf))
        return self._next()

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeEngine:
    """Mimics AICallerEngine's turn contract without an LLM.

    `replies` is consumed one per `end_bot_turn()`. `None` models the real
    engine's empty-bot-utterance path.
    """

    replies: list[CallerTurn | None]
    max_turns: int = 30
    hangup: bool = False
    turn_count: int = 0
    latencies: list[float] = field(default_factory=list)
    speech_seen: list[str] = field(default_factory=list)

    def accumulate_bot_speech(self, text: str) -> None:
        if text:
            self.speech_seen.append(text)

    async def end_bot_turn(self):
        reply = self.replies.pop(0) if self.replies else None
        self.turn_count += 1
        if reply is not None and reply.hangup:
            self.hangup = True
        if self.turn_count >= self.max_turns:
            self.hangup = True
        return reply

    def record_bot_response_latency(self, ms: float) -> None:
        self.latencies.append(ms)

    @property
    def is_complete(self) -> bool:
        return self.hangup or self.turn_count >= self.max_turns


def _bot(text: str, module: str | None = None, tools=None, ms: float | None = None):
    return BotTurn(
        text=text,
        current_module=module,
        tool_calls=tools or [],
        reasoning_ms=ms,
        message_type="RECORD",
        interaction_uuid="int-uuid-1",
    )


async def _run(engine, transport, scenario=None) -> CallResult:
    """Drive the frame path and map the outcome the way the runner does."""
    scenario = scenario or _scenario()
    conversation = ConversationEngine(scenario, engine)
    result = CallResult(
        id="r1", session_id="r1", flow_name=scenario.name, target=scenario.target
    )
    result.transcript = conversation.transcript
    result.status = CallStatus.IN_PROGRESS

    outcome = await conversation.run(TextFrameAdapter(transport))

    result.interaction_uuid = outcome.interaction_uuid or transport.interaction_uuid
    result.modules_visited = outcome.modules_visited
    result.tool_calls = outcome.tool_calls
    for ms in outcome.reasoning_latencies_ms:
        engine.record_bot_response_latency(ms)
    return result, outcome


@pytest.mark.asyncio
async def test_happy_path_records_transcript_modules_and_correlation():
    transport = FakeTransport(
        turns=[
            _bot("May I know your name?", module="GREETINGS", ms=1000.0),
            _bot("And your ZIP?", module="AUTH", ms=2000.0),
            _bot("Thank you, goodbye.", module="AUTH"),
        ]
    )
    engine = FakeEngine(
        replies=[
            CallerTurn(speech="Casey Italia"),
            CallerTurn(speech="10073"),
            CallerTurn(speech="Bye", hangup=True),
        ]
    )

    result, outcome = await _run(engine, transport)

    # Correlation is known immediately — no SIP header, no reconciliation.
    assert result.interaction_uuid == "int-uuid-1"
    # bot, caller, bot, caller, bot, caller
    assert [e.speaker for e in result.transcript] == [
        "bot", "caller", "bot", "caller", "bot", "caller",
    ]
    # Consecutive duplicate modules collapse: the path is the signal.
    assert result.modules_visited == ["GREETINGS", "AUTH"]
    assert engine.latencies == [1000.0, 2000.0]
    # D1: the goodbye is the LAST send now, not a dropped line.
    assert transport.sent == [("Casey Italia", ""), ("10073", ""), ("Bye", "")]


@pytest.mark.asyncio
async def test_hangup_sends_the_final_line_then_stops():
    """D1 flipped this test's old expectation: the parting line is POSTed so
    the bot-side interaction records it — and nothing is sent after it."""
    transport = FakeTransport(turns=[_bot("Hello?", module="GREETINGS")])
    engine = FakeEngine(replies=[CallerTurn(speech="wrong number", hangup=True)])

    result, outcome = await _run(engine, transport)

    assert transport.sent == [("wrong number", "")]
    # The bot's reply to the goodbye is post-hangup noise: NOT in the transcript.
    assert [e.speaker for e in result.transcript] == ["bot", "caller"]
    assert outcome.termination == Termination.CALLER_HANGUP


@pytest.mark.asyncio
async def test_dtmf_turn_is_forwarded_and_recorded():
    transport = FakeTransport(
        turns=[
            _bot("Please enter your 16-digit card number.", module="CARD_PAYMENT"),
            _bot("I have the card ending in zero zero zero two.", module="CARD_PAYMENT"),
        ]
    )
    engine = FakeEngine(
        replies=[
            CallerTurn(speech="", dtmf="4000000000000002"),
            CallerTurn(speech="Goodbye", hangup=True),
        ]
    )

    result, outcome = await _run(engine, transport)

    assert transport.sent[0] == ("", "4000000000000002")
    dtmf_entries = [e for e in result.transcript if e.dtmf]
    assert len(dtmf_entries) == 1
    assert dtmf_entries[0].dtmf == "4000000000000002"


@pytest.mark.asyncio
async def test_silent_bot_is_nudged_then_abandoned():
    """A silent bot gets nudged, like a real caller would — then we give up."""
    transport = FakeTransport(turns=[_bot("", module="GREETINGS")] * 3)
    engine = FakeEngine(replies=[])

    result, outcome = await _run(engine, transport)

    assert transport.sent == [
        ("Hello? Are you still there?", ""),
        ("Hello? Are you still there?", ""),
    ], "two nudges, then abandon — not an unbounded loop"
    # bot(silent), nudge, bot(silent), nudge, bot(silent)  (D2: silences kept)
    assert [e.speaker for e in result.transcript] == [
        "bot", "caller", "bot", "caller", "bot",
    ]
    assert outcome.termination == Termination.BOT_SILENCE


@pytest.mark.asyncio
async def test_a_recovered_bot_gets_a_fresh_nudge_allowance():
    """One blank turn must not permanently consume the budget."""
    transport = FakeTransport(
        turns=[
            _bot("", module="GREETINGS"),                    # silent -> nudge
            _bot("Sorry, still here.", module="GREETINGS"),  # recovered
            _bot("Anything else?", module="AUTH"),
        ]
    )
    engine = FakeEngine(replies=[CallerTurn(speech="No thanks", hangup=True)])

    result, outcome = await _run(engine, transport)

    assert transport.sent[0] == ("Hello? Are you still there?", "")
    # The engine only ever saw the real utterances, never the blank one.
    assert "Sorry, still here." in engine.speech_seen
    assert "" not in engine.speech_seen


@pytest.mark.asyncio
async def test_empty_non_hangup_turn_ends_the_loop():
    """A blank, non-hangup caller turn would POST an empty user_message and
    re-trigger the greeting path, so the loop stops instead."""
    transport = FakeTransport(turns=[_bot("Hello?", module="GREETINGS")])
    engine = FakeEngine(replies=[CallerTurn(speech="", dtmf="", hangup=False)])

    result, outcome = await _run(engine, transport)

    assert transport.sent == []
    assert outcome.termination == Termination.EMPTY_CALLER_TURN


@pytest.mark.asyncio
async def test_max_turns_terminates_the_loop():
    # Enough bot turns that only max_turns can stop it.
    transport = FakeTransport(turns=[_bot(f"turn {i}", module="M") for i in range(10)])
    engine = FakeEngine(
        replies=[CallerTurn(speech=f"reply {i}") for i in range(10)], max_turns=3
    )

    result, outcome = await _run(engine, transport, scenario=_scenario(max_turns=3))

    assert engine.turn_count == 3
    # D1: the third (final) reply is delivered too, alongside the hangup.
    assert len(transport.sent) == 3
    assert result.modules_visited == ["M"]
    assert outcome.termination == Termination.MAX_TURNS


@pytest.mark.asyncio
async def test_tool_calls_accumulate_across_turns():
    transport = FakeTransport(
        turns=[
            _bot("One moment.", module="PIF", tools=[{"name": "get_account_scheduled_payments"}]),
            _bot("Here are your options.", module="PIF", tools=[{"name": "get_account_payment_options"}]),
        ]
    )
    engine = FakeEngine(
        replies=[CallerTurn(speech="pay in full"), CallerTurn(speech="bye", hangup=True)]
    )

    result, outcome = await _run(engine, transport)

    # Ordered, so `tool_order` assertions become possible without an LLM.
    assert [t["name"] for t in result.tool_calls] == [
        "get_account_scheduled_payments",
        "get_account_payment_options",
    ]
