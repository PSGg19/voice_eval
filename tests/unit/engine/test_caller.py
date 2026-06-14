from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from opkura.engine.caller import AICallerEngine, CallerTurn
from opkura.core.models import LLMConfig, Scenario


def _scenario(max_turns: int = 30, persona: str = "You are Iris.") -> Scenario:
    return Scenario(
        name="t",
        target="+1",
        vars={"name": "Iris"},
        persona_rendered=persona,
        evaluation_prompt_rendered="eval",
        llm=LLMConfig(temperature=0.3),
        max_turns=max_turns,
    )


def _llm_returning(*payloads):
    mock = AsyncMock()
    mock.complete_json = AsyncMock(side_effect=list(payloads))
    mock.provider = "gemini"
    mock.model = "gemini-2.5-flash"
    # AsyncMock auto-creates last_usage as a truthy MagicMock — prevents the
    # `or {}` fallback in _record_llm_call and leaks MagicMock into recorded
    # input_tokens/output_tokens fields. Real GeminiClient seeds None.
    mock.last_usage = None
    return mock


def test_engine_initial_state():
    engine = AICallerEngine(_scenario(), _llm_returning())
    assert engine.turn_count == 0
    assert engine.hangup is False
    assert engine.history == []
    assert engine.is_complete is False


