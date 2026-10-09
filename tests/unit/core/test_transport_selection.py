"""Transport selection: scenario YAML and job payload, not just env.

Three layers are pinned here:

  * the loader accepts and validates `transport:` in a scenario
  * lint rejects a bad value AND an impossible combination (barge + text)
  * the runner prefers the scenario's transport over the deployment default

The last one is the point of the feature: `JUDGE_IT_CALL_TRANSPORT` stays the
default for scenarios that don't care, while a barge scenario can pin itself to
audio and a reasoning scenario to text — without anyone remembering to export a
variable.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from judge_it.core.config import Settings
from judge_it.core.lint import lint_scenario
from judge_it.core.scenario_loader import ALLOWED_TRANSPORTS, _parse_transport
from judge_it.core.scenario_loader import load as load_scenario

SCENARIO = """\
name: "transport selection fixture"
target: "+18445550000"
vars:
  who: "Casey"
persona: |
  You are {{ vars.who }}. Reply ONLY with JSON:
    { "speech": "...", "dtmf": "", "hangup": false }
evaluation_prompt: |
  Return structured output with overall_passed and overall_reasoning.
max_turns: 4
"""


def _write(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "scenario.yaml"
    p.write_text(SCENARIO + textwrap.dedent(extra))
    return p


# ----------------------------------------------------------------------
# parsing


def test_parse_transport_accepts_known_values_case_insensitively():
    for value in ALLOWED_TRANSPORTS:
        assert _parse_transport(value.upper()) == value
        assert _parse_transport(f"  {value}  ") == value


def test_parse_transport_none_means_use_the_deployment_default():
    assert _parse_transport(None) is None


@pytest.mark.parametrize("bad", ["twilio", "livekit", "", 7, True])
def test_parse_transport_rejects_anything_else(bad):
    with pytest.raises(ValueError):
        _parse_transport(bad)


# ----------------------------------------------------------------------
# loader


def test_loader_reads_transport_from_yaml(tmp_path):
    scenario = load_scenario(_write(tmp_path, '\ntransport: "text"\n'))
    assert scenario.transport == "text"


def test_loader_defaults_transport_to_none(tmp_path):
    """Pre-existing scenarios have no `transport:` and must keep working."""
    assert load_scenario(_write(tmp_path)).transport is None


def test_loader_rejects_an_unknown_transport(tmp_path):
    with pytest.raises(ValueError, match="transport"):
        load_scenario(_write(tmp_path, '\ntransport: "carrier-pigeon"\n'))


# ----------------------------------------------------------------------
# lint


def test_lint_passes_a_valid_transport(tmp_path):
    result = lint_scenario(_write(tmp_path, '\ntransport: "sip"\n'))
    assert [e.code for e in result.errors] == []


def test_lint_flags_an_invalid_transport(tmp_path):
    result = lint_scenario(_write(tmp_path, '\ntransport: "pigeon"\n'))
    assert "invalid_transport" in {e.code for e in result.errors}


def test_lint_rejects_barge_on_the_text_transport(tmp_path):
    """The capability check — caught before anything is dialled or spent.

    Text exchanges whole turns over HTTP, so there is no bot audio to interrupt.
    """
    result = lint_scenario(
        _write(
            tmp_path,
            """
            transport: "text"
            barge_in:
              enabled: true
              mode: scheduled
              triggers:
                - at_turn: 2
                  say: "wait, sorry"
            """,
        )
    )
    codes = {e.code for e in result.errors}
    assert "transport_capability" in codes
    message = " ".join(e.message for e in result.errors)
    assert "no bot audio to interrupt" in message


def test_lint_allows_barge_on_sip(tmp_path):
    result = lint_scenario(
        _write(
            tmp_path,
            """
            transport: "sip"
            barge_in:
              enabled: true
              mode: scheduled
              triggers:
                - at_turn: 2
                  say: "wait, sorry"
            """,
        )
    )
    assert "transport_capability" not in {e.code for e in result.errors}


def test_lint_allows_disabled_barge_on_text(tmp_path):
    """`barge_in.enabled: false` is inert, so it must not trip the check."""
    result = lint_scenario(
        _write(
            tmp_path,
            """
            transport: "text"
            barge_in:
              enabled: false
            """,
        )
    )
    assert "transport_capability" not in {e.code for e in result.errors}


# ----------------------------------------------------------------------
# runner resolution: scenario beats env


def _settings(transport: str) -> Settings:
    kwargs = dict(
        _env_file=None,
        twilio_account_sid="AC_test",
        twilio_auth_token="token_test",
        twilio_phone_number="+10000000000",
        deepgram_api_key="dg_test",
        public_url="https://judge_it.example.com",
        call_transport=transport,
    )
    if transport == "sip":
        # Settings validates SIP config at load time, so a sip fixture has to
        # supply it — that validation is itself worth relying on.
        kwargs.update(
            sip_destination_host="581mqcm8zvu.sip.livekit.cloud:5060",
            sip_trunk_number="1205",
        )
    return Settings(**kwargs)


@pytest.mark.parametrize(
    "scenario_transport, env_transport, expected_is_text",
    [
        ("text", "sip", True),    # scenario opts in despite a SIP deployment
        ("sip", "text", False),   # scenario opts out despite a text deployment
        (None, "text", True),     # silent scenario inherits the default
        (None, "sip", False),
    ],
)
def test_scenario_transport_overrides_the_deployment_default(
    tmp_path, scenario_transport, env_transport, expected_is_text
):
    extra = f'\ntransport: "{scenario_transport}"\n' if scenario_transport else ""
    scenario = load_scenario(_write(tmp_path, extra))
    settings = _settings(env_transport)

    # Mirrors the resolution in runner._run_v2_scenario_body.
    resolved = scenario.transport or settings.call_transport
    assert (resolved == "text") is expected_is_text


# ----------------------------------------------------------------------
# is_test: also YAML-configurable, same scenario-beats-env shape


def test_loader_reads_is_test_from_yaml(tmp_path):
    assert load_scenario(_write(tmp_path, "\nis_test: true\n")).is_test is True
    assert load_scenario(_write(tmp_path, "\nis_test: false\n")).is_test is False


def test_loader_defaults_is_test_to_none(tmp_path):
    """None, not False — so the deployment setting is what applies."""
    assert load_scenario(_write(tmp_path)).is_test is None


@pytest.mark.parametrize("bad", ['"false"', '"true"', "yes-please", "1"])
def test_loader_rejects_a_non_boolean_is_test(tmp_path, bad):
    """A quoted "false" is truthy — coercing it would invert the author's intent."""
    with pytest.raises(ValueError, match="is_test"):
        load_scenario(_write(tmp_path, f"\nis_test: {bad}\n"))


