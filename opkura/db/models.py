"""SQLAlchemy models for the V3 store (P3.1).

The DDL of record is the alembic migration (partitioned tables and partial
indexes read better as SQL); these mappings are how the app talks to it. The
integration tests exercise every model against the migrated schema, so drift
between the two fails a test rather than a production insert.

Design notes that answer "why is it like this":

* `runs` is RANGE-partitioned by `created_at` (monthly). Postgres requires
  the partition key in every unique constraint on a partitioned table, so the
  primary key is (run_uuid, created_at) — and a plain UNIQUE(idempotency_key)
  is impossible on `runs` itself. Global idempotency therefore lives in the
  small unpartitioned `run_idempotency` table: insert the key there FIRST
  (P3.4's row-before-work), let the PK conflict be the dedupe, then insert the
  run row. One conflict target, no partition arithmetic.

* Snapshots (`vars_snapshot`, `config_snapshot`) are copies, not references.
  Editing a variable set after a run must not change what the run says it ran
  with — that is the PRD's explicability requirement, tested explicitly.

* `scenarios` are versioned and immutable once run: (key, version) is unique,
  and `first_run_at` latches on first execution. Enforcement is app-level
  (the API refuses updates when set) — a trigger can come later if a second
  writer ever appears.

* `targets` carry the pre-flight facts (P3.7): `account_count` feeds the
  customer-data guard, `post_call_enabled` feeds the S3 refusal, and
  `provider_config` names the LiveKit project the DID lives on (P3.10 —
  e.g. `livekit-skittl-2` for +918062123187).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Runs the API/queue considers not-yet-settled. Shared with the partial index
# in the migration; keep the two lists identical.
IN_FLIGHT_STATUSES = ("created", "queued", "running", "correlating")

RUN_STATUSES = IN_FLIGHT_STATUSES + ("completed", "failed", "cancelled")


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


def _now() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Target(Base):
    __tablename__ = "targets"
    __table_args__ = (
        UniqueConstraint("region", "phone_number", name="targets_region_phone_key"),
        CheckConstraint("region IN ('us','in','ca')", name="targets_region_check"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    phone_number: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_uuid: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    assistant_id: Mapped[int | None] = mapped_column(Integer)
    assistant_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    client_uuid: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # LiveKit project the DID is provisioned on (OSV telephony_providerconfigs
    # name) — the P3.10 finer key, informational until that decision lands.
    provider_config: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Pre-flight facts (P3.7). account_count > 0 without allow_customer_data
    # is a refusal; post_call_enabled=True is the S3 refusal on text.
    account_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    post_call_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    allow_customer_data: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class VariableSet(Base):
    __tablename__ = "variable_sets"
    __table_args__ = (
        UniqueConstraint("client", "name", name="variable_sets_client_name_key"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    client: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    vars: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # Per-key source/trust, the shape core/variables.py resolves — stored so a
    # run's snapshot can say where each value came from.
    provenance: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Asserted by a human reviewer: these values identify no real person.
    # core/variables.py voids this trust the moment anything is overridden.
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class Scenario(Base):
    __tablename__ = "scenarios"
    __table_args__ = (
        UniqueConstraint("key", "version", name="scenarios_key_version_key"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    key: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # The full spec — persona/evaluation_prompt templates, metrics, assert,
    # llm/stt/tts, transport, region… — exactly what the YAML carried, as data.
    body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("targets.id"))
    variable_set_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("variable_sets.id")
    )
    source_path: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Latched by the first run; an update to a latched row is refused (422).
    first_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class RunBatch(Base):
    __tablename__ = "run_batches"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="created")
    created_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created','queued','running','evaluating',"
            "'correlating','completed','failed','cancelled')",
            name="runs_status_check",
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    run_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # Partition key — hence part of the primary key (Postgres requirement).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=text("now()")
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("run_batches.id"))
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    scenario_version: Mapped[int | None] = mapped_column(Integer)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    region: Mapped[str] = mapped_column(Text, nullable=False, server_default="us")
    transport: Mapped[str] = mapped_column(Text, nullable=False, server_default="text")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="created")
    passed: Mapped[bool | None] = mapped_column(Boolean)
    # Copies, not references — see module docstring.
    vars_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    config_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    verdict: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # P4.3 — the judge is its own stage. The conversation worker persists
    # transcript + observations here and frees its slot; the evaluate worker
    # reads them back on a different process. judge_policy: 'always' |
    # 'sampled' | 'assertions-only' (failures are always LLM-judged, whatever
    # the policy — that rule lives in the worker).
    transcript: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    observations: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    judge_policy: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="always"
    )
    error: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    interaction_uuid: Mapped[str | None] = mapped_column(Text)
    call_sid: Mapped[str | None] = mapped_column(Text)
    artifact_keys: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunIdempotency(Base):
    """Global idempotency for run creation — see module docstring for why this
    is not a unique index on `runs`."""

    __tablename__ = "run_idempotency"

    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)
    run_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = _now()
