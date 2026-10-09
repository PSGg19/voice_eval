"""Observability backends, selected by environment, with a working default.

    TRACING_PROVIDER = none | phoenix | langfuse | both
    LOG_SINK         = text | json | loki

**This is only for observability, and that is a deliberate boundary.** Postgres
and Redis are core: Postgres is the database, Redis is the cache and queue, and
both are present in local development exactly as in a deployment — see
`docker-compose.yml`, where they start by default and the tracing and logging
stacks sit behind opt-in profiles.

So there is no `RUN_STORE=sqlite|postgres` and no `RUN_QUEUE=memory|redis`.
Making core infrastructure selectable sounds flexible and is not: it commits you
to maintaining a second storage implementation forever, and the two drift until
the fallback is quietly broken and nobody notices because CI runs on the other
one. Whether traces go to Phoenix is a genuine choice; whether there is a
database is not.

Three properties hold for the backends that ARE selectable.

**Off costs nothing.** The value maps to a *factory*, not an instance, so a
backend's heavy dependency is imported inside its factory. With
`TRACING_PROVIDER=none` the opentelemetry packages are never imported and need
not be installed.

**A bad value degrades, it does not crash.** An unknown name, or a factory that
raises because a package is missing or a host is unreachable, logs a warning and
falls back to the default. A test harness that refuses to start because its
tracing sidecar is down would be a worse tool than one that runs untraced.

**The environment is read with plain `os.getenv`.** Not through
`core.config.Settings`, which sets `env_prefix="JUDGE_IT_"` and would silently
rename `LANGFUSE_ENABLED` to `JUDGE_IT_LANGFUSE_ENABLED`. These names mirror
orchestrator-service exactly so one runbook covers both services.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

Factory = Callable[[], Any]


def select(
    *,
    kind: str,
    env_var: str,
    default: str,
    factories: dict[str, Factory],
    override: Optional[str] = None,
) -> Any:
    """Resolve one backend. Never raises.

    `override` lets a caller (a CLI flag, or a per-run config) win over the
    environment — the "option gate" alongside the env gate.
    """
    raw = (override if override is not None else os.getenv(env_var, default) or default)
    name = raw.strip().lower()

    if name not in factories:
        logger.warning(
            "%s=%r is not a recognised %s backend (known: %s) — using %r",
            env_var, raw, kind, ", ".join(sorted(factories)), default,
        )
        name = default

    try:
        instance = factories[name]()
    except Exception as exc:
        if name == default:
            logger.error("the default %s backend %r failed to build: %s", kind, name, exc)
            raise
        logger.warning(
            "%s backend %r failed to build (%s) — falling back to %r. This is "
            "degraded, not fatal: the run proceeds without it.",
            kind, name, exc, default,
        )
        instance = factories[default]()

    if name != default:
        logger.info("%s backend: %s", kind, name)
    return instance