def test_lint_flags_a_non_boolean_is_test(tmp_path):
    result = lint_scenario(_write(tmp_path, '\nis_test: "false"\n'))
    assert "invalid_is_test" in {e.code for e in result.errors}


def test_lint_flags_is_test_on_a_voice_transport(tmp_path):
    """The voice path carries the intent as a SIP header judge_it sets itself, so
    the key would be silently ignored."""
    result = lint_scenario(
        _write(tmp_path, '\ntransport: "sip"\nis_test: true\n')
    )
    assert "transport_capability" in {e.code for e in result.errors}
    assert "X-Is-Test" in " ".join(e.message for e in result.errors)


def test_lint_allows_is_test_on_text(tmp_path):
    result = lint_scenario(
        _write(tmp_path, '\ntransport: "text"\nis_test: false\nrecipient_id: "2015550137"\n')
    )
    assert [e.code for e in result.errors] == []


def test_lint_allows_is_test_with_no_pinned_transport(tmp_path):
    """Unpinned means the deployment default decides, which may well be text."""
    result = lint_scenario(_write(tmp_path, "\nis_test: true\n"))
    assert [e.code for e in result.errors] == []


def test_job_config_carries_a_transport():
    from judge_it.jobs.models import JobConfig

    assert JobConfig(target="+18445550000").transport is None
    assert JobConfig(target="+18445550000", transport="text").transport == "text"


