"""Per-call context propagation for log routing.

`call_id_var` is a process-wide `ContextVar` set at the start of each
scenario run. Two places set it because the runner's event loop and
FastAPI's event loop run in different threads — and `ContextVar`
state is per-task (within a loop), not shared across loops:

  * `runner._run_v2_scenario` sets it for runner-side tasks
    (engine bookkeeping, evaluator, persistence).
  * `server.media_stream_ws` sets it for FastAPI-side tasks
    (STT recv loop, TTS WS, conversation loop, barge tasks).

`CallLogWriter` attaches a `Filter` to its per-call `FileHandler` that
reads this var and rejects records whose active `call_id_var` doesn't
match its own `call_id`. Without this, the broadcast-to-root-handler
design means every concurrent call's FileHandler captures every log
line — making per-call `debug.log` files unreadable in campaign mode.

See `plan/phases/phase-2/sub-task/task-2-logwriter-concurrent/TASK.md`
for the full rationale and the alternatives that were rejected.
"""

from __future__ import annotations

import contextvars
from typing import Optional

call_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "call_id", default=None
)


# Phase 2 Task 3: per-worker identifier in campaign mode. Set by each
# `CampaignRunner` worker at the top of its loop so log records emitted
# during that worker's scenario carry a stable `worker_id` for triage.
# Unset (None) outside campaign mode. Read by the campaign runner's log
# formatter (and any future per-worker diagnostics).
worker_id_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "worker_id", default=None
)
