"""Alembic environment for the V3 store.

The URL comes from AUTOCALLS_DATABASE_URL (falling back to the compose
default) via os.environ directly — NOT via Settings(), which requires Twilio
and Deepgram credentials that a migration host has no business holding.
"""
from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from opkura.db.engine import DEFAULT_DATABASE_URL
from opkura.db.models import Base

target_metadata = Base.metadata


def _url() -> str:
    return os.environ.get("AUTOCALLS_DATABASE_URL", DEFAULT_DATABASE_URL)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
