"""Tests for `TestRunner._run_text_conversation` — the text-transport turn loop.

Hermetic: fake transport, fake engine, no HTTP and no LLM. The loop is what
replaces `stream.py:_conversation_loop` on the text path, so what's pinned here
is the termination logic — the part that would otherwise hang a run or spend
turns after the caller has hung up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from opkura.core.config import Settings
from opkura.core.models import (
    CallResult,
    CallStatus,
    LLMConfig,
    STTConfig,
    Scenario,
    TTSConfig,
)
from opkura.engine.caller import CallerTurn
from opkura.orchestrator.runner import TestRunner
from opkura.transport.text import BotTurn


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        twilio_account_sid="AC_test",
        twilio_auth_token="token_test",
        twilio_phone_number="+10000000000",
        deepgram_api_key="dg_test",
        public_url="https://opkura.example.com",
        call_transport="text",
    )


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
    """Returns scripted bot turns; records what the loop sent."""

    turns: list[BotTurn]
    interaction_uuid: str = "int-uuid-1"
    sent: list[tuple[str, str]] = field(default_factory=list)
    closed: bool = False
    _i: int = 0

    async def start(self) -> BotTurn:
        turn = self.turns[self._i]
        self._i += 1
        return turn

    async def send(self, speech: str = "", dtmf: str = "") -> BotTurn:
        self.sent.append((speech, dtmf))
        if self._i >= len(self.turns):
            raise AssertionError("loop asked for more bot turns than the script has")
        turn = self.turns[self._i]
        self._i += 1
        return turn

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
    runner = TestRunner.__new__(TestRunner)  # bypass __init__: no Twilio/TTS/DB
    runner.settings = _settings()
    scenario = scenario or _scenario()
    result = CallResult(id="r1", session_id="r1", flow_name=scenario.name, target=scenario.target)
    await runner._run_text_conversation(
        scenario=scenario, engine=engine, transport=transport, result=result
    )
    return result


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

    result = await _run(engine, transport)

    # Correlation is known immediately — no SIP header, no reconciliation.
    assert result.interaction_uuid == "int-uuid-1"
    assert result.status == CallStatus.IN_PROGRESS  # the caller finalises status
    # bot, caller, bot, caller, bot, caller
    assert [e.speaker for e in result.transcript] == [
        "bot", "caller", "bot", "caller", "bot", "caller",
    ]
    # Consecutive duplicate modules collapse: the path is the signal.
    assert result.modules_visited == ["GREETINGS", "AUTH"]
    assert engine.latencies == [1000.0, 2000.0]
    assert transport.sent == [("Casey Italia", ""), ("10073", "")]


@pytest.mark.asyncio
async def test_hangup_stops_before_sending_another_turn():
    transport = FakeTransport(turns=[_bot("Hello?", module="GREETINGS")])
    engine = FakeEngine(replies=[CallerTurn(speech="wrong number", hangup=True)])

    result = await _run(engine, transport)

    # The caller's hangup turn is recorded, but nothing is POSTed after it —
    # otherwise the run would spend a turn talking to a closed conversation.
    assert transport.sent == []
    assert [e.speaker for e in result.transcript] == ["bot", "caller"]


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

    result = await _run(engine, transport)

    assert transport.sent == [("", "4000000000000002")]
    dtmf_entries = [e for e in result.transcript if e.dtmf]
    assert len(dtmf_entries) == 1
    assert dtmf_entries[0].dtmf == "4000000000000002"


@pytest.mark.asyncio
async def test_silent_bot_is_nudged_then_abandoned():
    """A silent bot gets nudged, like a real caller would — then we give up.

    Over voice an empty or tag-only bot turn is dead air the caller talks
    through, so the call recovers. Ending on the first silence made text
    STRICTER than voice and hid the real defect (a bot replying with a bare
    `<LANG:en-US>`, which strips to nothing). So: nudge twice, then stop.
    """
    transport = FakeTransport(turns=[_bot("", module="GREETINGS")] * 3)
    engine = FakeEngine(replies=[])

    result = await _run(engine, transport)

    assert transport.sent == [
        ("Hello? Are you still there?", ""),
        ("Hello? Are you still there?", ""),
    ], "two nudges, then abandon — not an unbounded loop"
    # bot(silent), nudge, bot(silent), nudge, bot(silent)
    assert [e.speaker for e in result.transcript] == [
        "bot", "caller", "bot", "caller", "bot",
    ]


@pytest.mark.asyncio
async def test_a_recovered_bot_gets_a_fresh_nudge_allowance():
    """One blank turn must not permanently consume the budget.

    Otherwise a call with two unrelated silences hundreds of turns apart would
    be abandoned at the second one.
    """
    transport = FakeTransport(
        turns=[
            _bot("", module="GREETINGS"),          # silent -> nudge
            _bot("Sorry, still here.", module="GREETINGS"),  # recovered
            _bot("Anything else?", module="AUTH"),
        ]
    )
    engine = FakeEngine(replies=[CallerTurn(speech="No thanks", hangup=True)])

    result = await _run(engine, transport)

    assert transport.sent[0] == ("Hello? Are you still there?", "")
    # The engine only ever saw the real utterance, never the blank one.
    assert engine.speech_seen == ["Sorry, still here."]


@pytest.mark.asyncio
async def test_empty_non_hangup_turn_ends_the_loop():
    """A blank, non-hangup caller turn would POST an empty user_message and
    re-trigger the greeting path, so the loop stops instead."""
    transport = FakeTransport(turns=[_bot("Hello?", module="GREETINGS")])
    engine = FakeEngine(replies=[CallerTurn(speech="", dtmf="", hangup=False)])

    await _run(engine, transport)

    assert transport.sent == []


@pytest.mark.asyncio
async def test_max_turns_terminates_the_loop():
    # Enough bot turns that only max_turns can stop it.
    transport = FakeTransport(turns=[_bot(f"turn {i}", module="M") for i in range(10)])
    engine = FakeEngine(
        replies=[CallerTurn(speech=f"reply {i}") for i in range(10)], max_turns=3
    )

    result = await _run(engine, transport, scenario=_scenario(max_turns=3))

    assert engine.turn_count == 3
    assert len(transport.sent) == 2
    assert result.modules_visited == ["M"]


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

    result = await _run(engine, transport)

    # Ordered, so `tool_order` assertions become possible without an LLM.
    assert [t["name"] for t in result.tool_calls] == [
        "get_account_scheduled_payments",
        "get_account_payment_options",
    ]
