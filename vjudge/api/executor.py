"""The execution seam (P3.3), post-P4: dispatch only.

The API's contract is rows and statuses; HOW a run executes is this Protocol.
Since Phase 4 the production implementation is `QueueExecutor`
(vjudge/queue/dispatch.py): `execute` marks the row 'queued' and XADDs one
stream entry — workers do the running. The in-process BackgroundTasks
execution path (`LocalRunnerExecutor`) is DELETED, per the phase's exit
criterion: nothing dials a bot from inside an API request's background task
any more. Tests keep injecting fakes through `install_v3(executor=…)`.
"""
from __future__ import annotations

import uuid as uuid_mod
from typing import Protocol


class Executor(Protocol):
    async def execute(self, run_uuid: uuid_mod.UUID) -> None: ...
