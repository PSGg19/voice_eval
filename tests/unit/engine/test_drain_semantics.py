"""P2.5 / D5 — the PRD's two distinct drain tests, verbatim:

* a cancelled run stops within its bound WITHOUT draining queued work;
* a gracefully-ended run drains first.

The fake transport queues real async work per delivery (as voice queues TTS
writes) so "drained" and "dropped" are observable facts, not flags.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from opkura.core.models import Scenario
from opkura.engine.caller import AICallerEngine
from opkura.engine.conversation import ConversationEngine, Termination
from opkura.frames import (
    BotFinalSpeech,
    BotTurnEnded,
    CallerSpeech,
    ControlCancelled,
    ControlStarted,
)
from opkura.llm.base import LLMResult


def make_scenario() -> Scenario:
    return Scenario(
        name="drain-test", target="+15550009999", vars={},
        persona_rendered="caller", evaluation_prompt_rendered="judge",
        max_turns=10,
    )


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def complete_json(self, system, messages, **kw) -> LLMResult:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LLMResult(dict(reply), {"input_tokens": 1, "output_tokens": 1})


class QueuedWorkTransport:
    """Each delivered caller turn queues a slow background job (a stand-in
    for voice's TTS write). finish(graceful=True) awaits the queue;
    finish(graceful=False) abandons it."""

    nudge_on_silence = True

    def __init__(self, frames, job_s: float = 0.15):
        self._frames = list(frames)
        self._job_s = job_s
        self._jobs: list[asyncio.Task] = []
        self.jobs_completed = 0
        self.jobs_dropped = 0
        self.finished_graceful: bool | None = None

    async def start(self) -> None:
        pass

    async def receive(self):
        if self._frames:
            return self._frames.pop(0)
        await asyncio.sleep(3600)   # nothing scripted left — hang until ended

    async def deliver(self, frames) -> None:
        if any(isinstance(f, CallerSpeech) for f in frames):
            self._jobs.append(asyncio.create_task(self._job()))

    async def _job(self):
        await asyncio.sleep(self._job_s)
        self.jobs_completed += 1

    async def finish(self, *, graceful: bool) -> None:
        self.finished_graceful = graceful
        if graceful:
            # DRAIN: queued work completes before the transport closes.
            if self._jobs:
                await asyncio.gather(*self._jobs, return_exceptions=True)
        else:
            # DROP: cancel whatever is still in flight; stop within bound.
            for t in self._jobs:
                if not t.done():
                    t.cancel()
                    self.jobs_dropped += 1


def bot_turn(text: str):
    return [BotFinalSpeech(text=text), BotTurnEnded()]


@pytest.mark.asyncio
async def test_gracefully_ended_run_drains_queued_work_first():
    scenario = make_scenario()
    llm = ScriptedLLM([
        {"speech": "Hello there.", "hangup": False},
        {"speech": "Goodbye.", "hangup": True},
    ])
    frames = [ControlStarted()] + bot_turn("Hi.") + bot_turn("Anything else?")
    transport = QueuedWorkTransport(frames, job_s=0.15)

    engine = ConversationEngine(scenario, AICallerEngine(scenario, llm))
    outcome = await engine.run(transport)

    assert outcome.termination == Termination.CALLER_HANGUP
    assert transport.finished_graceful is True
    # Both queued jobs COMPLETED before run() returned — the drain.
    assert transport.jobs_completed == 2
    assert transport.jobs_dropped == 0


@pytest.mark.asyncio
async def test_cancelled_run_stops_within_bound_without_draining():
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "Hello there.", "hangup": False}])
    # One normal turn (queues a slow job), then the cancel arrives.
    frames = (
        [ControlStarted()]
        + bot_turn("Hi.")
        + [ControlCancelled(reason="operator stop")]
    )
    transport = QueuedWorkTransport(frames, job_s=5.0)   # job outlives any bound

    engine = ConversationEngine(scenario, AICallerEngine(scenario, llm))
    t0 = time.monotonic()
    outcome = await engine.run(transport)
    elapsed = time.monotonic() - t0

    assert outcome.termination == Termination.CANCELLED
    assert transport.finished_graceful is False
    assert transport.jobs_dropped == 1 and transport.jobs_completed == 0
    assert elapsed < 1.0, "cancel must not wait for the 5s queued job"
