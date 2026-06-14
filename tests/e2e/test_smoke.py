"""End-to-end smoke tests + CLI parsing + summary printing.

Live tests are guarded by RUN_LIVE=1 — they place real Twilio calls and
incur LLM cost. They're skipped by default. The non-live tests
(CLI parsing, summary rendering) run on every `pytest tests/` invocation.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opkura.cli.main import _print_v2_summary, cli_app
from opkura.core.models import (
    CallResult,
    CallStatus,
    EvaluationCriterion,
    EvaluationResult,
)


# tests/e2e/test_smoke.py → repo root is .parent.parent.parent post-Task-9.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCENARIOS = REPO_ROOT / "clients" / "_examples"
LIVE = pytest.mark.skipif(
    not os.getenv("RUN_LIVE"),
    reason="live e2e test — set RUN_LIVE=1 to enable (places a real Twilio call)",
)


# --- CLI structure (no live) ------------------------------------------------

def test_top_level_help_lists_all_subcommands():
    runner = CliRunner()
    res = runner.invoke(cli_app, ["--help"])
    assert res.exit_code == 0
    for cmd in ("run", "lint", "bcg", "watch", "load", "results", "serve"):
        assert cmd in res.stdout, f"missing subcommand in --help: {cmd}"


@pytest.mark.parametrize("cmd", ["run", "lint", "bcg"])
def test_each_subcommand_has_help(cmd):
    runner = CliRunner()
    res = runner.invoke(cli_app, [cmd, "--help"])
    assert res.exit_code == 0
    assert "Usage" in res.stdout
    assert cmd in res.stdout.lower()


# --- opkura lint against all reference scenarios ----------------------------

def test_lint_all_reference_scenarios():
    """`opkura lint` over the three canonical scenarios should at minimum
    parse all three. example-barge-llm-scenario.yaml will fail with
    missing_bcd until someone runs `opkura bcg` — that's documented in
    the CHECKLIST. Skip that file here by linting the two we know pass.
    """
    runner = CliRunner()
    paths = [
        str(SCENARIOS / "happy-path.yaml"),
        str(SCENARIOS / "barge-scheduled.yaml"),
    ]
    for p in paths:
        res = runner.invoke(cli_app, ["lint", p])
        assert res.exit_code == 0, f"{p} lint failed:\n{res.stdout}"


def test_example_barge_llm_scenario_fails_lint_without_bcd():
    """Documented expected behavior: the LLM-mode scenario references a BCD
    file that doesn't exist until `opkura bcg` is run. Lint catches this.
    """
    runner = CliRunner()
    res = runner.invoke(
        cli_app, ["lint", str(SCENARIOS / "barge-llm.yaml")]
    )
    assert res.exit_code == 1
    assert "missing_bcd" in res.stdout


# --- V2 summary rendering ---------------------------------------------------

def _pass_result() -> CallResult:
    ev = EvaluationResult(
        overall_passed=True,
        overall_reasoning="all checks passed",
        criteria=[
            EvaluationCriterion(id=1, name="greeting", passed=True, evidence="turn 1"),
            EvaluationCriterion(id=2, name="identity", passed=True, evidence="turn 4"),
        ],
        metrics={"bot_turns": 12, "caller_turns": 11},
    )
    return CallResult(
        flow_name="happy path",
        target="+18005551234",
        status=CallStatus.COMPLETED,
        evaluation=ev,
    )


def _fail_result() -> CallResult:
    ev = EvaluationResult(
        overall_passed=False,
        overall_reasoning="card digits missing",
        criteria=[
            EvaluationCriterion(id=1, name="greeting", passed=True, evidence="turn 1"),
            EvaluationCriterion(id=6, name="card_digits_correct", passed=False, evidence="bot said 'last four digits'"),
        ],
    )
    return CallResult(
        flow_name="happy path",
        target="+18005551234",
        status=CallStatus.COMPLETED,
        evaluation=ev,
    )


def _capture_print(result: CallResult) -> str:
    """Render the V2 summary into a string we can substring-match."""
    from rich.console import Console as _Console

    buf = io.StringIO()
    fake = _Console(file=buf, force_terminal=False, width=120)
    with patch("opkura.cli.main.console", fake):
        _print_v2_summary(result)
    return buf.getvalue()


def test_summary_print_pass():
    out = _capture_print(_pass_result())
    assert "PASS" in out
    assert "happy path" in out
    assert "all checks passed" in out
    assert "greeting" in out


def test_summary_print_fail():
    out = _capture_print(_fail_result())
    assert "FAIL" in out
    assert "card_digits_correct" in out
    assert "card digits missing" in out


def test_summary_print_handles_missing_evaluation():
    """V2 scenarios that errored before reaching the evaluator have
    result.evaluation=None — summary must not crash, just print status.
    """
    result = CallResult(
        flow_name="t", target="+1", status=CallStatus.ERROR, error="dial failed"
    )
    out = _capture_print(result)
    assert "FAIL" in out  # passed is False when evaluation is None and no steps
    assert "dial failed" in out


def test_summary_print_escapes_brackets_in_llm_text():
    """Evaluator-generated fields (reasoning / criteria.name / criteria.evidence)
    often contain bracketed phrases like `[turn 3]` or `[pause]`. Rich would
    parse those as style tags and silently drop the contents — same class
    of bug Task 7 fixed for lint field paths. rich.markup.escape on every
    LLM field keeps them visible to the user.
    """
    ev = EvaluationResult(
        overall_passed=False,
        overall_reasoning="bot failed at [turn 3]",
        criteria=[
            EvaluationCriterion(
                id=1,
                name="ssn_collection",
                passed=False,
                evidence="turn 5: caller said '[pause] uh, five-four-five-seven'",
            ),
        ],
    )
    result = CallResult(
        flow_name="t", target="+1", status=CallStatus.COMPLETED, evaluation=ev
    )
    out = _capture_print(result)
    # Brackets must survive: `[turn 3]`, `[pause]` would otherwise be
    # consumed by Rich's markup parser.
    assert "[turn 3]" in out
    assert "[pause]" in out


def test_summary_print_surfaces_log_dir_on_setup_error():
    """v1 bugfix: setup-error calls (missing API key, provider mismatch)
    never populate recording_paths, but the log folder DOES exist —
    logwriter is constructed first in the runner. result.log_dir is the
    canonical source the CLI should use, falling back to recording_paths
    only for V1 / legacy results.
    """
    result = CallResult(
        flow_name="t",
        target="+1",
        status=CallStatus.ERROR,
        error="setup failed: missing API key",
        log_dir="/tmp/logs/call-foo-bar-baz",
    )
    out = _capture_print(result)
    assert "/tmp/logs/call-foo-bar-baz" in out


def test_summary_print_falls_back_to_recording_paths_when_log_dir_unset():
    """Back-compat: if log_dir is None (V1 path or pre-Task-8 result),
    fall back to deriving the folder from recording_paths.
    """
    result = CallResult(
        flow_name="t",
        target="+1",
        status=CallStatus.COMPLETED,
        recording_paths={"bot": "/tmp/legacy/folder/bot.wav"},
    )
    out = _capture_print(result)
    assert "/tmp/legacy/folder" in out


def test_summary_print_handles_malformed_evaluation():
    """An evaluator that produced a malformed verdict (synthesized) — must
    show the yellow advisory line. CHECKLIST: surface this to the user.
    """
    ev = EvaluationResult(
        overall_passed=False,
        overall_reasoning="evaluator output malformed",
        malformed=True,
    )
    result = CallResult(
        flow_name="t", target="+1", status=CallStatus.COMPLETED, evaluation=ev
    )
    out = _capture_print(result)
    assert "malformed" in out


# --- run dispatch + --no-lint bypass ----------------------------------------

def _patch_runner_returning(result: CallResult):
    """Patch the run command's runtime so it doesn't actually dial. Returns
    the patch context manager — use with `with`. Also stubs settings resolution
    so the test doesn't depend on env vars being set in CI.

    The seam is `resolve_settings`, not `Settings`: `run` calls that helper so
    `--transport` can override the deployment default before validation runs.
    """
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, MagicMock

    from opkura.core.config import Settings

    def _stub_settings(*a, **kw):
        return Settings(
            twilio_account_sid="x",
            twilio_auth_token="x",
            twilio_phone_number="x",
            deepgram_api_key="x",
            gemini_api_key="x",
            anthropic_api_key="",
            openai_api_key="",
            _env_file=None,
        )

    stack = ExitStack()
    fake_runner = MagicMock()
    fake_runner.run_scenario_file = AsyncMock(return_value=result)
    fake_runner.close = AsyncMock()

    stack.enter_context(patch("opkura.cli.run.resolve_settings", _stub_settings))
    stack.enter_context(patch("opkura.cli.run._start_server"))
    stack.enter_context(patch("opkura.cli.run.TestRunner", return_value=fake_runner))
    return stack, fake_runner


def test_run_v1_yaml_uses_v1_summary(tmp_path, monkeypatch):
    """Backward-compat invariant (Task 8 acceptance): V1 flow YAMLs still
    work via `opkura run`. Schema detection should route to _print_result
    (V1 summary), NOT _print_v2_summary.
    """
    from opkura.core.models import StepResult, StepStatus

    monkeypatch.chdir(tmp_path)
    # Minimal V1 flow YAML.
    v1_path = tmp_path / "v1.yaml"
    v1_path.write_text(
        'name: v1-test\ntarget: "+18005551234"\nsteps:\n  - say: hi\n',
        encoding="utf-8",
    )
    v1_result = CallResult(
        flow_name="v1-test",
        target="+18005551234",
        status=CallStatus.COMPLETED,
        steps=[StepResult(step_index=0, status=StepStatus.PASSED, bot_transcript="hi")],
    )

    stack, _ = _patch_runner_returning(v1_result)
    runner = CliRunner()
    with stack, \
         patch("opkura.cli.run._print_v2_summary") as v2_print, \
         patch("opkura.cli.run._print_result") as v1_print:
        res = runner.invoke(cli_app, ["run", str(v1_path)])
    assert res.exit_code == 0
    # V1 summary called, V2 summary NOT called.
    v1_print.assert_called_once()
    v2_print.assert_not_called()


def test_run_v2_no_lint_bypasses_lint(tmp_path, monkeypatch):
    """`--no-lint` must skip the pre-flight lint even when the scenario
    would fail it. Locks the bypass semantics (Task 8 spec § 'Implementation
    plan / Step 1 / no_lint flag').
    """
    monkeypatch.chdir(tmp_path)
    # V2 YAML with an invalid E.164 target — lint would reject this.
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        'name: t\n'
        'target: "not-e164"\n'
        'vars: {x: 1}\n'
        'persona: \'with "speech" and "dtmf"\'\n'
        'evaluation_prompt: \'with "overall_passed" and "criteria"\'\n',
        encoding="utf-8",
    )
    v2_result = CallResult(
        flow_name="t",
        target="not-e164",
        status=CallStatus.COMPLETED,
        evaluation=EvaluationResult(overall_passed=True, overall_reasoning="ok"),
    )

    runner = CliRunner()
    # First sanity check: without --no-lint, the bad YAML must fail at lint.
    stack_strict, _ = _patch_runner_returning(v2_result)
    with stack_strict:
        res_strict = runner.invoke(cli_app, ["run", str(bad)])
    assert res_strict.exit_code == 1, "lint was expected to reject the bad YAML"

    # Now with --no-lint, the runner should be invoked despite the bad YAML.
    stack_bypass, fake_runner = _patch_runner_returning(v2_result)
    with stack_bypass:
        res_bypass = runner.invoke(cli_app, ["run", str(bad), "--no-lint"])
    assert res_bypass.exit_code == 0, res_bypass.stdout
    fake_runner.run_scenario_file.assert_awaited_once()


# --- F12 patterns landed in example-scenario.yaml ---------------------------

def test_example_scenario_has_f12_persona_block():
    """F12 deliverable: persona must instruct the LLM to inject natural
    fillers / self-corrections / hesitation. Checked at the YAML level so
    a future edit can't silently drop it.
    """
    text = (SCENARIOS / "happy-path.yaml").read_text(encoding="utf-8")
    assert "Natural human speech" in text
    for marker in ("umm", "uh", "let me see", "wait, no"):
        assert marker in text, f"missing F12 marker: {marker!r}"


def test_example_scenario_has_three_filler_criteria():
    """F12 evaluator side: three new criteria with their IDs in the schema."""
    text = (SCENARIOS / "happy-path.yaml").read_text(encoding="utf-8")
    # Criteria descriptions
    assert "Filler resilience" in text
    assert "No filler misinterpretation" in text
    assert "Self-correction handled" in text
    # JSON-output array entries (by id, matching the names used in criteria)
    assert '"id": 11' in text and '"name": "filler_resilience"' in text
    assert '"id": 12' in text and '"name": "no_filler_misinterp"' in text
    assert '"id": 13' in text and '"name": "self_correction"' in text


# --- live e2e (RUN_LIVE=1) --------------------------------------------------

@LIVE
def test_e2e_happy_path_scenario(tmp_path, monkeypatch):
    """Live: place a real Twilio call against the configured target and
    verify the log folder + evaluator verdict were produced.
    """
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        cli_app,
        ["run", str(SCENARIOS / "happy-path.yaml")],
        catch_exceptions=False,
    )
    # We don't pin pass/fail — the bot's behavior is what's being tested.
    # We just need the call to complete and write artifacts.
    log_dirs = list((tmp_path / "logs").iterdir())
    assert log_dirs, "no log folder produced"
    folder = log_dirs[0]
    for required in (
        "transcript.json",
        "transcript.txt",
        "evaluation.json",
        "llm_calls.json",
        "metadata.json",
        "debug.log",
    ):
        assert (folder / required).exists(), f"missing {required}"
    ev = json.loads((folder / "evaluation.json").read_text())
    assert "overall_passed" in ev


@LIVE
def test_e2e_bcg_roundtrip(tmp_path):
    """Live: bcg against a real LLM produces a valid BCD with the expected
    header; re-running without --regenerate fails with FileExistsError.
    """
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("You are a friendly debt-collection bot named Aiden.", encoding="utf-8")
    out = tmp_path / "bcd.md"
    runner = CliRunner()
    res = runner.invoke(cli_app, ["bcg", str(prompt), "-o", str(out)])
    assert res.exit_code == 0, res.stdout
    content = out.read_text(encoding="utf-8")
    assert "Generated from:" in content
    assert "Generator: bcg" in content
    # Second run without --regenerate fails with exit code 2.
    res2 = runner.invoke(cli_app, ["bcg", str(prompt), "-o", str(out)])
    assert res2.exit_code == 2
