"""Observer dispatch (P5.1): sync fan-out, individually exception-isolated.

The contract every test pins:

* an observer that RAISES on every event changes nothing about the run —
  the exception is logged once per observer per run and swallowed;
* an observer slower than 50ms per event gets named in a warning, because a
  slow observer is a run-latency bug hiding in instrumentation;
* `emit()` with no dispatcher installed is a no-op costing one ContextVar
  read — code below the run boundary never checks "is tracing on".

The active dispatcher travels in a ContextVar, so concurrent runs in one
process each see their own (same mechanism as log routing's call_id_var).
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional, Protocol, Sequence

logger = logging.getLogger(__name__)

_SLOW_MS = 50.0


class Observer(Protocol):
    def on_event(self, event: Any) -> None: ...


class Dispatcher:
    def __init__(self, observers: Sequence[Observer]):
        self._observers = list(observers)
        self._broken: set[int] = set()  # ids of observers already reported

    def emit(self, event: Any) -> None:
        for obs in self._observers:
            started = time.monotonic()
            try:
                obs.on_event(event)
            except Exception as exc:
                if id(obs) not in self._broken:
                    self._broken.add(id(obs))
                    logger.warning(
                        "observer %s raised on %s and is now best-effort: %s",
                        type(obs).__name__, type(event).__name__, exc,
                    )
                continue
            elapsed_ms = (time.monotonic() - started) * 1000
            if elapsed_ms > _SLOW_MS and not isinstance(
                event, getattr(obs, "slow_ok_events", ())
            ):
                # slow_ok_events lets an observer declare its terminal-event
                # work (an exporter flush after the run's outcome exists) as
                # not-run-latency. Everything mid-run stays under the 50ms eye.
                logger.warning(
                    "observer %s took %.0fms on %s — instrumentation is "
                    "adding run latency",
                    type(obs).__name__, elapsed_ms, type(event).__name__,
                )


_dispatcher_var: ContextVar[Optional[Dispatcher]] = ContextVar(
    "opkura_dispatcher", default=None
)


def emit(event: Any) -> None:
    """Fire an event at the current run's observers. No dispatcher → no-op."""
    dispatcher = _dispatcher_var.get()
    if dispatcher is not None:
        dispatcher.emit(event)


@contextmanager
def run_dispatcher(observers: Sequence[Observer]) -> Iterator[Optional[Dispatcher]]:
    """Install observers for the duration of one run (this task subtree)."""
    if not observers:
        yield None
        return
    dispatcher = Dispatcher(observers)
    token = _dispatcher_var.set(dispatcher)
    try:
        yield dispatcher
    finally:
        _dispatcher_var.reset(token)
