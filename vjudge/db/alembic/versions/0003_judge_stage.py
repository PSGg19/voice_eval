"""P4.3 — the judge is its own stage; the row carries what it needs.

Three additions to `runs`:

* `transcript`   — the conversation stage's output, consumed by the evaluate
                   stage on a different worker. JSONB list of transcript
                   entries, exactly `TranscriptEntry.model_dump()` shape.
* `observations` — everything else the judge reads: modules_visited,
                   tool_calls, tag_only turns, termination reason, call
                   status, latency streams. One blob, because its shape
                   belongs to the engine, not to SQL.
* `judge_policy` — per run: 'always' (LLM judge every run), 'sampled'
                   (LLM judge a fraction), 'assertions-only' (deterministic
                   checks only). Whatever the policy, a FAILING run is always
                   LLM-judged — that rule lives in the worker, not the schema.

Plus one status: 'evaluating' — the gap between conversation end (slot
freed) and verdict written. ALTER on the partitioned parent propagates to
every partition; the check constraints are parent-level, so drop + re-add.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE runs "
        "ADD COLUMN transcript jsonb NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute(
        "ALTER TABLE runs "
        "ADD COLUMN observations jsonb NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE runs "
        "ADD COLUMN judge_policy text NOT NULL DEFAULT 'always'"
    )
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT runs_judge_policy_check "
        "CHECK (judge_policy IN ('always','sampled','assertions-only'))"
    )
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_status_check")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT runs_status_check "
        "CHECK (status IN ('created','queued','running','evaluating',"
        "'correlating','completed','failed','cancelled'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_status_check")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT runs_status_check "
        "CHECK (status IN ('created','queued','running','correlating',"
        "'completed','failed','cancelled'))"
    )
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_judge_policy_check")
    op.execute("ALTER TABLE runs DROP COLUMN judge_policy")
    op.execute("ALTER TABLE runs DROP COLUMN observations")
    op.execute("ALTER TABLE runs DROP COLUMN transcript")
