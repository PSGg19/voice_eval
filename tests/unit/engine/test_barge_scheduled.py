from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from judge_it.engine.caller import AICallerEngine
from judge_it.engine.barge import ScheduledTriggerEvaluator
from judge_it.core.models import (
    BargeInConfig,
    CallResult,
    CallStatus,
    LLMConfig,
    Scenario,
    ScheduledTrigger,
)
from judge_it.core.scenario_loader import load


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
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio


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
