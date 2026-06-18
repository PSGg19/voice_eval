"""variable_sets.is_synthetic — the reviewed-once trust bit.

core/variables.py's safety property keys on this: a set asserted synthetic
passes the PII value guard quietly; any inline override voids that trust for
the run. Default FALSE — trust is granted by a human, never assumed.

(Plain ADD COLUMN with a default: instant on PG11+ regardless of table size,
so the CONCURRENTLY discipline note in 0001 does not apply here.)

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE variable_sets "
        "ADD COLUMN is_synthetic boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE variable_sets DROP COLUMN is_synthetic")
