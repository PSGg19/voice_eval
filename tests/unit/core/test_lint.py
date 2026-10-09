from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from judge_it.cli.main import cli_app
from judge_it.core.lint import lint_scenario


def _write(tmp_path: Path, body: str, name: str = "scenario.yaml") -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _valid_body(**overrides) -> str:
    """Baseline valid V2 scenario. Tests override specific fields by substring
    replacement before writing.
    """
    return textwrap.dedent('''
        name: t
        target: "+18005551234"
        vars:
          name: Iris
        persona: |
          You are {{ vars.name }}. Respond as JSON: "speech" and "dtmf".
        evaluation_prompt: |
          Return JSON with "overall_passed" and "criteria".
    ''')


# --- 1. valid scenario ------------------------------------------------------

def test_valid_scenario_passes(tmp_path):
    p = _write(tmp_path, _valid_body())
    result = lint_scenario(p)
    assert result.passed
    assert result.errors == []


# --- 2. missing required key ------------------------------------------------

def test_missing_required_key(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        """,
    )
    result = lint_scenario(p)
    codes = [e.code for e in result.errors]
    assert codes.count("missing_key") == 1
    missing = next(e for e in result.errors if e.code == "missing_key")
    assert missing.field == "evaluation_prompt"


# --- 3. legacy key rejected -------------------------------------------------

def test_legacy_key_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        vars_ref: shared.yaml
        """,
    )
    result = lint_scenario(p)
    assert any(e.code == "legacy_key" and e.field == "vars_ref" for e in result.errors)


# --- 4. unresolved jinja ----------------------------------------------------

def test_unresolved_jinja(tmp_path):
    p = _write(
        tmp_path,
        '''
        name: t
        target: "+18005551234"
        vars:
          name: Iris
        persona: 'Hi {{ vars.naem }} "speech" "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        ''',
    )
    result = lint_scenario(p)
    assert any(
        e.code == "unresolved_jinja" and e.field == "persona" for e in result.errors
    )


# --- 5. persona missing schema hint → warning -------------------------------

def test_persona_missing_schema_hint(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: |
          You are a caller. No mention of the output schema here.
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        """,
    )
    result = lint_scenario(p)
    # Schema-hint is a WARNING, not an error — file still passes
    assert result.passed
    assert any(w.code == "persona_missing_schema_hint" for w in result.warnings)


def test_evaluation_missing_schema_hint(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: |
          Score the call somehow.
        """,
    )
    result = lint_scenario(p)
    assert result.passed
    assert any(
        w.code == "evaluation_missing_schema_hint" for w in result.warnings
    )


# --- 6. invalid E.164 -------------------------------------------------------

def test_invalid_e164(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "5551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        """,
    )
    result = lint_scenario(p)
    assert any(e.code == "invalid_e164" for e in result.errors)


# --- 7. unknown model prefix -----------------------------------------------

def test_unknown_model_prefix(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        llm:
          caller_model: mistral-7b
        """,
    )
    result = lint_scenario(p)
    assert any(
        e.code == "unknown_model_prefix" and e.field == "llm.caller_model"
        for e in result.errors
    )


# --- 8. explicit provider override accepted --------------------------------

def test_explicit_provider_skips_prefix_check(tmp_path):
    """A valid explicit `llm.provider` lets the prefix-detection check skip
    for the caller/evaluator model — useful when a model name doesn't follow
    the known prefix conventions yet (new release, internal alias).
    """
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        llm:
          caller_model: some-future-model
          provider: gemini
        """,
    )
    result = lint_scenario(p)
    assert not any(e.code == "unknown_model_prefix" for e in result.errors)
    assert not any(e.code == "unknown_provider" for e in result.errors)


# --- explicit provider must be a known value -------------------------------

