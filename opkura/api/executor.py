"""Run execution behind a seam (P3.3).

The API's contract is rows and statuses; HOW a run executes is this Protocol.
Today the default is in-process (the PRD's "runs still execute in-process via
BackgroundTasks" — queue/workers are Phase 4, and swapping this object is the
whole migration). Tests inject a fake and never dial anything.

LocalRunnerExecutor bridges to the existing V2 TestRunner: scenario body +
vars_snapshot → parse_scenario_data → run_scenario → the row is updated with
what actually happened. Every failure lands ON the row (`status=failed`,
`error=`): a background task that only logged would leave the run `running`
forever with the truth in a terminal nobody is watching.
"""
from __future__ import annotations

import logging
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from opkura.core.config import Settings
from opkura.db.models import Run, Scenario

logger = logging.getLogger(__name__)


class Executor(Protocol):
    async def execute(self, run_uuid: uuid_mod.UUID) -> None: ...


class LocalRunnerExecutor:
    """In-process execution via the existing TestRunner."""

    def __init__(self, *, settings: Settings, engine: Engine):
        self.settings = settings
        self.engine = engine

    async def execute(self, run_uuid: uuid_mod.UUID) -> None:
        with Session(self.engine) as session:
            run = session.execute(
                select(Run).where(Run.run_uuid == run_uuid)
            ).scalar_one_or_none()
            if run is None:  # pragma: no cover — row committed before dispatch
                logger.error("executor: run %s not found", run_uuid)
                return
            scenario_row = session.get(Scenario, run.scenario_id)
            if scenario_row is None:
                self._finish(session, run, status="failed",
                             error=f"scenario {run.scenario_id} vanished")
                return
            run.status = "running"
            run.started_at = datetime.now(timezone.utc)
            session.commit()
            body = dict(scenario_row.body)
            run_key = (run.run_uuid, run.created_at)

        try:
            result = await self._run(body, run_uuid)
        except Exception as exc:
            logger.exception("executor: run %s failed", run_uuid)
            with Session(self.engine) as session:
                run = session.get(Run, run_key)
                self._finish(session, run, status="failed", error=str(exc))
            return

        with Session(self.engine) as session:
            run = session.get(Run, run_key)
            status_name = getattr(getattr(result, "status", None), "value", None) or str(
                getattr(result, "status", "")
            )
            ok = status_name.upper().endswith("COMPLETED")
            run.passed = getattr(result, "passed", None)
            run.interaction_uuid = getattr(result, "interaction_uuid", None)
            run.call_sid = getattr(result, "call_sid", None)
            run.verdict = _result_verdict(result)
            self._finish(
                session, run,
                status="completed" if ok else "failed",
                error=getattr(result, "error", "") or "",
            )

    async def _run(self, body: dict, run_uuid: uuid_mod.UUID):
        # Imported here, not at module top: TestRunner pulls in the transports
        # (Twilio SDK, Deepgram), which an API-only process may never need.
        from opkura.core.scenario_loader import parse_scenario_data
        from opkura.orchestrator.runner import TestRunner

        with Session(self.engine) as session:
            run = session.execute(
                select(Run).where(Run.run_uuid == run_uuid)
            ).scalar_one()
            # The snapshot IS the variable truth for this run; the body's
            # inline vars were already folded into it at admission.
            body = {**body, "vars": run.vars_snapshot, "region": run.region,
                    "transport": run.transport}
        scenario = parse_scenario_data(body, source_path=f"<run {run_uuid}>")
        return await TestRunner(self.settings).run_scenario(scenario)

    @staticmethod
    def _finish(session: Session, run: Run, *, status: str, error: str = "") -> None:
        run.status = status
        run.error = error
        run.finished_at = datetime.now(timezone.utc)
        session.commit()


def _result_verdict(result) -> dict:
    """The judge's output as a JSON-safe dict, defensively."""
    out: dict = {}
    for attr in ("overall_reasoning", "criteria", "metrics", "assertion_outcomes"):
        value = getattr(result, attr, None)
        if value is None:
            continue
        try:
            import json

            json.dumps(value)
            out[attr] = value
        except (TypeError, ValueError):
            out[attr] = str(value)
    return out
