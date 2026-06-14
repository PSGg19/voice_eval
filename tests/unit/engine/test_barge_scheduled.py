from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opkura.engine.caller import AICallerEngine
from opkura.engine.barge import ScheduledTriggerEvaluator
from opkura.core.models import (
    BargeInConfig,
    CallResult,
    CallStatus,
    LLMConfig,
    Scenario,
    ScheduledTrigger,
)
from opkura.core.scenario_loader import load
from opkura.transport.stream import MediaStreamSession


def _scheduled_scenario(triggers: list[ScheduledTrigger], max_turns: int = 30) -> Scenario:
    return Scenario(
        name="t",
        target="+1",
        vars={"name": "Iris"},
        persona_rendered="You are Iris.",
        evaluation_prompt_rendered="evaluate",
        llm=LLMConfig(temperature=0.3),
        max_turns=max_turns,
        barge_in=BargeInConfig(enabled=True, mode="scheduled", triggers=triggers),
    )


def _mock_llm():
    llm = AsyncMock()
    llm.provider = "gemini"
    llm.model = "gemini-2.5-flash"
    llm.complete_json = AsyncMock(
        side_effect=lambda **kw: {"speech": "ok", "dtmf": "", "hangup": False, "barge": False}
    )
    llm.last_usage = None
    return llm


def _make_session(engine: AICallerEngine) -> MediaStreamSession:
    """Construct a MediaStreamSession with STT/TTS stubbed out. Tests drive the
    barge machinery via direct method calls (no real Twilio WebSocket).
    """
    result = CallResult(flow_name=engine.scenario.name, target=engine.scenario.target, status=CallStatus.IN_PROGRESS)
    with patch("opkura.transport.stream.DeepgramSTT") as stt_cls:
        stt_cls.return_value = MagicMock(start=AsyncMock(), stop=AsyncMock(), send_audio=AsyncMock())
        tts = MagicMock(synthesize=AsyncMock(return_value=b""))
        session = MediaStreamSession(
            engine=engine,
            deepgram_api_key="x",
            tts=tts,
            result=result,
            # streaming_tts=False so barge tests can assert on _send_audio
            # mock (the streaming path bypasses _send_audio and writes to
            # _ws.send_text directly).
            streaming_tts=False,
        )
    session._start_time = 0.0
    # Replace TTS sender with a no-op so _emit_barge doesn't try to write to a
    # WebSocket. The audio buffer accumulates but goes nowhere.
    session._send_audio = AsyncMock()
    return session


# --- 1. Trigger evaluator: pure logic ----------------------------------------

def test_trigger_fires_at_correct_turn():
    cfg = BargeInConfig(
        enabled=True,
        mode="scheduled",
        triggers=[ScheduledTrigger(at_turn=3, say="hello", after_bot_words=0)],
    )
    ev = ScheduledTriggerEvaluator(cfg)
    fire = ev.check(current_turn=3, bot_word_count=0, already_fired=set())
    assert fire is not None
    assert fire.trigger_index == 0
    assert fire.trigger.say == "hello"


def test_trigger_respects_after_bot_words():
    cfg = BargeInConfig(
        enabled=True,
        mode="scheduled",
        triggers=[ScheduledTrigger(at_turn=2, say="hi", after_bot_words=5)],
    )
    ev = ScheduledTriggerEvaluator(cfg)
    assert ev.check(current_turn=2, bot_word_count=3, already_fired=set()) is None
    assert ev.check(current_turn=2, bot_word_count=4, already_fired=set()) is None
    fire = ev.check(current_turn=2, bot_word_count=5, already_fired=set())
    assert fire is not None and fire.trigger_index == 0


def test_each_trigger_fires_only_once():
    """Same trigger queried after being marked fired returns None — no re-fire."""
    cfg = BargeInConfig(
        enabled=True,
        mode="scheduled",
        triggers=[ScheduledTrigger(at_turn=3, say="hi", after_bot_words=0)],
    )
    ev = ScheduledTriggerEvaluator(cfg)
    fire = ev.check(current_turn=3, bot_word_count=10, already_fired=set())
    assert fire is not None
    already = {fire.trigger_index}
    assert ev.check(current_turn=3, bot_word_count=20, already_fired=already) is None


