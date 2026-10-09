from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from judge_it.core.config import Settings
from judge_it.core.models import CallResult, CallStatus, EvaluationResult, LLMConfig, Scenario
from judge_it.orchestrator.runner import EVALUATOR_TEMPERATURE, EVALUATOR_TIMEOUT_S, TestRunner, detect_schema


def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "scenario.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def _runner_with(settings_overrides: dict) -> TestRunner:
    # Force empty API key defaults so the test isn't influenced by developer
    # env vars (JUDGE_IT_GEMINI_API_KEY etc.) — init kwargs win over env
    # vars in pydantic-settings, so this guarantees isolation.
    base = dict(
        twilio_account_sid="x",
        twilio_auth_token="x",
        twilio_phone_number="x",
        deepgram_api_key="x",
        gemini_api_key="",
        anthropic_api_key="",
        openai_api_key="",
    )
    base.update(settings_overrides)
    settings = Settings(**base, _env_file=None)
    with patch("judge_it.orchestrator.runner.CallManager"), \
         patch("judge_it.orchestrator.runner.DeepgramTTS"), \
         patch("judge_it.orchestrator.runner.PgResultStore"):
        # persist_result=False matters more than the PgResultStore patch
        # above: `TestRunner.store` is LAZY, so the store is constructed at
        # save time — after this patch context has exited — and every run
        # that reached persistence wrote a junk `t` row into the REAL
        # database (one per suite run, "gemini not configured", 0s).
        return TestRunner(settings, persist_result=False)


def test_detect_schema_rejects_hybrid_yaml(tmp_path):
    path = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        steps:
          - say: hi
        """,
    )
    with pytest.raises(ValueError, match="mixes V1.*V2"):
        detect_schema(path)


@pytest.mark.parametrize("v2_key", ["persona", "evaluation_prompt"])
def test_detect_schema_rejects_partial_v2_with_steps(tmp_path, v2_key):
    """User typo'd one V2 key but still has V1 steps → orphan V2 key would be
    silently dropped. Must error instead.
    """
    path = _write(
        tmp_path,
        f"""
        name: t
        target: "+1"
        {v2_key}: oops typo
        steps:
          - say: hi
        """,
    )
    with pytest.raises(ValueError, match="mixes V1.*V2"):
        detect_schema(path)


def test_detect_schema_returns_v1_for_steps(tmp_path):
    path = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        steps:
          - say: hi
        """,
    )
    assert detect_schema(path) == "v1"


def test_detect_schema_returns_v2_for_persona_and_evaluation_prompt(tmp_path):
    path = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        """,
    )
    assert detect_schema(path) == "v2"


def test_api_key_for_rejects_provider_model_mismatch():
    runner = _runner_with({"gemini_api_key": "g-key", "anthropic_api_key": "a-key"})
    with pytest.raises(ValueError, match="provider/model mismatch"):
        runner._api_key_for("claude-sonnet-4-6", "gemini")


def test_api_key_for_allows_explicit_provider_for_unknown_model():
    runner = _runner_with({"gemini_api_key": "g-key"})
    # Custom model name with no recognized prefix — explicit provider is the only signal
    key = runner._api_key_for("custom-model-name", "gemini")
    assert key == "g-key"


def test_api_key_for_accepts_matching_provider_and_model():
    runner = _runner_with({"gemini_api_key": "g-key"})
    key = runner._api_key_for("gemini-2.5-flash", "gemini")
    assert key == "g-key"


@pytest.mark.parametrize(
    "model,env_var",
    [
        ("gemini-2.5-flash", "JUDGE_IT_GEMINI_API_KEY"),
        ("claude-sonnet-4-6", "JUDGE_IT_ANTHROPIC_API_KEY"),
        ("gpt-4o", "JUDGE_IT_OPENAI_API_KEY"),
    ],
)
def test_api_key_for_raises_when_key_not_configured(model, env_var):
    """If the matching provider's key slot is empty, raise BEFORE make_call."""
    runner = _runner_with({})  # no keys set; all default to ""
    with pytest.raises(ValueError, match=env_var):
        runner._api_key_for(model, None)


def test_api_key_for_raises_on_unknown_model_with_no_provider():
    """Unknown prefix + no explicit provider → detect_provider raises through."""
    runner = _runner_with({"gemini_api_key": "g-key"})
    with pytest.raises(ValueError, match="unknown provider prefix"):
        runner._api_key_for("custom-model-no-prefix", None)


def _scenario() -> Scenario:
    return Scenario(
        name="t",
        target="+1",
        vars={},
        persona_rendered="be a caller",
        evaluation_prompt_rendered="judge it",
        llm=LLMConfig(temperature=0.7),  # high — must NOT leak to evaluator
    )


