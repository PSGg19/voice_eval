"""Deterministic assertions. These veto the LLM judge, so their edge cases are
load-bearing: a false failure blocks a good run, a false pass defeats the point.
"""
from __future__ import annotations

import pytest

from vjudge.core.models import AssertionSpec, CallResult, EvaluationResult
from vjudge.engine.assertions import FAILED, INCONCLUSIVE, PASSED, evaluate_assertions


def _by_name(report):
    return {outcome.name: outcome for outcome in report.outcomes}


def _tools(*names):
    return [{"function": {"name": n}} for n in names]


# ----------------------------------------------------------------- no-op cases

@pytest.mark.parametrize("spec", [None, AssertionSpec()])
def test_no_spec_produces_no_outcomes(spec):
    report = evaluate_assertions(spec)
    assert report.outcomes == []
    assert report.failed is False
    assert report.summary() == "no assertions"


def test_only_declared_assertions_are_reported():
    """An unspecified assertion must not appear as a vacuous pass."""
    report = evaluate_assertions(
        AssertionSpec(must_visit_modules=["A"]),
        modules_visited=["A"], tools_observed=True,
    )
    assert list(_by_name(report)) == ["must_visit_modules"]


# -------------------------------------------------------------------- modules

def test_must_visit_modules_passes_regardless_of_order():
    report = evaluate_assertions(
        AssertionSpec(must_visit_modules=["AUTH", "GREETINGS"]),
        modules_visited=["GREETINGS", "AUTH"],
    )
    assert _by_name(report)["must_visit_modules"].status == PASSED


def test_must_visit_modules_names_what_was_missed():
    report = evaluate_assertions(
        AssertionSpec(must_visit_modules=["AUTH", "EXPLAIN_DUE"]),
        modules_visited=["GREETINGS", "AUTH"],
    )
    outcome = _by_name(report)["must_visit_modules"]
    assert outcome.status == FAILED
    assert "EXPLAIN_DUE" in outcome.detail
    assert "AUTH" in outcome.detail  # shows what WAS visited


def test_unobserved_modules_are_inconclusive_not_failed():
    """A voice run observes no modules; that is not a violation."""
    report = evaluate_assertions(
        AssertionSpec(must_visit_modules=["AUTH"]), modules_visited=None
    )
    assert _by_name(report)["must_visit_modules"].status == INCONCLUSIVE
    assert report.failed is False


def test_must_not_visit_modules_fails_on_a_hit():
    report = evaluate_assertions(
        AssertionSpec(must_not_visit_modules=["CARD_COLLECTION"]),
        modules_visited=["GREETINGS", "CARD_COLLECTION"],
    )
    assert _by_name(report)["must_not_visit_modules"].status == FAILED


def test_must_not_visit_modules_passes_on_an_empty_run():
    """"never reached card collection" is supported by having visited nothing."""
    report = evaluate_assertions(
        AssertionSpec(must_not_visit_modules=["CARD_COLLECTION"]), modules_visited=[]
    )
    assert _by_name(report)["must_not_visit_modules"].status == PASSED


# ---------------------------------------------------------------------- tools

def test_must_call_tools_passes_when_present():
    report = evaluate_assertions(
        AssertionSpec(must_call_tools=["get_account"]),
        tool_calls=_tools("get_account"), tools_observed=True,
    )
    assert _by_name(report)["must_call_tools"].status == PASSED


def test_must_call_tools_fails_when_absent():
    report = evaluate_assertions(
        AssertionSpec(must_call_tools=["get_account"]),
        tool_calls=_tools("get_balance"), tools_observed=True,
    )
    outcome = _by_name(report)["must_call_tools"]
    assert outcome.status == FAILED
    assert "get_account" in outcome.detail


def test_unobserved_tools_make_a_positive_assertion_inconclusive():
    """The text runtime never returns tool_calls. Treating the empty list as
    "called nothing" would fail every run that simply had no OSV token."""
    report = evaluate_assertions(
        AssertionSpec(must_call_tools=["get_account"]),
        tool_calls=[], tools_observed=False,
    )
    outcome = _by_name(report)["must_call_tools"]
    assert outcome.status == INCONCLUSIVE
    assert "VJUDGE_OSV_ACCESS_TOKEN" in outcome.detail
    assert report.failed is False


def test_a_safety_assertion_is_not_a_free_pass_without_evidence():
    """"it never called pay_with_card" is unproven when we never looked. An
    unproven safety claim must not read as PASS."""
    report = evaluate_assertions(
        AssertionSpec(must_not_call_tools=["pay_with_card"]),
        tool_calls=[], tools_observed=False,
    )
    assert _by_name(report)["must_not_call_tools"].status == INCONCLUSIVE


def test_a_safety_violation_is_reported_even_when_tools_were_not_fully_observed():
    """If we saw the forbidden call, that is conclusive whatever the flag says."""
    report = evaluate_assertions(
        AssertionSpec(must_not_call_tools=["pay_with_card"]),
        tool_calls=_tools("pay_with_card"), tools_observed=False,
    )
    outcome = _by_name(report)["must_not_call_tools"]
    assert outcome.status == FAILED
    assert "pay_with_card" in outcome.detail


def test_tool_names_are_read_from_either_shape():
    report = evaluate_assertions(
        AssertionSpec(must_call_tools=["a", "b"]),
        tool_calls=[{"function": {"name": "a"}}, {"name": "b"}],
        tools_observed=True,
    )
    assert _by_name(report)["must_call_tools"].status == PASSED


