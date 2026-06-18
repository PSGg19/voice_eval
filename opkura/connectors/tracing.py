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
from typing import Any, Iterator, Optional, Protocol

from opkura.connectors import select

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
        self._tracer = trace.get_tracer("opkura", tracer_provider=provider)

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
        span = self._tracer.start_span(name, start_time=start_ns)
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

    provider = TracerProvider(
        resource=Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "opkura")})
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
