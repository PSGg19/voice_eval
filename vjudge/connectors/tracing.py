"""Tracing: Phoenix, Langfuse, both, or nothing.

**One instrumentation layer, not two SDKs.** This is settled by working code
rather than preference: orchestrator-service already exports to Phoenix,
Langfuse *and* Tempo from a single OpenTelemetry `TracerProvider`, attaching one
`BatchSpanProcessor` per enabled backend — with no `langfuse` package installed
at all, because Langfuse ingests plain OTLP at
`{LANGFUSE_HOST}/api/public/otel/v1/traces` over HTTP Basic.

So adding a third backend later is another span processor, not another adapter.

Env names mirror orchestrator-service deliberately, including `OTEL_ENABLE`
missing its trailing D — that is a real quirk over there and matching it means
one runbook covers both services.
"""
from __future__ import annotations

import base64
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional, Protocol

from vjudge.connectors import select

# Set by OTelTracer.begin_trace for the duration of ONE start_span call: the
# id generator reads it so a run's trace id equals its run uuid, and the
# ROOT span id equals the uuid's top 64 bits — both derivable later from the
# run row alone, which is what lets the evaluate stage (a different process,
# after the trace closed) append its judge spans under the same root.
_PINNED_TRACE_ID: ContextVar[Optional[int]] = ContextVar(
    "vjudge_pinned_trace_id", default=None
)
_PINNED_SPAN_ID: ContextVar[Optional[int]] = ContextVar(
    "vjudge_pinned_span_id", default=None
)


def _root_span_id(run_uuid_int: int) -> int:
    sid = run_uuid_int >> 64
    return sid or 1  # span id 0 is invalid in OTel

logger = logging.getLogger(__name__)

ENV_VAR = "TRACING_PROVIDER"
DEFAULT = "none"


class Tracer(Protocol):
    """What the run path is allowed to assume exists.

    Two families. The context managers instrument live code. The `record_*`
    family (P5.2) writes spans AFTER THE FACT with explicit timestamps —
    observers learn about a turn or an LLM call once it has happened, and the
    recorded duration must be the event's own measurement, never inflated by
    however long the observer took to hear about it.
    """

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Any]: ...

    @contextmanager
    def generation(self, name: str, *, model: str, **attributes: Any) -> Iterator[Any]: ...

    def begin_trace(self, name: str, *, start_ns: int, **attributes: Any) -> Any: ...

    def record_span(
        self, name: str, *, parent: Any, start_ns: int, end_ns: int,
        error: Optional[str] = None, **attributes: Any,
    ) -> None: ...

    def record_generation(
        self, name: str, *, parent: Any, start_ns: int, end_ns: int,
        model: str, error: Optional[str] = None, **attributes: Any,
    ) -> None: ...

    def begin_span(
        self, name: str, *, parent: Any, start_ns: int, **attributes: Any,
    ) -> Any: ...

    def end_span(
        self, handle: Any, *, end_ns: int, **attributes: Any,
    ) -> None: ...

    def end_trace(
        self, handle: Any, *, end_ns: int, error: Optional[str] = None,
        **attributes: Any,
    ) -> None: ...

    def flush(self) -> None: ...


class _NoSpan:
    def set_attribute(self, *_a: Any, **_k: Any) -> None: ...
    def set_attributes(self, *_a: Any, **_k: Any) -> None: ...
    def record_exception(self, *_a: Any, **_k: Any) -> None: ...


class NoOpTracer:
    """The default. Costs a generator frame and imports nothing."""

    is_noop = True

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Any]:
        yield _NoSpan()

    @contextmanager
    def generation(self, name: str, *, model: str, **attributes: Any) -> Iterator[Any]:
        yield _NoSpan()

    def begin_trace(self, name: str, *, start_ns: int, **attributes: Any) -> Any:
        return _NoSpan()

    def record_span(self, name: str, *, parent: Any, start_ns: int, end_ns: int,
                    error: Any = None, **attributes: Any) -> None: ...

    def record_generation(self, name: str, *, parent: Any, start_ns: int, end_ns: int,
                          model: str, error: Any = None, **attributes: Any) -> None: ...

    def begin_span(self, name: str, *, parent: Any, start_ns: int,
                   **attributes: Any) -> Any:
        return _NoSpan()

    def end_span(self, handle: Any, *, end_ns: int, **attributes: Any) -> None: ...

    def end_trace(self, handle: Any, *, end_ns: int, error: Any = None,
                  **attributes: Any) -> None: ...

    def flush(self) -> None: ...


