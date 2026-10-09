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

from judge_it.observers.events import (
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
        """Write the buffered run as an OSV-style turn tree:

            run: <scenario>
            ├── turn 1                (group: input=bot line, output=reply)
            │   ├── bot
            │   ├── llm · caller
            │   └── caller
            ├── turn 2 …
            └── llm · evaluator       (post-conversation, stays at root)

        A turn group opens at a BotTurn and closes when its CallerTurn (or
        the next BotTurn / RunFinished) arrives; caller-role generations in
        between belong to the turn. Phoenix and Langfuse both render the
        grouping as a collapsible tree — the OSV conversations dropdown."""
        root: Optional[Any] = None
        group: Optional[Any] = None          # the open `turn N` span
        group_end_ns: int = 0                # last child end inside the group
        group_out: str = ""                  # the caller reply (group output)
        last_bot_text = ""
        last_caller_text = ""

        def close_group() -> None:
            nonlocal group, group_out
            if group is not None:
                self._tracer.end_span(
                    group, end_ns=group_end_ns,
                    **{"output.value": group_out or None},
                )
            group, group_out = None, ""

        for event in events:
            if isinstance(event, RunStarted):
                # The short run id in the NAME is deliberate: Langfuse search
                # matches ids/names only, never attributes — without it a
                # console's "find this run's traces" search comes up empty.
                root = self._tracer.begin_trace(
                    f"run: {event.scenario_name} · {event.run_id[:8]}",
                    start_ns=event.at_ns,
                    **{
                        "openinference.span.kind": "AGENT",
                        # input/output on the ROOT is what Phoenix's span list
                        # and Langfuse's trace preview render — without them
                        # both show "--"/null and the trace looks empty.
                        "input.value": (
                            f"{event.scenario_name} → {event.target} "
                            f"({event.region}/{event.transport})"
                        ),
                        "judge_it.run_id": event.run_id,
                        "judge_it.target": event.target,
                        "judge_it.region": event.region,
                        "judge_it.transport": event.transport,
                    },
                )
            elif isinstance(event, BotTurn):
                close_group()
                start = (
                    event.at_ns - int(event.latency_ms * _MS)
                    if event.latency_ms
                    else event.at_ns
                )
                group = self._tracer.begin_span(
                    f"turn {event.turn_index}",
                    parent=root, start_ns=start,
                    **{
                        "openinference.span.kind": "CHAIN",
                        "input.value": event.text,
                    },
                )
                group_end_ns = event.at_ns
                self._tracer.record_span(
                    "bot",
                    parent=group,
                    start_ns=start,
                    end_ns=event.at_ns,
                    **{
                        "input.value": last_caller_text or None,
                        "output.value": event.text,
                        "judge_it.module": event.module or None,
                        "judge_it.tag_only": event.tag_only or None,
                        "judge_it.bot_latency_ms": event.latency_ms,
                    },
                )
                last_bot_text = event.text
            elif isinstance(event, CallerTurn):
                self._tracer.record_span(
                    "caller",
                    parent=group if group is not None else root,
                    start_ns=event.at_ns,
                    end_ns=event.at_ns + _MS,  # utterances are instants; 1ms renders
                    **{
                        "input.value": last_bot_text or None,
                        "output.value": event.text,
                        "judge_it.dtmf": event.dtmf or None,
                    },
                )
                last_caller_text = event.text
                group_end_ns = max(group_end_ns, event.at_ns + _MS)
                group_out = event.text
                close_group()
            elif isinstance(event, Generation):
                # Caller generations belong to the open turn; the evaluator
                # (and anything outside a turn) stays at root.
                parent = group if (group is not None and event.role == "caller") else root
                self._tracer.record_generation(
                    f"llm · {event.role}",
                    parent=parent,
                    start_ns=event.started_ns,
                    end_ns=event.ended_ns,
                    model=event.model,
                    error=event.error,
                    **{
                        "llm.provider": event.provider or None,
                        "llm.token_count.prompt": event.prompt_tokens,
                        "llm.token_count.completion": event.completion_tokens,
                        # Populated only under TRACE_CAPTURE_LLM_IO (see
                        # events.Generation) — otherwise None and omitted.
                        "input.value": event.prompt_text or None,
                        "output.value": event.completion_text or None,
                        "judge_it.role": event.role,
                    },
                )
                if parent is group:
                    group_end_ns = max(group_end_ns, event.ended_ns)
            elif isinstance(event, RunFinished):
                close_group()
                if root is not None:
                    self._tracer.end_trace(
                        root,
                        end_ns=event.at_ns,
                        error=event.error or None,
                        **{
                            "output.value": (
                                f"{event.status} · passed={event.passed}"
                                + (f" · {event.error}" if event.error else "")
                            ),
                            "judge_it.status": event.status,
                            "judge_it.passed": event.passed,
                            "judge_it.interaction_uuid": event.interaction_uuid or None,
                        },
                    )
        close_group()
        self._tracer.flush()
