"""Unit tests for the Phase 2 campaign reporter — JSON + Markdown rendering."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from opkura.core.models import (
    CallResult,
    CallStatus,
    CampaignResult,
    EvaluationCriterion,
    EvaluationResult,
)
from opkura.orchestrator.campaign_reporter import (
    campaign_log_dir,
    render_json,
    render_markdown,
    slugify,
)


def _make_call_result(
    path: str,
    passed: bool = True,
    status: CallStatus = CallStatus.COMPLETED,
    error: str | None = None,
    with_eval: bool = True,
    eval_reasoning: str = "test reasoning",
    log_dir: str | None = None,
    duration_s: float = 1.5,
) -> CallResult:
    started = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    cr = CallResult(
        flow_name=path,
        target="+15555550100",
        call_sid="CAtest",
        started_at=started,
    )
    cr.finish(status, error=error)
    # finish() sets ended_at = now; override for deterministic snapshot.
    cr.ended_at = started + timedelta(seconds=duration_s)
    if with_eval:
        cr.evaluation = EvaluationResult(
            overall_passed=passed,
            overall_reasoning=eval_reasoning,
        )
    if log_dir is not None:
        cr.log_dir = log_dir
    return cr


def _make_campaign_result(
    name: str = "test",
    call_results: list[CallResult] | None = None,
    fail_fast_triggered: bool = False,
    skipped_paths: list[str] | None = None,
) -> CampaignResult:
    started = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    results = call_results or []
    passed = sum(1 for cr in results if cr.passed)
    failed = sum(
        1 for cr in results if not cr.passed and cr.status == CallStatus.COMPLETED
    )
    errored = sum(
        1 for cr in results if cr.status in (CallStatus.ERROR, CallStatus.FAILED)
    )
    return CampaignResult(
        campaign_name=name,
        source_path=f"campaigns/{slugify(name)}.yaml",
        started_at=started,
        ended_at=started + timedelta(seconds=10),
        duration_s=10.0,
        total_scenarios=len(results) + len(skipped_paths or []),
        passed=passed,
        failed=failed,
        errored=errored,
        pass_rate=passed / max(len(results), 1),
        call_results=results,
        fail_fast_triggered=fail_fast_triggered,
        skipped_paths=skipped_paths or [],
    )


# ----- slugify -----


def test_slugify_basic():
    assert slugify("Resurgent · Auth full sweep") == "resurgent-auth-full-sweep"


def test_slugify_empty_string_returns_empty():
    assert slugify("") == ""


# ----- campaign_log_dir -----


def test_campaign_log_dir_includes_slug_and_id_prefix():
    started = datetime(2026, 5, 15, 12, 30, 45, tzinfo=timezone.utc)
    path = campaign_log_dir(
        root=Path("logs/campaigns"),
        campaign_name="My Campaign",
        started_at=started,
        call_id="abc123def4567890",
    )
    assert path == Path("logs/campaigns/campaign-my-campaign-2026-05-15T123045Z-abc123de")


# ----- render_json -----


def test_render_json_all_pass(snapshot=None):
    """Minimal happy-path JSON output: 2 passing scenarios."""
    result = _make_campaign_result(
        name="happy",
        call_results=[
            _make_call_result("a.yaml", passed=True, log_dir="logs/a"),
            _make_call_result("b.yaml", passed=True, log_dir="logs/b"),
        ],
    )
    rendered = render_json(result)
    parsed = json.loads(rendered)

    assert parsed["campaign_name"] == "happy"
    assert parsed["total_scenarios"] == 2
    assert parsed["passed"] == 2
    assert parsed["failed"] == 0
    assert parsed["errored"] == 0
    assert parsed["pass_rate"] == 1.0
    assert parsed["fail_fast_triggered"] is False
    assert parsed["skipped_paths"] == []
    assert len(parsed["results"]) == 2
    assert parsed["results"][0]["scenario_path"] == "a.yaml"
    assert parsed["results"][0]["passed"] is True
    assert parsed["results"][0]["log_dir"] == "logs/a"


def test_render_json_with_failure_includes_eval():
    cr_fail = _make_call_result(
        "fail.yaml",
        passed=False,
        eval_reasoning="bot hung up early",
        log_dir="logs/fail",
    )
    cr_fail.evaluation.criteria = [
        EvaluationCriterion(
            id=1, name="auth_first_attempt", passed=False, evidence="..."
        ),
    ]
    result = _make_campaign_result(call_results=[cr_fail])
    parsed = json.loads(render_json(result))

    entry = parsed["results"][0]
    assert entry["passed"] is False
    assert entry["evaluation"]["overall_reasoning"] == "bot hung up early"
    assert entry["evaluation"]["criteria"][0]["name"] == "auth_first_attempt"
    assert entry["evaluation"]["criteria"][0]["passed"] is False


def test_render_json_with_skipped_paths():
    result = _make_campaign_result(
        call_results=[_make_call_result("a.yaml", passed=False)],
        fail_fast_triggered=True,
        skipped_paths=["b.yaml", "c.yaml"],
    )
    parsed = json.loads(render_json(result))
    assert parsed["fail_fast_triggered"] is True
    assert parsed["skipped_paths"] == ["b.yaml", "c.yaml"]
    assert parsed["total_scenarios"] == 3


def test_render_json_includes_error_for_errored_call():
    cr_err = _make_call_result(
        "err.yaml",
        passed=False,
        status=CallStatus.ERROR,
        error="setup failed: bad api key",
        with_eval=False,
    )
    result = _make_campaign_result(call_results=[cr_err])
    parsed = json.loads(render_json(result))
    assert parsed["results"][0]["error"] == "setup failed: bad api key"
    assert parsed["results"][0]["status"] == "error"


# ----- render_markdown -----


def test_render_markdown_includes_title_and_summary():
    result = _make_campaign_result(
        call_results=[_make_call_result("a.yaml", passed=True)],
    )
    md = render_markdown(result)
    assert md.startswith("# Campaign Report: test")
    assert "## Summary" in md
    assert "| Total scenarios | 1 |" in md
    assert "| Passed | 1 |" in md
    assert "| Pass rate | 100.0% |" in md


def test_render_markdown_failed_section_shows_eval_reasoning():
    cr_fail = _make_call_result(
        "fail.yaml",
        passed=False,
        eval_reasoning="bot hung up early",
        log_dir="logs/fail",
    )
    result = _make_campaign_result(call_results=[cr_fail])
    md = render_markdown(result)
    assert "## Failed scenarios (1)" in md
    assert "fail.yaml" in md
    assert "bot hung up early" in md
    assert "logs/fail" in md


def test_render_markdown_skipped_section_only_when_skipped():
    # No skipped → no section
    result_no_skip = _make_campaign_result(
        call_results=[_make_call_result("a.yaml", passed=True)],
    )
    assert "## Skipped scenarios" not in render_markdown(result_no_skip)

    # With skipped → section present
    result_skipped = _make_campaign_result(
        call_results=[_make_call_result("a.yaml", passed=False)],
        fail_fast_triggered=True,
        skipped_paths=["b.yaml"],
    )
    md_skip = render_markdown(result_skipped)
    assert "## Skipped scenarios (1)" in md_skip
    assert "`b.yaml`" in md_skip
    assert "fail_fast" in md_skip


def test_render_markdown_passed_section_lists_each():
    result = _make_campaign_result(
        call_results=[
            _make_call_result("a.yaml", passed=True),
            _make_call_result("b.yaml", passed=True),
        ],
    )
    md = render_markdown(result)
    assert "## Passed scenarios (2)" in md
    assert "`a.yaml`" in md
    assert "`b.yaml`" in md


def test_render_markdown_errored_call_shows_error_message():
    cr_err = _make_call_result(
        "err.yaml",
        passed=False,
        status=CallStatus.ERROR,
        error="setup failed: bad api key",
        with_eval=False,
    )
    result = _make_campaign_result(call_results=[cr_err])
    md = render_markdown(result)
    assert "setup failed: bad api key" in md


def test_render_markdown_handles_fail_fast_banner():
    result = _make_campaign_result(
        call_results=[_make_call_result("a.yaml", passed=False)],
        fail_fast_triggered=True,
        skipped_paths=["b.yaml"],
    )
    md = render_markdown(result)
    assert "**fail_fast:** triggered" in md
