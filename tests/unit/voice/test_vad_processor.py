"""P6.3 — VAD off the event loop, Silero turn-end contract intact."""
from __future__ import annotations

import asyncio
import time

import pytest

from opkura.voice.frames import (
    CallEnded,
    InboundAudio,
    SpeechStarted,
    UtteranceBoundary,
)
from opkura.voice.vad_processor import VADProcessor

from tests.unit.voice.test_processor import run_processor


class FakeVAD:
    """Deterministic SileroBotVAD surface with a controllable block time."""

    def __init__(self, block_s: float = 0.0):
        self.block_s = block_s
        self.fed: list[bytes] = []
        self.voice = False
        self.quiet_since = time.monotonic()
        self.resets = 0

    def feed_mulaw(self, chunk: bytes) -> None:
        if self.block_s:
            time.sleep(self.block_s)         # BLOCKING, like real torch
        self.fed.append(chunk)
        if chunk == b"VOICE":
            self.voice = True
            self.quiet_since = time.monotonic()

    def has_seen_voice(self) -> bool:
        return self.voice

    def time_since_voice_s(self) -> float:
        return time.monotonic() - self.quiet_since

    def reset(self) -> None:
        self.voice = False
        self.resets += 1


@pytest.mark.asyncio
async def test_onset_then_silence_emits_speech_started_then_boundary():
    vad = FakeVAD()
    proc = VADProcessor(lambda: vad, silence_threshold_s=0.15)
    out: list = []
    proc.to_sink(out)
    await proc.start()
    proc._run_task = asyncio.create_task(proc.run())

    await proc.inbox.put(InboundAudio(b"VOICE"))
    for _ in range(50):
        await asyncio.sleep(0.02)
        if any(isinstance(f, UtteranceBoundary) for f in out):
            break
    await proc.inbox.put(CallEnded())
    await asyncio.wait_for(proc._run_task, 5)
    await proc.reap()

    kinds = [type(f).__name__ for f in out]
    assert kinds.index("SpeechStarted") < kinds.index("UtteranceBoundary")
    assert vad.resets == 1                    # armed for the next turn
    assert b"VOICE" in vad.fed


@pytest.mark.asyncio
async def test_two_utterances_two_boundaries():
    vad = FakeVAD()
    proc = VADProcessor(lambda: vad, silence_threshold_s=0.1)
    out: list = []
    proc.to_sink(out)
    await proc.start()
    proc._run_task = asyncio.create_task(proc.run())

    for _ in range(2):
        await proc.inbox.put(InboundAudio(b"VOICE"))
        for _ in range(50):
            await asyncio.sleep(0.02)
            if sum(isinstance(f, UtteranceBoundary) for f in out) >= 1:
                break
        out_count = sum(isinstance(f, UtteranceBoundary) for f in out)
        if out_count >= 2:
            break
    for _ in range(80):
        await asyncio.sleep(0.02)
        if sum(isinstance(f, UtteranceBoundary) for f in out) >= 2:
            break

    await proc.inbox.put(CallEnded())
    await asyncio.wait_for(proc._run_task, 5)
    await proc.reap()

    assert sum(isinstance(f, SpeechStarted) for f in out) == 2
    assert sum(isinstance(f, UtteranceBoundary) for f in out) == 2


@pytest.mark.asyncio
async def test_inference_runs_off_the_event_loop():
    """The P6.3 point: a VAD that BLOCKS 20ms per chunk must not stall the
    loop — measured with the same lag probe as the P1.4 acceptance."""
    vad = FakeVAD(block_s=0.02)
    proc = VADProcessor(lambda: vad, silence_threshold_s=60)

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
    frames = [InboundAudio(b"x" * 160) for _ in range(30)]
    t0 = time.monotonic()
    await run_processor(proc, frames, settle_s=0)
    elapsed = time.monotonic() - t0
    stop.set()
    await probe

    assert len(vad.fed) == 30
    assert elapsed >= 0.5, "the blocking VAD really did block somewhere"
    worst = max(lags)
    assert worst < 0.05, f"event loop stalled {worst*1000:.0f}ms during inference"
