"""P6.1 — the processor framework: isolated links, structural backpressure,
drain-vs-cancel lifecycle, and the task registry P6.5's reaping asserts on.

Includes the home-grown run_test harness the per-processor acceptance tests
use: frames in → frames out, one link, no chain required.
"""
from __future__ import annotations

import asyncio

import pytest

from judge_it.voice.frames import CallEnded, CallStarted, InboundAudio
from judge_it.voice.processor import Chain, Processor


async def run_processor(processor: Processor, frames: list, *,
                        settle_s: float = 0.05) -> list:
    """The frame-in/frame-out harness: feed `frames`, return what came out."""
    out: list = []
    processor.to_sink(out)
    await processor.start()
    processor._run_task = asyncio.create_task(processor.run())
    for f in frames:
        await processor.inbox.put(f)
    await processor.inbox.put(CallEnded(reason="test over"))
    await asyncio.wait_for(processor._run_task, 5)
    await processor.reap()
    await asyncio.sleep(0)  # let sink appends settle
    return out


class Upper(Processor):
    async def handle(self, frame):
        if isinstance(frame, str):
            await self.emit(frame.upper())
        else:
            await self.emit(frame)


class Doubler(Processor):
    async def handle(self, frame):
        await self.emit(frame)
        await self.emit(frame)


class Slow(Processor):
    def __init__(self, delay_s: float, **kw):
        super().__init__(**kw)
        self.delay_s = delay_s
        self.seen = 0

    async def handle(self, frame):
        self.seen += 1
        await asyncio.sleep(self.delay_s)
        await self.emit(frame)


# ------------------------------------------------------- isolated frame I/O

@pytest.mark.asyncio
async def test_run_processor_harness_frames_in_frames_out():
    out = await run_processor(Upper(), ["hello", InboundAudio(b"\xff"), "bye"])
    # unknown frames forwarded untouched; own slice transformed; sentinel last
    assert out[:3] == ["HELLO", InboundAudio(b"\xff"), "BYE"]
    assert isinstance(out[-1], CallEnded)


@pytest.mark.asyncio
async def test_unrecognised_frames_are_forwarded_by_default():
    out = await run_processor(Processor("passthrough"), [CallStarted(), "x"])
    assert out[0] == CallStarted() and out[1] == "x"


@pytest.mark.asyncio
async def test_a_crashing_handle_drops_the_frame_not_the_link():
    class Fragile(Processor):
        async def handle(self, frame):
            if frame == "boom":
                raise RuntimeError("bad frame")
            await self.emit(frame)

    out = await run_processor(Fragile(), ["a", "boom", "b"])
    assert out[:2] == ["a", "b"]              # the link survived the poison


# ------------------------------------------------------------- backpressure

@pytest.mark.asyncio
async def test_backpressure_is_structural():
    """A slow tail blocks the head's emit at the bounded queue — no code
    measures anything."""
    slow = Slow(delay_s=0.05, inbox_size=2)
    head = Upper(inbox_size=2)
    chain = Chain([head, slow])
    sink: list = []
    slow.to_sink(sink)
    await chain.start()

    async def producer():
        for i in range(10):
            await chain.feed(f"f{i}")
        await chain.feed(CallEnded())

    t0 = asyncio.get_event_loop().time()
    await asyncio.wait_for(producer(), 10)
    produce_s = asyncio.get_event_loop().time() - t0
    # 10 frames × 50ms with a 2-deep buffer: the producer CANNOT finish fast;
    # it was blocked at the full inbox for most of the run.
    assert produce_s > 0.2, f"producer never felt backpressure ({produce_s:.2f}s)"

    await asyncio.wait_for(
        asyncio.gather(*(p._run_task for p in chain.processors)), 10
    )
    assert [f for f in sink if isinstance(f, str)] == [f"F{i}" for i in range(10)]
    assert chain.orphans() == []


# ------------------------------------------------------ lifecycle and reaping

@pytest.mark.asyncio
async def test_graceful_stop_drains_then_reaps():
    class WithSideLoop(Processor):
        def __init__(self, name: str = "", **kw):
            super().__init__(name, **kw)
            self.flushed = False
            self.side_cancelled = False

        async def start(self):
            self.spawn(self._side(), name="keepalive")

        async def _side(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.side_cancelled = True
                raise

        async def on_call_ended(self, frame):
            self.flushed = True

    p1, p2 = WithSideLoop("a"), WithSideLoop("b")
    chain = Chain([p1, p2])
    sink: list = []
    p2.to_sink(sink)
    await chain.start()
    await chain.feed("frame")

    await asyncio.wait_for(chain.stop(reason="done"), 5)

    assert p1.flushed and p2.flushed          # every link saw the sentinel
    assert p1.side_cancelled and p2.side_cancelled
    assert chain.orphans() == []              # P6.5's acceptance shape
    assert sink[0] == "frame"                 # in-flight work drained first


@pytest.mark.asyncio
async def test_cancel_reaps_processor_level_tasks_not_just_the_pipeline():
    """The pipecat-documented leak: cancelling pipeline tasks but leaving the
    STT/TTS side loops running. cancel() must reap BOTH."""
    started = asyncio.Event()

    class Sticky(Processor):
        async def start(self):
            self.spawn(self._recv_loop(), name="stt-recv")

        async def _recv_loop(self):
            started.set()
            while True:                        # never exits on its own
                await asyncio.sleep(0.01)

        async def handle(self, frame):
            await asyncio.sleep(3600)          # wedged mid-frame

    p = Sticky()
    chain = Chain([p])
    await chain.start()
    await chain.feed("wedge")
    await asyncio.wait_for(started.wait(), 2)

    await asyncio.wait_for(chain.cancel(), 5)

    assert chain.orphans() == []
    assert all(t.done() for t in p.all_tasks)


@pytest.mark.asyncio
async def test_stop_escalates_to_cancel_past_the_deadline():
    class Wedged(Processor):
        async def handle(self, frame):
            await asyncio.sleep(3600)

    chain = Chain([Wedged()])
    await chain.start()
    await chain.feed("wedge")
    await asyncio.sleep(0.02)                  # let it wedge mid-handle

    await asyncio.wait_for(chain.stop(timeout_s=0.1), 5)

    assert chain.orphans() == []
