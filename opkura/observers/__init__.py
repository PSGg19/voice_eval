"""Instrumentation beside the run (Phase 5): observers over an event stream.

`build_observers()` at the run boundary + `emit(event)` below it is the whole
integration surface. Off means OFF: with TRACING_PROVIDER=none the list is
empty, `emit` is one ContextVar read, and no OTel module is ever imported
(pinned by a subprocess test).
"""
from __future__ import annotations

import time
from typing import Any, List, Optional

from opkura.observers.dispatcher import Dispatcher, Observer, emit, run_dispatcher  # noqa: F401
from opkura.observers.events import (  # noqa: F401
    BotTurn,
    CallerTurn,
    Generation,
    RunFinished,
    RunStarted,
    now_ns,
)


def _flag(name: str) -> bool:
    import os

    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def build_observers() -> List[Observer]:
    """The observers the environment asks for. Empty list when everything is
    off — and an empty list means run_dispatcher installs nothing at all."""
    observers: List[Observer] = []

    from opkura.connectors.tracing import build as build_tracer

    tracer = build_tracer()
    if not getattr(tracer, "is_noop", False):
        from opkura.observers.tracing_observer import TracingObserver

        observers.append(TracingObserver(tracer))

    if _flag("METRICS_ENABLED"):
        try:
            from opkura.observers.metrics_observer import MetricsObserver

            observers.append(MetricsObserver())
        except Exception as exc:  # missing prometheus_client → degraded, not fatal
            import logging

            logging.getLogger(__name__).warning(
                "METRICS_ENABLED but the metrics observer failed to build: %s", exc
            )

    if _flag("ARTIFACT_OBSERVER"):
        try:
            from opkura.artifacts import build_store
            from opkura.observers.artifact_observer import ArtifactObserver

            observers.append(ArtifactObserver(build_store()))
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "ARTIFACT_OBSERVER set but the artifact observer failed to "
                "build: %s", exc,
            )

    return observers


class ObservedLLM:
    """Wraps an LLM client so every `complete_json` emits a Generation event.

    Measurement happens HERE, around the actual await — the observer receives
    finished timestamps and can never stretch them. Everything else proxies
    through untouched (`last_usage`, `provider`, `model`, …), so the engines
    cannot tell the difference; that transparency is pinned by test.
    """

    def __init__(self, client: Any, role: str):
        self._client = client
        self._role = role

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def complete_json(self, *args: Any, **kwargs: Any) -> Any:
        started = time.time_ns()
        error: Optional[str] = None
        try:
            return await self._client.complete_json(*args, **kwargs)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            usage = getattr(self._client, "last_usage", None) or {}
            emit(
                Generation(
                    role=self._role,
                    model=getattr(self._client, "model", "") or "",
                    provider=getattr(self._client, "provider", "") or "",
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    error=error,
                    started_ns=started,
                    ended_ns=time.time_ns(),
                )
            )
