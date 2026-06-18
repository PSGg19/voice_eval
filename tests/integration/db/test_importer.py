"""P3.6 — the YAML importer against a real store.

PRD acceptance: import of the full corpus completes with zero data loss —
re-exporting a sampled scenario reproduces semantically identical YAML.
Semantic identity is `to_jsonable`: YAML dates become ISO strings (JSONB has
no date type); everything else survives byte-for-byte.
"""
from __future__ import annotations

import uuid

import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opkura.db.importer import (
    export_scenario_yaml,
    import_paths,
    summarize,
    to_jsonable,
)
from opkura.db.models import Scenario, VariableSet

VALID = """\
name: Importer smoke
target: "+15559990001"
vars:
  customer_name: Iris Crawford
  amount_due: "125.40"
persona: "You are {{ vars.customer_name }}."
evaluation_prompt: "Judge the greeting to {{ vars.customer_name }}."
max_turns: 5
"""

SAME_VARS_OTHER_SCENARIO = VALID.replace("Importer smoke", "Importer smoke 2")

CAMPAIGN = """\
name: a campaign
scenarios:
  - happy.yaml
"""

BROKEN = """\
name: broken
target: not-a-number
vars: {}
persona: hi
evaluation_prompt: judge
"""


@pytest.fixture()
def corpus(tmp_path):
    client = tmp_path / f"acme-{uuid.uuid4().hex[:6]}"
    client.mkdir()
    (client / "happy.yaml").write_text(VALID)
    (client / "second.yaml").write_text(SAME_VARS_OTHER_SCENARIO)
    (client / "campaign.yaml").write_text(CAMPAIGN)
    (client / "broken.yaml").write_text(BROKEN)
    return tmp_path


def test_import_reports_every_file(migrated_engine, corpus):
    with Session(migrated_engine) as s:
        reports = import_paths(s, [corpus])
    by_name = {r.path.rsplit("/", 1)[-1]: r for r in reports}
    assert by_name["happy.yaml"].action == "imported"
    assert by_name["happy.yaml"].version == 1
    assert "/" in by_name["happy.yaml"].key  # key is client-relative
    assert by_name["second.yaml"].action == "imported"
    assert by_name["campaign.yaml"].action == "skipped_campaign"
    assert by_name["broken.yaml"].action == "failed"
    assert "lint" in by_name["broken.yaml"].detail


def test_identical_vars_dedupe_into_one_variable_set(migrated_engine, corpus):
    with Session(migrated_engine) as s:
        reports = import_paths(s, [corpus])
        imported = [r for r in reports if r.action == "imported"]
        rows = [
            s.execute(
                select(Scenario).where(
                    Scenario.key == r.key, Scenario.version == r.version
                )
            ).scalar_one()
            for r in imported
        ]
        vs_ids = {row.variable_set_id for row in rows}
        assert len(vs_ids) == 1 and None not in vs_ids
        vs = s.get(VariableSet, vs_ids.pop())
        assert vs.vars["customer_name"] == "Iris Crawford"
        assert vs.is_synthetic is False  # importers do not grant trust


def test_reimport_is_idempotent_and_edits_become_versions(migrated_engine, corpus):
    with Session(migrated_engine) as s:
        import_paths(s, [corpus])
    with Session(migrated_engine) as s:
        second = import_paths(s, [corpus])
    counts = summarize(second)
    assert counts.get("unchanged") == 2 and "imported" not in counts

    # Edit → new version, old version untouched.
    edited = next(p for p in corpus.rglob("happy.yaml"))
    edited.write_text(VALID.replace("125.40", "999.99"))
    with Session(migrated_engine) as s:
        third = import_paths(s, [corpus])
        report = next(r for r in third if r.path.endswith("happy.yaml"))
        assert report.action == "new_version" and report.version == 2
        versions = s.execute(
            select(func.count()).select_from(Scenario).where(Scenario.key == report.key)
        ).scalar_one()
        assert versions == 2


def test_yaml_dates_survive_as_iso_strings(migrated_engine, tmp_path):
    d = tmp_path / "cli"
    d.mkdir()
    (d / "dated.yaml").write_text(
        VALID.replace('amount_due: "125.40"', "due_date: 2026-09-15")
    )
    with Session(migrated_engine) as s:
        [report] = [r for r in import_paths(s, [tmp_path]) if r.key]
        row = s.execute(
            select(Scenario).where(Scenario.key == report.key)
        ).scalar_one()
        assert row.body["vars"]["due_date"] == "2026-09-15"


def test_export_round_trips(migrated_engine, corpus):
    with Session(migrated_engine) as s:
        reports = import_paths(s, [corpus])
        r = next(r for r in reports if r.path.endswith("happy.yaml"))
        row = s.execute(
            select(Scenario).where(Scenario.key == r.key, Scenario.version == r.version)
        ).scalar_one()
        body = dict(row.body)
    # export → parse → identical to what is stored…
    assert yaml.safe_load(export_scenario_yaml(body)) == body
    # …and what is stored is the coerced original file, nothing lost.
    original = yaml.safe_load(VALID)
    assert body == to_jsonable(original)


# ------------------------------------------------------------- full corpus

def test_full_corpus_imports_with_zero_data_loss(migrated_engine):
    """The PRD acceptance test, against the real clients/ tree when present."""
    from pathlib import Path

    clients = Path("clients")
    if not clients.is_dir():
        pytest.skip("clients/ corpus not present on this checkout")

    with Session(migrated_engine) as s:
        reports = import_paths(s, [clients])
        counts = summarize(reports)
        imported = [r for r in reports if r.action in ("imported", "new_version")]
        assert len(imported) >= 1000, counts
        # Known-broken files only (4 pre-existing + the example with a missing
        # BCD reference); anything beyond that is a new regression.
        assert counts.get("failed", 0) <= 6, [
            (r.path, r.detail) for r in reports if r.action == "failed"
        ]

        # Zero data loss, sampled: stored body == coerced original, and the
        # YAML export re-loads to exactly the stored body.
        sample = imported[:: max(1, len(imported) // 20)][:20]
        for r in sample:
            row = s.execute(
                select(Scenario).where(
                    Scenario.key == r.key, Scenario.version == r.version
                )
            ).scalar_one()
            original = yaml.safe_load(Path(row.source_path).read_text())
            assert row.body == to_jsonable(original), row.source_path
            assert yaml.safe_load(export_scenario_yaml(row.body)) == row.body
