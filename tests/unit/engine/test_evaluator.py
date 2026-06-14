from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opkura.engine.evaluator import Evaluator
from opkura.core.models import TranscriptEntry


def _mk_evaluator(*responses, temperature: float = 0.3) -> tuple[Evaluator, AsyncMock]:
    llm = AsyncMock()
    llm.provider = "gemini"
    llm.model = "gemini-2.5-pro"
    llm.complete_json = AsyncMock(side_effect=list(responses))
    # AsyncMock auto-creates last_usage as a truthy MagicMock, defeating the
    # `or {}` fallback in _record_llm_call. Real GeminiClient seeds None.
    llm.last_usage = None
    ev = Evaluator(
        evaluation_prompt="evaluate the call",
        llm=llm,
        temperature=temperature,
    )
    return ev, llm


def _transcript() -> list[TranscriptEntry]:
    return [
        TranscriptEntry(speaker="bot", text="Hello, Michelle here.", timestamp_ms=500),
        TranscriptEntry(speaker="caller", text="Hi, this is Iris.", timestamp_ms=2300),
        TranscriptEntry(speaker="bot", text="Please press 1.", timestamp_ms=4000),
        TranscriptEntry(speaker="caller", text="[DTMF: 1]", timestamp_ms=4800, dtmf="1"),
    ]


@pytest.mark.asyncio
async def test_evaluate_well_formed():
    ev, _ = _mk_evaluator(
        {
            "overall_passed": True,
            "overall_reasoning": "all checks passed",
            "criteria": [
                {"id": 1, "name": "greeting", "passed": True, "evidence": "turn 1"},
                {"id": 2, "name": "identity", "passed": True, "evidence": "turn 2"},
                {"id": 3, "name": "dtmf_sent", "passed": True, "evidence": "turn 4"},
            ],
            "metrics": {"bot_turns": 2, "caller_turns": 2},
        }
    )
    result = await ev.evaluate(_transcript())
    assert result.overall_passed is True
    assert result.overall_reasoning == "all checks passed"
    assert len(result.criteria) == 3
    assert all(isinstance(c.id, int) for c in result.criteria)
    assert all(isinstance(c.passed, bool) for c in result.criteria)
    assert result.criteria[0].name == "greeting"
    assert result.malformed is False


@pytest.mark.asyncio
async def test_evaluate_formats_transcript():
    ev, llm = _mk_evaluator(
        {"overall_passed": True, "overall_reasoning": "ok", "criteria": [], "metrics": {}}
    )
    await ev.evaluate(_transcript())

    user_msg = llm.complete_json.await_args.kwargs["messages"][0]["content"]
    assert "Turn 1 (bot," in user_msg
    assert "Turn 2 (caller," in user_msg
    assert "Turn 3 (bot," in user_msg
    assert "Turn 4 (caller," in user_msg
    # Closing paren disambiguates the DTMF metadata tag from the [DTMF: 1]
    # marker that appears inside the text field — we want to assert the
    # metadata path specifically.
    assert "DTMF: 1)" in user_msg
    assert "Hello, Michelle here." in user_msg
    assert "Hi, this is Iris." in user_msg
    # Timestamp formatting (ms → seconds with one decimal)
    assert "0.5s" in user_msg
    assert "4.8s" in user_msg


@pytest.mark.asyncio
async def test_evaluate_uses_evaluation_prompt_as_system():
    ev, llm = _mk_evaluator(
        {"overall_passed": True, "overall_reasoning": "ok", "criteria": [], "metrics": {}}
    )
    await ev.evaluate(_transcript())

    assert llm.complete_json.await_args.kwargs["system"] == "evaluate the call"


@pytest.mark.asyncio
async def test_evaluate_malformed_first_call_retries():
    ev, llm = _mk_evaluator(
        {"overall_reasoning": "missing overall_passed", "criteria": []},
        {
            "overall_passed": True,
            "overall_reasoning": "valid on retry",
            "criteria": [],
            "metrics": {},
        },
    )
    result = await ev.evaluate(_transcript())

    assert result.overall_passed is True
    assert result.overall_reasoning == "valid on retry"
    assert llm.complete_json.await_count == 2
    # Retry should include the strict suffix and zero temperature
    retry_kwargs = llm.complete_json.await_args_list[1].kwargs
    assert "STRICT JSON only" in retry_kwargs["system"]
    assert retry_kwargs["temperature"] == 0.0


