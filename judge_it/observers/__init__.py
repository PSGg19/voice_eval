"""Instrumentation beside the run (Phase 5): observers over an event stream.

`build_observers()` at the run boundary + `emit(event)` below it is the whole
integration surface. Off means OFF: with TRACING_PROVIDER=none the list is
empty, `emit` is one ContextVar read, and no OTel module is ever imported
(pinned by a subprocess test).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, List, Optional

from judge_it.observers.dispatcher import Dispatcher, Observer, emit, run_dispatcher  # noqa: F401
from judge_it.observers.events import (  # noqa: F401
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

    from judge_it.connectors.tracing import build as build_tracer

    tracer = build_tracer()
    if not getattr(tracer, "is_noop", False):
        from judge_it.observers.tracing_observer import TracingObserver

        observers.append(TracingObserver(tracer))

    if _flag("METRICS_ENABLED"):
        try:
            from judge_it.observers.metrics_observer import MetricsObserver

            observers.append(MetricsObserver())
        except Exception as exc:  # missing prometheus_client → degraded, not fatal
            import logging

            logging.getLogger(__name__).warning(
                "METRICS_ENABLED but the metrics observer failed to build: %s", exc
            )

    if _flag("ARTIFACT_OBSERVER"):
        try:
            from judge_it.artifacts import build_store
            from judge_it.observers.artifact_observer import ArtifactObserver

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
    through untouched (`provider`, `model`, …), so the engines cannot tell
    the difference; that transparency is pinned by test. Token counts come
    from the call's own LLMResult (P1.2), never from client state.
    """

    def __init__(self, client: Any, role: str):
        self._client = client
        self._role = role

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def complete_json(self, *args: Any, **kwargs: Any) -> Any:
        started = time.time_ns()
        error: Optional[str] = None
        result: Any = None
        try:
            result = await self._client.complete_json(*args, **kwargs)
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            usage = getattr(result, "usage", None) or {}
            prompt_text = completion_text = ""
            if os.getenv("TRACE_CAPTURE_LLM_IO", "").strip().lower() in (
                "1", "true", "on", "yes",
            ):
                prompt_text = _render_llm_io(args, kwargs)
                # LLMResult IS the parsed JSON mapping (a dict subclass) —
                # there is no .data attribute.
                if isinstance(result, dict):
                    completion_text = _clip(json.dumps(dict(result), default=str))
                elif result is not None:
                    completion_text = _clip(str(result))
            emit(
                Generation(
                    role=self._role,
                    model=getattr(self._client, "model", "") or "",
                    provider=getattr(self._client, "provider", "") or "",
                    prompt_tokens=usage.get("input_tokens"),
                    completion_tokens=usage.get("output_tokens"),
                    error=error,
                    started_ns=started,
                    ended_ns=time.time_ns(),
                    prompt_text=prompt_text,
                    completion_text=completion_text,
                )
            )


_LLM_IO_MAX = 8_000  # chars per side; spans must stay span-sized


def _clip(text: str) -> str:
    if len(text) <= _LLM_IO_MAX:
        return text
    return text[:_LLM_IO_MAX] + f"… [+{len(text) - _LLM_IO_MAX} chars]"


def _render_llm_io(args: tuple, kwargs: dict) -> str:
    """complete_json(system, messages, …) → a readable transcript-ish blob."""
    system = kwargs.get("system", args[0] if args else "")
    messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
    parts = []
    if system:
        parts.append(f"[system]\n{system}")
    for m in messages if isinstance(messages, list) else []:
        if isinstance(m, dict):
            parts.append(f"[{m.get('role', '?')}]\n{m.get('content', '')}")
    return _clip("\n\n".join(parts))