def test_unknown_explicit_provider_rejected(tmp_path):
    """An explicit `llm.provider` outside the known set (gemini/anthropic/
    openai) used to pass lint silently — runtime would then fail at
    get_client. Catch it at load-time.
    """
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        llm:
          caller_model: some-custom-model
          provider: fake
        """,
    )
    result = lint_scenario(p)
    assert any(
        e.code == "unknown_provider" and e.field == "llm.provider"
        for e in result.errors
    )


def test_known_explicit_providers_pass(tmp_path):
    for provider in ("gemini", "anthropic", "openai"):
        p = _write(
            tmp_path,
            f"""
            name: t
            target: "+18005551234"
            vars: {{x: 1}}
            persona: 'with "speech" and "dtmf"'
            evaluation_prompt: 'with "overall_passed" and "criteria"'
            llm:
              caller_model: some-custom-model
              provider: {provider}
            """,
            name=f"{provider}.yaml",
        )
        result = lint_scenario(p)
        assert not any(
            e.code in ("unknown_provider", "unknown_model_prefix")
            for e in result.errors
        ), f"unexpected error for provider={provider}: {[e.code for e in result.errors]}"


# --- missing-key suppresses redundant secondary errors ---------------------

def test_missing_target_does_not_double_error_as_invalid_e164(tmp_path):
    """When `target` is omitted entirely, _check_required_keys already
    reports it. The E.164 check must skip rather than re-report as
    `invalid_e164: got None` — that's noise, not signal.
    """
    p = _write(
        tmp_path,
        """
        name: t
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        """,
    )
    result = lint_scenario(p)
    codes = [e.code for e in result.errors]
    assert codes.count("missing_key") == 1
    assert "invalid_e164" not in codes


def test_missing_persona_does_not_double_error_as_schema_hint(tmp_path):
    """Persona omitted → only missing_key error, not also a schema-hint
    warning for the absent field.
    """
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        """,
    )
    result = lint_scenario(p)
    assert any(e.code == "missing_key" and e.field == "persona" for e in result.errors)
    assert not any(
        w.code == "persona_missing_schema_hint" for w in result.warnings
    )


def test_missing_evaluation_prompt_does_not_double_warn(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        """,
    )
    result = lint_scenario(p)
    assert any(
        e.code == "missing_key" and e.field == "evaluation_prompt" for e in result.errors
    )
    assert not any(
        w.code == "evaluation_missing_schema_hint" for w in result.warnings
    )


# --- 9. scheduled missing triggers -----------------------------------------

def test_barge_scheduled_missing_triggers(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        barge_in:
          enabled: true
          mode: scheduled
          triggers: []
        """,
    )
    result = lint_scenario(p)
    assert any(e.code == "missing_triggers" for e in result.errors)


# --- 10. scheduled + decide_every_ms conflict ------------------------------

def test_barge_scheduled_with_decide_every_ms_conflict(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        barge_in:
          enabled: true
          mode: scheduled
          decide_every_ms: 500
          triggers:
            - at_turn: 1
              say: "hi"
        """,
    )
    result = lint_scenario(p)
    assert any(
        e.code == "scheduled_rejects_decide_every_ms" for e in result.errors
    )


def test_barge_scheduled_with_bot_context_path_conflict(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        barge_in:
          enabled: true
          mode: scheduled
          bot_context_path: "ignored.md"
          triggers:
            - at_turn: 1
              say: "hi"
        """,
    )
    result = lint_scenario(p)
    assert any(
        e.code == "scheduled_rejects_bot_context_path" for e in result.errors
    )


# --- 11. llm + triggers conflict -------------------------------------------

def test_barge_llm_with_triggers_conflict(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        barge_in:
          enabled: true
          mode: llm
          decide_every_ms: 500
          triggers:
            - at_turn: 1
              say: "hi"
        """,
    )
    result = lint_scenario(p)
    assert any(e.code == "llm_rejects_triggers" for e in result.errors)


def test_barge_llm_missing_decide_every_ms(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        barge_in:
          enabled: true
          mode: llm
        """,
    )
    result = lint_scenario(p)
    assert any(e.code == "llm_missing_decide_every_ms" for e in result.errors)


# --- 12. trigger missing say -----------------------------------------------

def test_trigger_missing_say(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        barge_in:
          enabled: true
          mode: scheduled
          triggers:
            - at_turn: 1
        """,
    )
    result = lint_scenario(p)
    assert any(e.code == "missing_say" for e in result.errors)


def test_trigger_missing_at_turn(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        barge_in:
          enabled: true
          mode: scheduled
          triggers:
            - say: "hello"
        """,
    )
    result = lint_scenario(p)
    assert any(e.code == "missing_at_turn" for e in result.errors)