@pytest.mark.asyncio
async def test_evaluate_malformed_both_calls_returns_synthetic():
    ev, _ = _mk_evaluator(
        {"missing": "fields"},
        {"still": "missing"},
    )
    result = await ev.evaluate(_transcript())

    assert result.overall_passed is False
    assert result.malformed is True
    assert "malformed" in result.overall_reasoning.lower()


@pytest.mark.asyncio
async def test_judgement_metrics_pass_through_but_counts_are_measured():
    """Metrics are no longer pure passthrough — counts are measured, not taken.

    The fixture transcript has 2 bot and 2 caller turns. An LLM claiming 12 and
    11 is exactly the failure mode this closes: plausible-looking arithmetic
    nobody checks. Judgement metrics the harness cannot measure
    (`compliance_violations`) still come from the model untouched.
    """
    ev, _ = _mk_evaluator(
        {
            "overall_passed": True,
            "overall_reasoning": "ok",
            "criteria": [],
            "metrics": {"bot_turns": 12, "caller_turns": 11, "compliance_violations": 0},
        }
    )
    result = await ev.evaluate(_transcript())

    assert result.metrics["compliance_violations"] == 0  # not ours, untouched
    assert result.metrics["bot_turns"] == 2  # measured
    assert result.metrics["caller_turns"] == 2
    assert result.metrics["total_turns"] == 4
    # the model's claim is kept, not discarded — evaluator accuracy is otherwise
    # entirely unmeasured
    assert result.metrics["bot_turns_reported_by_llm"] == 12
    assert result.metrics["caller_turns_reported_by_llm"] == 11


@pytest.mark.asyncio
async def test_an_agreeing_llm_leaves_no_disagreement_marker():
    ev, _ = _mk_evaluator(
        {
            "overall_passed": True,
            "overall_reasoning": "ok",
            "criteria": [],
            "metrics": {"bot_turns": 2, "caller_turns": 2},
        }
    )
    result = await ev.evaluate(_transcript())

    assert result.metrics["bot_turns"] == 2
    assert not any(k.endswith("_reported_by_llm") for k in result.metrics)


@pytest.mark.asyncio
async def test_a_string_count_is_not_treated_as_a_disagreement():
    """Models return "2" as often as 2. That is agreement, not a discrepancy."""
    ev, _ = _mk_evaluator(
        {
            "overall_passed": True,
            "overall_reasoning": "ok",
            "criteria": [],
            "metrics": {"bot_turns": " 2 ", "caller_turns": "2"},
        }
    )
    result = await ev.evaluate(_transcript())

    assert result.metrics["bot_turns"] == 2
    assert not any(k.endswith("_reported_by_llm") for k in result.metrics)


@pytest.mark.asyncio
async def test_no_criteria_is_valid():
    ev, _ = _mk_evaluator(
        {"overall_passed": True, "overall_reasoning": "ok", "criteria": []}
    )
    result = await ev.evaluate(_transcript())
    assert result.overall_passed is True
    assert result.criteria == []
    # Metrics are measured now, so an LLM that reports none still yields counts.
    assert result.metrics["bot_turns"] == 2
    assert result.metrics["caller_turns"] == 2


@pytest.mark.asyncio
async def test_measurements_survive_a_malformed_verdict():
    """The transcript is still countable when the model's JSON is garbage — and
    those runs are the ones that most need explaining."""
    ev, _ = _mk_evaluator({"no": "verdict"}, {"still": "broken"})
    result = await ev.evaluate(_transcript())

    assert result.malformed is True
    assert result.metrics["bot_turns"] == 2
    assert result.metrics["total_turns"] == 4


