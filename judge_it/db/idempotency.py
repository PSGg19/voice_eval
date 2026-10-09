"""Run-creation idempotency (P3.4) — the row-before-work primitive.

The contract: two racing POSTs with one idempotency key produce exactly one
run, and both callers receive the same run_uuid. The mechanism is a single
INSERT … ON CONFLICT DO NOTHING against `run_idempotency`'s primary key —
concurrency-safe by construction, no advisory locks, no read-then-write
window. The caller inserts the claim BEFORE doing any work; the loser of a
race reads the winner's run_uuid and returns it as its own.

Why this table exists at all (rather than UNIQUE(idempotency_key) on `runs`):
`runs` is partitioned by created_at, and Postgres requires the partition key
in every unique constraint on a partitioned table — a global unique key is
structurally impossible there. See judge_it/db/models.py.
"""
from __future__ import annotations

import uuid as uuid_mod

from sqlalchemy import text
from sqlalchemy.engine import Connection

_CLAIM = text(
    """
    INSERT INTO run_idempotency (idempotency_key, run_uuid)
    VALUES (:key, :run_uuid)
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING run_uuid
    """
)
_READ = text("SELECT run_uuid FROM run_idempotency WHERE idempotency_key = :key")


def claim(conn: Connection, key: str, run_uuid: uuid_mod.UUID) -> tuple[uuid_mod.UUID, bool]:
    """Claim `key` for `run_uuid`. Returns (owning_run_uuid, we_won).

    `we_won=True` → this caller owns the key and should create the run row.
    `we_won=False` → someone already did; return their run to the caller.
    """
    row = conn.execute(_CLAIM, {"key": key, "run_uuid": run_uuid}).first()
    if row is not None:
        return row[0], True
    existing = conn.execute(_READ, {"key": key}).first()
    if existing is None:  # pragma: no cover — only under concurrent DELETE
        raise RuntimeError(f"idempotency key {key!r} vanished between claim and read")
    return existing[0], False
