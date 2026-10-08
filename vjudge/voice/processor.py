"""P6.1 — Processor + Chain: bounded links, structural backpressure.

A processor is one worker coroutine over one bounded inbox. Emitting to the
next link is `await put` on a bounded queue — when a downstream link is slow
its producer BLOCKS at the queue, and the pressure propagates up the chain to
the wire. No code measures or decides backpressure; the topology is the
policy.

Lifecycle, and the leak class it exists to kill (P6.5): a processor may own
side tasks (an STT receive loop, a TTS keepalive). Those are registered via
`spawn()`, and `Chain.stop()`/`Chain.cancel()` reap them PER PROCESSOR —
cancelling only the pipeline tasks and leaking the side loops is the pipecat-
documented failure this design refuses to reproduce.

Shutdown semantics mirror D5:
* `stop()` (graceful) — inject `CallEnded` at the head; every processor
  drains its inbox, handles the sentinel, forwards it, and returns. Side
  tasks are cancelled after the drain.
* `cancel()` — cancel everything now: run tasks and side tasks alike.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Sequence

from vjudge.voice.frames import CallEnded

logger = logging.getLogger(__name__)

DEFAULT_INBOX = 64  # frames; ~1.3s of 20ms audio — deep enough to smooth
                    # jitter, shallow enough that pressure reaches the wire


class Processor:
    """One link. Override `handle(frame)`; call `emit(frame)` to pass
    downstream. Frames you don't recognise should be forwarded — the chain
    carries mixed vocabularies (audio plumbing + engine frames) and a link
    only transforms its own slice."""

    def __init__(self, name: str = "", *, inbox_size: int = DEFAULT_INBOX):
        self.name = name or type(self).__name__
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=inbox_size)
        self._next: Optional["Processor"] = None
        self._sink: Optional[list] = None
        self._tasks: list[asyncio.Task] = []
        self._run_task: Optional[asyncio.Task] = None
        self._stopped = False

    # ---- wiring -----------------------------------------------------------

    def link(self, downstream: "Processor") -> "Processor":
        self._next = downstream
        return downstream

    def to_sink(self, sink: list) -> None:
        """Terminal link for tests and taps: emitted frames append here."""
        self._sink = sink

    # ---- data plane ---------------------------------------------------------

    async def emit(self, frame) -> None:
        """Pass one frame downstream. Blocks when the next inbox is full —
        THE backpressure mechanism; never bypass it with put_nowait."""
        if self._next is not None:
            await self._next.inbox.put(frame)
        elif self._sink is not None:
            self._sink.append(frame)

    async def handle(self, frame) -> None:  # pragma: no cover - interface
        await self.emit(frame)

    async def on_call_ended(self, frame: CallEnded) -> None:
        """Last chance to flush state before the link stops. Default: none."""

    async def cleanup(self) -> None:
        """Release RESOURCES (sockets, executors, clients) — called on BOTH
        shutdown paths: after a graceful drain and after cancel(). A cancelled
        chain never reaches on_call_ended, so anything opened in start() must
        be closed here or it leaks an FD per call (P6.5). Must be idempotent."""

    # ---- side tasks (the P6.5 registry) -------------------------------------

    def spawn(self, coro, *, name: str = "") -> asyncio.Task:
        """Start a side task OWNED by this processor. Reaped on stop/cancel —
        never orphaned. STT receive loops, TTS keepalives, pollers go here."""
        task = asyncio.create_task(coro, name=f"{self.name}:{name or 'side'}")
        self._tasks.append(task)
        return task

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Called by Chain.start() before frames flow. Override to open
        connections; register loops via spawn()."""

    async def run(self) -> None:
        while True:
            frame = await self.inbox.get()
            if isinstance(frame, CallEnded):
                try:
                    await self.on_call_ended(frame)
                finally:
                    await self.emit(frame)     # the sentinel walks the chain
                    self._stopped = True
                return
            try:
                await self.handle(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("processor %s failed on %r — frame dropped",
                                 self.name, type(frame).__name__)

    async def reap(self) -> None:
        """Cancel and await every side task. Idempotent."""
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    @property
    def all_tasks(self) -> list[asyncio.Task]:
        """Everything this processor owns — the FD/task budget's census."""
        tasks = list(self._tasks)
        if self._run_task is not None:
            tasks.append(self._run_task)
        return tasks


class Chain:
    """A linear chain of processors. Frames enter at the head via `feed`."""

    def __init__(self, processors: Sequence[Processor]):
        if not processors:
            raise ValueError("a chain needs at least one processor")
        self.processors = list(processors)
        for a, b in zip(self.processors, self.processors[1:]):
            a.link(b)
        self._started = False

    @property
    def head(self) -> Processor:
        return self.processors[0]

    @property
    def tail(self) -> Processor:
        return self.processors[-1]

    async def start(self) -> None:
        for p in self.processors:
            await p.start()
        for p in self.processors:
            p._run_task = asyncio.create_task(p.run(), name=f"chain:{p.name}")
        self._started = True

    async def feed(self, frame) -> None:
        await self.head.inbox.put(frame)

    async def stop(self, *, reason: str = "chain stop",
                   timeout_s: float = 10.0) -> None:
        """Graceful (D5): CallEnded drains through every link, then side
        tasks are reaped. Past `timeout_s`, escalate to cancel()."""
        if not self._started:
            return
        await self.feed(CallEnded(reason=reason))
        run_tasks = [p._run_task for p in self.processors if p._run_task]
        try:
            await asyncio.wait_for(
                asyncio.gather(*run_tasks, return_exceptions=True), timeout_s
            )
        except asyncio.TimeoutError:
            logger.error("chain drain exceeded %.0fs — cancelling", timeout_s)
            await self.cancel()
            return
        for p in self.processors:
            await p.reap()
            await self._cleanup(p)

    async def cancel(self) -> None:
        """Drop everything now (the D5 cancel half): run tasks AND side
        tasks, awaited so nothing is left orphaned (P6.5)."""
        for p in self.processors:
            if p._run_task is not None and not p._run_task.done():
                p._run_task.cancel()
        for p in self.processors:
            if p._run_task is not None:
                try:
                    await p._run_task
                except (asyncio.CancelledError, Exception):
                    pass
            await p.reap()
            await self._cleanup(p)

    @staticmethod
    async def _cleanup(p: Processor) -> None:
        try:
            await p.cleanup()
        except Exception:
            logger.exception("cleanup failed for processor %s", p.name)

    def orphans(self) -> list[asyncio.Task]:
        """Tasks still alive after stop/cancel — the acceptance assertion is
        that this is EMPTY."""
        return [t for p in self.processors for t in p.all_tasks
                if not t.done()]