@pytest.mark.asyncio
async def test_string_false_does_not_flip_to_passed():
    """If the LLM returns "false" as a string (loose JSON), the verdict must NOT flip to passed.
    bool('false') is True in Python — this test guards the _to_bool helper.
    """
    ev, _ = _mk_evaluator(
        {
            "overall_passed": "false",
            "overall_reasoning": "model said no but as string",
            "criteria": [
                {"id": 1, "name": "fail-as-string", "passed": "false", "evidence": "x"},
                {"id": 2, "name": "true-as-string", "passed": "true", "evidence": "y"},
            ],
            "metrics": {},
        }
    )
    result = await ev.evaluate(_transcript())
    assert result.overall_passed is False
    assert result.criteria[0].passed is False
    assert result.criteria[1].passed is True


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "yes", "1"])
@pytest.mark.asyncio
async def test_string_truthy_values_pass(value):
    """Whitelist must include 'true'/'yes'/'1' — guards against accidentally
    shrinking _to_bool's truthy set in the evaluator.
    """
    ev, _ = _mk_evaluator(
        {
            "overall_passed": value,
            "overall_reasoning": "passes",
            "criteria": [
                {"id": 1, "name": "x", "passed": value, "evidence": ""},
            ],
            "metrics": {},
        }
    )
    result = await ev.evaluate(_transcript())
    assert result.overall_passed is True
    assert result.criteria[0].passed is True


@pytest.mark.asyncio
async def test_malformed_audit_trail_preserves_both_responses():
    """Both the first and retry LLM payloads must end up in raw_response when
    both attempts succeed network-wise but parse fails — that's the data triage
    needs to debug a malformed verdict.
    """
    malformed_first = {"first_marker": "no_overall_passed_field"}
    malformed_second = {"second_marker": "also_broken"}
    ev, _ = _mk_evaluator(malformed_first, malformed_second)

    result = await ev.evaluate(_transcript())
    assert result.malformed is True
    assert "first_marker" in result.raw_response
    assert "no_overall_passed_field" in result.raw_response
    assert "second_marker" in result.raw_response
    assert "also_broken" in result.raw_response


@pytest.mark.asyncio
async def test_empty_transcript_short_circuits():
    """Empty transcript → synthetic verdict without burning an LLM call."""
    ev, llm = _mk_evaluator()
    result = await ev.evaluate([])
    assert result.overall_passed is False
    assert "empty transcript" in result.overall_reasoning.lower()
    assert result.malformed is False
    assert llm.complete_json.await_count == 0


@pytest.mark.asyncio
async def test_attached_logwriter_records_evaluator_llm_call():
    """When a logwriter is attached, evaluator records its LLM call with
    role='evaluator' so llm_calls.json captures both halves of the conversation.
    """
    recorded = []

    class StubLogwriter:
        def record_llm_call(self, **kw):
            recorded.append(kw)

        def record_bot_response_latency(self, ms):
            pass

    ev, llm = _mk_evaluator(
        {"overall_passed": True, "overall_reasoning": "ok", "criteria": [], "metrics": {}}
    )
    llm.last_usage = {"input_tokens": 200, "output_tokens": 80}
    ev.attach_logwriter(StubLogwriter())

    await ev.evaluate(_transcript())

    assert len(recorded) == 1
    entry = recorded[0]
    assert entry["role"] == "evaluator"
    assert entry["turn"] == 0  # end-of-call sentinel
    assert entry["input_tokens"] == 200
    assert entry["output_tokens"] == 80
    assert entry["error"] is None


@pytest.mark.asyncio
async def test_attached_logwriter_records_both_evaluator_attempts():
    """First-attempt-then-retry: both LLM attempts must appear in the log.
    Each entry has output=<actual response>; parse failure of the response
    is reflected in the recorded output (the bad dict), not as an error.
    """
    recorded = []

    class StubLogwriter:
        def record_llm_call(self, **kw):
            recorded.append(kw)

        def record_bot_response_latency(self, ms):
            pass

    bad_first = {"missing": "fields"}  # parses to dict but _parse raises on overall_passed
    good_retry = {"overall_passed": True, "overall_reasoning": "ok-on-retry", "criteria": []}
    ev, _ = _mk_evaluator(bad_first, good_retry)
    ev.attach_logwriter(StubLogwriter())

    await ev.evaluate(_transcript())

    assert len(recorded) == 2
    assert all(e["role"] == "evaluator" for e in recorded)
    # Both LLM calls succeeded at the network level — no error field.
    assert recorded[0]["error"] is None
    assert recorded[1]["error"] is None
    # The first response's actual content is preserved for triage of why
    # the parse failed.
    assert recorded[0]["output"] == bad_first
    assert recorded[1]["output"] == good_retry