def test_generator_stamps_transport_onto_the_scenario_doc():
    """So a generated scenario records what it ran on, not the server's env."""
    from judge_it.jobs.generator import normalise_doc

    doc, changed = normalise_doc({"name": "x"}, target="+18445550000", transport="text")
    assert doc["transport"] == "text"
    assert changed is True

    # No transport requested => key left absent, deployment default applies.
    doc2, _ = normalise_doc({"name": "x"}, target="+18445550000")
    assert "transport" not in doc2


# ----------------------------------------------------------------------
# --transport: the CLI flag layer


def test_resolve_settings_none_leaves_the_env_default_alone(monkeypatch):
    from judge_it.cli.main import resolve_settings

    monkeypatch.setenv("JUDGE_IT_CALL_TRANSPORT", "text")
    assert resolve_settings(None).call_transport == "text"


def test_resolve_settings_flag_overrides_the_env_default(monkeypatch):
    """`--transport text` on a sip deployment, without editing .env."""
    from judge_it.cli.main import resolve_settings

    monkeypatch.setenv("JUDGE_IT_CALL_TRANSPORT", "sip")
    monkeypatch.setenv("JUDGE_IT_SIP_DESTINATION_HOST", "x.sip.livekit.cloud:5060")
    monkeypatch.setenv("JUDGE_IT_SIP_TRUNK_NUMBER", "1205")
    assert resolve_settings("text").call_transport == "text"
    # Case and whitespace tolerated, same as the YAML parser.
    assert resolve_settings("  TEXT ").call_transport == "text"


@pytest.mark.parametrize("bad", ["pigeon", "websocket", ""])
def test_resolve_settings_rejects_an_unknown_transport(bad):
    """Exit code 2, not a traceback — it is a usage error."""
    import typer
    from judge_it.cli.main import resolve_settings

    with pytest.raises(typer.Exit) as exc:
        resolve_settings(bad)
    assert exc.value.exit_code == 2


def test_resolve_settings_flag_cannot_defeat_a_scenario_pin(tmp_path):
    """The documented precedence: scenario YAML > --transport > env.

    A barge scenario pins sip because it needs audio. `--transport text` must
    not be able to reroute it, or lint's capability check is bypassed at will.
    """
    from judge_it.cli.main import resolve_settings

    scenario = load_scenario(_write(tmp_path, '\ntransport: "sip"\n'))
    settings = resolve_settings("text")
    # Mirrors runner._run_v2_scenario_body's resolution.
    assert (scenario.transport or settings.call_transport) == "sip"


# ----------------------------------------------------------------------
# assistant_uuid / fc_version: WHICH bot, per scenario


def test_loader_reads_assistant_uuid_and_fc_version(tmp_path):
    sc = load_scenario(
        _write(
            tmp_path,
            """
            assistant_uuid: "00000000-0000-4000-8000-00000000dead"
            fc_version: 10
            """,
        )
    )
    assert sc.assistant_uuid == "00000000-0000-4000-8000-00000000dead"
    assert sc.fc_version == 10


def test_loader_defaults_bot_selection_to_none(tmp_path):
    sc = load_scenario(_write(tmp_path))
    assert sc.assistant_uuid is None and sc.fc_version is None


@pytest.mark.parametrize("bad", ["fc_version: -1", "fc_version: true", 'fc_version: "10"'])
def test_loader_rejects_a_bad_fc_version(tmp_path, bad):
    """`true` matters: bool is an int subclass, so it would pass as version 1."""
    with pytest.raises(ValueError, match="fc_version"):
        load_scenario(_write(tmp_path, f"\n{bad}\n"))


def test_loader_rejects_an_empty_assistant_uuid(tmp_path):
    with pytest.raises(ValueError, match="assistant_uuid"):
        load_scenario(_write(tmp_path, '\nassistant_uuid: ""\n'))


