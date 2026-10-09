"""P5.1 — dispatcher contract: isolation, slow detection, context scoping."""
from __future__ import annotations

import asyncio
import logging
import time

from judge_it.observers.dispatcher import Dispatcher, emit, run_dispatcher
from judge_it.observers.events import RunStarted


def make_event(name: str = "s") -> RunStarted:
    return RunStarted(run_id="r", scenario_name=name, target="+1", region="us",
                      transport="text")


class Recorder:
    def __init__(self):
        self.events = []

    def on_event(self, event):
        self.events.append(event)


class Chaos:
    """The PRD's chaos observer: raises on every event."""

    def on_event(self, event):
        raise RuntimeError("chaos")


def test_chaos_observer_cannot_affect_the_others(caplog):
    recorder = Recorder()
    d = Dispatcher([Chaos(), recorder, Chaos()])
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            d.emit(make_event())
    assert len(recorder.events) == 5  # every event still delivered
    # one warning per broken observer, not per event
    warnings = [r for r in caplog.records if "best-effort" in r.getMessage()]
    assert len(warnings) == 2


def test_slow_observer_is_named(caplog):
    class Slow:
        def on_event(self, event):
            time.sleep(0.06)

    d = Dispatcher([Slow()])
    with caplog.at_level(logging.WARNING):
        d.emit(make_event())
    assert any("Slow" in r.getMessage() and "ms" in r.getMessage()
               for r in caplog.records)


def test_emit_without_dispatcher_is_a_noop():
    emit(make_event())  # must not raise, must not require anything installed


def test_empty_observer_list_installs_nothing():
    with run_dispatcher([]) as d:
        assert d is None
        emit(make_event())  # still a no-op


def test_concurrent_runs_each_see_their_own_dispatcher():
    """Two async tasks, two dispatchers, zero cross-talk (the ContextVar
    property that makes concurrent campaign runs traceable independently)."""

    async def one_run(name: str, recorder: Recorder):
        with run_dispatcher([recorder]):
            await asyncio.sleep(0.01)
            emit(make_event(name))
            await asyncio.sleep(0.01)
            emit(make_event(name))

    async def main():
        a, b = Recorder(), Recorder()
        await asyncio.gather(one_run("A", a), one_run("B", b))
        return a, b

    a, b = asyncio.run(main())
    assert [e.scenario_name for e in a.events] == ["A", "A"]
    assert [e.scenario_name for e in b.events] == ["B", "B"]
