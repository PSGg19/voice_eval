"""Deterministic assertions over what the bot actually did.

JudgeIt's verdict has, until now, come entirely from an LLM judge whose accuracy
is unmeasured. For anything *structural* that is the wrong instrument: whether
the bot called `get_account` before quoting a balance is a fact about the run,
not a matter of opinion, and a judge that reads the transcript can only infer it.

The text transport observes `current_module` per turn, and the conversations API
supplies the tool calls, so these questions are now answerable exactly. This
module answers them and the answer **vetoes** the LLM's verdict — a run where the
bot skipped a required auth module is a failure no matter how convincing the
conversation read.

Three properties that matter more than they look:

* **Inconclusive is not the same as failed.** Tool calls come only from the
  post-run OSV fetch; with no token that list is empty, and treating empty as
  "called nothing" would fail every tool assertion for a configuration reason.
  Unobserved data yields INCONCLUSIVE, which does not fail the run but is
  reported loudly.
* **`must_not_*` is checked even when the data is thin**, because it is the
  safety direction. "Never settle a payment" must not be quietly skipped.
* **`tool_order` is a subsequence, not an exact sequence.** Real flows interleave
  other calls; requiring adjacency would make the assertion useless.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from judge_it.core.models import AssertionSpec

logger = logging.getLogger(__name__)

PASSED = "passed"
FAILED = "failed"
INCONCLUSIVE = "inconclusive"


@dataclass
class AssertionOutcome:
    name: str
    status: str  # PASSED | FAILED | INCONCLUSIVE
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAILED


@dataclass
class AssertionReport:
    outcomes: list[AssertionOutcome] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """True when any assertion failed. Inconclusive ones do not fail a run."""
        return any(o.failed for o in self.outcomes)

    @property
    def failures(self) -> list[AssertionOutcome]:
        return [o for o in self.outcomes if o.failed]

    @property
    def inconclusive(self) -> list[AssertionOutcome]:
        return [o for o in self.outcomes if o.status == INCONCLUSIVE]

    def summary(self) -> str:
        if not self.outcomes:
            return "no assertions"
        counts = {PASSED: 0, FAILED: 0, INCONCLUSIVE: 0}
        for o in self.outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        parts = [f"{counts[PASSED]} passed"]
        if counts[FAILED]:
            parts.append(f"{counts[FAILED]} FAILED")
        if counts[INCONCLUSIVE]:
            parts.append(f"{counts[INCONCLUSIVE]} inconclusive")
        return ", ".join(parts)


def _tool_names(tool_calls: list[dict]) -> list[str]:
    names: list[str] = []
    for call in tool_calls or []:
        fn = call.get("function")
        name = fn.get("name") if isinstance(fn, dict) else call.get("name")
        if name:
            names.append(str(name))
    return names


def _is_subsequence(needles: list[str], haystack: list[str]) -> tuple[bool, Optional[str]]:
    """Do `needles` appear in `haystack` in order (gaps allowed)?

    Returns (ok, first-unmatched-name).
    """
    it = iter(haystack)
    for needle in needles:
        if not any(candidate == needle for candidate in it):
            return False, needle
    return True, None


def evaluate_assertions(
    spec: Optional[AssertionSpec],
    *,
    modules_visited: Optional[list[str]] = None,
    tool_calls: Optional[list[dict]] = None,
    tag_only_bot_turns: Optional[list[dict]] = None,
    tools_observed: bool = False,
) -> AssertionReport:
    """Check `spec` against one run's observations.

    `tools_observed` says whether the OSV fetch actually supplied the tool list.
    Without it, tool assertions cannot distinguish "called nothing" from "we
    never looked", so positive tool assertions go INCONCLUSIVE rather than
    failing on a missing token.
    """
    report = AssertionReport()
    if spec is None or spec.is_empty:
        return report

    modules = list(modules_visited or [])
    tools = _tool_names(tool_calls or [])

    # ---- modules -----------------------------------------------------------
    if spec.must_visit_modules:
        if modules_visited is None:
            report.outcomes.append(AssertionOutcome(
                "must_visit_modules", INCONCLUSIVE,
                "modules were not observed on this transport",
            ))
        else:
            missing = [m for m in spec.must_visit_modules if m not in modules]
            report.outcomes.append(AssertionOutcome(
                "must_visit_modules",
                FAILED if missing else PASSED,
                f"never visited: {', '.join(missing)} (visited: "
                f"{' -> '.join(modules) or 'none'})" if missing
                else f"all present in {' -> '.join(modules)}",
            ))

    if spec.must_not_visit_modules:
        # Checked even when the list is empty: "never reached card collection" is
        # a safety claim, and an empty observation supports it.
        hit = [m for m in spec.must_not_visit_modules if m in modules]
        report.outcomes.append(AssertionOutcome(
            "must_not_visit_modules",
            FAILED if hit else PASSED,
            f"forbidden module(s) visited: {', '.join(hit)}" if hit
            else "none of the forbidden modules were visited",
        ))

    # ---- tools -------------------------------------------------------------
    if spec.must_call_tools:
        if not tools_observed:
            report.outcomes.append(AssertionOutcome(
                "must_call_tools", INCONCLUSIVE,
                "tool calls were not retrieved — the text runtime does not "
                "return them, so this needs JUDGE_IT_OSV_ACCESS_TOKEN set for "
                "the post-run conversations fetch",
            ))
        else:
            missing = [t for t in spec.must_call_tools if t not in tools]
            report.outcomes.append(AssertionOutcome(
                "must_call_tools",
                FAILED if missing else PASSED,
                f"never called: {', '.join(missing)} (called: "
                f"{', '.join(tools) or 'none'})" if missing
                else f"all present in {', '.join(tools)}",
            ))

    if spec.must_not_call_tools:
        # Deliberately evaluated even when tools were not observed: with no data
        # the claim "it never called pay_with_card" is unproven, so this is
        # INCONCLUSIVE rather than a free pass. A safety assertion must never
        # silently succeed for lack of evidence.
        hit = [t for t in spec.must_not_call_tools if t in tools]
        if hit:
            report.outcomes.append(AssertionOutcome(
                "must_not_call_tools", FAILED,
                f"forbidden tool(s) called: {', '.join(hit)}",
            ))
        elif not tools_observed:
            report.outcomes.append(AssertionOutcome(
                "must_not_call_tools", INCONCLUSIVE,
                "tool calls were not retrieved, so this cannot be confirmed — "
                "an unproven safety assertion is not a pass",
            ))
        else:
            report.outcomes.append(AssertionOutcome(
                "must_not_call_tools", PASSED,
                "none of the forbidden tools were called",
            ))

    if spec.tool_order:
        if not tools_observed:
            report.outcomes.append(AssertionOutcome(
                "tool_order", INCONCLUSIVE,
                "tool calls were not retrieved",
            ))
        else:
            ok, stuck = _is_subsequence(spec.tool_order, tools)
            report.outcomes.append(AssertionOutcome(
                "tool_order",
                PASSED if ok else FAILED,
                f"expected {' -> '.join(spec.tool_order)} as a subsequence; "
                f"got {', '.join(tools) or 'none'}"
                + (f" — stalled at {stuck!r}" if stuck else ""),
            ))

    # ---- silence -----------------------------------------------------------
    if spec.max_tag_only_turns is not None:
        if tag_only_bot_turns is None:
            report.outcomes.append(AssertionOutcome(
                "max_tag_only_turns", INCONCLUSIVE,
                "tag-only turns were not retrieved",
            ))
        else:
            count = len(tag_only_bot_turns)
            report.outcomes.append(AssertionOutcome(
                "max_tag_only_turns",
                FAILED if count > spec.max_tag_only_turns else PASSED,
                f"{count} tag-only bot turn(s), limit {spec.max_tag_only_turns}"
                + (
                    "; each reached the caller as silence"
                    if count > spec.max_tag_only_turns else ""
                ),
            ))

    for outcome in report.outcomes:
        if outcome.failed:
            logger.error("assertion FAILED — %s: %s", outcome.name, outcome.detail)
        elif outcome.status == INCONCLUSIVE:
            logger.warning(
                "assertion inconclusive — %s: %s", outcome.name, outcome.detail
            )
    return report
