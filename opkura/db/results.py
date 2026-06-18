"""CLI results on Postgres (P3.3 exit): the SQLite ResultStore's replacement.

Same async surface the runner and CLI already call — `save_result`,
`get_results`, `get_stats`, `save_load_result` — but every row lands in the
V3 `runs` table, so a CLI run and an API run are the SAME kind of record and
one `opkura results` (or one SQL query) reads them all.

Mechanics worth knowing:

* Writes run through `asyncio.to_thread`: psycopg here is sync, and a DB
  write must never stall the event loop mid-campaign (the P1.4 rule).
* Month partitions are ensured once per process before the first write, so
  `opkura run` works on a fresh database with no manual step.
* Transcripts and audio do NOT go in the row — the per-call log folder owns
  them, and `artifact_keys` points there. The row is the index, not the blob.
* `run_uuid` is the opkura session id whenever it is a real UUID (the V2
  path always is — it is the X-Opkura-Session-Id SIP header), so the DB row,
  the logs, and the bot-side interaction all join on one key.
"""
from __future__ import annotations

import asyncio
import json
import uuid as uuid_mod
from dataclasses import asdict, is_dataclass
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from opkura.core.models import CallResult, CallStatus, LoadTestResult
from opkura.db.engine import DEFAULT_DATABASE_URL, sync_engine
from opkura.db.models import Run, RunBatch
from opkura.db.partitions import ensure_month_partitions

_TERMINAL_OK = (CallStatus.COMPLETED,)


def _jsonable(value):
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))


def _run_uuid_for(result: CallResult) -> uuid_mod.UUID:
    for candidate in (result.session_id, result.id):
        try:
            return uuid_mod.UUID(candidate)
        except (ValueError, AttributeError, TypeError):
            continue
    return uuid_mod.uuid4()


class PgResultStore:
    def __init__(self, database_url: str = DEFAULT_DATABASE_URL):
        self.engine = sync_engine(database_url)
        self._partitions_ensured = False

    # ------------------------------------------------------------- writes

    async def save_result(
        self,
        result: CallResult,
        *,
        scenario=None,
        transport: Optional[str] = None,
        batch_id=None,
    ) -> uuid_mod.UUID:
        """Persist one finished call. `scenario` (the V2 Scenario dataclass,
        when the caller has it) enriches the row with region/vars; `transport`
        is the EFFECTIVE transport the caller resolved (scenario pin beats
        deployment default — the runner knows, this store does not guess)."""
        return await asyncio.to_thread(
            self._save_sync, result, scenario, transport, batch_id
        )

    def _save_sync(self, result: CallResult, scenario, transport, batch_id) -> uuid_mod.UUID:
        if not self._partitions_ensured:
            with self.engine.begin() as conn:
                ensure_month_partitions(conn, ahead=1)
            self._partitions_ensured = True

        run_uuid = _run_uuid_for(result)
        verdict: dict = {}
        if result.evaluation is not None:
            verdict["evaluation"] = _jsonable(result.evaluation)
        if result.assertion_outcomes:
            verdict["assertion_outcomes"] = _jsonable(result.assertion_outcomes)
        if result.modules_visited:
            verdict["modules_visited"] = list(result.modules_visited)

        artifact_keys: dict = {}
        if result.log_dir:
            artifact_keys["log_dir"] = result.log_dir
        if result.recording_paths:
            artifact_keys["recordings"] = dict(result.recording_paths)

        config_snapshot = {
            "source": "cli",
            "target": result.target,
            "session_id": result.session_id,
            "call_status": result.status.value,
            "duration_ms": result.duration_ms,
        }
        region = "us"
        vars_snapshot: dict = {}
        if scenario is not None:
            region = scenario.region or region
            transport = transport or scenario.transport
            vars_snapshot = _jsonable(dict(scenario.vars or {}))
            config_snapshot["scenario_source_path"] = scenario.source_path

        row = Run(
            run_uuid=run_uuid,
            batch_id=batch_id,
            scenario_key=result.flow_name,
            region=region,
            transport=transport or "text",
            status="completed" if result.status in _TERMINAL_OK else "failed",
            passed=result.passed,
            vars_snapshot=vars_snapshot,
            config_snapshot=config_snapshot,
            verdict=verdict,
            error=result.error or "",
            interaction_uuid=result.interaction_uuid or None,
            call_sid=result.call_sid or None,
            artifact_keys=artifact_keys,
            started_at=result.started_at,
            finished_at=result.ended_at,
        )
        with Session(self.engine) as session:
            session.add(row)
            session.commit()
        return run_uuid

    async def save_load_result(self, result: LoadTestResult) -> uuid_mod.UUID:
        return await asyncio.to_thread(self._save_load_sync, result)

    def _save_load_sync(self, result: LoadTestResult) -> uuid_mod.UUID:
        with Session(self.engine) as session:
            batch = RunBatch(
                name=f"load:{result.flow_name}",
                config=_jsonable(result.config.model_dump()),
                status="completed",
            )
            session.add(batch)
            session.commit()
            batch_id = batch.id
        for call in result.call_results:
            self._save_sync(call, None, None, batch_id)
        return batch_id

    # -------------------------------------------------------------- reads

    async def get_results(
        self,
        flow_name: Optional[str] = None,
        target: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        return await asyncio.to_thread(self._get_results_sync, flow_name, target, limit)

    def _get_results_sync(self, flow_name, target, limit) -> list[dict]:
        q = select(Run).order_by(Run.created_at.desc()).limit(limit)
        if flow_name:
            q = q.where(Run.scenario_key == flow_name)
        if target:
            q = q.where(Run.config_snapshot["target"].astext == target)
        with Session(self.engine) as session:
            rows = session.execute(q).scalars().all()
            return [
                {
                    "id": str(r.run_uuid)[:12],
                    "flow_name": r.scenario_key,
                    "target": r.config_snapshot.get("target", ""),
                    "status": r.config_snapshot.get("call_status", r.status),
                    "passed": r.passed,
                    "duration_ms": r.config_snapshot.get("duration_ms"),
                    "created_at": r.created_at.isoformat(sep=" ", timespec="seconds"),
                }
                for r in rows
            ]

    async def get_stats(self, flow_name: Optional[str] = None) -> dict:
        return await asyncio.to_thread(self._get_stats_sync, flow_name)

    def _get_stats_sync(self, flow_name) -> dict:
        where = "WHERE scenario_key = :flow" if flow_name else ""
        sql = text(
            f"""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE passed) AS passed,
                   avg((config_snapshot->>'duration_ms')::float) AS avg_duration_ms
            FROM runs {where}
            """
        )
        with self.engine.connect() as conn:
            row = conn.execute(sql, {"flow": flow_name} if flow_name else {}).one()
        total = row.total or 0
        passed = row.passed or 0
        return {
            "total": total,
            "passed": passed,
            "pass_rate": f"{(passed / total * 100):.1f}%" if total else "0.0%",
            "avg_duration_ms": row.avg_duration_ms,
        }

    async def close(self) -> None:
        await asyncio.to_thread(self.engine.dispose)
