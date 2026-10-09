from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vjudge.engine.caller import AICallerEngine
from vjudge.core.models import (
    BargeInConfig,
    CallResult,
    CallStatus,
    LLMConfig,
    Scenario,
)
from vjudge.core.scenario_loader import load


def _llm_scenario(decide_every_ms: int = 50) -> Scenario:
    return Scenario(
        name="t",
        target="+1",
        vars={"name": "Iris"},
        persona_rendered="You are Iris.",
        evaluation_prompt_rendered="evaluate",
        llm=LLMConfig(temperature=0.3),
        max_turns=30,
        barge_in=BargeInConfig(enabled=True, mode="llm", decide_every_ms=decide_every_ms),
    )


def _llm_returning(*payloads):
    mock = AsyncMock()
    mock.provider = "gemini"
    mock.model = "gemini-2.5-flash"
    mock.complete_json = AsyncMock(side_effect=list(payloads))
    mock.last_usage = None
    return mock


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_maybe_barge_does_not_persist_partial_to_history():
    """Provisional decisions are not committed to engine history. Only the
    actual fire (record_barge) writes to history.
    """
    engine = AICallerEngine(
        _llm_scenario(),
        _llm_returning(
            {"speech": "", "dtmf": "", "hangup": False, "barge": False}
        ),
    )
    before = list(engine.history)
    result = await engine.maybe_barge("bot is saying something")
    assert result is None
    assert engine.history == before


@pytest.mark.asyncio
async def test_maybe_barge_returns_turn_when_llm_decides_to_barge():
    engine = AICallerEngine(
        _llm_scenario(),
        _llm_returning(
            {"speech": "interrupt", "dtmf": "", "hangup": False, "barge": True}
        ),
    )
    result = await engine.maybe_barge("bot mid-sentence")
    assert result is not None
    assert result.barge is True
    assert result.speech == "interrupt"


@pytest.mark.asyncio
async def test_maybe_barge_does_not_end_call_on_double_llm_failure():
    """v1 bugfix: _invoke_llm sets self.hangup=True on its double-failure
    synthetic return (correct for end_bot_turn, wrong for a polling check). A
    flaky LLM during a barge decision must not kill the call mid-utterance —
    end_bot_turn will hangup naturally if the LLM stays unreachable.
    """
    llm = AsyncMock()
    llm.provider = "gemini"
    llm.model = "gemini-2.5-flash"
    llm.complete_json = AsyncMock(
        side_effect=[
            ConnectionError("upstream 500"),
            ConnectionError("upstream 500 retry"),
        ]
    )
    llm.last_usage = None
    engine = AICallerEngine(_llm_scenario(), llm)
    assert engine.hangup is False

    result = await engine.maybe_barge("bot speaking")
    assert result is None
    assert engine.hangup is False  # call lifecycle untouched


@pytest.mark.asyncio
async def test_maybe_barge_filters_barge_true_with_empty_speech_and_dtmf():
    """LLM returning barge=true but nothing to say is meaningless — filter it
    so the stream caller doesn't emit an empty audio frame.
    """
    engine = AICallerEngine(
        _llm_scenario(),
        _llm_returning(
            {"speech": "", "dtmf": "", "hangup": False, "barge": True}
        ),
    )
    result = await engine.maybe_barge("bot speaking")
    assert result is None


# --- record_barge: appends paired user/assistant history entries ------------

def test_record_barge_appends_paired_history():
    engine = AICallerEngine(_llm_scenario(), _llm_returning())
    engine.record_barge(speech="WAIT", dtmf="", bot_partial="bot mid sentence")
    assert len(engine.history) == 2
    assert engine.history[0] == {"role": "user", "content": "bot mid sentence"}
    assert engine.history[1]["role"] == "assistant"
    assert '"barge": true' in engine.history[1]["content"]
    assert '"speech": "WAIT"' in engine.history[1]["content"]


# --- 13 & 14: scenario_loader rejections ------------------------------------

def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "scenario.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_llm_mode_rejects_triggers_in_loader(tmp_path):
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
          mode: llm
          triggers:
            - at_turn: 1
              say: "hi"
        """,
    )
    with pytest.raises(ValueError, match="llm.*does not allow.*triggers"):
        load(p)


def test_llm_mode_stores_bot_context_path_field(tmp_path):
    """The loader parses bot_context_path onto BargeInConfig. (Task 6 also
    appends BCD content to persona_rendered when the file exists — verified
    separately in test_bcg.py. Here we just confirm the field is stored.)
    """
    bcd = tmp_path / "bargin-context.md"
    bcd.write_text("# Barge Context — Bot", encoding="utf-8")
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
          mode: llm
          decide_every_ms: 500
          bot_context_path: "bargin-context.md"
        """,
    )
    sc = load(p)
    assert sc.barge_in.mode == "llm"
    assert sc.barge_in.decide_every_ms == 500
    assert sc.barge_in.bot_context_path == "bargin-context.md"
    # Defense-in-depth: also verify Task 6's _apply_bcd ran. test_bcg.py owns
    # the primary coverage; this catches a future refactor that moves the BCD
    # apply step out of load() and leaves only the parse step here.
    from vjudge.core.scenario_loader import BCD_DELIMITER

    assert BCD_DELIMITER in sc.persona_rendered


def test_llm_mode_decide_every_ms_must_be_positive(tmp_path):
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
          mode: llm
          decide_every_ms: 0
        """,
    )
    with pytest.raises(ValueError, match="decide_every_ms.*>= 1"):
        load(p)
