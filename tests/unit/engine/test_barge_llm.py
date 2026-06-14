from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opkura.engine.caller import AICallerEngine
from opkura.core.models import (
    BargeInConfig,
    CallResult,
    CallStatus,
    LLMConfig,
    Scenario,
)
from opkura.core.scenario_loader import load
from opkura.transport.stream import MediaStreamSession


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


def _make_session(engine: AICallerEngine) -> MediaStreamSession:
    result = CallResult(
        flow_name=engine.scenario.name,
        target=engine.scenario.target,
        status=CallStatus.IN_PROGRESS,
    )
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
            # _ws.send_text directly — different assertion surface).
            streaming_tts=False,
        )
    session._start_time = 0.0
    session._send_audio = AsyncMock()
    return session


# --- 9: periodic invocation cadence ------------------------------------------

@pytest.mark.asyncio
async def test_llm_mode_periodic_invocation():
    """Engine asks the LLM roughly once per decide_every_ms while bot speaks
    and the partial keeps changing.
    """
    # 6 "no-barge" decisions: enough headroom for the timing window below.
    no_barge = [
        {"speech": "", "dtmf": "", "hangup": False, "barge": False} for _ in range(6)
    ]
    engine = AICallerEngine(_llm_scenario(decide_every_ms=50), _llm_returning(*no_barge))
    session = _make_session(engine)

    async def feed_partials():
        for i in range(1, 5):
            session._partial_bot_transcript = f"hello word{i}"
            await asyncio.sleep(0.05)
        session._utterance_boundary.set()

    task = asyncio.create_task(session._llm_decision_loop())
    await feed_partials()
    await task
    # 4 distinct partials over ~200ms with 50ms interval → at least 2 invocations.
    assert engine.llm.complete_json.await_count >= 2


# --- 10: barge=true emits audio and records transcript marker ----------------

@pytest.mark.asyncio
async def test_llm_returns_barge_emits_audio():
    """LLM returns barge=true with speech on the first decision → engine emits
    audio mid-utterance and decision loop exits (one barge per bot turn).
    """
    engine = AICallerEngine(
        _llm_scenario(decide_every_ms=20),
        _llm_returning(
            {"speech": "WAIT", "dtmf": "", "hangup": False, "barge": True}
        ),
    )
    session = _make_session(engine)
    session._partial_bot_transcript = "Hello, I am Mich"
    session._utterance_boundary.clear()

    # Run the decision loop briefly — first tick should return a barge.
    task = asyncio.create_task(session._llm_decision_loop())
    await asyncio.sleep(0.15)
    session._utterance_boundary.set()
    await task
    # Emit is spawned as a separate task (post-bugfix) so the decision loop
    # can return immediately. Drain to make the assertions deterministic.
    await session._drain_barge_tasks()

    # Audio sent and transcript marker recorded
    assert session._send_audio.await_count >= 1
    assert len(session.result.transcript) == 1
    entry = session.result.transcript[0]
    assert entry.barge is True
    assert entry.text == "WAIT"
    assert entry.bot_partial_at_barge == "Hello, I am Mich"


# --- v1 bugfix: decision loop spawns emit as a separate task ----------------

@pytest.mark.asyncio
async def test_llm_decision_loop_spawns_emit_as_separate_task():
    """v1 bugfix: emit_barge is spawned (not awaited inline) so cancelling
    the decision task at end-of-bot-turn doesn't truncate the audio chunk
    loop mid-frame. The conversation loop's _drain_barge_tasks finishes the
    emit before the next caller turn.
    """
    engine = AICallerEngine(
        _llm_scenario(decide_every_ms=20),
        _llm_returning(
            {"speech": "WAIT", "dtmf": "", "hangup": False, "barge": True}
        ),
    )
    session = _make_session(engine)
    session._partial_bot_transcript = "Hello"

    task = asyncio.create_task(session._llm_decision_loop())
    # Let one tick fire, then signal utterance end (simulates conversation
    # loop's finally cancelling the decision task).
    await asyncio.sleep(0.05)
    session._utterance_boundary.set()
    await task

    # Emit got spawned as a tracked task — not held by the (now-done)
    # decision task. _conversation_loop's drain will finish it.
    assert len(session._barge_tasks) >= 1
    await session._drain_barge_tasks()
    # And the barge entry made it into the transcript.
    assert any(e.barge for e in session.result.transcript)


# --- 11: skip when partial unchanged -----------------------------------------

@pytest.mark.asyncio
async def test_llm_skip_if_partial_unchanged():
    """Decision loop only invokes the LLM when the partial has changed since
    the previous tick — repeated identical partials cost zero LLM calls.
    """
    engine = AICallerEngine(
        _llm_scenario(decide_every_ms=20),
        _llm_returning(
            {"speech": "", "dtmf": "", "hangup": False, "barge": False}
        ),
    )
    session = _make_session(engine)
    session._partial_bot_transcript = "same partial"

    task = asyncio.create_task(session._llm_decision_loop())
    await asyncio.sleep(0.15)
    session._utterance_boundary.set()
    await task

    # One invocation for the first sighting; subsequent ticks see identical
    # partial → skipped.
    assert engine.llm.complete_json.await_count == 1


# --- 12: barge_in: off skips the decision loop --------------------------------

@pytest.mark.asyncio
async def test_barge_in_off_skips_decision_loop():
    """Scenario with no barge_in → session reports barge_mode 'off', so the
    decision loop is never spawned (gated in _conversation_loop on barge_mode).
    """
    sc = Scenario(
        name="t",
        target="+1",
        vars={},
        persona_rendered="p",
        evaluation_prompt_rendered="e",
        barge_in=None,
    )
    engine = AICallerEngine(sc, _llm_returning())
    session = _make_session(engine)
    assert session._barge_mode == "off"
    assert session._trigger_evaluator is None
    assert session._decision_task is None


# --- maybe_barge: provisional decision, doesn't mutate history --------------

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
    from opkura.core.scenario_loader import BCD_DELIMITER

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