class _OtelTracer:
    """Wraps a real TracerProvider. Only constructed when a backend is on."""

    is_noop = False

    def __init__(self, provider: Any) -> None:
        from opentelemetry import trace

        self._provider = provider
        self._trace_api = trace
        self._tracer = trace.get_tracer("vjudge", tracer_provider=provider)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Any]:
        with self._tracer.start_as_current_span(name) as s:
            for k, v in attributes.items():
                if v is not None:
                    s.set_attribute(k, v)
            yield s

    @contextmanager
    def generation(self, name: str, *, model: str, **attributes: Any) -> Iterator[Any]:
        # OpenInference semantic conventions, which is what makes the same span
        # render as an LLM generation in Phoenix AND in Langfuse.
        with self.span(
            name,
            **{"openinference.span.kind": "LLM", "llm.model_name": model, **attributes},
        ) as s:
            yield s

    # ---- after-the-fact recording (P5.2) --------------------------------
    # Spans are created with explicit start_time and closed with explicit
    # end_time, both from the EVENT's own clock — the observer's processing
    # time never appears in a duration.

    @staticmethod
    def _set(span: Any, attributes: dict, error: Optional[str]) -> None:
        for k, v in attributes.items():
            if v is not None:
                span.set_attribute(k, v)
        if error:
            from opentelemetry.trace import Status, StatusCode

            span.set_status(Status(StatusCode.ERROR, error))
            span.set_attribute("error.message", error)

    def begin_trace(self, name: str, *, start_ns: int, **attributes: Any) -> Any:
        # Pin the OTel trace id to the run id when one is present: the run's
        # uuid is exactly 16 bytes, so trace_id == run_uuid (hex, no dashes)
        # and Phoenix/Langfuse trace URLs become derivable from the run row
        # alone — the console deep-links straight to the trace.
        run_id = attributes.get("vjudge.run_id")
        if run_id:
            try:
                import uuid as _uuid

                rid = _uuid.UUID(str(run_id)).int
                _PINNED_TRACE_ID.set(rid)
                _PINNED_SPAN_ID.set(_root_span_id(rid))
            except (ValueError, AttributeError, TypeError):
                pass  # not uuid-shaped (tests, markers) — random id as before
        try:
            span = self._tracer.start_span(name, start_time=start_ns)
        finally:
            _PINNED_TRACE_ID.set(None)
            _PINNED_SPAN_ID.set(None)
        self._set(span, attributes, None)
        return span

    def record_span(
        self, name: str, *, parent: Any, start_ns: int, end_ns: int,
        error: Optional[str] = None, **attributes: Any,
    ) -> None:
        ctx = self._trace_api.set_span_in_context(parent) if parent is not None else None
        span = self._tracer.start_span(name, context=ctx, start_time=start_ns)
        self._set(span, attributes, error)
        span.end(end_time=end_ns)

    def begin_span(
        self, name: str, *, parent: Any, start_ns: int, **attributes: Any,
    ) -> Any:
        """An open child span — for GROUPING (a turn wrapping its bot line,
        llm call and caller reply); ended explicitly via end_span."""
        ctx = self._trace_api.set_span_in_context(parent) if parent is not None else None
        span = self._tracer.start_span(name, context=ctx, start_time=start_ns)
        self._set(span, attributes, None)
        return span

    def end_span(self, handle: Any, *, end_ns: int, **attributes: Any) -> None:
        self._set(handle, attributes, None)
        handle.end(end_time=end_ns)

    def record_generation(
        self, name: str, *, parent: Any, start_ns: int, end_ns: int,
        model: str, error: Optional[str] = None, **attributes: Any,
    ) -> None:
        # OpenInference kind=LLM is what renders as a generation in Phoenix
        # and maps onto Langfuse's observation model over plain OTLP.
        self.record_span(
            name, parent=parent, start_ns=start_ns, end_ns=end_ns, error=error,
            **{"openinference.span.kind": "LLM", "llm.model_name": model, **attributes},
        )

    def end_trace(
        self, handle: Any, *, end_ns: int, error: Optional[str] = None,
        **attributes: Any,
    ) -> None:
        self._set(handle, attributes, error)
        handle.end(end_time=end_ns)

    def flush(self) -> None:
        try:
            self._provider.force_flush()
        except Exception as exc:  # a flush failure must not fail a run
            logger.debug("span flush failed: %s", exc)