def test_trigger_does_not_fire_on_wrong_turn():
    cfg = BargeInConfig(
        enabled=True,
        mode="scheduled",
        triggers=[ScheduledTrigger(at_turn=3, say="hi", after_bot_words=0)],
    )
    ev = ScheduledTriggerEvaluator(cfg)
    assert ev.check(current_turn=2, bot_word_count=100, already_fired=set()) is None
    assert ev.check(current_turn=4, bot_word_count=100, already_fired=set()) is None


def test_evaluator_rejects_non_scheduled_mode():
    cfg = BargeInConfig(enabled=True, mode="llm", decide_every_ms=500)
    with pytest.raises(ValueError, match="scheduled"):
        ScheduledTriggerEvaluator(cfg)


# --- 7. Engine state: word count resets per turn -----------------------------

@pytest.mark.asyncio
async def test_word_count_resets_per_turn():
    engine = AICallerEngine(
        _scheduled_scenario([ScheduledTrigger(at_turn=1, say="hi")]),
        _mock_llm(),
    )
    engine.bot_word_count_in_current_turn = 7
    engine.reset_bot_turn_counters()
    assert engine.bot_word_count_in_current_turn == 0


def test_barge_mode_off_when_no_barge_in():
    sc = Scenario(
        name="t",
        target="+1",
        vars={},
        persona_rendered="p",
        evaluation_prompt_rendered="e",
        barge_in=None,
    )
    engine = AICallerEngine(sc, _mock_llm())
    assert engine.barge_mode == "off"


def test_barge_mode_off_when_disabled():
    sc = Scenario(
        name="t",
        target="+1",
        vars={},
        persona_rendered="p",
        evaluation_prompt_rendered="e",
        barge_in=BargeInConfig(enabled=False),
    )
    engine = AICallerEngine(sc, _mock_llm())
    assert engine.barge_mode == "off"


# --- 5 & 6: Stream-level emission + transcript marker + latency fill ---------

@pytest.mark.asyncio
async def test_transcript_marker_format_and_engine_history_recorded():
    engine = AICallerEngine(
        _scheduled_scenario([ScheduledTrigger(at_turn=1, say="WAIT", after_bot_words=0)]),
        _mock_llm(),
    )
    session = _make_session(engine)

    await session._emit_barge(
        speech="WAIT",
        dtmf="",
        bot_partial="hello I am Mich",
    )

    # Transcript marker
    assert len(session.result.transcript) == 1
    entry = session.result.transcript[0]
    assert entry.speaker == "caller"
    assert entry.text == "WAIT"
    assert entry.barge is True
    assert entry.bot_partial_at_barge == "hello I am Mich"
    assert entry.bot_response_after_ms is None  # filled on next bot turn

    # Engine history got a paired user (bot partial) + assistant (barge) entry
    # so the next end_bot_turn LLM call sees what happened mid-utterance.
    assert any("hello I am Mich" in m["content"] for m in engine.history if m["role"] == "user")
    barge_msg = next(m for m in engine.history if m["role"] == "assistant")
    assert '"barge": true' in barge_msg["content"]


@pytest.mark.asyncio
async def test_bot_response_after_ms_filled_when_barge_pending():
    engine = AICallerEngine(
        _scheduled_scenario([ScheduledTrigger(at_turn=1, say="x")]),
        _mock_llm(),
    )
    session = _make_session(engine)

    await session._emit_barge(speech="WAIT", dtmf="", bot_partial="hi")
    assert session._pending_barge_entry is not None
    # Simulate bot reaction time
    await asyncio.sleep(0.02)
    session._fill_bot_response_after_barge_if_pending()

    entry = session.result.transcript[0]
    assert entry.bot_response_after_ms is not None
    assert entry.bot_response_after_ms > 0
    # State cleared so a second turn without a barge doesn't try to fill again
    assert session._pending_barge_entry is None
    assert session._barge_emitted_at is None


@pytest.mark.asyncio
async def test_fill_is_no_op_when_no_pending_barge():
    engine = AICallerEngine(
        _scheduled_scenario([ScheduledTrigger(at_turn=1, say="x")]),
        _mock_llm(),
    )
    session = _make_session(engine)
    # No barge fired this turn — fill should be a clean no-op, not raise.
    session._fill_bot_response_after_barge_if_pending()
    assert session._pending_barge_entry is None
    assert len(session.result.transcript) == 0


