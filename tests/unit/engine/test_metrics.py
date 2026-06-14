"""Measured metrics. `bot_turns`/`caller_turns` are requested by 1108 scenarios,
so this is arithmetic the corpus was outsourcing to a language model."""
from __future__ import annotations

import logging

import pytest

from opkura.core.models import TranscriptEntry
from opkura.engine.metrics import (
    COMPUTED_KEYS,
    compute_metrics,
    find_duplicated_text,
    merge_metrics,
)


def _t(speaker, text, ms=0):
    return TranscriptEntry(speaker=speaker, text=text, timestamp_ms=ms)


def _convo():
    return [
        _t("bot", "Hello, Michelle here."),
        _t("caller", "Hi."),
        _t("bot", "Your balance is forty dollars."),
        _t("caller", "Thanks."),
    ]


# ------------------------------------------------------------------- turn counts

def test_counts_turns_by_speaker():
    m = compute_metrics(_convo())
    assert m["bot_turns"] == 2
    assert m["caller_turns"] == 2
    assert m["total_turns"] == 4


def test_an_empty_transcript_counts_zero_not_crash():
    m = compute_metrics([])
    assert m["bot_turns"] == m["caller_turns"] == m["total_turns"] == 0


def test_an_unknown_speaker_counts_toward_total_only():
    m = compute_metrics([_t("system", "note")])
    assert m["total_turns"] == 1
    assert m["bot_turns"] == 0 and m["caller_turns"] == 0


# ------------------------------------------------- voice runs gain no false keys

def test_a_voice_run_gets_counts_but_no_text_only_keys():
    """"0 modules visited" would be a lie; modules are unobservable over audio."""
    m = compute_metrics(_convo())
    assert "bot_turns" in m
    for absent in ("modules_visited", "module_count", "tool_call_count",
                   "empty_or_tag_only_turns"):
        assert absent not in m


def test_a_text_run_reports_modules_as_the_string_the_prompts_expect():
    m = compute_metrics(_convo(), modules_visited=["GREETINGS", "AUTH"])
    assert m["modules_visited"] == "GREETINGS -> AUTH"
    assert m["module_count"] == 2


def test_an_observed_but_empty_module_list_says_none():
    m = compute_metrics(_convo(), modules_visited=[])
    assert m["modules_visited"] == "none"
    assert m["module_count"] == 0


def test_tool_calls_are_counted_and_ordered():
    m = compute_metrics(
        _convo(),
        tool_calls=[
            {"function": {"name": "get_details_by_phone_number"}},
            {"name": "get_balance"},
            {"arguments": "{}"},
        ],
    )
    assert m["tool_call_count"] == 3
    assert m["tool_calls_in_order"] == (
        "get_details_by_phone_number, get_balance, (unnamed)"
    )


def test_tag_only_turns_are_counted():
    assert compute_metrics(_convo(), tag_only_bot_turns=[])["empty_or_tag_only_turns"] == 0
    assert compute_metrics(
        _convo(), tag_only_bot_turns=[{"turn_index": 2}]
    )["empty_or_tag_only_turns"] == 1


# ----------------------------------------------------------- duplicated bot text

def test_detects_a_sentence_repeated_across_bot_turns():
    """The OSV `response_text` accumulation bug (text_bots.py:72) repeats earlier
    sentences 2x/3x/5x/7x. Seen on two different Veros bots."""
    convo = [
        _t("bot", "Your current balance is four hundred dollars."),
        _t("caller", "ok"),
        _t("bot", "Your current balance is four hundred dollars. Would you like to pay?"),
    ]
    assert find_duplicated_text(convo) == "Your current balance is four hundred dollars."
    assert compute_metrics(convo)["duplicated_text_seen"].startswith("Your current balance")


def test_detects_a_sentence_repeated_within_one_turn():
    convo = [_t("bot", "Please confirm your date of birth. Please confirm your date of birth.")]
    assert find_duplicated_text(convo) is not None


