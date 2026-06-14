from __future__ import annotations

from opkura.core.models import (
    CallResult,
    CallStatus,
    EvaluationCriterion,
    EvaluationResult,
    StepResult,
    StepStatus,
)


def test_passed_uses_evaluation_when_present():
    """V2 calls — passed must reflect evaluator verdict, not the empty steps list."""
    ev = EvaluationResult(
        overall_passed=False,
        overall_reasoning="bot failed FDCPA disclosure",
        criteria=[EvaluationCriterion(id=1, name="fdcpa", passed=False, evidence="turn 3")],
    )
    r = CallResult(flow_name="v2-scenario", target="+1", evaluation=ev)
    assert r.passed is False


def test_passed_true_when_evaluation_passes():
    ev = EvaluationResult(overall_passed=True, overall_reasoning="all good")
    r = CallResult(flow_name="v2-scenario", target="+1", evaluation=ev)
    assert r.passed is True


def test_passed_false_for_empty_steps_v1():
    """V1 call that errored before any step ran. Previously `all([]) is True` → pass."""
    r = CallResult(flow_name="v1-scenario", target="+1", status=CallStatus.ERROR)
    assert r.steps == []
    assert r.evaluation is None
    assert r.passed is False


def test_passed_true_for_all_passing_steps_v1():
    r = CallResult(
        flow_name="v1-scenario",
        target="+1",
        steps=[
            StepResult(step_index=0, status=StepStatus.PASSED, bot_transcript="hi"),
            StepResult(step_index=1, status=StepStatus.PASSED, bot_transcript="ok"),
        ],
    )
    assert r.evaluation is None
    assert r.passed is True


def test_passed_false_if_any_step_failed_v1():
    r = CallResult(
        flow_name="v1-scenario",
        target="+1",
        steps=[
            StepResult(step_index=0, status=StepStatus.PASSED, bot_transcript="hi"),
            StepResult(step_index=1, status=StepStatus.FAILED, bot_transcript="bad"),
        ],
    )
    assert r.passed is False


def test_evaluation_overrides_step_status():
    """If both steps and evaluation exist, evaluation wins.
    This matches the PRD: 'overall_passed drives pass/fail status.'
    """
    ev = EvaluationResult(overall_passed=False, overall_reasoning="LLM said no")
    r = CallResult(
        flow_name="hybrid",
        target="+1",
        steps=[StepResult(step_index=0, status=StepStatus.PASSED, bot_transcript="x")],
        evaluation=ev,
    )
    assert r.passed is False
