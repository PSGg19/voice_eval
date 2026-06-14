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
    """What the run path is allowed to assume exists."""

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Any]: ...

    @contextmanager
    def generation(self, name: str, *, model: str, **attributes: Any) -> Iterator[Any]: ...

    def flush(self) -> None: ...


class _NoSpan:
    def set_attribute(self, *_a: Any, **_k: Any) -> None: ...
    def set_attributes(self, *_a: Any, **_k: Any) -> None: ...
    def record_exception(self, *_a: Any, **_k: Any) -> None: ...


class NoOpTracer:
    """The default. Costs a generator frame and imports nothing."""

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Any]:
        yield _NoSpan()

    @contextmanager
    def generation(self, name: str, *, model: str, **attributes: Any) -> Iterator[Any]:
        yield _NoSpan()

    def flush(self) -> None: ...


class _OtelTracer:
    """Wraps a real TracerProvider. Only constructed when a backend is on."""

    def __init__(self, provider: Any) -> None:
        from opentelemetry import trace

        self._provider = provider
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
