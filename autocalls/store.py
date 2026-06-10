"""SQLite storage for test results."""

from __future__ import annotations

import json
import logging

import aiosqlite

from .models import CallResult, LoadTestResult

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id TEXT PRIMARY KEY,
    flow_name TEXT NOT NULL,
    target TEXT NOT NULL,
    call_sid TEXT,
    status TEXT NOT NULL,
    passed INTEGER NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    duration_ms REAL,
    steps_json TEXT,
    transcript_json TEXT,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_results_flow ON results(flow_name);
CREATE INDEX IF NOT EXISTS idx_results_target ON results(target);
CREATE INDEX IF NOT EXISTS idx_results_created ON results(created_at);

CREATE TABLE IF NOT EXISTS load_results (
    id TEXT PRIMARY KEY,
    flow_name TEXT NOT NULL,
    target TEXT NOT NULL,
    total_calls INTEGER NOT NULL,
    passed_calls INTEGER NOT NULL,
    failed_calls INTEGER NOT NULL,
    error_calls INTEGER NOT NULL,
    pass_rate REAL NOT NULL,
    avg_duration_ms REAL,
    min_duration_ms REAL,
    max_duration_ms REAL,
    p50_duration_ms REAL,
    p95_duration_ms REAL,
    total_test_duration_ms REAL,
    config_json TEXT,
    call_ids_json TEXT,
    started_at TEXT,
    ended_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_load_results_flow ON load_results(flow_name);
CREATE INDEX IF NOT EXISTS idx_load_results_created ON load_results(created_at);
"""


class ResultStore:
    def __init__(self, db_path: str = "autocalls.db"):
        self.db_path = db_path
        self._initialized = False

    async def _ensure_schema(self, db: aiosqlite.Connection):
        if not self._initialized:
            await db.executescript(SCHEMA)
            self._initialized = True

    async def save_result(self, result: CallResult):
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_schema(db)
            await db.execute(
                """
                INSERT OR REPLACE INTO results
                (id, flow_name, target, call_sid, status, passed,
                 started_at, ended_at, duration_ms,
                 steps_json, transcript_json, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.id,
                    result.flow_name,
                    result.target,
                    result.call_sid,
                    result.status.value,
                    int(result.passed),
                    result.started_at.isoformat() if result.started_at else None,
                    result.ended_at.isoformat() if result.ended_at else None,
                    result.duration_ms,
                    json.dumps([s.model_dump() for s in result.steps]),
                    json.dumps([t.model_dump() for t in result.transcript]),
                    result.error,
                ),
            )
            await db.commit()
            logger.info("Saved result %s (%s)", result.id, result.status.value)

    async def get_results(
        self,
        flow_name: str | None = None,
        target: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row

            where_parts = []
            params: list = []
            if flow_name:
                where_parts.append("flow_name = ?")
                params.append(flow_name)
            if target:
                where_parts.append("target = ?")
                params.append(target)

            where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
            query = f"SELECT * FROM results {where} ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = await db.execute_fetchall(query, params)
            return [dict(row) for row in rows]

    async def save_load_result(self, result: LoadTestResult):
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_schema(db)
            call_ids = [c.id for c in result.call_results]
            await db.execute(
                """
                INSERT OR REPLACE INTO load_results
                (id, flow_name, target, total_calls, passed_calls,
                 failed_calls, error_calls, pass_rate,
                 avg_duration_ms, min_duration_ms, max_duration_ms,
                 p50_duration_ms, p95_duration_ms, total_test_duration_ms,
                 config_json, call_ids_json, started_at, ended_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.id,
                    result.flow_name,
                    result.target,
                    result.total_calls,
                    result.passed_calls,
                    result.failed_calls,
                    result.error_calls,
                    result.pass_rate,
                    result.avg_duration_ms,
                    result.min_duration_ms,
                    result.max_duration_ms,
                    result.p50_duration_ms,
                    result.p95_duration_ms,
                    result.total_duration_ms,
                    json.dumps(result.config.model_dump()),
                    json.dumps(call_ids),
                    result.started_at.isoformat() if result.started_at else None,
                    result.ended_at.isoformat() if result.ended_at else None,
                ),
            )
            await db.commit()
            logger.info("Saved load test %s (%d calls)", result.id, result.total_calls)

    async def get_load_results(self, flow_name: str | None = None, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row

            where = "WHERE flow_name = ?" if flow_name else ""
            params: list = [flow_name] if flow_name else []
            params.append(limit)

            rows = await db.execute_fetchall(
                f"SELECT * FROM load_results {where} ORDER BY created_at DESC LIMIT ?",
                params,
            )
            return [dict(row) for row in rows]

    async def get_stats(self, flow_name: str | None = None) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_schema(db)

            where = "WHERE flow_name = ?" if flow_name else ""
            params = [flow_name] if flow_name else []

            row = await db.execute_fetchall(
                f"""
                SELECT
                    COUNT(*) as total,
                    SUM(passed) as passed,
                    AVG(duration_ms) as avg_duration_ms,
                    MIN(created_at) as first_run,
                    MAX(created_at) as last_run
                FROM results {where}
                """,
                params,
            )
            if row:
                r = row[0]
                total = r[0] or 0
                passed = r[1] or 0
                return {
                    "total": total,
                    "passed": passed,
                    "failed": total - passed,
                    "pass_rate": f"{(passed / total * 100):.1f}%" if total else "N/A",
                    "avg_duration_ms": round(r[2], 1) if r[2] else None,
                    "first_run": r[3],
                    "last_run": r[4],
                }
            return {"total": 0}
