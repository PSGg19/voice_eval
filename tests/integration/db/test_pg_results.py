"""P3.3 exit — CLI results persist to Postgres (PgResultStore).

The SQLite ResultStore is deleted; a CLI run and an API run are now the same
kind of `runs` row. These tests exercise the store the way the runner and the
`results`/`load` CLI commands actually call it.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from vjudge.core.models import (
    CallResult,
    CallStatus,
    EvaluationResult,
    LoadTestConfig,
    LoadTestResult,
    Scenario,
)
from vjudge.db.models import Run, RunBatch
from vjudge.db.results import PgResultStore


@pytest.fixture()
def store(migrated_engine):
    s = PgResultStore.__new__(PgResultStore)
    s.engine = migrated_engine  # reuse the throwaway DB's engine
    s._partitions_ensured = False
    return s


def make_result(**over) -> CallResult:
    now = datetime.now(timezone.utc)
    base = dict(
        flow_name=f"cli-test-{uuid.uuid4().hex[:8]}",
        target="+15559990001",
        session_id=uuid.uuid4().hex,
        call_sid="CAtest123",
        interaction_uuid="int-abc",
        status=CallStatus.COMPLETED,
        started_at=now - timedelta(seconds=42),
        ended_at=now,
        log_dir="logs/2026-08-31/whatever",
        recording_paths={"bot": "bot.wav"},
        # `passed` is derived: V2 verdicts come from the evaluation. A bare
        # COMPLETED with no evaluation is passed=False by design.
        evaluation=EvaluationResult(overall_passed=True, overall_reasoning="ok"),
    )
    base.update(over)
    return CallResult(**base)


def make_scenario(**over) -> Scenario:
    base = dict(
        name="s",
        target="+15559990001",
        vars={"customer_name": "Iris"},
        persona_rendered="p",
        evaluation_prompt_rendered="e",
        region="in",
        source_path="clients/x/y.yaml",
    )
    base.update(over)
    return Scenario(**base)


def test_save_result_maps_onto_the_runs_table(store, migrated_engine):
    result = make_result()
    run_uuid = asyncio.run(
        store.save_result(result, scenario=make_scenario(), transport="sip")
    )
    # run_uuid IS the vjudge session id — one join key across DB, logs, SIP.
    assert run_uuid == uuid.UUID(result.session_id)

    with Session(migrated_engine) as s:
        row = s.execute(select(Run).where(Run.run_uuid == run_uuid)).scalar_one()
    assert row.scenario_key == result.flow_name
    assert row.status == "completed"
    assert row.region == "in"
    assert row.transport == "sip"
    assert row.vars_snapshot == {"customer_name": "Iris"}
    assert row.config_snapshot["target"] == "+15559990001"
    assert row.config_snapshot["source"] == "cli"
    assert row.config_snapshot["scenario_source_path"] == "clients/x/y.yaml"
    assert row.interaction_uuid == "int-abc"
    assert row.call_sid == "CAtest123"
    assert row.artifact_keys["log_dir"].startswith("logs/")
    assert row.artifact_keys["recordings"] == {"bot": "bot.wav"}
    assert row.started_at is not None and row.finished_at is not None


def test_error_status_maps_to_failed(store, migrated_engine):
    result = make_result(status=CallStatus.ERROR, error="boom", evaluation=None)
    run_uuid = asyncio.run(store.save_result(result))
    with Session(migrated_engine) as s:
        row = s.execute(select(Run).where(Run.run_uuid == run_uuid)).scalar_one()
    assert row.status == "failed"
    assert row.error == "boom"
    assert row.transport == "text"  # honest default when nothing is known


def test_get_results_and_stats_shapes_match_the_cli(store):
    flow = f"cli-shape-{uuid.uuid4().hex[:8]}"
    asyncio.run(store.save_result(make_result(flow_name=flow)))
    asyncio.run(
        store.save_result(
            make_result(
                flow_name=flow, status=CallStatus.ERROR, error="x", evaluation=None
            )
        )
    )

    rows = asyncio.run(store.get_results(flow_name=flow, limit=10))
    assert len(rows) == 2
    assert set(rows[0]) == {
        "id", "flow_name", "target", "status", "passed", "duration_ms", "created_at",
    }
    # `status` is the CALL status the CLI has always printed, not the row's.
    assert {r["status"] for r in rows} == {"completed", "error"}

    stats = asyncio.run(store.get_stats(flow_name=flow))
    assert stats["total"] == 2
    assert stats["pass_rate"] == "50.0%"
    assert stats["avg_duration_ms"] == pytest.approx(42_000, rel=0.1)


def test_target_filter_reads_the_snapshot(store):
    flow = f"cli-target-{uuid.uuid4().hex[:8]}"
    asyncio.run(store.save_result(make_result(flow_name=flow, target="+15550000001")))
    asyncio.run(store.save_result(make_result(flow_name=flow, target="+15550000002")))
    rows = asyncio.run(store.get_results(flow_name=flow, target="+15550000002"))
    assert len(rows) == 1 and rows[0]["target"] == "+15550000002"


def test_save_load_result_creates_batch_plus_runs(store, migrated_engine):
    flow = f"cli-load-{uuid.uuid4().hex[:8]}"
    load = LoadTestResult(
        flow_name=flow,
        target="+15559990001",
        config=LoadTestConfig(calls=2, cps=1.0),
        total_calls=2,
        passed_calls=2,
        call_results=[make_result(flow_name=flow), make_result(flow_name=flow)],
    )
    batch_id = asyncio.run(store.save_load_result(load))
    with Session(migrated_engine) as s:
        batch = s.get(RunBatch, batch_id)
        assert batch.name == f"load:{flow}"
        runs = s.execute(select(Run).where(Run.batch_id == batch_id)).scalars().all()
    assert len(runs) == 2


def test_non_uuid_ids_still_get_a_run_uuid(store):
    result = make_result(session_id="", id="abc123def456")  # V1-style short id
    run_uuid = asyncio.run(store.save_result(result))
    assert isinstance(run_uuid, uuid.UUID)