def test_trigger_negative_after_bot_words(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        barge_in:
          enabled: true
          mode: scheduled
          triggers:
            - at_turn: 1
              say: "hello"
              after_bot_words: -3
        """,
    )
    result = lint_scenario(p)
    assert any(e.code == "invalid_after_bot_words" for e in result.errors)


# --- 13. duplicate at_turn -------------------------------------------------

def test_duplicate_at_turn(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        barge_in:
          enabled: true
          mode: scheduled
          triggers:
            - at_turn: 3
              say: "first"
            - at_turn: 3
              say: "duplicate"
        """,
    )
    result = lint_scenario(p)
    assert any(e.code == "duplicate_at_turn" for e in result.errors)


# --- 14. at_turn exceeds max_turns -----------------------------------------

def test_at_turn_exceeds_max_turns(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        max_turns: 10
        barge_in:
          enabled: true
          mode: scheduled
          triggers:
            - at_turn: 15
              say: "too far"
        """,
    )
    result = lint_scenario(p)
    assert any(e.code == "at_turn_out_of_range" for e in result.errors)


# --- 15. missing BCD file --------------------------------------------------

def test_missing_bcd_file(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        barge_in:
          enabled: true
          mode: llm
          decide_every_ms: 500
          bot_context_path: "./no-such-file.md"
        """,
    )
    result = lint_scenario(p)
    assert any(e.code == "missing_bcd" for e in result.errors)


