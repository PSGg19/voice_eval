"""P6.2 — barge as a processor: scheduled and LLM modes, one processor.

Pinned against the monolith's semantics: word-count high-water mark,
synchronous trigger claim, one barge per bot turn, the caller-history commit,
and no-op on text by construction (no partial frames → nothing happens).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from vjudge.core.models import BargeInConfig, Scenario, ScheduledTrigger
from vjudge.engine.caller import AICallerEngine
from vjudge.frames import BotPartialSpeech, BotTurnEnded, CallerSpeech
from vjudge.llm.base import LLMResult
from vjudge.voice.barge_processor import BargeProcessor

from tests.unit.voice.test_processor import run_processor


def scheduled_scenario(*, at_turn=1, after_words=4) -> Scenario:
    return Scenario(
        name="barge-test", target="+15550009999", vars={},
        persona_rendered="caller", evaluation_prompt_rendered="judge",
        barge_in=BargeInConfig(
            enabled=True, mode="scheduled",
            triggers=[ScheduledTrigger(at_turn=at_turn, say="Wait, slow down!",
                                       after_bot_words=after_words)],
        ),
    )


def llm_scenario() -> Scenario:
    return Scenario(
        name="barge-llm-test", target="+15550009999", vars={},
        persona_rendered="caller", evaluation_prompt_rendered="judge",
        barge_in=BargeInConfig(enabled=True, mode="llm", decide_every_ms=20),
    )


class BargeLLM:
    """maybe_barge-shaped: barges once the partial mentions the magic word."""

    def __init__(self, replies=None):
        self.replies = replies or []
        self.calls = 0

    async def complete_json(self, system, messages, **kw) -> LLMResult:
        self.calls += 1
        partial = messages[-1]["content"]
        if "balance" in partial:
            return LLMResult(
                {"speech": "Hold on!", "dtmf": "", "hangup": False, "barge": True},
                {"input_tokens": 1, "output_tokens": 1},
            )
        return LLMResult(
            {"speech": "", "dtmf": "", "hangup": False, "barge": False},
            {"input_tokens": 1, "output_tokens": 1},
        )


def partials(*texts):
    return [BotPartialSpeech(text=t) for t in texts]


# --------------------------------------------------------------- scheduled

@pytest.mark.asyncio
async def test_scheduled_trigger_fires_once_at_the_word_threshold():
    scenario = scheduled_scenario(at_turn=1, after_words=4)
    caller = AICallerEngine(scenario, BargeLLM())
    out = await run_processor(BargeProcessor(caller), partials(
        "Hello there",                       # 2 words — below threshold
        "Hello there, this is",              # 4 words — fires
        "Hello there, this is Vera speaking",  # must NOT re-fire
    ))
    barges = [f for f in out if isinstance(f, CallerSpeech) and f.barge]
    assert [b.text for b in barges] == ["Wait, slow down!"]
    # the interruption is in the caller's history for the next reply
    committed = json.loads(caller.history[-1]["content"])
    assert committed["speech"] == "Wait, slow down!" and committed["barge"] is True
    # partials still flowed through to downstream links
    assert sum(isinstance(f, BotPartialSpeech) for f in out) == 3


@pytest.mark.asyncio
async def test_word_count_high_water_mark_survives_interim_regression():
    """Deepgram interims occasionally REGRESS on re-segmentation; a shorter
    partial must not un-fire the threshold."""
    scenario = scheduled_scenario(at_turn=1, after_words=4)
    caller = AICallerEngine(scenario, BargeLLM())
    out = await run_processor(BargeProcessor(caller), partials(
        "one two three",                      # 3 — below
        "one two",                            # regression — hwm stays 3
        "one two three four",                 # 4 — fires
    ))
    barges = [f for f in out if isinstance(f, CallerSpeech) and f.barge]
    assert len(barges) == 1


@pytest.mark.asyncio
async def test_wrong_turn_does_not_fire():
    scenario = scheduled_scenario(at_turn=2, after_words=1)
    caller = AICallerEngine(scenario, BargeLLM())
    out = await run_processor(BargeProcessor(caller), partials(
        "plenty of words in the first turn here",
    ))
    assert not [f for f in out if isinstance(f, CallerSpeech)]


@pytest.mark.asyncio
async def test_turn_end_resets_the_budget_and_counters():
    scenario = scheduled_scenario(at_turn=1, after_words=2)
    caller = AICallerEngine(scenario, BargeLLM())
    frames = partials("some words here") + [BotTurnEnded()] + partials(
        "second turn words now"
    )
    out = await run_processor(BargeProcessor(caller), frames)
    barges = [f for f in out if isinstance(f, CallerSpeech) and f.barge]
    # trigger is at_turn=1 only, and it fired there; turn 2's partials with a
    # reset word count don't re-fire it (already claimed)
    assert len(barges) == 1
    assert caller.bot_word_count_in_current_turn <= 4   # reset happened


# ---------------------------------------------------------------------- llm

@pytest.mark.asyncio
async def test_llm_mode_polls_and_barges_on_decision():
    scenario = llm_scenario()
    llm = BargeLLM()
    caller = AICallerEngine(scenario, llm)
    proc = BargeProcessor(caller)
    out: list = []
    proc.to_sink(out)
    await proc.start()
    proc._run_task = asyncio.create_task(proc.run())

    await proc.inbox.put(BotPartialSpeech(text="Checking your account now"))
    await asyncio.sleep(0.06)                 # polled; no barge decision
    await proc.inbox.put(BotPartialSpeech(text="your balance is nine hundred"))
    for _ in range(50):
        await asyncio.sleep(0.02)
        if proc.fired:
            break

    from vjudge.voice.frames import CallEnded

    await proc.inbox.put(CallEnded())
    await asyncio.wait_for(proc._run_task, 5)
    await proc.reap()

    assert proc.fired == ["Hold on!"]
    barges = [f for f in out if isinstance(f, CallerSpeech) and f.barge]
    assert [b.text for b in barges] == ["Hold on!"]
    assert llm.calls >= 2                     # polled the changing partials
    # maybe_barge must not have poisoned the engine's lifecycle
    assert caller.hangup is False


# ------------------------------------------------------------- off and text

@pytest.mark.asyncio
async def test_off_mode_is_pure_passthrough():
    scenario = Scenario(
        name="no-barge", target="+15550009999", vars={},
        persona_rendered="caller", evaluation_prompt_rendered="judge",
    )
    caller = AICallerEngine(scenario, BargeLLM())
    frames = partials("anything at all here") + [BotTurnEnded()]
    out = await run_processor(BargeProcessor(caller), frames)
    assert not [f for f in out if isinstance(f, CallerSpeech)]
    assert sum(isinstance(f, BotPartialSpeech) for f in out) == 1


@pytest.mark.asyncio
async def test_noop_on_text_by_construction():
    """Text produces no BotPartialSpeech frames — either mode starves."""
    scenario = scheduled_scenario(at_turn=1, after_words=0)
    caller = AICallerEngine(scenario, BargeLLM())
    # a text-shaped stream: finals and turn ends only, no partials
    from vjudge.frames import BotFinalSpeech

    out = await run_processor(BargeProcessor(caller), [
        BotFinalSpeech(text="Hello, this is the bank."), BotTurnEnded(),
    ])
    assert not [f for f in out if isinstance(f, CallerSpeech)]