def test_lint_flags_bot_selection_on_a_voice_transport(tmp_path):
    """Over voice the assistant is whoever answers `target` — these are ignored."""
    result = lint_scenario(
        _write(
            tmp_path,
            """
            transport: "sip"
            assistant_uuid: "00000000-0000-4000-8000-00000000dead"
            fc_version: 10
            """,
        )
    )
    assert [e.code for e in result.errors] == ["transport_capability"] * 2


def test_lint_allows_bot_selection_on_text(tmp_path):
    result = lint_scenario(
        _write(
            tmp_path,
            """
            transport: "text"
            assistant_uuid: "00000000-0000-4000-8000-00000000dead"
            fc_version: 10
            recipient_id: "2015550137"
            """,
        )
    )
    assert [e.code for e in result.errors] == []


def test_lint_flags_a_bad_fc_version(tmp_path):
    result = lint_scenario(_write(tmp_path, '\ntransport: "text"\nfc_version: -3\n'))
    assert "invalid_fc_version" in {e.code for e in result.errors}


# ----------------------------------------------------------------------
# recipient_id is REQUIRED for text — it reaches the client's CRM


def test_lint_requires_recipient_id_on_a_text_scenario(tmp_path):
    """Caught before anything is POSTed. OSV assigns recipient_id to the bot's
    `phone_number`, from where it flows into integration payloads — so judge_it
    refuses to invent one, and a missing key must fail loudly rather than at
    turn 1."""
    result = lint_scenario(_write(tmp_path, '\ntransport: "text"\n'))
    assert "missing_recipient_id" in {e.code for e in result.errors}


def test_lint_does_not_require_recipient_id_for_voice(tmp_path):
    """Voice resolves the caller from the Twilio caller id, so the key is
    meaningless there and must not be demanded."""
    result = lint_scenario(_write(tmp_path, '\ntransport: "sip"\n'))
    assert "missing_recipient_id" not in {e.code for e in result.errors}


def test_lint_does_not_require_recipient_id_when_transport_is_unpinned(tmp_path):
    """An unpinned scenario may well be a voice scenario — do not break the 299
    hand-written ones that predate text mode."""
    result = lint_scenario(_write(tmp_path))
    assert "missing_recipient_id" not in {e.code for e in result.errors}


# --- declared metrics reach the Scenario ------------------------------------

def test_metrics_parse_into_specs(tmp_path):
    from judge_it.core.scenario_loader import load

    p = tmp_path / "s.yaml"
    p.write_text(
        'name: t\n'
        'target: "+18005551234"\n'
        'vars: {}\n'
        'persona: "p"\n'
        'evaluation_prompt: "e"\n'
        'metrics:\n'
        '  - key: required_gates_passed\n'
        '    description: required gates passed\n'
        '    type: string\n'
        '    example: "4/4"\n'
        '  - key: unanswered\n'
        '    type: array\n'
        '    required: false\n',
        encoding="utf-8",
    )
    sc = load(p)

    assert [m.key for m in sc.metrics] == ["required_gates_passed", "unanswered"]
    first = sc.metrics[0]
    assert (first.type, first.example, first.required) == ("string", "4/4", True)
    assert sc.metrics[1].required is False
    assert sc.metrics[1].type == "array"


def test_no_metrics_key_yields_an_empty_list(tmp_path):
    from judge_it.core.scenario_loader import load

    p = tmp_path / "s.yaml"
    p.write_text(
        'name: t\ntarget: "+18005551234"\nvars: {}\n'
        'persona: "p"\nevaluation_prompt: "e"\n',
        encoding="utf-8",
    )
    assert load(p).metrics == []


def test_type_defaults_to_string_and_is_lowercased(tmp_path):
    from judge_it.core.scenario_loader import load

    p = tmp_path / "s.yaml"
    p.write_text(
        'name: t\ntarget: "+18005551234"\nvars: {}\n'
        'persona: "p"\nevaluation_prompt: "e"\n'
        'metrics:\n  - key: a\n  - key: b\n    type: BOOLEAN\n',
        encoding="utf-8",
    )
    sc = load(p)
    assert sc.metrics[0].type == "string"
    assert sc.metrics[1].type == "boolean"
