"""Log sink: human text, structured JSON, or a direct Loki push.

Three sinks because opkura runs in two very different places:

* `text`  — a person watching a terminal. The default, and what the CLI does now.
* `json`  — one structured line per record on stdout. What a cluster wants:
            orchestrator-service does exactly this and lets a collector
            (Promtail/Alloy) do the Loki ingestion. No push code in-process.
* `loki`  — a direct push, for the local case where a developer is running
            `docker run grafana/loki` on a laptop with no collector to rely on.

**Label discipline is a correctness rule, not a style preference.** Loki opens a
new stream per distinct label combination, and stream explosion degrades its
index badly. So labels are only ever low-cardinality, stable dimensions —
service, env, region, transport. `run_id` and `interaction_uuid` are unbounded
by definition and belong in the line *body*, which is where the existing
`call_id_var` contextvar already puts them.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from opkura.connectors import select

logger = logging.getLogger(__name__)

ENV_VAR = "LOG_SINK"
DEFAULT = "text"

# Never labels. Enforced rather than documented, because the failure is gradual
# index degradation that nobody notices until Loki is slow.
_FORBIDDEN_LABELS = {
    "run_id", "call_id", "interaction_uuid", "session_id", "worker_id",
    "scenario", "request_id", "trace_id", "recipient_id",
}


def _labels_from_env() -> dict[str, str]:
    labels = {
        "service": os.getenv("OTEL_SERVICE_NAME", "opkura"),
        "env": os.getenv("DEPLOY_ENV", "local"),
    }
    for extra in ("region", "transport"):
        value = os.getenv(f"LOG_LABEL_{extra.upper()}")
        if value:
            labels[extra] = value

    dropped = _FORBIDDEN_LABELS & set(labels)
    if dropped:
        # Belt and braces: if someone widens the set above, this still refuses.
        logger.warning(
            "refusing high-cardinality Loki label(s) %s — these belong in the "
            "log line, not the stream identity",
            sorted(dropped),
        )
        for key in dropped:
            labels.pop(key, None)
    return labels


class _JsonFormatter(logging.Formatter):
    """One JSON object per record, with the run id as a field."""

    def format(self, record: logging.LogRecord) -> str:
        from opkura.core.log_context import call_id_var

        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "file": f"{record.filename}:{record.lineno}",
        }
        run_id = call_id_var.get()
        if run_id:
            payload["run_id"] = run_id          # a field, never a label
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _text_handler() -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                                           datefmt="%H:%M:%S"))
    return handler


def _json_handler() -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    return handler


def _loki_handler() -> logging.Handler:
    """Direct push. Queued, so emitting a log never blocks the event loop."""
    import logging_loki  # imported only when this sink is chosen

    url = os.getenv("LOKI_URL", "http://localhost:3100/loki/api/v1/push")
    import multiprocessing

    handler = logging_loki.LokiQueueHandler(
        multiprocessing.Queue(-1),
        url=url,
        tags=_labels_from_env(),
        auth=((os.environ["LOKI_USER"], os.environ["LOKI_PASSWORD"])
              if os.getenv("LOKI_USER") else None),
        version="1",
    )
    handler.setFormatter(_JsonFormatter())
    return handler


def build(override: Optional[str] = None) -> logging.Handler:
    """Resolve the sink. Falls back to readable text if a sink cannot be built."""
    return select(
        kind="log sink",
        env_var=ENV_VAR,
        default=DEFAULT,
        override=override,
        factories={"text": _text_handler, "json": _json_handler, "loki": _loki_handler},
    )