def _basic_auth(public_key: str, secret_key: str) -> dict[str, str]:
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _build(*, phoenix: bool, langfuse: bool) -> Tracer:
    """One provider, one processor per enabled backend."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

    class _RunPinnedIdGenerator(RandomIdGenerator):
        """Random ids, except a root span started under a pinned run id gets
        trace_id == that run's uuid (and its own span id from the uuid's top
        bits) — see begin_trace."""

        def generate_trace_id(self) -> int:
            pinned = _PINNED_TRACE_ID.get()
            if pinned:
                return pinned
            return super().generate_trace_id()

        def generate_span_id(self) -> int:
            pinned = _PINNED_SPAN_ID.get()
            if pinned:
                # One-shot: only the root span (started inside begin_trace)
                # takes the pinned id; its children must stay random.
                _PINNED_SPAN_ID.set(None)
                return pinned
            return super().generate_span_id()

    provider = TracerProvider(
        resource=Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "vjudge")}),
        id_generator=_RunPinnedIdGenerator(),
    )
    attached = []

    if phoenix:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces"),
            headers=({"api_key": os.environ["PHOENIX_API_KEY"]}
                     if os.getenv("PHOENIX_API_KEY") else None),
            timeout=int(os.getenv("PHOENIX_EXPORT_TIMEOUT", "10")),
        ), max_queue_size=8192))
        attached.append("phoenix")

    if langfuse:
        host = os.getenv("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=f"{host}/api/public/otel/v1/traces",
            headers={
                **_basic_auth(os.environ["LANGFUSE_PUBLIC_KEY"],
                             os.environ["LANGFUSE_SECRET_KEY"]),
                "x-langfuse-ingestion-version": "4",
            },
            timeout=int(os.getenv("LANGFUSE_EXPORT_TIMEOUT", "10")),
        ), max_queue_size=8192))
        attached.append("langfuse")

    if not attached:
        return NoOpTracer()
    logger.info("tracing exporters attached: %s", ", ".join(attached))
    return _OtelTracer(provider)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def build(override: Optional[str] = None) -> Tracer:
    """Resolve the tracer. `override` is the option gate; env is the default gate.

    `PHOENIX_ENABLED` / `LANGFUSE_ENABLED` also work on their own, so a
    deployment already carrying orchestrator-service's variables needs no new
    ones.
    """
    if override is None and (_env_flag("PHOENIX_ENABLED") or _env_flag("LANGFUSE_ENABLED")):
        # Translate OSV's flags into a provider name and fall through, rather
        # than building directly — otherwise this path would skip `select` and
        # lose its graceful-degradation guarantee, so a missing opentelemetry
        # package would crash the run instead of disabling tracing.
        phoenix, langfuse = _env_flag("PHOENIX_ENABLED"), _env_flag("LANGFUSE_ENABLED")
        override = "both" if (phoenix and langfuse) else ("phoenix" if phoenix else "langfuse")

    return select(
        kind="tracing",
        env_var=ENV_VAR,
        default=DEFAULT,
        override=override,
        factories={
            "none": NoOpTracer,
            "phoenix": lambda: _build(phoenix=True, langfuse=False),
            "langfuse": lambda: _build(phoenix=False, langfuse=True),
            "both": lambda: _build(phoenix=True, langfuse=True),
        },
    )


def record_detached_evaluation(
    run_id: str,
    *,
    started_ns: int,
    ended_ns: int,
    passed: Optional[bool],
    reasoning: str,
    transcript_turns: int,
    generations: list,
    tracer: Optional[Tracer] = None,
) -> None:
    """Append the judge's spans to a run's ALREADY-CLOSED trace.

    In queue mode the evaluator runs in the evaluate stage — a different
    process, after RunFinished ended the trace — so it can never ride the
    observer replay. But the trace id is the run uuid and the root span id is
    the uuid's top bits (both pinned at begin_trace), so a remote parent
    context reconstructs the root exactly and the judge lands under it:

        run: <scenario>
        └── evaluation            (input: what was judged, output: verdict)
            └── llm · evaluator   (prompt → completion when captured)

    Best-effort: a NoOp tracer, a malformed id, or any exporter trouble is a
    silent skip — judging must never fail because tracing hiccuped.
    """
    t = tracer if tracer is not None else build()
    if getattr(t, "is_noop", False) or not hasattr(t, "_tracer"):
        return
    try:
        import uuid as _uuid

        from opentelemetry import trace as trace_api
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            TraceFlags,
        )

        rid = _uuid.UUID(str(run_id)).int
        root_ctx = SpanContext(
            trace_id=rid,
            span_id=_root_span_id(rid),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        root = NonRecordingSpan(root_ctx)

        group = t.begin_span(
            "evaluation",
            parent=root,
            start_ns=started_ns,
            **{
                "openinference.span.kind": "CHAIN",
                "input.value": f"judge the {transcript_turns}-turn transcript",
            },
        )
        for g in generations or []:
            t.record_generation(
                "llm · evaluator",
                parent=group,
                start_ns=g.started_ns or started_ns,
                end_ns=g.ended_ns or ended_ns,
                model=g.model,
                error=g.error,
                **{
                    "llm.provider": g.provider or None,
                    "llm.token_count.prompt": g.prompt_tokens,
                    "llm.token_count.completion": g.completion_tokens,
                    "input.value": g.prompt_text or None,
                    "output.value": g.completion_text or None,
                    "vjudge.role": g.role,
                },
            )
        t.end_span(
            group,
            end_ns=ended_ns,
            **{
                "output.value": f"passed={passed} · {reasoning}"[:2000],
                "vjudge.passed": passed,
            },
        )
        t.flush()
    except Exception:  # pragma: no cover - never fail the judge for a trace
        logger.debug("detached evaluation trace failed", exc_info=True)
