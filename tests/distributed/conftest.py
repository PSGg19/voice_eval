"""Fixtures for the `-m distributed` two-worker proof.

Real infrastructure, deliberately: the compose Redis (DB index 7, flushed per
test) and a throwaway migrated Postgres database (`vjudge_dist`). Unreachable
infrastructure skips the module with the command that fixes it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest
import redis as sync_redis
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config

REDIS_URL = os.environ.get("VJUDGE_DIST_REDIS_URL", "redis://localhost:6379/7")
ADMIN_URL = os.environ.get(
    "VJUDGE_DATABASE_URL",
    "postgresql+psycopg://postgres:root@localhost:5432/vjudge",
)
TEST_DB = "vjudge_dist"

SCENARIO_BODY = {
    "name": "proof",
    "target": "+15550009999",
    "vars": {},
    "persona": "You are the caller.",
    "evaluation_prompt": "Judge it.",
    "transport": "text",
    "recipient_id": "2015550137",   # NANP fictional range (API lint requires it)
}


def _test_url() -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{TEST_DB}"


@pytest.fixture(scope="session")
def dist_engine():
    try:
        admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT",
                              pool_pre_ping=True)
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Postgres not reachable at {ADMIN_URL}: {exc}")

    cfg = Config("alembic.ini")
    prev = os.environ.get("VJUDGE_DATABASE_URL")
    os.environ["VJUDGE_DATABASE_URL"] = _test_url()
    try:
        command.upgrade(cfg, "head")
    finally:
        if prev is None:
            os.environ.pop("VJUDGE_DATABASE_URL", None)
        else:
            os.environ["VJUDGE_DATABASE_URL"] = prev

    engine = create_engine(_test_url(), pool_pre_ping=True)
    from vjudge.db.partitions import ensure_month_partitions

    with engine.begin() as conn:
        ensure_month_partitions(conn, ahead=1)
    yield engine
    engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))


@pytest.fixture()
def dist_redis():
    try:
        client = sync_redis.from_url(REDIS_URL, decode_responses=True)
        client.ping()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Redis not reachable at {REDIS_URL} "
                    f"(docker compose up -d redis): {exc}")
    client.flushdb()
    yield client
    client.flushdb()
    client.close()


@pytest.fixture()
def clean_runs(dist_engine):
    """Each proof starts from an empty runs table."""
    with dist_engine.begin() as conn:
        conn.execute(text("TRUNCATE runs, run_idempotency, run_batches CASCADE"))
    yield


def seed_runs(engine, count: int, *, target: str = "+15550009999",
              batch_id=None, name_prefix: str = "proof") -> list:
    """Rows only — dispatch is the test's business."""
    from vjudge.db.models import Run, Scenario as ScenarioRow

    out = []
    with Session(engine) as session:
        scen = ScenarioRow(
            key=f"{name_prefix}-{uuid.uuid4().hex[:6]}",
            body={**SCENARIO_BODY, "target": target},
        )
        session.add(scen)
        session.commit()
        for i in range(count):
            body_name = f"{name_prefix}-{i:04d}-{uuid.uuid4().hex[:6]}"
            row = ScenarioRow(key=body_name,
                              body={**SCENARIO_BODY, "target": target,
                                    "name": body_name})
            session.add(row)
            session.commit()
            run = Run(scenario_id=row.id, scenario_key=row.key, region="us",
                      transport="text", status="queued", vars_snapshot={},
                      batch_id=batch_id)
            session.add(run)
            session.commit()
            out.append((run.run_uuid, run.created_at, body_name))
    return out


def spawn_worker(*, consumer: str, redis_url: str = REDIS_URL,
                 db_url: str | None = None, **kw) -> subprocess.Popen:
    args = [
        sys.executable, "-m", "tests.distributed._worker_proc",
        "--consumer", consumer,
        "--redis-url", redis_url,
        "--db-url", db_url or _test_url(),
    ]
    for key, value in kw.items():
        args.extend([f"--{key.replace('_', '-')}", str(value)])
    return subprocess.Popen(args, cwd=os.getcwd())
