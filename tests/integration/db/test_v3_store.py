"""P3.1 + P3.4 — the V3 store against a real, freshly-migrated Postgres.

PRD acceptance tests covered here:
* migration applies from empty (the conftest fixture does it every run);
* two racing claims with one idempotency key → exactly one winner, both
  callers see the same run_uuid (repeated 100×);
* editing a variable set after a run leaves the run's vars_snapshot unchanged;
* a run is explicable from its row alone (snapshots come back verbatim);
* the three hot queries use indexes, not seq scans, on a populated table.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opkura.db.idempotency import claim
from opkura.db.models import Run, RunBatch, Scenario, Target, VariableSet
from opkura.db.partitions import PARTITION_NAME

NOW = datetime.now(timezone.utc)


# ------------------------------------------------------------------ helpers

def _mk_target(session: Session, **over) -> Target:
    t = Target(
        phone_number=over.pop("phone_number", f"+1844{uuid.uuid4().int % 10**7:07d}"),
        region=over.pop("region", "us"),
        **over,
    )
    session.add(t)
    session.flush()
    return t


# -------------------------------------------------------------------- CRUD
# Round-trips every model against the migrated schema — this is the
# models↔migration drift tripwire.

def test_every_model_round_trips(migrated_engine):
    with Session(migrated_engine) as s:
        target = _mk_target(
            s,
            assistant_name="Imported - SBI YONO V2 Abhay",
            provider_config="livekit-skittl-2",
            region="in",
            phone_number="+918062123187",
            account_count=0,
            post_call_enabled=False,
        )
        vs = VariableSet(
            name=f"sbi-defaults-{uuid.uuid4().hex[:8]}", client="sbi",
            vars={"customer_name": "Test"}, provenance={"customer_name": "example"},
        )
        s.add(vs)
        s.flush()
        scen = Scenario(
            # uuid-suffixed: the importer test loads the REAL corpus into the
            # same session-scoped DB, which owns the real sbi/… keys at v1.
            key=f"sbi/yono-general/tool-multihop-routing-{uuid.uuid4().hex[:8]}",
            version=1,
            body={"persona": "You are …", "evaluation_prompt": "Judge …"},
            target_id=target.id, variable_set_id=vs.id,
            source_path="clients/sbi/yono-general/tool-multihop-routing.yaml",
        )
        s.add(scen)
        batch = RunBatch(name="smoke", config={"max_concurrency": 2})
        s.add(batch)
        s.flush()
        run = Run(
            batch_id=batch.id, scenario_id=scen.id,
            scenario_key=scen.key, scenario_version=1,
            target_id=target.id, region="in", transport="sip",
            vars_snapshot={"customer_name": "Test"},
            config_snapshot={"sip_destination_host": "inproj.sip.livekit.cloud:5060",
                             "provider_config": "livekit-skittl-2"},
        )
        s.add(run)
        s.commit()

        got = s.get(Run, (run.run_uuid, run.created_at))
        assert got is not None
        assert got.config_snapshot["provider_config"] == "livekit-skittl-2"
        assert got.region == "in"


# -------------------------------------------------------------- constraints

def test_target_unique_per_region_not_globally(migrated_engine):
    with Session(migrated_engine) as s:
        _mk_target(s, phone_number="+18440000001", region="us")
        _mk_target(s, phone_number="+18440000001", region="ca")  # fine
        s.commit()
    with Session(migrated_engine) as s:
        # No _mk_target here: its flush() would raise before pytest.raises saw
        # the commit. Add unflushed, let commit trip the constraint.
        s.add(Target(phone_number="+18440000001", region="us"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_bad_region_rejected_by_check_constraint(migrated_engine):
    with Session(migrated_engine) as s:
        s.add(Target(phone_number="+18440000002", region="eu"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_scenario_key_version_unique(migrated_engine):
    with Session(migrated_engine) as s:
        s.add(Scenario(key="dup/key", version=3, body={}))
        s.commit()
    with Session(migrated_engine) as s:
        s.add(Scenario(key="dup/key", version=3, body={}))
        with pytest.raises(IntegrityError):
            s.commit()


def test_run_status_check_constraint(migrated_engine):
    with Session(migrated_engine) as s:
        s.add(Run(status="exploded"))
        with pytest.raises(IntegrityError):
            s.commit()


# --------------------------------------------------------------- partitions

def test_run_lands_in_its_month_partition(migrated_engine):
    with Session(migrated_engine) as s:
        run = Run(created_at=NOW)
        s.add(run)
        s.commit()
        part = s.execute(
            text("SELECT tableoid::regclass::text FROM runs WHERE run_uuid = :u"),
            {"u": run.run_uuid},
        ).scalar_one()
    assert part == PARTITION_NAME.format(y=NOW.year, m=NOW.month)


def test_missing_partition_fails_loudly_not_silently(migrated_engine):
    """No DEFAULT partition, by design: a row for an uncovered month must be
    a loud insert error, not a quiet catch-all row."""
    with Session(migrated_engine) as s:
        s.add(Run(created_at=NOW + timedelta(days=200)))
        with pytest.raises(Exception) as exc:
            s.commit()
        assert "no partition" in str(exc.value).lower()


# -------------------------------------------------------------- idempotency

def test_duplicate_claim_returns_original_run(migrated_engine):
    key = f"idem-{uuid.uuid4()}"
    first, second = uuid.uuid4(), uuid.uuid4()
    with migrated_engine.begin() as conn:
        owner1, won1 = claim(conn, key, first)
    with migrated_engine.begin() as conn:
        owner2, won2 = claim(conn, key, second)
    assert (won1, won2) == (True, False)
    assert owner1 == owner2 == first


def test_racing_claims_produce_exactly_one_winner_100x(migrated_engine):
    """PRD acceptance: two racing creates with one key → one run, both callers
    receive the same run_uuid. Repeated 100×, real threads, real Postgres."""
    for _ in range(100):
        key = f"race-{uuid.uuid4()}"
        results: list[tuple[uuid.UUID, bool]] = []
        barrier = threading.Barrier(2)

        def contender():
            mine = uuid.uuid4()
            barrier.wait()
            with migrated_engine.begin() as conn:
                results.append(claim(conn, key, mine))

        threads = [threading.Thread(target=contender) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r[1]]
        assert len(winners) == 1, f"key {key}: {results}"
        assert results[0][0] == results[1][0]  # both callers: same run_uuid


# ------------------------------------------------------------ explicability

def test_editing_variable_set_leaves_run_snapshot_unchanged(migrated_engine):
    with Session(migrated_engine) as s:
        vs = VariableSet(name=f"vs-{uuid.uuid4()}", vars={"amount": "100.00"})
        s.add(vs)
        s.flush()
        run = Run(vars_snapshot=dict(vs.vars))
        s.add(run)
        s.commit()
        run_key = (run.run_uuid, run.created_at)
        vs_id = vs.id

    with Session(migrated_engine) as s:
        s.get(VariableSet, vs_id).vars = {"amount": "999.99"}
        s.commit()

    with Session(migrated_engine) as s:
        assert s.get(Run, run_key).vars_snapshot == {"amount": "100.00"}


# ----------------------------------------------------------- hot-query plans

@pytest.fixture(scope="module")
def populated(migrated_engine):
    """A few thousand runs spread over batches/targets/statuses — enough for
    the planner to prefer the indexes it would use at the PRD's 1M scale."""
    batch_ids = [uuid.uuid4() for _ in range(20)]
    target_ids = [uuid.uuid4() for _ in range(50)]
    with migrated_engine.begin() as conn:
        for b in batch_ids:
            conn.execute(
                text("INSERT INTO run_batches (id, name) VALUES (:i, 'seed')"),
                {"i": b},
            )
        rows = [
            {
                "b": batch_ids[i % len(batch_ids)],
                "t": target_ids[i % len(target_ids)],
                "st": "completed" if i % 25 else "running",
                "p": (i % 7 != 0),
            }
            for i in range(5000)
        ]
        conn.execute(
            text(
                "INSERT INTO runs (batch_id, target_id, status, passed) "
                "VALUES (:b, :t, :st, :p)"
            ),
            rows,
        )
        conn.execute(text("ANALYZE runs"))
    return {"batch": batch_ids[0], "target": target_ids[0]}