def test_a_clean_conversation_reports_none():
    assert find_duplicated_text(_convo()) is None
    assert compute_metrics(_convo())["duplicated_text_seen"] == "none"


def test_short_repeated_acknowledgements_are_not_duplication():
    """"Thank you." twice is normal speech, not the accumulation bug."""
    convo = [_t("bot", "Thank you."), _t("bot", "Thank you."), _t("bot", "Sure.")]
    assert find_duplicated_text(convo) is None


def test_caller_repetition_is_not_bot_duplication():
    long = "I already told you my account number is one two three four five."
    convo = [_t("caller", long), _t("caller", long)]
    assert find_duplicated_text(convo) is None


def test_punctuation_and_spacing_differences_still_match():
    convo = [
        _t("bot", "Your payment of forty dollars is scheduled."),
        _t("bot", "your   payment of forty dollars is scheduled"),
    ]
    assert find_duplicated_text(convo) is not None


def test_empty_bot_text_is_skipped():
    assert find_duplicated_text([_t("bot", ""), _t("bot", "")]) is None


# -------------------------------------------------------------------- merge rules

def test_measured_values_win_over_reported_ones():
    merged = merge_metrics({"bot_turns": 12}, {"bot_turns": 2})
    assert merged["bot_turns"] == 2
    assert merged["bot_turns_reported_by_llm"] == 12


def test_a_disagreement_is_logged_loudly(caplog):
    with caplog.at_level(logging.WARNING, logger="opkura.engine.metrics"):
        merge_metrics({"bot_turns": 12}, {"bot_turns": 2})
    assert "metric disagreement" in caplog.text
    assert "LLM said 12, measured 2" in caplog.text


def test_judgement_metrics_are_left_alone():
    merged = merge_metrics({"required_gates_passed": "4/4"}, {"bot_turns": 2})
    assert merged["required_gates_passed"] == "4/4"
    assert merged["bot_turns"] == 2


@pytest.mark.parametrize("reported", [2, "2", " 2 ", "2 "])
def test_equivalent_values_are_not_flagged_as_disagreement(reported):
    merged = merge_metrics({"bot_turns": reported}, {"bot_turns": 2})
    assert "bot_turns_reported_by_llm" not in merged


def test_missing_llm_metrics_still_yields_measurements():
    assert merge_metrics({}, {"bot_turns": 2})["bot_turns"] == 2
    assert merge_metrics(None, {"bot_turns": 2})["bot_turns"] == 2


def test_every_computed_key_is_declared():
    """COMPUTED_KEYS documents what the harness owns; drift makes it a lie."""
    produced = set(
        compute_metrics(
            _convo(), modules_visited=["A"], tool_calls=[], tag_only_bot_turns=[]
        )
    )
    assert produced == set(COMPUTED_KEYS)


# --------------------------------------------------------- declared metric specs

from opkura.core.models import MetricSpec  # noqa: E402
from opkura.engine.metrics import (  # noqa: E402
    metrics_to_request,
    render_metric_spec,
    validate_metrics,
)


def _specs():
    return [
        MetricSpec(key="bot_turns", type="integer"),  # measured by the harness
        MetricSpec(key="required_gates_passed", description="required gates passed",
                   type="string", example="4/4"),
        MetricSpec(key="caller_had_to_repeat", description="did the caller repeat",
                   type="boolean", example=False),
        MetricSpec(key="unanswered", description="questions never answered",
                   type="array", example=["due date"], required=False),
    ]


def test_measured_metrics_are_not_asked_of_the_model():
    """The "drop it if it only confuses the AI" rule, applied mechanically."""
    asked = [s.key for s in metrics_to_request(_specs())]
    assert "bot_turns" not in asked
    assert asked == ["required_gates_passed", "caller_had_to_repeat", "unanswered"]


