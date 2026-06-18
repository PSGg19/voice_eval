"""Tracing observer (P5.2): run = trace, turn = span, LLM call = generation.

**Buffer, then decide.** Events are held until RunFinished, and only then
written through the tracer. Because every span is recorded after-the-fact
with the EVENT's own timestamps, buffering costs nothing in fidelity — and it
buys true tail sampling: `TRACE_SAMPLE_RATE` (default 1.0) drops a fraction
of PASSED runs, while a failed, errored or vetoed run is ALWAYS kept. Head
sampling cannot make that promise; this design gets it for free.

The trade, stated plainly: spans appear in Phoenix/Langfuse when the run
ends, not live during it. For runs measured in minutes that is the right
price for never sampling away the trace you actually needed.

Span tree (all children of the run root — finer nesting when P2's frames
carry turn identity through the engine):

    run: <scenario>              openinference.span.kind=AGENT
    ├── turn 1 · bot             bot text, module, reasoning latency
    ├── turn 1 · caller          caller text (+ dtmf)
    ├── llm · caller             model, token counts
    └── llm · evaluator          model, token counts

Message TEXT appears on turn spans (that is the observable value of a trace);
resolved variables never do — traces must not become a PII side channel.
"""
from __future__ import annotations

import logging
import os
import random
from typing import Any, List, Optional

from opkura.observers.events import (
    BotTurn,
    CallerTurn,
    Generation,
    RunFinished,
    RunStarted,
)

logger = logging.getLogger(__name__)

_MS = 1_000_000  # ns per ms


def _sample_rate() -> float:
    try:
        return min(1.0, max(0.0, float(os.getenv("TRACE_SAMPLE_RATE", "1.0"))))
    except ValueError:
        return 1.0


class TracingObserver:
    # The terminal event carries the replay + exporter flush — network time
    # that happens after the run's outcome exists, not run latency. Exempted
    # from the dispatcher's slow-observer warning by declaration.
    slow_ok_events = (RunFinished,)

    def __init__(self, tracer: Any, *, sample_rate: Optional[float] = None):
        self._tracer = tracer
        self._sample_rate = _sample_rate() if sample_rate is None else sample_rate
        self._buffer: List[Any] = []

    # ------------------------------------------------------------- buffering

    def on_event(self, event: Any) -> None:
        if isinstance(event, RunStarted):
            self._buffer = [event]
            return
        self._buffer.append(event)
        if isinstance(event, RunFinished):
            events, self._buffer = self._buffer, []
            if self._keep(event):
                self._replay(events)

    def _keep(self, finished: RunFinished) -> bool:
        # Failures always kept — the whole point of deciding at the tail.
        if finished.passed is not True or finished.status != "completed":
            return True
        return random.random() < self._sample_rate

    # --------------------------------------------------------------- writing

    def _replay(self, events: List[Any]) -> None:
        root: Optional[Any] = None
        for event in events:
            if isinstance(event, RunStarted):
                root = self._tracer.begin_trace(
                    f"run: {event.scenario_name}",
                    start_ns=event.at_ns,
                    **{
                        "openinference.span.kind": "AGENT",
                        "opkura.run_id": event.run_id,
                        "opkura.target": event.target,
                        "opkura.region": event.region,
                        "opkura.transport": event.transport,
                    },
                )
            elif isinstance(event, BotTurn):
                start = (
                    event.at_ns - int(event.latency_ms * _MS)
                    if event.latency_ms
                    else event.at_ns
                )
                self._tracer.record_span(
                    f"turn {event.turn_index} · bot",
                    parent=root,
                    start_ns=start,
                    end_ns=event.at_ns,
                    **{
                        "output.value": event.text,
                        "opkura.module": event.module or None,
                        "opkura.tag_only": event.tag_only or None,
                        "opkura.bot_latency_ms": event.latency_ms,
                    },
                )
            elif isinstance(event, CallerTurn):
                self._tracer.record_span(
                    f"turn {event.turn_index} · caller",
                    parent=root,
                    start_ns=event.at_ns,
                    end_ns=event.at_ns + _MS,  # utterances are instants; 1ms renders
                    **{
                        "output.value": event.text,
                        "opkura.dtmf": event.dtmf or None,
                    },
                )
            elif isinstance(event, Generation):
                self._tracer.record_generation(
                    f"llm · {event.role}",
                    parent=root,
                    start_ns=event.started_ns,
                    end_ns=event.ended_ns,
                    model=event.model,
                    error=event.error,
                    **{
                        "llm.provider": event.provider or None,
                        "llm.token_count.prompt": event.prompt_tokens,
                        "llm.token_count.completion": event.completion_tokens,
                        "opkura.role": event.role,
                    },
                )
            elif isinstance(event, RunFinished):
                if root is not None:
                    self._tracer.end_trace(
                        root,
                        end_ns=event.at_ns,
                        error=event.error or None,
                        **{
                            "opkura.status": event.status,
                            "opkura.passed": event.passed,
                            "opkura.interaction_uuid": event.interaction_uuid or None,
                        },
                    )
        self._tracer.flush()
