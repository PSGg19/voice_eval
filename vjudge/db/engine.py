"""Engine/session factories for the V3 store.

One driver (psycopg v3) serves both worlds: alembic migrates over the sync
engine, the API reads/writes over the async one. The URL is one setting —
`VJUDGE_DATABASE_URL` — whose default matches docker-compose.yml, so
`docker compose up -d postgres` is the entire local setup.

Pool limits are explicit because the P1 rule ("shared clients with explicit
pool limits") applies to the DB exactly as to HTTP: an unbounded pool hides a
concurrency bug until Postgres runs out of connections in the middle of a
campaign.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

# The machine's native Postgres (the same server the local OSV stack uses,
# postgres/root on 5432) — chosen over a compose container so the developer's
# existing SQL tooling sees vjudge's data with zero extra setup. The app owns
# ONLY the `vjudge` database on that server; the orchestrator_service*
# databases are OSV's and nothing here may touch them.
DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:root@localhost:5432/vjudge"

_POOL_KWARGS = dict(pool_size=5, max_overflow=5, pool_pre_ping=True)


def sync_engine(url: str = DEFAULT_DATABASE_URL) -> Engine:
    return create_engine(url, **_POOL_KWARGS)


def async_engine(url: str = DEFAULT_DATABASE_URL) -> AsyncEngine:
    return create_async_engine(url, **_POOL_KWARGS)


def sync_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def async_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