def test_the_rendered_contract_carries_type_meaning_and_example():
    out = render_metric_spec(metrics_to_request(_specs()))
    assert "required_gates_passed (string): required gates passed" in out
    assert "[e.g. '4/4']" in out
    assert "caller_had_to_repeat (boolean)" in out
    assert "unanswered (array, optional)" in out  # required: false is shown
    assert "bot_turns" not in out
    # the type rules the models actually get wrong
    assert 'a boolean is true/false (not "yes")' in out


def test_no_specs_renders_nothing():
    """Scenarios without `metrics:` must be byte-identical to before."""
    assert render_metric_spec([]) == ""


def test_a_spec_with_no_description_or_example_still_renders_its_type():
    out = render_metric_spec([MetricSpec(key="x", type="integer")])
    assert "- x (integer)" in out


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True), (False, False),
        ("true", True), ("yes", True), ("1", True), ("Y", True),
        ("false", False), ("no", False), ("0", False), (" NO ", False),
    ],
)
def test_boolean_near_misses_are_coerced(raw, expected):
    out, problems = validate_metrics(
        [MetricSpec(key="b", type="boolean")], {"b": raw}
    )
    assert out["b"] is expected
    assert problems == []


def test_an_uncoercible_boolean_is_reported_and_kept():
    out, problems = validate_metrics(
        [MetricSpec(key="b", type="boolean")], {"b": "probably"}
    )
    assert out["b"] == "probably"  # never silently dropped
    assert any("expected boolean" in p for p in problems)


@pytest.mark.parametrize("raw,expected", [(3, 3), ("3", 3), (" 3 ", 3), (3.7, 3)])
def test_integer_near_misses_are_coerced(raw, expected):
    out, problems = validate_metrics([MetricSpec(key="n", type="integer")], {"n": raw})
    assert out["n"] == expected and problems == []


def test_a_boolean_is_not_accepted_as_an_integer():
    """`bool` is an int subclass in Python; True is not a count."""
    out, problems = validate_metrics([MetricSpec(key="n", type="integer")], {"n": True})
    assert any("expected integer, got boolean" in p for p in problems)


def test_a_non_numeric_integer_is_reported():
    _, problems = validate_metrics([MetricSpec(key="n", type="integer")], {"n": "many"})
    assert any("expected integer" in p for p in problems)


def test_a_string_where_an_array_was_declared_is_reported():
    out, problems = validate_metrics(
        [MetricSpec(key="a", type="array")], {"a": "none"}
    )
    assert out["a"] == "none"
    assert any("expected array" in p for p in problems)


def test_a_tuple_is_accepted_as_an_array():
    out, problems = validate_metrics([MetricSpec(key="a", type="array")], {"a": ("x",)})
    assert out["a"] == ["x"] and problems == []


def test_a_missing_required_metric_is_reported():
    _, problems = validate_metrics([MetricSpec(key="r", type="string")], {})
    assert any("r: missing" in p for p in problems)


def test_a_missing_optional_metric_is_silent():
    _, problems = validate_metrics(
        [MetricSpec(key="o", type="string", required=False)], {}
    )
    assert problems == []


def test_undeclared_metrics_are_reported_but_preserved():
    out, problems = validate_metrics(
        [MetricSpec(key="declared", type="string")],
        {"declared": "x", "surprise": 1},
    )
    assert out["surprise"] == 1
    assert any("undeclared metrics returned: surprise" in p for p in problems)


def test_measured_keys_are_neither_validated_nor_flagged_as_undeclared():
    """They are about to be overridden, so the model's value is irrelevant."""
    out, problems = validate_metrics(
        [MetricSpec(key="bot_turns", type="integer")],
        {"bot_turns": "nonsense", "caller_turns": 4},
    )
    assert problems == []
    assert out["bot_turns"] == "nonsense"  # merge_metrics replaces it


def test_the_llm_disagreement_marker_is_not_flagged_as_undeclared():
    _, problems = validate_metrics(
        [MetricSpec(key="d", type="string")],
        {"d": "x", "bot_turns_reported_by_llm": 12},
    )
    assert problems == []