@pytest.mark.asyncio
async def test_attached_logwriter_records_call_level_error():
    """If complete_json itself raises (network blip), the entry records error."""
    recorded = []

    class StubLogwriter:
        def record_llm_call(self, **kw):
            recorded.append(kw)

        def record_bot_response_latency(self, ms):
            pass

    llm = AsyncMock()
    llm.provider = "gemini"
    llm.model = "gemini-2.5-pro"
    llm.complete_json = AsyncMock(side_effect=[ConnectionError("upstream 500"), ConnectionError("upstream 500 again")])
    llm.last_usage = None  # prevent MagicMock leak into recorded token fields
    ev = Evaluator(evaluation_prompt="evaluate", llm=llm, temperature=0.0)
    ev.attach_logwriter(StubLogwriter())

    result = await ev.evaluate(_transcript())

    assert len(recorded) == 2
    for e in recorded:
        assert e["role"] == "evaluator"
        assert e["output"] is None
        assert e["error"] is not None
        assert "500" in e["error"]
    # Both attempts failed → malformed verdict synthesized.
    assert result.malformed is True


# Note: runner-side wiring (_run_evaluator returns malformed on timeout,
# uses EVALUATOR_TEMPERATURE not scenario.llm.temperature, etc.) is covered
# in tests/test_runner.py. This file stays focused on Evaluator class behavior.


@pytest.mark.parametrize(
    "raw_id,expected",
    [
        (1, 1),
        ("2", 2),
        ("C3", 3),
        ("criterion_4", 4),
        (5.0, 5),
        ("no-digits-here", 1),  # falls back to 1-based position
        (None, 1),
        ("", 1),
        (True, 1),  # bool is an int subclass but is not an id
    ],
)
@pytest.mark.asyncio
async def test_non_numeric_criterion_id_does_not_discard_the_verdict(raw_id, expected):
    """A criterion id is a label — it must never cost us the evaluation.

    `int(c["id"])` used to raise on anything non-numeric, and _parse's caller
    treats any exception as "output malformed": the whole verdict was thrown
    away (including a good `overall_passed`), a retry LLM call was burned, and
    the run was reported malformed. Seen twice on live Veros runs.
    """
    ev, llm = _mk_evaluator(
        {
            "overall_passed": True,
            "overall_reasoning": "the bot refused to fabricate",
            "criteria": [
                {"id": raw_id, "name": "no_fabrication", "passed": True, "evidence": "turn 3"}
            ],
        }
    )
    result = await ev.evaluate(_transcript())

    assert result.malformed is False
    assert result.overall_passed is True
    assert llm.complete_json.await_count == 1, "must not burn a retry on a label"
    assert result.criteria[0].id == expected
    assert result.criteria[0].passed is True


@pytest.mark.asyncio
async def test_missing_criterion_name_falls_back_to_position():
    ev, llm = _mk_evaluator(
        {
            "overall_passed": False,
            "overall_reasoning": "gate skipped",
            "criteria": [
                {"id": 1, "passed": True, "evidence": "a"},
                {"id": 2, "name": None, "passed": False, "evidence": "b"},
            ],
        }
    )
    result = await ev.evaluate(_transcript())

    assert result.malformed is False
    assert llm.complete_json.await_count == 1
    assert [c.name for c in result.criteria] == ["criterion 1", "criterion 2"]
    assert [c.passed for c in result.criteria] == [True, False]


@pytest.mark.asyncio
async def test_missing_passed_is_still_malformed():
    """`passed` is the verdict, not a label — its absence must still retry."""
    ev, llm = _mk_evaluator(
        {"overall_passed": True, "criteria": [{"id": 1, "name": "x", "evidence": "y"}]},
        {"overall_passed": True, "criteria": [{"id": 1, "name": "x", "evidence": "y"}]},
    )
    result = await ev.evaluate(_transcript())

    assert result.malformed is True
    assert llm.complete_json.await_count == 2