def test_existing_bcd_file_passes(tmp_path):
    bcd = tmp_path / "bargin-context.md"
    bcd.write_text("# Barge Context — Bot", encoding="utf-8")
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        barge_in:
          enabled: true
          mode: llm
          decide_every_ms: 500
          bot_context_path: "bargin-context.md"
        """,
    )
    result = lint_scenario(p)
    assert not any(e.code == "missing_bcd" for e in result.errors)


# --- accumulates errors instead of short-circuiting ------------------------

def test_lint_collects_multiple_errors(tmp_path):
    """A single file with three independent problems must surface ALL three —
    short-circuiting on the first would force users to fix-and-rerun N times.
    """
    p = _write(
        tmp_path,
        """
        name: t
        target: not-e164
        vars: {x: 1}
        persona: '{{ vars.bad }}'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        llm:
          caller_model: mistral-7b
        """,
    )
    result = lint_scenario(p)
    codes = {e.code for e in result.errors}
    assert "invalid_e164" in codes
    assert "unresolved_jinja" in codes
    assert "unknown_model_prefix" in codes


# --- missing file / invalid YAML -------------------------------------------

def test_missing_file_reports_error(tmp_path):
    result = lint_scenario(tmp_path / "no-such-scenario.yaml")
    assert not result.passed
    assert any(e.code == "missing_file" for e in result.errors)


def test_invalid_yaml_reports_error(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("name: t\n  bad: indent\n", encoding="utf-8")
    result = lint_scenario(p)
    assert any(e.code == "invalid_yaml" for e in result.errors)


# --- 16, 17. CLI exit codes -------------------------------------------------

def test_cli_exit_code_on_failure(tmp_path):
    """A glob that includes one failing file → exit 1."""
    good = _write(tmp_path, _valid_body(), name="good.yaml")
    bad = _write(
        tmp_path,
        """
        name: t
        target: "not-e164"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        """,
        name="bad.yaml",
    )
    runner = CliRunner()
    res = runner.invoke(cli_app, ["lint", str(tmp_path / "*.yaml")])
    assert res.exit_code == 1, res.stdout
    assert "FAIL" in res.stdout
    assert "invalid_e164" in res.stdout


def test_cli_exit_code_on_success(tmp_path):
    _write(tmp_path, _valid_body(), name="good.yaml")
    runner = CliRunner()
    res = runner.invoke(cli_app, ["lint", str(tmp_path / "*.yaml")])
    assert res.exit_code == 0, res.stdout
    assert "0 failed" in res.stdout


def test_cli_no_files_matched_exits_nonzero(tmp_path):
    runner = CliRunner()
    res = runner.invoke(cli_app, ["lint", str(tmp_path / "nope_*.yaml")])
    assert res.exit_code == 1
    assert "No files matched" in res.stdout


def test_cli_renders_bracketed_field_paths_without_eating_index(tmp_path):
    """Rich treats [N] as a style tag. Field paths like
    `barge_in.triggers[0].at_turn` must escape brackets so the index survives
    rendering — otherwise users see the path with the `[0]` silently dropped.
    """
    p = _write(
        tmp_path,
        """
        name: t
        target: "+18005551234"
        vars: {x: 1}
        persona: 'with "speech" and "dtmf"'
        evaluation_prompt: 'with "overall_passed" and "criteria"'
        barge_in:
          enabled: true
          mode: scheduled
          triggers:
            - at_turn: 3
              say: "first"
            - at_turn: 3
              say: "duplicate"
        """,
    )
    runner = CliRunner()
    res = runner.invoke(cli_app, ["lint", str(p)])
    assert res.exit_code == 1
    # The duplicate-at_turn error's field is barge_in.triggers[1].at_turn.
    # If Rich ate the brackets, the `1` would be gone and the line would
    # read `barge_in.triggers.at_turn` instead.
    assert "barge_in.triggers[1].at_turn" in res.stdout


def test_cli_probe_fast_fails_when_api_key_missing(tmp_path, monkeypatch):
    """--probe without JUDGE_IT_GEMINI_API_KEY must fail fast BEFORE iterating
    scenarios — otherwise N identical errors print for an N-scenario glob.
    """
    # Settings() reads .env via pydantic-settings, so monkeypatch.delenv alone
    # isn't enough to isolate this test from a developer's .env file. Patch
    # the Settings constructor itself to return a stub with no Gemini key.
    from unittest.mock import patch

    from judge_it.core.config import Settings

    def _no_key_settings(*a, **kw):
        return Settings(
            twilio_account_sid="x",
            twilio_auth_token="x",
            twilio_phone_number="x",
            deepgram_api_key="x",
            gemini_api_key="",
            anthropic_api_key="",
            openai_api_key="",
            _env_file=None,
        )

    # Write three valid scenarios so the failure can't be from lint itself.
    for i in range(3):
        _write(tmp_path, _valid_body(), name=f"s{i}.yaml")
    runner = CliRunner()
    with patch("judge_it.cli.lint.Settings", _no_key_settings):
        res = runner.invoke(cli_app, ["lint", str(tmp_path / "s*.yaml"), "--probe"])
    assert res.exit_code == 1, res.stdout
    # Exactly one error line, not three.
    assert res.stdout.count("--probe requires JUDGE_IT_GEMINI_API_KEY") == 1
    # And NO per-scenario PASS/FAIL output — we never entered the loop.
    assert "PASS" not in res.stdout
    assert "FAIL" not in res.stdout


# --- 18. performance budget (skip until full suite exists) -----------------

@pytest.mark.skip(reason="scenario suite not yet built; revisit when Resurgent suite lands")
def test_lint_under_5_seconds_for_resurgent_suite():
    pass


# --- declared metrics -------------------------------------------------------

def _with_metrics(block: str) -> str:
    return _valid_body() + textwrap.dedent(block)


def test_a_well_formed_metrics_block_passes(tmp_path):
    p = _write(tmp_path, _with_metrics('''
        metrics:
          - key: required_gates_passed
            description: required gates passed, as n/m
            type: string
            example: "4/4"
          - key: caller_had_to_repeat
            type: boolean
            required: false
    '''))
    result = lint_scenario(p)
    assert result.passed, [e.message for e in result.errors]
    assert result.warnings == []


def test_metrics_must_be_a_list(tmp_path):
    p = _write(tmp_path, _with_metrics('''
        metrics:
          bot_turns: 0
    '''))
    result = lint_scenario(p)
    assert not result.passed
    assert [e.code for e in result.errors] == ["invalid_metrics"]


def test_a_metric_needs_a_key(tmp_path):
    p = _write(tmp_path, _with_metrics('''
        metrics:
          - description: no key here
            type: string
    '''))
    result = lint_scenario(p)
    assert not result.passed
    assert any("must be a non-empty string" in e.message for e in result.errors)


def test_an_unknown_metric_type_is_rejected_with_the_allowed_list(tmp_path):
    p = _write(tmp_path, _with_metrics('''
        metrics:
          - key: x
            type: enum
    '''))
    result = lint_scenario(p)
    assert not result.passed
    msg = " ".join(e.message for e in result.errors)
    assert "must be one of" in msg and "boolean" in msg


def test_an_unknown_metric_field_is_rejected(tmp_path):
    """A typo'd field would otherwise be silently ignored."""
    p = _write(tmp_path, _with_metrics('''
        metrics:
          - key: x
            typ: string
    '''))
    result = lint_scenario(p)
    assert not result.passed
    assert any("unknown field(s) ['typ']" in e.message for e in result.errors)


