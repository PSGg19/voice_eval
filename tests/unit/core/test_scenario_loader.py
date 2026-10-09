from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from vjudge.core.scenario_loader import load


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "scenario.yaml"
    p.write_text(dedent(body))
    return p


def test_load_valid_scenario(tmp_path):
    p = _write(
        tmp_path,
        """
        name: example scenario
        target: "+18005551234"
        vars:
          name: Iris Crawford
          creditor: Verizon Wireless
        persona: "You are {{ vars.name }} calling about {{ vars.creditor }}."
        evaluation_prompt: "Evaluate the call with {{ vars.creditor }}."
        max_turns: 25
        """,
    )
    scenario = load(p)
    assert scenario.name == "example scenario"
    assert scenario.target == "+18005551234"
    assert scenario.vars["name"] == "Iris Crawford"
    assert scenario.max_turns == 25
    assert "Iris Crawford" in scenario.persona_rendered
    assert "Verizon Wireless" in scenario.evaluation_prompt_rendered
    assert scenario.source_path == str(p)
    assert scenario.llm.caller_model is None
    assert scenario.llm.evaluator_model is None
    assert scenario.llm.provider is None
    assert scenario.llm.temperature == pytest.approx(0.3)
    assert scenario.barge_in is None


def test_missing_required_key(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        """,
    )
    with pytest.raises(ValueError, match="evaluation_prompt"):
        load(p)


def test_unresolved_jinja_reference(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars:
          name: Iris
        persona: "hello {{ vars.naem }}"
        evaluation_prompt: ok
        """,
    )
    with pytest.raises(ValueError, match="naem"):
        load(p)


def test_nested_vars_substitution(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars:
          card:
            last_4: "0125"
        persona: "last four {{ vars.card.last_4 }}"
        evaluation_prompt: ok
        """,
    )
    scenario = load(p)
    assert "0125" in scenario.persona_rendered


def test_llm_block_parsed(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        llm:
          caller_model: gemini-2.5-flash
          evaluator_model: gemini-2.5-pro
          temperature: 0.2
          provider: gemini
        """,
    )
    scenario = load(p)
    assert scenario.llm.caller_model == "gemini-2.5-flash"
    assert scenario.llm.evaluator_model == "gemini-2.5-pro"
    assert scenario.llm.temperature == pytest.approx(0.2)
    assert scenario.llm.provider == "gemini"


def test_barge_in_disabled_parses_to_disabled_config(tmp_path):
    """barge_in present but enabled: false → parsed BargeInConfig with
    enabled=False, mode/triggers untouched. Engine treats this as 'off'.
    """
    from vjudge.core.models import BargeInConfig

    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        barge_in:
          enabled: false
        """,
    )
    scenario = load(p)
    assert isinstance(scenario.barge_in, BargeInConfig)
    assert scenario.barge_in.enabled is False


def test_unknown_top_level_key_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        max_trns: 50
        """,
    )
    with pytest.raises(ValueError, match="unknown top-level key.*max_trns"):
        load(p)


def test_unknown_llm_key_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        llm:
          caller_modle: gpt-4
        """,
    )
    with pytest.raises(ValueError, match="unknown key.*caller_modle"):
        load(p)


@pytest.mark.parametrize("field", ["persona", "evaluation_prompt"])
def test_non_string_text_field_rejected(tmp_path, field):
    lines = [
        "name: t",
        'target: "+1"',
        "vars: {x: 1}",
        "persona: hi",
        "evaluation_prompt: ok",
    ]
    body = "\n".join(line if not line.startswith(f"{field}:") else f"{field}: 42" for line in lines)
    p = tmp_path / "scenario.yaml"
    p.write_text(body)
    with pytest.raises(ValueError, match=f"`{field}` must be a string"):
        load(p)


def test_vars_must_be_a_mapping(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: not-a-dict
        persona: hi
        evaluation_prompt: ok
        """,
    )
    with pytest.raises(ValueError, match="`vars` must be a mapping"):
        load(p)


def test_llm_temperature_null_raises_clear_value_error(tmp_path):
    """`temperature: null` (or any non-numeric) raises ValueError, not the
    confusing TypeError that bare float(None) would surface. Matches the
    file's pattern of wrapping numeric coercions.
    """
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        llm:
          temperature: null
        """,
    )
    with pytest.raises(ValueError, match="`llm.temperature` must be a number"):
        load(p)


def test_llm_must_be_a_mapping(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        llm: gemini
        """,
    )
    with pytest.raises(ValueError, match="`llm` must be a mapping"):
        load(p)


def test_barge_in_must_be_a_mapping(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        barge_in: enabled
        """,
    )
    with pytest.raises(ValueError, match="`barge_in` must be a mapping"):
        load(p)


def test_empty_file_rejected(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    with pytest.raises(ValueError, match="must contain a YAML mapping"):
        load(p)


def test_max_turns_must_be_positive(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        max_turns: 0
        """,
    )
    with pytest.raises(ValueError, match="`max_turns` must be >= 1"):
        load(p)


def test_max_turns_must_be_integer(tmp_path):
    p = _write(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        max_turns: not-a-number
        """,
    )
    with pytest.raises(ValueError, match="`max_turns` must be an integer"):
        load(p)


def test_malformed_yaml_mentions_path(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("name: t\ntarget: '+1'\n  bad_indent: oops\n")
    with pytest.raises(ValueError, match=f"failed to parse YAML.*{p.name}"):
        load(p)