def _plan(engine, sql, params):
    with engine.connect() as conn:
        rows = conn.execute(text("EXPLAIN " + sql), params).scalars().all()
    return "\n".join(rows)


# EXPLAIN shows CHILD indexes (Postgres auto-names them per partition, e.g.
# runs_y2026m08_status_idx), never the parent index name — and an EMPTY
# partition is legitimately seq-scanned at cost 0. The property that matters:
# the POPULATED partition (this month's, where the 5k rows live) is reached
# via an Index/Bitmap scan, not a Seq Scan.
_HOT_PARTITION = PARTITION_NAME.format(y=NOW.year, m=NOW.month)


def _assert_indexed(plan: str, name: str) -> None:
    assert f"Seq Scan on {_HOT_PARTITION}" not in plan, f"{name} seq-scans:\n{plan}"
    assert "Index Scan" in plan or "Bitmap Heap Scan" in plan, (
        f"{name} plan has no index access:\n{plan}"
    )


@pytest.mark.parametrize(
    "name,sql,param_key",
    [
        (
            "a batch's runs",
            "SELECT * FROM runs WHERE batch_id = :p ORDER BY created_at",
            "batch",
        ),
        (
            "a target's recent failures",
            "SELECT * FROM runs WHERE target_id = :p AND passed = false "
            "ORDER BY created_at DESC LIMIT 20",
            "target",
        ),
    ],
)
def test_hot_queries_use_their_indexes(populated, migrated_engine, name, sql, param_key):
    plan = _plan(migrated_engine, sql, {"p": populated[param_key]})
    _assert_indexed(plan, name)


def test_in_flight_query_uses_partial_index(populated, migrated_engine):
    plan = _plan(
        migrated_engine,
        "SELECT * FROM runs WHERE status IN "
        "('created','queued','running','correlating')",
        {},
    )
    # The partial index's child on the populated partition carries the
    # partition name + column: assert it specifically.
    assert f"{_HOT_PARTITION}_status_idx" in plan, plan
    _assert_indexed(plan, "runs in flight")
