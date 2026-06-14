"""Fixtures for V3-store integration tests.

Every test run proves the PRD's "migration applies from empty" acceptance
criterion as a side effect: the fixture creates a THROWAWAY database
(opkura_test), runs `alembic upgrade head` against it, and drops it at the
end. No test ever touches the developer's working `opkura` database.

Requires the compose Postgres (host port 5433 — see docker-compose.yml).
Unreachable server → the whole module SKIPS with the command to fix it,
so `pytest tests/` stays green on a machine without containers.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from alembic import command
from alembic.config import Config

# The native server (shared with the local OSV stack). These tests create and
# drop ONLY the opkura_test database — never the developer's `opkura`, and
# never OSV's orchestrator_service* databases.
ADMIN_URL = os.environ.get(
    "AUTOCALLS_DATABASE_URL",
    "postgresql+psycopg://postgres:root@localhost:5432/opkura",
)
TEST_DB = "opkura_test"


def _test_url() -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{TEST_DB}"


@pytest.fixture(scope="session")
def migrated_engine():
    try:
        admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(
            f"Postgres not reachable at {ADMIN_URL} "
            f"(docker compose up -d postgres): {exc}"
        )

    cfg = Config("alembic.ini")
    prev = os.environ.get("AUTOCALLS_DATABASE_URL")
    os.environ["AUTOCALLS_DATABASE_URL"] = _test_url()
    try:
        command.upgrade(cfg, "head")  # ← "applies from empty", every run
    finally:
        if prev is None:
            os.environ.pop("AUTOCALLS_DATABASE_URL", None)
        else:
            os.environ["AUTOCALLS_DATABASE_URL"] = prev

    engine = create_engine(_test_url(), pool_pre_ping=True)
    from opkura.db.partitions import ensure_month_partitions

    with engine.begin() as conn:
        ensure_month_partitions(conn, ahead=1)

    yield engine

    engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
    admin.dispose()