@pytest.mark.asyncio
async def test_run_evaluator_uses_low_temperature_not_scenario_temperature():
    """Evaluator must use EVALUATOR_TEMPERATURE (low variance), not scenario.llm.temperature."""
    runner = _runner_with({"gemini_api_key": "g-key"})
    scenario = _scenario()
    assert scenario.llm.temperature == 0.7  # sanity

    captured = {}

    class FakeEvaluator:
        def __init__(self, evaluation_prompt, llm, temperature, metric_spec=None):
            captured["temperature"] = temperature
            captured["metric_spec"] = metric_spec

        async def evaluate(self, transcript, **observed):
            captured["observed"] = observed
            return EvaluationResult(overall_passed=True, overall_reasoning="ok")

    with patch("judge_it.orchestrator.runner.Evaluator", FakeEvaluator), \
         patch("judge_it.orchestrator.runner.get_client"):
        result = CallResult(flow_name="t", target="+1", status=CallStatus.COMPLETED)
        ev = await runner._run_evaluator(scenario, result)

    assert captured["temperature"] == EVALUATOR_TEMPERATURE == 0.0
    # the declared-metric spec reaches the evaluator (empty here, but wired)
    assert captured["metric_spec"] == []
    assert ev.overall_passed is True


@pytest.mark.asyncio
async def test_run_evaluator_times_out_to_malformed_verdict(monkeypatch):
    """If evaluator.evaluate hangs, runner produces a malformed verdict, not a hang."""
    runner = _runner_with({"gemini_api_key": "g-key"})
    scenario = _scenario()

    class HangingEvaluator:
        def __init__(self, **kw): pass

        async def evaluate(self, transcript):
            await asyncio.sleep(10)  # would hang past our patched timeout

    # Patch the timeout to a tiny value so the test runs fast
    monkeypatch.setattr("judge_it.orchestrator.runner.EVALUATOR_TIMEOUT_S", 0.05)
    with patch("judge_it.orchestrator.runner.Evaluator", HangingEvaluator), \
         patch("judge_it.orchestrator.runner.get_client"):
        result = CallResult(flow_name="t", target="+1", status=CallStatus.COMPLETED)
        ev = await runner._run_evaluator(scenario, result)

    assert ev.overall_passed is False
    assert ev.malformed is True
    assert "timed out" in ev.overall_reasoning.lower()


@pytest.mark.asyncio
async def test_run_evaluator_crash_to_malformed_verdict():
    """If evaluator.evaluate raises, runner produces a malformed verdict, not a crash."""
    runner = _runner_with({"gemini_api_key": "g-key"})
    scenario = _scenario()

    class CrashingEvaluator:
        def __init__(self, **kw): pass

        async def evaluate(self, transcript):
            raise RuntimeError("boom: upstream LLM 500")

    with patch("judge_it.orchestrator.runner.Evaluator", CrashingEvaluator), \
         patch("judge_it.orchestrator.runner.get_client"):
        result = CallResult(flow_name="t", target="+1", status=CallStatus.COMPLETED)
        ev = await runner._run_evaluator(scenario, result)

    assert ev.overall_passed is False
    assert ev.malformed is True
    assert "crashed" in ev.overall_reasoning.lower()
    assert "boom" in ev.overall_reasoning


@pytest.mark.asyncio
async def test_run_v2_config_error_still_writes_folder(tmp_path, monkeypatch):
    """Spec invariant: a failure BEFORE dialing (missing API key, bad provider
    config) still produces a per-call log folder.
    """
    # No keys configured → _api_key_for raises before make_call would.
    runner = _runner_with({})
    monkeypatch.chdir(tmp_path)  # logwriter writes under "logs/" relative to cwd

    scenario = _scenario()
    # _run_v2_scenario must not raise even though _api_key_for will.
    result = await runner._run_v2_scenario(scenario)

    assert result.status == CallStatus.ERROR
    assert "setup failed" in (result.error or "").lower()

    # Exactly one folder under logs/ exists with the required files.
    log_dirs = list((tmp_path / "logs").iterdir())
    assert len(log_dirs) == 1
    folder = log_dirs[0]
    for required in (
        "scenario.yaml",
        "rendered_persona.txt",
        "rendered_eval.txt",
        "metadata.json",
        "transcript.json",
        "transcript.txt",
        "evaluation.json",
        "llm_calls.json",
        "debug.log",
    ):
        assert (folder / required).exists(), f"missing {required}"


@pytest.mark.asyncio
async def test_run_v2_always_closes_logwriter_on_setup_error(tmp_path, monkeypatch):
    """Outer finally must close the logwriter (detach FileHandler) even when
    setup raises — otherwise leaked handlers pollute later calls' debug.log.
    """
    import logging as _logging

    runner = _runner_with({})  # no keys → setup error
    monkeypatch.chdir(tmp_path)
    scenario = _scenario()

    # logwriter attaches its FileHandler to the ROOT logger (so debug.log
    # captures third-party records like twilio.http_client / google_genai too).
    root_logger = _logging.getLogger()
    handlers_before = len(root_logger.handlers)

    await runner._run_v2_scenario(scenario)

    handlers_after = len(root_logger.handlers)
    assert handlers_after == handlers_before, "logwriter FileHandler leaked"