def test_a_duplicate_metric_key_is_rejected(tmp_path):
    p = _write(tmp_path, _with_metrics('''
        metrics:
          - key: x
            type: string
          - key: x
            type: integer
    '''))
    result = lint_scenario(p)
    assert not result.passed
    assert any(e.code == "duplicate_metric" for e in result.errors)


def test_required_must_be_a_bool(tmp_path):
    p = _write(tmp_path, _with_metrics('''
        metrics:
          - key: x
            required: "yes"
    '''))
    result = lint_scenario(p)
    assert not result.passed
    assert any("must be true or false" in e.message for e in result.errors)


def test_declaring_a_harness_measured_metric_warns_but_passes(tmp_path):
    """It works — the key is dropped from the prompt and measured anyway — but
    the declaration implies the model decides it, and it does not."""
    p = _write(tmp_path, _with_metrics('''
        metrics:
          - key: bot_turns
            type: integer
          - key: required_gates_passed
            type: string
    '''))
    result = lint_scenario(p)
    assert result.passed
    assert [w.code for w in result.warnings] == ["metric_already_measured"]
    assert "bot_turns" in result.warnings[0].message


def test_no_metrics_block_is_still_valid(tmp_path):
    """1110 existing scenarios have none. This must stay purely additive."""
    result = lint_scenario(_write(tmp_path, _valid_body()))
    assert result.passed
    assert not any(w.code.startswith("metric") for w in result.warnings)


# --- customer-data guard ----------------------------------------------------

_LIVE_1160 = "90cf2cf7-a2be-4f29-9327-2a5c11d0f7d9"   # 13,399 real accounts
_UNLISTED = "00000000-0000-4000-8000-00000000dead"


def test_pointing_at_an_assistant_with_customer_accounts_is_an_error(tmp_path):
    """The check that was missing when a test run wrote to Veros's production CRM."""
    p = _write(tmp_path, _valid_body() + textwrap.dedent(f'''
        transport: "text"
        recipient_id: "2015550137"
        assistant_uuid: "{_LIVE_1160}"
    '''))
    result = lint_scenario(p)
    assert not result.passed
    err = next(e for e in result.errors if e.code == "assistant_has_customer_data")
    assert "13,399" in err.message
    assert "is_test` suppresses only the compliance event" in err.message


def test_an_unlisted_assistant_passes(tmp_path):
    p = _write(tmp_path, _valid_body() + textwrap.dedent(f'''
        transport: "text"
        recipient_id: "2015550137"
        assistant_uuid: "{_UNLISTED}"
    '''))
    result = lint_scenario(p)
    assert result.passed
    assert not any(w.code == "assistant_has_customer_data" for w in result.warnings)


def test_allow_customer_data_downgrades_the_error_to_a_warning(tmp_path):
    """An explicit, reviewable acknowledgement — not a silent bypass."""
    p = _write(tmp_path, _valid_body() + textwrap.dedent(f'''
        transport: "text"
        recipient_id: "2015550137"
        assistant_uuid: "{_LIVE_1160}"
        allow_customer_data: true
    '''))
    result = lint_scenario(p)
    assert result.passed
    assert [w.code for w in result.warnings] == ["assistant_has_customer_data"]


def test_allow_customer_data_must_be_literally_true(tmp_path):
    """A truthy string must not open the gate."""
    p = _write(tmp_path, _valid_body() + textwrap.dedent(f'''
        transport: "text"
        recipient_id: "2015550137"
        assistant_uuid: "{_LIVE_1160}"
        allow_customer_data: "yes"
    '''))
    assert not lint_scenario(p).passed


def test_a_text_scenario_without_a_pinned_assistant_is_flagged_as_unguardable(tmp_path):
    """No `assistant_uuid` means the bot comes from an env var lint cannot see,
    so the guard is blind — and silence would read as approval."""
    p = _write(tmp_path, _valid_body() + textwrap.dedent('''
        transport: "text"
        recipient_id: "2015550137"
    '''))
    result = lint_scenario(p)
    assert result.passed  # a warning, not a refusal
    assert [w.code for w in result.warnings] == ["assistant_not_pinned"]


def test_a_voice_scenario_is_not_nagged_about_pinning_an_assistant(tmp_path):
    """`assistant_uuid` does nothing on a phone call — lint rejects it there."""
    result = lint_scenario(_write(tmp_path, _valid_body()))
    assert not any(w.code == "assistant_not_pinned" for w in result.warnings)