def test_a_nameless_tool_call_does_not_crash():
    report = evaluate_assertions(
        AssertionSpec(must_call_tools=["a"]),
        tool_calls=[{"arguments": "{}"}, {"function": "not-a-dict"}],
        tools_observed=True,
    )
    assert _by_name(report)["must_call_tools"].status == FAILED


# ----------------------------------------------------------------- tool_order

def test_tool_order_allows_other_calls_in_between():
    """A subsequence, not an exact sequence — real flows interleave calls."""
    report = evaluate_assertions(
        AssertionSpec(tool_order=["first", "last"]),
        tool_calls=_tools("first", "middle", "last"), tools_observed=True,
    )
    assert _by_name(report)["tool_order"].status == PASSED


def test_tool_order_fails_when_reversed():
    report = evaluate_assertions(
        AssertionSpec(tool_order=["first", "last"]),
        tool_calls=_tools("last", "first"), tools_observed=True,
    )
    outcome = _by_name(report)["tool_order"]
    assert outcome.status == FAILED
    assert "stalled at 'last'" in outcome.detail


def test_tool_order_reports_where_it_stalled():
    report = evaluate_assertions(
        AssertionSpec(tool_order=["a", "b", "c"]),
        tool_calls=_tools("a", "b"), tools_observed=True,
    )
    assert "stalled at 'c'" in _by_name(report)["tool_order"].detail


def test_tool_order_handles_a_repeated_call():
    report = evaluate_assertions(
        AssertionSpec(tool_order=["a", "a"]),
        tool_calls=_tools("a", "x", "a"), tools_observed=True,
    )
    assert _by_name(report)["tool_order"].status == PASSED


def test_tool_order_needs_a_second_occurrence_for_a_repeat():
    report = evaluate_assertions(
        AssertionSpec(tool_order=["a", "a"]),
        tool_calls=_tools("a"), tools_observed=True,
    )
    assert _by_name(report)["tool_order"].status == FAILED


# ------------------------------------------------------------ tag-only silence

@pytest.mark.parametrize("count,limit,expected", [
    (0, 0, PASSED), (1, 0, FAILED), (1, 1, PASSED), (2, 1, FAILED),
])
def test_max_tag_only_turns(count, limit, expected):
    report = evaluate_assertions(
        AssertionSpec(max_tag_only_turns=limit),
        tag_only_bot_turns=[{"turn_index": i} for i in range(count)],
    )
    assert _by_name(report)["max_tag_only_turns"].status == expected


def test_zero_is_a_real_limit_not_a_missing_value():
    """`max_tag_only_turns: 0` must assert, not be treated as unset."""
    report = evaluate_assertions(
        AssertionSpec(max_tag_only_turns=0),
        tag_only_bot_turns=[{"turn_index": 1}],
    )
    assert report.failed is True


def test_unobserved_tag_only_turns_are_inconclusive():
    report = evaluate_assertions(
        AssertionSpec(max_tag_only_turns=0), tag_only_bot_turns=None
    )
    assert _by_name(report)["max_tag_only_turns"].status == INCONCLUSIVE


# ----------------------------------------------------------- report + the veto

def test_summary_counts_each_status():
    report = evaluate_assertions(
        AssertionSpec(must_visit_modules=["MISSING"], must_call_tools=["t"],
                      must_not_visit_modules=["NOPE"]),
        modules_visited=["GREETINGS"], tool_calls=[], tools_observed=False,
    )
    assert report.summary() == "1 passed, 1 FAILED, 1 inconclusive"
    assert [o.name for o in report.failures] == ["must_visit_modules"]
    assert [o.name for o in report.inconclusive] == ["must_call_tools"]


def test_a_failed_assertion_vetoes_a_passing_llm_verdict():
    """The whole point: structural facts outrank the judge's opinion."""
    result = CallResult(flow_name="f", target="+1")
    result.evaluation = EvaluationResult(
        overall_passed=True, overall_reasoning="the call read beautifully"
    )
    assert result.passed is True

    result.assertion_outcomes = [
        {"name": "must_call_tools", "status": "failed", "detail": "never called get_account"}
    ]
    assert result.passed is False


def test_an_inconclusive_assertion_does_not_veto():
    result = CallResult(flow_name="f", target="+1")
    result.evaluation = EvaluationResult(overall_passed=True, overall_reasoning="ok")
    result.assertion_outcomes = [
        {"name": "must_call_tools", "status": "inconclusive", "detail": "no token"}
    ]
    assert result.passed is True


def test_assertions_cannot_rescue_a_failed_llm_verdict():
    """The veto is one-directional — passing assertions do not override a FAIL."""
    result = CallResult(flow_name="f", target="+1")
    result.evaluation = EvaluationResult(
        overall_passed=False, overall_reasoning="fabricated a balance"
    )
    result.assertion_outcomes = [
        {"name": "must_call_tools", "status": "passed", "detail": "all present"}
    ]
    assert result.passed is False


def test_the_veros_ib2_stall_fails_on_three_independent_grounds():
    """Regression anchor for the real defect, from measured data: the bot asked
    for the SSN, replied with a bare <LANG:en-US>, and never reached EXPLAIN_DUE.
    """
    report = evaluate_assertions(
        AssertionSpec(
            must_visit_modules=["AUTH", "EXPLAIN_DUE"],
            tool_order=["get_details_by_phone_number", "get_account_payment_options"],
            max_tag_only_turns=0,
        ),
        modules_visited=["GREETINGS", "AUTH"],
        tool_calls=_tools("get_details_by_phone_number"),
        tag_only_bot_turns=[{"turn_index": 4, "tags": ["<LANG:en-US>"]}],
        tools_observed=True,
    )
    assert {o.name for o in report.failures} == {
        "must_visit_modules", "tool_order", "max_tag_only_turns"
    }
