"""P5.2 — the tracing observer against a fake tracer.

The PRD acceptance test pinned here: a run's recorded LLM latency equals the
MEASURED call latency (explicit-timestamp test) — observer processing time
appears nowhere in a span duration.
"""
from __future__ import annotations

import asyncio

import pytest

from opkura.observers import ObservedLLM
from opkura.observers.dispatcher import run_dispatcher
from opkura.observers.events import (
    BotTurn,
    CallerTurn,
    Generation,
    RunFinished,
    RunStarted,
)
from opkura.observers.tracing_observer import TracingObserver


class FakeTracer:
    is_noop = False

    def __init__(self):
        self.calls = []
        self.flushed = 0

    def begin_trace(self, name, *, start_ns, **attrs):
        self.calls.append(("begin", name, start_ns, attrs))
        return "ROOT"

    def record_span(self, name, *, parent, start_ns, end_ns, error=None, **attrs):
        self.calls.append(("span", name, parent, start_ns, end_ns, error, attrs))

    def record_generation(self, name, *, parent, start_ns, end_ns, model,
                          error=None, **attrs):
        self.calls.append(("gen", name, parent, start_ns, end_ns, model, error, attrs))

    def end_trace(self, handle, *, end_ns, error=None, **attrs):
        self.calls.append(("end", handle, end_ns, error, attrs))

    def flush(self):
        self.flushed += 1


@pytest.fixture()
def traced():
    tracer = FakeTracer()
    return tracer, TracingObserver(tracer)


def test_run_becomes_one_trace_with_turn_and_generation_children(traced):
    tracer, obs = traced
    obs.on_event(RunStarted(run_id="r1", scenario_name="smoke", target="+1",
                            region="in", transport="sip", at_ns=1000))
    obs.on_event(BotTurn(turn_index=1, text="hello", module="greeting",
                         latency_ms=2.0, at_ns=5_000_000))
    obs.on_event(CallerTurn(turn_index=1, text="hi", at_ns=6_000_000))
    obs.on_event(Generation(role="caller", model="gemini-2.5-flash",
                            prompt_tokens=10, completion_tokens=5,
                            started_ns=7_000_000, ended_ns=9_000_000))
    obs.on_event(RunFinished(status="completed", passed=True, at_ns=10_000_000))

    kinds = [c[0] for c in tracer.calls]
    assert kinds == ["begin", "span", "span", "gen", "end"]

    begin = tracer.calls[0]
    assert begin[2] == 1000 and begin[3]["opkura.region"] == "in"

    bot = tracer.calls[1]
    # start reconstructed from the bot's OWN latency: at - 2ms
    assert (bot[3], bot[4]) == (3_000_000, 5_000_000)
    assert bot[2] == "ROOT" and bot[6]["opkura.module"] == "greeting"

    gen = tracer.calls[3]
    assert (gen[3], gen[4]) == (7_000_000, 9_000_000)  # exactly as measured
    assert gen[5] == "gemini-2.5-flash"
    assert gen[7]["llm.token_count.prompt"] == 10

    end = tracer.calls[4]
    assert end[2] == 10_000_000 and end[4]["opkura.passed"] is True
    assert tracer.flushed == 1


def test_failed_run_carries_the_error_on_the_root(traced):
    tracer, obs = traced
    obs.on_event(RunStarted(run_id="r", scenario_name="s", target="+1",
                            region="us", transport="text", at_ns=1))
    obs.on_event(RunFinished(status="error", passed=False, error="boom", at_ns=2))
    end = tracer.calls[-1]
    assert end[3] == "boom"


def test_variables_never_appear_in_span_attributes(traced):
    """Traces must not become a PII side channel: only utterance text and
    metadata are attached, never resolved vars."""
    tracer, obs = traced
    obs.on_event(RunStarted(run_id="r", scenario_name="s", target="+1",
                            region="us", transport="text", at_ns=1))
    obs.on_event(BotTurn(turn_index=1, text="hi", at_ns=2))
    for call in tracer.calls:
        attrs = call[-1]
        assert not any("vars" in str(k) for k in attrs)


# ----------------------------------------------------------------- sampling

def _run_events(passed: bool):
    return [
        RunStarted(run_id="r", scenario_name="s", target="+1", region="us",
                   transport="text", at_ns=1),
        BotTurn(turn_index=1, text="hi", at_ns=2),
        RunFinished(status="completed" if passed else "failed",
                    passed=passed, at_ns=3),
    ]


def test_sampled_out_passed_run_writes_nothing():
    tracer = FakeTracer()
    obs = TracingObserver(tracer, sample_rate=0.0)
    for e in _run_events(passed=True):
        obs.on_event(e)
    assert tracer.calls == [] and tracer.flushed == 0


def test_failed_run_is_always_kept_even_at_rate_zero():
    """The P5.2 promise: failures always kept. Tail sampling makes it true."""
    tracer = FakeTracer()
    obs = TracingObserver(tracer, sample_rate=0.0)
    for e in _run_events(passed=False):
        obs.on_event(e)
    assert [c[0] for c in tracer.calls] == ["begin", "span", "end"]


def test_rate_one_keeps_passed_runs():
    tracer = FakeTracer()
    obs = TracingObserver(tracer, sample_rate=1.0)
    for e in _run_events(passed=True):
        obs.on_event(e)
    assert [c[0] for c in tracer.calls] == ["begin", "span", "end"]


def test_terminal_flush_is_exempt_from_slow_warning(caplog):
    import logging as _logging
    import time as _time

    from opkura.observers.dispatcher import Dispatcher

    class SlowFlushTracer(FakeTracer):
        def flush(self):
            _time.sleep(0.06)
            super().flush()

    obs = TracingObserver(SlowFlushTracer(), sample_rate=1.0)
    d = Dispatcher([obs])
    with caplog.at_level(_logging.WARNING):
        for e in _run_events(passed=True):
            d.emit(e)
    assert not any("adding run latency" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------- ObservedLLM

class FakeLLM:
    provider = "fake"
    model = "fake-model"
    last_usage = None

    def __init__(self, fail=False):
        self._fail = fail

    async def complete_json(self, *a, **k):
        await asyncio.sleep(0)
        if self._fail:
            raise TimeoutError("llm timeout")
        self.last_usage = {"prompt_tokens": 7, "completion_tokens": 3}
        return {"ok": True}


class Recorder:
    def __init__(self):
        self.events = []

    def on_event(self, event):
        self.events.append(event)


def test_observed_llm_is_transparent_and_emits_generation():
    recorder = Recorder()

    async def main():
        llm = ObservedLLM(FakeLLM(), role="caller")
        assert llm.provider == "fake"          # attribute passthrough
        with run_dispatcher([recorder]):
            out = await llm.complete_json("prompt")
        assert out == {"ok": True}
        assert llm.last_usage == {"prompt_tokens": 7, "completion_tokens": 3}

    asyncio.run(main())
    [gen] = recorder.events
    assert isinstance(gen, Generation)
    assert (gen.role, gen.model, gen.prompt_tokens) == ("caller", "fake-model", 7)
    assert gen.ended_ns >= gen.started_ns > 0
    assert gen.error is None


def test_observed_llm_reports_failures_and_reraises():
    recorder = Recorder()

    async def main():
        llm = ObservedLLM(FakeLLM(fail=True), role="evaluator")
        with run_dispatcher([recorder]):
            with pytest.raises(TimeoutError):
                await llm.complete_json("prompt")

    asyncio.run(main())
    [gen] = recorder.events
    assert gen.error and "TimeoutError" in gen.error
