"""Phase 2 Task 4 — render `CampaignResult` to JSON + Markdown.

Both formats live side-by-side in the campaign log folder:

    logs/campaigns/<slug>-<utc>-<id8>/
        campaign-report.json   ← machine-readable; for `--rerun-failed`
        campaign-report.md     ← human-readable; for sharing with managers

Pure rendering — no I/O of its own. The CLI orchestrator picks the
folder and writes the rendered strings. Splitting render from write
keeps these functions easy to snapshot-test.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from judge_it.core.models import CallResult, CallStatus, CampaignResult


def slugify(s: str) -> str:
    """Mirror logwriter.slugify so campaign folder names stay consistent."""
    normalized = unicodedata.normalize("NFKD", s)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")


def render_json(result: CampaignResult) -> str:
    """Render a CampaignResult to JSON.

    Each per-scenario entry includes: path, passed, status, duration,
    log_dir (so the reader can jump straight to artifacts), and the
    evaluator reasoning when present.
    """
    payload = {
        "campaign_name": result.campaign_name,
        "source_path": result.source_path,
        "started_at": _iso(result.started_at),
        "ended_at": _iso(result.ended_at),
        "duration_s": round(result.duration_s, 2),
        "total_scenarios": result.total_scenarios,
        "passed": result.passed,
        "failed": result.failed,
        "errored": result.errored,
        "pass_rate": round(result.pass_rate, 4),
        "fail_fast_triggered": result.fail_fast_triggered,
        "skipped_paths": list(result.skipped_paths),
        "results": [_render_call_entry(cr) for cr in result.call_results],
    }
    return json.dumps(payload, indent=2)


def render_markdown(result: CampaignResult) -> str:
    """Render a CampaignResult to Markdown — human-readable summary.

    Failed and errored scenarios get a per-scenario subsection with the
    evaluator reasoning (V2) or error message (setup-failed calls) so
    a manager skimming the report doesn't have to open per-call logs.
    """
    lines: list[str] = []
    lines.append(f"# Campaign Report: {result.campaign_name}")
    lines.append("")
    if result.source_path:
        lines.append(f"**Source:** `{result.source_path}`")
    lines.append(f"**Started:** {_iso(result.started_at)}")
    lines.append(f"**Ended:** {_iso(result.ended_at)}")
    lines.append(f"**Duration:** {result.duration_s:.1f}s")
    if result.fail_fast_triggered:
        lines.append("**fail_fast:** triggered (queue drained on first failure)")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Total scenarios | {result.total_scenarios} |")
    lines.append(f"| Passed | {result.passed} |")
    lines.append(f"| Failed | {result.failed} |")
    lines.append(f"| Errored | {result.errored} |")
    if result.skipped_paths:
        lines.append(f"| Skipped (fail_fast) | {len(result.skipped_paths)} |")
    lines.append(f"| Pass rate | {result.pass_rate * 100:.1f}% |")
    lines.append("")

    # Failures and errors get expanded sections so the reader doesn't
    # need to open each per-call log folder to triage.
    non_passing = [cr for cr in result.call_results if not cr.passed]
    if non_passing:
        lines.append(f"## Failed scenarios ({len(non_passing)})")
        lines.append("")
        for cr in non_passing:
            lines.append(f"### {cr.flow_name}")
            lines.append("")
            lines.append(f"- **Status:** `{cr.status.value}`")
            if cr.duration_ms is not None:
                lines.append(f"- **Duration:** {cr.duration_ms / 1000:.1f}s")
            if cr.log_dir:
                lines.append(f"- **Log directory:** `{cr.log_dir}`")
            if cr.evaluation is not None and cr.evaluation.overall_reasoning:
                lines.append(
                    f"- **Reasoning:** {cr.evaluation.overall_reasoning}"
                )
                if cr.evaluation.criteria:
                    lines.append("- **Criteria:**")
                    for c in cr.evaluation.criteria:
                        marker = "✅" if c.passed else "❌"
                        lines.append(f"  - {marker} `{c.name}` — {c.evidence}")
            if cr.error:
                lines.append(f"- **Error:** {cr.error}")
            lines.append("")

    if result.skipped_paths:
        lines.append(f"## Skipped scenarios ({len(result.skipped_paths)})")
        lines.append("")
        lines.append("Pulled-but-not-started due to `fail_fast`.")
        lines.append("")
        for p in result.skipped_paths:
            lines.append(f"- `{p}`")
        lines.append("")

    # Passing list is at the end since it's the boring part — useful
    # for confirming nothing got accidentally skipped, but not the
    # first thing a triager needs to see.
    passing = [cr for cr in result.call_results if cr.passed]
    if passing:
        lines.append(f"## Passed scenarios ({len(passing)})")
        lines.append("")
        for cr in passing:
            dur = (
                f" ({cr.duration_ms / 1000:.1f}s)"
                if cr.duration_ms is not None
                else ""
            )
            lines.append(f"- `{cr.flow_name}`{dur}")
        lines.append("")

    return "\n".join(lines)


def campaign_log_dir(
    root: Path, campaign_name: str, started_at: datetime, call_id: str
) -> Path:
    """Compute the canonical log folder path for one campaign run.

    Pattern: `logs/campaigns/<slug>-<utc>-<id8>/` — mirrors the
    per-call log folder shape from CallLogWriter so engineers can
    navigate the two trees with the same intuition.
    """
    utc = started_at.strftime("%Y-%m-%dT%H%M%SZ")
    slug = slugify(campaign_name) or "campaign"
    return root / f"campaign-{slug}-{utc}-{call_id[:8]}"


def _render_call_entry(cr: CallResult) -> dict:
    entry: dict = {
        "scenario_path": cr.flow_name,
        "passed": cr.passed,
        "status": cr.status.value,
        "duration_ms": cr.duration_ms,
        "log_dir": cr.log_dir,
        "call_sid": cr.call_sid or None,
    }
    if cr.evaluation is not None:
        entry["evaluation"] = {
            "overall_passed": cr.evaluation.overall_passed,
            "overall_reasoning": cr.evaluation.overall_reasoning,
            "criteria": [
                {
                    "id": c.id,
                    "name": c.name,
                    "passed": c.passed,
                    "evidence": c.evidence,
                }
                for c in cr.evaluation.criteria
            ],
        }
    if cr.error:
        entry["error"] = cr.error
    return entry


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    # Preserve timezone offset; CallLogWriter does the same.
    return dt.isoformat()
