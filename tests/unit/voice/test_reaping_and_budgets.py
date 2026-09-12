"""P6.5 — the leak-class acceptance: zero orphaned tasks, zero leaked FDs
after a cancelled run; 100 concurrent chains keep loop lag under 50ms with a
bounded FD count.

FDs are counted from /dev/fd (macOS and Linux both serve it), tasks from
asyncio.all_tasks — before/after, in-test, as the PRD words it.
"""
from __future__ import annotations

import asyncio
import os
import socket
import time

import pytest

from opkura.core.models import Scenario
from opkura.engine.caller import AICallerEngine
from opkura.engine.conversation import ConversationEngine
from opkura.llm.base import LLMResult
from opkura.voice.audio_out import AudioOut
from opkura.voice.engine_bridge import EngineBridge
from opkura.voice.frames import InboundAudio
from opkura.voice.processor import Chain
from opkura.voice.serializers import TwilioSerializer
from opkura.voice.stt_processor import STTProcessor
from opkura.voice.turn_detect import TurnDetector
from opkura.voice.tts_processor import TTSProcessor


def fd_count() -> int:
    return len(os.listdir("/dev/fd"))


def make_scenario() -> Scenario:
    return Scenario(
        name="budget-test", target="+15550009999", vars={},
        persona_rendered="caller", evaluation_prompt_rendered="judge",
        max_turns=6,
    )


class ScriptedLLM:
    async def complete_json(self, system, messages, **kw) -> LLMResult:
        return LLMResult(
            {"speech": "Okay.", "dtmf": "", "hangup": False, "barge": False},
            {"input_tokens": 1, "output_tokens": 1},
        )


class SocketSTT:
    """An STT client that holds REAL file descriptors (a socketpair), like a
    live Deepgram WebSocket does — the thing a leaked cancel path leaks."""

    def __init__(self, **callbacks):
        self.cb = callbacks
        self.pair: tuple[socket.socket, socket.socket] | None = None

    async def start(self):
        self.pair = socket.socketpair()

    async def stop(self):
        if self.pair is not None:
            for s in self.pair:
                try:
                    s.close()
                except OSError:
                    pass
            self.pair = None

    async def send_audio(self, payload: bytes):
        if payload == b"VOICE":
            self.cb["on_speech_started"]()
            self.cb["on_final"]("hello")
            self.cb["on_utterance_end"]()


class FakeTTS:
    async def synthesize_stream(self, text: str):
        yield b"\x22" * 160


def build_chain() -> Chain:
    scenario = make_scenario()
    conversation = ConversationEngine(scenario, AICallerEngine(scenario, ScriptedLLM()))

    async def send_text(msg: str) -> None:
        return None

    return Chain([
        STTProcessor(lambda **cb: SocketSTT(**cb)),
        TurnDetector(turn_timeout_s=30.0, grace_s=0.1),
        EngineBridge(conversation),
        TTSProcessor(lambda: FakeTTS()),
        AudioOut(send_text, TwilioSerializer()),
    ])


@pytest.mark.asyncio
async def test_cancelled_run_leaks_zero_tasks_and_zero_fds():
    baseline_fds = fd_count()
    baseline_tasks = len(asyncio.all_tasks())

    chain = build_chain()
    await chain.start()
    await chain.feed(InboundAudio(b"VOICE"))
    await asyncio.sleep(0.1)                   # genuinely mid-run
    assert fd_count() > baseline_fds           # the STT sockets are real

    await asyncio.wait_for(chain.cancel(), 10)
    await asyncio.sleep(0.05)

    assert chain.orphans() == []
    assert len(asyncio.all_tasks()) <= baseline_tasks + 1  # (this test's own)
    assert fd_count() == baseline_fds, "cancel leaked file descriptors"


@pytest.mark.asyncio
async def test_graceful_stop_also_returns_to_fd_baseline():
    baseline_fds = fd_count()
    chain = build_chain()
    await chain.start()
    await chain.feed(InboundAudio(b"VOICE"))
    await asyncio.sleep(0.05)

    await asyncio.wait_for(chain.stop(reason="over"), 10)

    assert chain.orphans() == []
    assert fd_count() == baseline_fds


@pytest.mark.asyncio
async def test_100_concurrent_chains_loop_lag_and_fd_budget():
    """The density acceptance: 100 simulated voice runs on one worker keep
    loop lag <50ms and FDs ≤ 6×runs + constant."""
    runs = 100
    baseline_fds = fd_count()

    lags: list[float] = []
    stop = asyncio.Event()

    async def lag_probe():
        interval = 0.005
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(interval)
            now = time.monotonic()
            lags.append(now - last - interval)
            last = now

    probe = asyncio.create_task(lag_probe())

    chains = [build_chain() for _ in range(runs)]
    for chain in chains:
        await chain.start()
    # every call is mid-conversation: audio arriving, engines settling turns
    for chain in chains:
        await chain.feed(InboundAudio(b"VOICE"))
    await asyncio.sleep(1.0)

    in_flight_fds = fd_count()
    assert in_flight_fds - baseline_fds <= 6 * runs + 32, (
        f"FD budget blown: {in_flight_fds - baseline_fds} over baseline"
    )

    for chain in chains:
        await chain.cancel()
    stop.set()
    await probe

    worst = max(lags)
    assert worst < 0.05, f"loop lag hit {worst*1000:.0f}ms with {runs} chains"
    assert all(c.orphans() == [] for c in chains)
    await asyncio.sleep(0.05)
    assert fd_count() == baseline_fds
