"""Unit tests for judge_it.core.campaign_loader.

Covers schema validation (required keys, type checks, bounds),
glob expansion (relative paths, globs, deduplication), and
error-message quality for the common failure modes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from judge_it.core.campaign_loader import DEFAULT_MAX_CONCURRENCY, load


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _minimal_scenario(path: Path, name: str = "scenario-x") -> None:
    """Write a syntactically valid V2 scenario YAML at `path`."""
    _write(
        path,
        f"""\
name: "{name}"
target: "+15555550100"
vars: {{}}
persona: "test persona"
evaluation_prompt: "test eval"
""",
    )


# --- Valid campaigns ---


def test_load_minimal_campaign(tmp_path: Path):
    _minimal_scenario(tmp_path / "scenarios" / "a.yaml", name="A")
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: "minimal"
scenarios:
  - scenarios/a.yaml
""",
    )

    result = load(campaign)
    assert result.name == "minimal"
    assert result.max_concurrency == DEFAULT_MAX_CONCURRENCY  # default (50)
    assert result.fail_fast is False  # default
    assert len(result.resolved_scenarios) == 1
    assert result.resolved_scenarios[0].endswith("scenarios/a.yaml")
    assert result.source_path == str(campaign)


def test_load_with_all_optional_fields(tmp_path: Path):
    _minimal_scenario(tmp_path / "scenarios" / "a.yaml")
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: "full"
description: "all the things"
max_concurrency: 5
fail_fast: true
scenarios:
  - scenarios/a.yaml
""",
    )

    result = load(campaign)
    assert result.description == "all the things"
    assert result.max_concurrency == 5
    assert result.fail_fast is True


# --- Glob expansion ---


def test_glob_expands_to_all_matching_scenarios(tmp_path: Path):
    for n in ("a", "b", "c"):
        _minimal_scenario(tmp_path / "scenarios" / f"{n}.yaml", name=n)
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: "glob test"
scenarios:
  - "scenarios/*.yaml"
""",
    )

    result = load(campaign)
    assert len(result.resolved_scenarios) == 3
    # Glob results are sorted (deterministic ordering)
    basenames = [Path(p).name for p in result.resolved_scenarios]
    assert basenames == sorted(basenames)


def test_explicit_paths_and_glob_dedupe(tmp_path: Path):
    _minimal_scenario(tmp_path / "scenarios" / "a.yaml")
    _minimal_scenario(tmp_path / "scenarios" / "b.yaml")
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: "dedupe"
scenarios:
  - scenarios/a.yaml
  - "scenarios/*.yaml"
  - scenarios/a.yaml
""",
    )

    result = load(campaign)
    # 2 unique files, even though a.yaml appears 3 times across entries
    assert len(result.resolved_scenarios) == 2


def test_glob_matches_nothing_raises(tmp_path: Path):
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: "no match"
scenarios:
  - "scenarios/*.yaml"
""",
    )
    with pytest.raises(ValueError, match="matched no scenario files"):
        load(campaign)


def test_explicit_missing_file_raises(tmp_path: Path):
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: "missing"
scenarios:
  - scenarios/nonexistent.yaml
""",
    )
    with pytest.raises(ValueError, match="scenario file not found"):
        load(campaign)


def test_absolute_glob_rejected(tmp_path: Path):
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: "abs glob"
scenarios:
  - "/abs/path/*.yaml"
""",
    )
    with pytest.raises(ValueError, match="must be relative"):
        load(campaign)


# --- Schema validation ---


def test_missing_required_keys(tmp_path: Path):
    campaign = tmp_path / "campaign.yaml"
    _write(campaign, "description: nope\n")
    with pytest.raises(ValueError, match="missing required key"):
        load(campaign)


def test_unknown_top_level_key(tmp_path: Path):
    _minimal_scenario(tmp_path / "scenarios" / "a.yaml")
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: "x"
scenarios:
  - scenarios/a.yaml
extra_field: oops
""",
    )
    with pytest.raises(ValueError, match="unknown top-level key"):
        load(campaign)


def test_empty_name_rejected(tmp_path: Path):
    _minimal_scenario(tmp_path / "scenarios" / "a.yaml")
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: ""
scenarios:
  - scenarios/a.yaml
""",
    )
    with pytest.raises(ValueError, match="`name` must be"):
        load(campaign)


def test_empty_scenarios_list_rejected(tmp_path: Path):
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: "x"
scenarios: []
""",
    )
    with pytest.raises(ValueError, match="non-empty list"):
        load(campaign)


def test_max_concurrency_below_one_rejected(tmp_path: Path):
    _minimal_scenario(tmp_path / "scenarios" / "a.yaml")
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: "x"
max_concurrency: 0
scenarios:
  - scenarios/a.yaml
""",
    )
    with pytest.raises(ValueError, match=">= 1"):
        load(campaign)


def test_max_concurrency_large_value_honored(tmp_path: Path):
    # No hard upper cap: an explicit large value is honored as-is (the caller
    # owns the Twilio / tunnel / RPM trade-off).
    _minimal_scenario(tmp_path / "scenarios" / "a.yaml")
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: "x"
max_concurrency: 100
scenarios:
  - scenarios/a.yaml
""",
    )
    result = load(campaign)
    assert result.max_concurrency == 100


def test_fail_fast_must_be_bool(tmp_path: Path):
    _minimal_scenario(tmp_path / "scenarios" / "a.yaml")
    campaign = tmp_path / "campaign.yaml"
    _write(
        campaign,
        """\
name: "x"
fail_fast: "yes"
scenarios:
  - scenarios/a.yaml
""",
    )
    with pytest.raises(ValueError, match="`fail_fast` must be a boolean"):
        load(campaign)


def test_non_dict_top_level_rejected(tmp_path: Path):
    campaign = tmp_path / "campaign.yaml"
    _write(campaign, "- not\n- a\n- mapping\n")
    with pytest.raises(ValueError, match="YAML mapping at top level"):
        load(campaign)


def test_invalid_yaml_syntax(tmp_path: Path):
    campaign = tmp_path / "campaign.yaml"
    # Unclosed bracket — actual syntax error PyYAML will reject.
    _write(campaign, "name: [unclosed\n")
    with pytest.raises(ValueError, match="failed to parse YAML"):
        load(campaign)