@pytest.mark.asyncio
async def test_we_finished_at_cleared_after_barge_emit():
    """v1 bugfix: a barge isn't a turn-completing caller utterance, so its
    end timestamp must not anchor the NEXT bot_response_latency measurement
    in _wait_for_bot_turn. Clear _we_finished_at after the emit.
    """
    engine = AICallerEngine(
        _scheduled_scenario([ScheduledTrigger(at_turn=1, say="x")]),
        _mock_llm(),
    )
    session = _make_session(engine)
    # Simulate a prior caller turn having set this.
    session._we_finished_at = 12345.0
    await session._emit_barge(speech="WAIT", dtmf="", bot_partial="hi")
    assert session._we_finished_at is None


@pytest.mark.asyncio
async def test_drain_barge_tasks_awaits_inflight_emissions():
    """v1 bugfix: between bot turns the conversation loop must await any
    in-flight barge audio so the next caller _speak doesn't interleave
    chunks on the WebSocket (and in _our_audio_buffer).
    """
    engine = AICallerEngine(
        _scheduled_scenario([ScheduledTrigger(at_turn=1, say="x")]),
        _mock_llm(),
    )
    session = _make_session(engine)

    finished = False

    async def slow_emit():
        nonlocal finished
        await asyncio.sleep(0.05)
        finished = True

    session._barge_tasks.append(asyncio.create_task(slow_emit()))
    await session._drain_barge_tasks()

    assert finished is True
    # List pruned so it doesn't grow unboundedly across turns.
    assert session._barge_tasks == []


@pytest.mark.asyncio
async def test_drain_barge_tasks_swallows_task_exceptions():
    """A misbehaving fire task shouldn't kill the conversation loop. The
    drain should swallow exceptions (gather(return_exceptions=True)).
    """
    engine = AICallerEngine(
        _scheduled_scenario([ScheduledTrigger(at_turn=1, say="x")]),
        _mock_llm(),
    )
    session = _make_session(engine)

    async def boom():
        raise RuntimeError("simulated barge fire failure")

    session._barge_tasks.append(asyncio.create_task(boom()))
    # Must not raise — exception is captured by gather.
    await session._drain_barge_tasks()
    assert session._barge_tasks == []


@pytest.mark.asyncio
async def test_emit_barge_with_empty_speech_and_dtmf_is_no_op():
    """Defensive: LLM-mode could in theory return barge=true with empty fields;
    maybe_barge filters these out, but if one slips through _emit_barge must
    not write an empty transcript entry or start a latency timer.
    """
    engine = AICallerEngine(
        _scheduled_scenario([ScheduledTrigger(at_turn=1, say="x")]),
        _mock_llm(),
    )
    session = _make_session(engine)
    await session._emit_barge(speech="", dtmf="", bot_partial="hi")
    assert len(session.result.transcript) == 0
    assert session._pending_barge_entry is None


# --- 8: scenario_loader rejects duplicate at_turn ----------------------------

def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "scenario.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_at_turn_uniqueness_enforced_in_loader(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        barge_in:
          enabled: true
          mode: scheduled
          triggers:
            - at_turn: 3
              say: "first"
            - at_turn: 3
              say: "duplicate"
        """,
    )
    with pytest.raises(ValueError, match="duplicate at_turn=3"):
        load(p)


def test_scheduled_mode_requires_triggers(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        barge_in:
          enabled: true
          mode: scheduled
        """,
    )
    with pytest.raises(ValueError, match="requires a non-empty `triggers`"):
        load(p)


def test_scheduled_mode_rejects_decide_every_ms(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        barge_in:
          enabled: true
          mode: scheduled
          decide_every_ms: 500
          triggers:
            - at_turn: 1
              say: "hi"
        """,
    )
    with pytest.raises(ValueError, match="scheduled.*does not allow.*decide_every_ms"):
        load(p)


def test_scheduled_mode_rejects_bot_context_path(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        barge_in:
          enabled: true
          mode: scheduled
          bot_context_path: "x.md"
          triggers:
            - at_turn: 1
              say: "hi"
        """,
    )
    with pytest.raises(ValueError, match="scheduled.*does not allow.*bot_context_path"):
        load(p)


def test_trigger_missing_say_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        barge_in:
          enabled: true
          mode: scheduled
          triggers:
            - at_turn: 1
        """,
    )
    with pytest.raises(ValueError, match="say.*required"):
        load(p)


def test_invalid_mode_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        barge_in:
          enabled: true
          mode: vad
        """,
    )
    with pytest.raises(ValueError, match="must be 'scheduled' or 'llm'"):
        load(p)