@pytest.mark.asyncio
async def test_end_bot_turn_normal_flow():
    llm = _llm_returning(
        {"speech": "hello", "dtmf": "", "hangup": False, "barge": False}
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("Hi there")
    turn = await engine.end_bot_turn()

    assert turn is not None
    assert turn.speech == "hello"
    assert len(engine.history) == 2
    assert engine.history[0] == {"role": "user", "content": "Hi there"}
    assert engine.history[1]["role"] == "assistant"
    assert json.loads(engine.history[1]["content"])["speech"] == "hello"
    assert engine.turn_count == 1


@pytest.mark.asyncio
async def test_dtmf_passthrough():
    llm = _llm_returning(
        {"speech": "", "dtmf": "1234", "hangup": False, "barge": False}
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("Enter PIN")
    turn = await engine.end_bot_turn()
    assert turn is not None
    assert turn.dtmf == "1234"


@pytest.mark.asyncio
async def test_hangup_signal():
    llm = _llm_returning(
        {"speech": "bye", "dtmf": "", "hangup": True, "barge": False}
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("Goodbye")
    await engine.end_bot_turn()
    assert engine.hangup is True
    assert engine.is_complete is True


@pytest.mark.asyncio
async def test_max_turns_cap():
    llm = _llm_returning(
        {"speech": "a", "dtmf": "", "hangup": False, "barge": False},
        {"speech": "b", "dtmf": "", "hangup": False, "barge": False},
    )
    engine = AICallerEngine(_scenario(max_turns=2), llm)

    engine.accumulate_bot_speech("turn 1")
    await engine.end_bot_turn()
    assert engine.is_complete is False

    engine.accumulate_bot_speech("turn 2")
    await engine.end_bot_turn()
    assert engine.is_complete is True
    assert engine.hangup is True


@pytest.mark.asyncio
async def test_barge_ignored_in_turn_taking(caplog):
    llm = _llm_returning(
        {"speech": "ok", "dtmf": "", "hangup": False, "barge": True}
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("Hi")

    with caplog.at_level("WARNING"):
        turn = await engine.end_bot_turn()

    assert turn is not None
    assert turn.barge is False
    assert any("barge" in r.message.lower() for r in caplog.records)
    # history must not contain barge=true — otherwise the LLM sees inconsistent
    # context on the next turn ("I asked to barge and was accepted").
    assistant_msg = json.loads(engine.history[1]["content"])
    assert assistant_msg["barge"] is False


@pytest.mark.asyncio
async def test_malformed_json_retries_once():
    llm = AsyncMock()
    llm.complete_json = AsyncMock(
        side_effect=[
            ValueError("Gemini returned non-JSON: ''"),
            {"speech": "after-retry", "dtmf": "", "hangup": False, "barge": False},
        ]
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("Hi")
    turn = await engine.end_bot_turn()

    assert turn is not None
    assert turn.speech == "after-retry"
    assert llm.complete_json.await_count == 2


@pytest.mark.asyncio
async def test_malformed_json_twice_ends_call():
    llm = AsyncMock()
    llm.complete_json = AsyncMock(
        side_effect=[
            ValueError("non-JSON 1"),
            ValueError("non-JSON 2"),
        ]
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("Hi")
    turn = await engine.end_bot_turn()

    assert turn is not None
    assert turn.hangup is True
    assert engine.hangup is True
    assert engine.is_complete is True


@pytest.mark.asyncio
async def test_empty_bot_utterance():
    llm = _llm_returning()
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("")
    turn = await engine.end_bot_turn()
    assert turn is None
    assert engine.turn_count == 0
    assert engine.history == []
    assert llm.complete_json.await_count == 0


@pytest.mark.asyncio
async def test_persona_used_as_system_prompt():
    llm = _llm_returning(
        {"speech": "ok", "dtmf": "", "hangup": False, "barge": False}
    )
    engine = AICallerEngine(_scenario(persona="PERSONA-MARKER"), llm)
    engine.accumulate_bot_speech("Hi")
    await engine.end_bot_turn()

    call = llm.complete_json.await_args_list[0]
    assert call.kwargs["system"] == "PERSONA-MARKER"


@pytest.mark.asyncio
async def test_retry_uses_strict_prompt_and_zero_temperature():
    llm = AsyncMock()
    llm.complete_json = AsyncMock(
        side_effect=[
            ValueError("non-JSON"),
            {"speech": "ok", "dtmf": "", "hangup": False, "barge": False},
        ]
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("Hi")
    await engine.end_bot_turn()

    first_call, retry_call = llm.complete_json.await_args_list
    assert first_call.kwargs["temperature"] == 0.3
    assert retry_call.kwargs["temperature"] == 0.0
    assert "EXACTLY THIS JSON" in retry_call.kwargs["system"]
    # messages must be identical between attempts — a regression that mutates
    # history before retry would silently change what the LLM sees.
    assert retry_call.kwargs["messages"] == first_call.kwargs["messages"]


@pytest.mark.asyncio
async def test_history_stays_consistent_across_turns():
    llm = _llm_returning(
        {"speech": "a", "dtmf": "", "hangup": False, "barge": True},
        {"speech": "b", "dtmf": "", "hangup": False, "barge": False},
    )
    engine = AICallerEngine(_scenario(), llm)

    engine.accumulate_bot_speech("first bot")
    await engine.end_bot_turn()
    engine.accumulate_bot_speech("second bot")
    await engine.end_bot_turn()

    assert len(engine.history) == 4
    assert engine.history[0]["content"] == "first bot"
    assert engine.history[2]["content"] == "second bot"
    # First assistant entry must show barge=false in history, even though
    # the LLM returned barge=true.
    assert json.loads(engine.history[1]["content"])["barge"] is False
    # Second LLM call must see the corrected (barge=false) context.
    second_call_messages = llm.complete_json.await_args_list[1].kwargs["messages"]
    assert json.loads(second_call_messages[1]["content"])["barge"] is False


@pytest.mark.asyncio
async def test_accumulates_multiple_chunks():
    llm = _llm_returning(
        {"speech": "ok", "dtmf": "", "hangup": False, "barge": False}
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("Hi")
    engine.accumulate_bot_speech("there")
    await engine.end_bot_turn()
    assert engine.history[0]["content"] == "Hi there"


@pytest.mark.asyncio
async def test_string_booleans_do_not_flip_hangup_barge():
    """LLM returning 'false' string must not be coerced to True via bool('false')."""
    llm = _llm_returning(
        {"speech": "ok", "dtmf": "", "hangup": "false", "barge": "false"}
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("Hi")
    turn = await engine.end_bot_turn()
    assert turn.hangup is False
    assert turn.barge is False
    assert engine.hangup is False


@pytest.mark.asyncio
async def test_dtmf_hallucination_dropped(caplog):
    """LLM hallucinating non-digit dtmf (e.g. 'press 1') must be dropped,
    not passed to generate_sequence which raises ValueError on invalid chars.
    """
    llm = _llm_returning(
        {"speech": "ok", "dtmf": "press 1", "hangup": False, "barge": False}
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("Hi")
    with caplog.at_level("WARNING"):
        turn = await engine.end_bot_turn()
    assert turn.dtmf == ""
    assert any("invalid dtmf" in r.message.lower() for r in caplog.records)
    # History must reflect the sanitized dtmf too — otherwise the LLM sees its
    # original hallucination on the next turn and may double down.
    assistant_msg = json.loads(engine.history[1]["content"])
    assert assistant_msg["dtmf"] == ""


@pytest.mark.asyncio
async def test_dtmf_valid_chars_preserved():
    """Standard DTMF chars (digits, *, #, w pause) must pass through unchanged."""
    llm = _llm_returning(
        {"speech": "", "dtmf": "1234*5#w6", "hangup": False, "barge": False}
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("Enter")
    turn = await engine.end_bot_turn()
    assert turn.dtmf == "1234*5#w6"


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "yes", "1"])
@pytest.mark.asyncio
async def test_string_booleans_truthy_paths(value):
    """Whitelist must include 'true'/'yes'/'1' — guards against accidentally
    shrinking _to_bool's truthy set.
    """
    llm = _llm_returning(
        {"speech": "bye", "dtmf": "", "hangup": value, "barge": False}
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("Hi")
    turn = await engine.end_bot_turn()
    assert turn.hangup is True
    assert engine.hangup is True


@pytest.mark.asyncio
async def test_logwriter_records_llm_call_with_token_usage():
    """When the LLM exposes last_usage, the engine forwards tokens to the logwriter."""
    llm = _llm_returning(
        {"speech": "ok", "dtmf": "", "hangup": False, "barge": False}
    )
    llm.last_usage = {"input_tokens": 42, "output_tokens": 17, "total_tokens": 59}

    recorded = []

    class StubLogwriter:
        def record_llm_call(self, **kw):
            recorded.append(kw)

        def record_bot_response_latency(self, ms):
            pass

    engine = AICallerEngine(_scenario(), llm)
    engine.attach_logwriter(StubLogwriter())
    engine.accumulate_bot_speech("Hi")
    await engine.end_bot_turn()

    assert len(recorded) == 1
    entry = recorded[0]
    assert entry["role"] == "caller"
    assert entry["turn"] == 1
    assert entry["input_tokens"] == 42
    assert entry["output_tokens"] == 17
    assert entry["error"] is None
    assert entry["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_logwriter_records_llm_call_failure_with_error_field():
    """Failed LLM call: output=None, error=<repr>, instead of cramming into output."""
    llm = AsyncMock()
    llm.complete_json = AsyncMock(
        side_effect=[
            ValueError("non-JSON 1"),
            ValueError("non-JSON 2"),
        ]
    )

    recorded = []

    class StubLogwriter:
        def record_llm_call(self, **kw):
            recorded.append(kw)

        def record_bot_response_latency(self, ms):
            pass

    engine = AICallerEngine(_scenario(), llm)
    engine.attach_logwriter(StubLogwriter())
    engine.accumulate_bot_speech("Hi")
    await engine.end_bot_turn()

    assert len(recorded) == 2
    for entry in recorded:
        assert entry["output"] is None
        assert entry["error"] is not None
        assert "non-JSON" in entry["error"]


@pytest.mark.asyncio
async def test_accumulate_whitespace_only_is_noop():
    """Whitespace-only chunks → join+strip → empty utterance → no-op."""
    llm = _llm_returning()
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("   ")
    engine.accumulate_bot_speech("\t\n")
    turn = await engine.end_bot_turn()
    assert turn is None
    assert engine.turn_count == 0
    assert engine.history == []
    assert llm.complete_json.await_count == 0


@pytest.mark.asyncio
async def test_last_bot_utterance_surfaces_joined_speech():
    """V2 path: stream.py reads engine.last_bot_utterance to append bot rows
    to result.transcript (step_results is permanently []). Without this,
    transcript.json and the evaluator both see caller-only conversations.
    """
    llm = _llm_returning(
        {"speech": "ok", "dtmf": "", "hangup": False, "barge": False}
    )
    engine = AICallerEngine(_scenario(), llm)

    assert engine.last_bot_utterance == ""  # initial

    engine.accumulate_bot_speech("Hello,")
    engine.accumulate_bot_speech("Michelle here.")
    await engine.end_bot_turn()
    assert engine.last_bot_utterance == "Hello, Michelle here."


@pytest.mark.asyncio
async def test_last_bot_utterance_resets_on_empty_turn():
    """If a later turn times out with no speech, last_bot_utterance must NOT
    retain the previous turn's value — stream.py would otherwise duplicate
    the prior bot row.
    """
    llm = _llm_returning(
        {"speech": "ok", "dtmf": "", "hangup": False, "barge": False}
    )
    engine = AICallerEngine(_scenario(), llm)
    engine.accumulate_bot_speech("first bot turn")
    await engine.end_bot_turn()
    assert engine.last_bot_utterance == "first bot turn"

    # No accumulate_bot_speech this round → empty buffer
    turn = await engine.end_bot_turn()
    assert turn is None
    assert engine.last_bot_utterance == ""