# ------------------------------------------- observed facts (text transport only)

def _obs(ev, **kw):
    kw.setdefault("modules_visited", None)
    kw.setdefault("tool_calls", None)
    kw.setdefault("tag_only_bot_turns", None)
    return ev._format_observations(**kw)


@pytest.mark.asyncio
async def test_a_voice_run_prompt_is_unchanged():
    """No observations -> not one byte added. Voice must be unaffected."""
    ev, llm = _mk_evaluator({"overall_passed": True, "criteria": []})
    await ev.evaluate(_transcript())
    sent = llm.complete_json.await_args.kwargs["messages"][0]["content"]
    assert "OBSERVED FACTS" not in sent
    assert _obs(ev) == ""


@pytest.mark.asyncio
async def test_modules_and_tools_reach_the_evaluator_prompt():
    """The gap this closes: the 1740 probe passed a `flow_depth` criterion on the
    evidence "modules_visited was not provided" — the data existed, unused."""
    ev, llm = _mk_evaluator({"overall_passed": True, "criteria": []})
    await ev.evaluate(
        _transcript(),
        modules_visited=["GREETINGS", "AUTH"],
        tool_calls=[{"function": {"name": "get_details_by_phone_number"}, "_turn_index": 1}],
        tag_only_bot_turns=[],
    )
    sent = llm.complete_json.await_args.kwargs["messages"][0]["content"]

    assert "OBSERVED FACTS" in sent
    assert "GREETINGS -> AUTH" in sent
    assert "get_details_by_phone_number @turn 1" in sent
    assert "empty_or_tag_only_turns: 0" in sent
    # the transcript itself must survive alongside them
    assert "Hello, Michelle here." in sent


def test_zero_tag_only_turns_is_stated_not_omitted():
    """Silence invites the model to invent a number; 0 is an assertable fact."""
    ev, _ = _mk_evaluator()
    assert "empty_or_tag_only_turns: 0" in _obs(ev, tag_only_bot_turns=[])


def test_tag_only_turns_are_labelled_as_a_bot_defect():
    ev, _ = _mk_evaluator()
    out = _obs(ev, tag_only_bot_turns=[{"turn_index": 4, "tags": ["<LANG:en-US>"]}])
    assert "turn 4: <LANG:en-US>" in out
    assert "reached the caller as SILENCE" in out
    assert "not a harness defect" in out


def test_empty_modules_list_says_none_recorded_rather_than_nothing():
    ev, _ = _mk_evaluator()
    assert "(none recorded)" in _obs(ev, modules_visited=[])


@pytest.mark.parametrize(
    "call,expected",
    [
        ({"function": {"name": "f"}, "_turn_index": 2}, "f @turn 2"),
        ({"name": "g", "_turn_index": 0}, "g @turn 0"),
        ({"name": "h"}, "h"),                      # no turn index -> no @turn
        ({"arguments": "{}"}, "(unnamed)"),        # must not crash on a nameless call
        ({"function": "not-a-dict"}, "(unnamed)"),
    ],
)
def test_tool_call_shapes_render_without_crashing(call, expected):
    ev, _ = _mk_evaluator()
    assert expected in _obs(ev, tool_calls=[call])


def test_observations_are_framed_as_measurements_not_suggestions():
    ev, _ = _mk_evaluator()
    out = _obs(ev, modules_visited=["A"])
    assert "Do not contradict them" in out
    assert "not report a value as unavailable" in out


@pytest.mark.asyncio
async def test_one_name_per_measurement_across_both_prompt_blocks():
    """The facts block and the metrics block must not name the same measurement
    differently — two spellings in one prompt invite the model to guess which
    key to emit, and 1108 scenarios ask for `empty_or_tag_only_turns`."""
    ev, llm = _mk_evaluator({"overall_passed": True, "criteria": []})
    await ev.evaluate(
        _transcript(),
        modules_visited=["GREETINGS"],
        tool_calls=[],
        tag_only_bot_turns=[],
    )
    sent = llm.complete_json.await_args.kwargs["messages"][0]["content"]
    assert "empty_or_tag_only_bot_turns" not in sent
    assert "empty_or_tag_only_turns" in sent
