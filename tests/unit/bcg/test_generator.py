from __future__ import annotations

import re
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from judge_it.bcg.generator import BCG_VERSION, generate_bcd
from judge_it.core.scenario_loader import BCD_DELIMITER, load


# --- generate_bcd helpers ---------------------------------------------------

def _patch_llm_returning(bcd_markdown: str):
    """Patch judge_it.bcg.generator.get_client so the LLM returns {bcd_markdown: ...}.
    Returns the mock client object for assertions on call args.
    """
    mock_client = AsyncMock()
    mock_client.provider = "gemini"
    mock_client.model = "gemini-2.5-pro"
    mock_client.complete_json = AsyncMock(return_value={"bcd_markdown": bcd_markdown})
    mock_client.last_usage = None
    return patch("judge_it.bcg.generator.get_client", return_value=mock_client), mock_client


def _write_prompt(tmp_path: Path, body: str = "You are Michelle from Resurgent.") -> Path:
    p = tmp_path / "bot-prompt.txt"
    p.write_text(body, encoding="utf-8")
    return p


# --- 1: writes file with header + body --------------------------------------

@pytest.mark.asyncio
async def test_generate_bcd_writes_file(tmp_path):
    prompt = _write_prompt(tmp_path)
    out = tmp_path / "bcd.md"
    patcher, _ = _patch_llm_returning(
        "# Barge Context — Michelle (Resurgent)\n\n## Modules (in order)\n1. Greeting — hello"
    )
    with patcher:
        path = await generate_bcd(prompt, out, api_key="fake-key")
    assert path == out
    content = out.read_text(encoding="utf-8")
    # Header has metadata for traceability
    assert "Generated from:" in content
    assert "Generated at:" in content
    assert f"Generator: bcg v{BCG_VERSION} (gemini-2.5-pro)" in content
    # Body contains LLM-generated sections
    assert "## Modules (in order)" in content
    assert "1. Greeting — hello" in content


# --- 2: refuses overwrite without --regenerate ------------------------------

@pytest.mark.asyncio
async def test_refuses_overwrite_without_regenerate(tmp_path):
    prompt = _write_prompt(tmp_path)
    out = tmp_path / "bcd.md"
    out.write_text("# pre-existing hand-edited BCD\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="--regenerate"):
        await generate_bcd(prompt, out, api_key="fake-key")
    # File not touched
    assert out.read_text(encoding="utf-8") == "# pre-existing hand-edited BCD\n"


# --- 3: --regenerate overwrites --------------------------------------------

@pytest.mark.asyncio
async def test_regenerate_overwrites(tmp_path):
    prompt = _write_prompt(tmp_path)
    out = tmp_path / "bcd.md"
    out.write_text("# stale content\n", encoding="utf-8")
    patcher, _ = _patch_llm_returning(
        "# Barge Context — Fresh\n\n## Modules (in order)\n1. Greeting — new"
    )
    with patcher:
        await generate_bcd(prompt, out, api_key="fake-key", regenerate=True)
    content = out.read_text(encoding="utf-8")
    assert "# Barge Context — Fresh" in content
    assert "stale content" not in content


# --- 4: missing prompt file -------------------------------------------------

@pytest.mark.asyncio
async def test_missing_prompt_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="bot prompt file not found"):
        await generate_bcd(
            tmp_path / "nope.txt",
            tmp_path / "bcd.md",
            api_key="fake-key",
        )


# --- 5: missing api key -----------------------------------------------------

@pytest.mark.asyncio
async def test_missing_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("JUDGE_IT_GEMINI_API_KEY", raising=False)
    # Run from tmp_path so judge_it's Settings() doesn't pick up the project's
    # `.env` file (which would set the key and bypass the "missing" branch).
    monkeypatch.chdir(tmp_path)
    prompt = _write_prompt(tmp_path)
    with pytest.raises(ValueError, match="JUDGE_IT_GEMINI_API_KEY"):
        await generate_bcd(prompt, tmp_path / "bcd.md")


# --- api_key check fires before output_path-exists check --------------------

@pytest.mark.asyncio
async def test_missing_api_key_raises_before_output_exists_check(tmp_path, monkeypatch):
    """UX: validate api_key before the overwrite guard so users hit one error
    trip ("missing key") instead of two (FileExists → re-run with --regenerate
    → missing key).
    """
    monkeypatch.delenv("JUDGE_IT_GEMINI_API_KEY", raising=False)
    # Run from tmp_path so judge_it's Settings() doesn't pick up the project's
    # `.env` file (which would set the key and bypass the "missing" branch).
    monkeypatch.chdir(tmp_path)
    prompt = _write_prompt(tmp_path)
    out = tmp_path / "bcd.md"
    out.write_text("pre-existing\n", encoding="utf-8")  # would trip FileExists check
    with pytest.raises(ValueError, match="JUDGE_IT_GEMINI_API_KEY"):
        await generate_bcd(prompt, out)  # no regenerate, no key — key error wins
    # Pre-existing file untouched
    assert out.read_text(encoding="utf-8") == "pre-existing\n"


# --- 6: header format -------------------------------------------------------

@pytest.mark.asyncio
async def test_header_format(tmp_path):
    prompt = _write_prompt(tmp_path)
    out = tmp_path / "bcd.md"
    patcher, _ = _patch_llm_returning(
        "# Barge Context — Foo (Bar Inc)\n\n## Modules (in order)\n1. Greeting — hi"
    )
    with patcher:
        await generate_bcd(prompt, out, model="gemini-2.5-pro", api_key="fake-key")
    content = out.read_text(encoding="utf-8")
    # H1 comes from the LLM, metadata block follows it
    lines = content.splitlines()
    assert lines[0].startswith("# Barge Context"), f"first line was: {lines[0]!r}"
    assert any(line.startswith("Generated from:") and str(prompt) in line for line in lines[:6])
    # ISO8601 with Z or +00:00; isoformat(timespec=seconds) → e.g. 2026-05-14T01:23:45+00:00
    # Matches PRD §6.11 example: `Z` suffix form, not `+00:00`.
    assert any(
        re.match(r"Generated at: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", line)
        for line in lines[:6]
    )
    assert any(
        line == f"Generator: bcg v{BCG_VERSION} (gemini-2.5-pro)" for line in lines[:6]
    )


# --- _split_llm_markdown fallback when LLM omits leading H1 ----------------

@pytest.mark.asyncio
async def test_synthesizes_default_h1_when_llm_omits_one(tmp_path):
    """If the LLM returns body content without a leading `# ...` heading,
    _split_llm_markdown falls back to a generic `# Barge Context` so the
    output file is still well-formed.
    """
    prompt = _write_prompt(tmp_path)
    out = tmp_path / "bcd.md"
    patcher, _ = _patch_llm_returning("## Modules (in order)\n1. Greeting — hi")
    with patcher:
        await generate_bcd(prompt, out, api_key="fake-key")
    content = out.read_text(encoding="utf-8")
    assert content.startswith("# Barge Context"), f"first line was: {content.splitlines()[0]!r}"
    assert "## Modules (in order)" in content
    assert "1. Greeting — hi" in content


# --- env-var API key fallback ----------------------------------------------

@pytest.mark.asyncio
async def test_api_key_from_env_var_when_kwarg_absent(tmp_path, monkeypatch):
    """Symmetric coverage with test_missing_api_key: when the env var IS set
    and no kwarg is passed, generate_bcd should succeed using the env key.
    """
    monkeypatch.setenv("JUDGE_IT_GEMINI_API_KEY", "env-key")
    prompt = _write_prompt(tmp_path)
    out = tmp_path / "bcd.md"
    patcher, mock = _patch_llm_returning("# Barge Context — Bot\n\n## Modules\n1. Greeting")
    with patcher:
        await generate_bcd(prompt, out)  # no api_key kwarg
    mock.complete_json.assert_called_once()
    assert out.exists()


# --- LLM call args (max_tokens, temperature, system prompt) -----------------

@pytest.mark.asyncio
async def test_complete_json_called_with_expected_args(tmp_path):
    """Lock the default extraction params so a future refactor can't silently
    change cost/quality characteristics.
    """
    prompt = _write_prompt(tmp_path, body="bot prompt body here")
    out = tmp_path / "bcd.md"
    patcher, mock = _patch_llm_returning("# Barge Context — Bot\n\n## Modules\n1. x")
    with patcher:
        await generate_bcd(prompt, out, api_key="fake-key")
    kwargs = mock.complete_json.await_args.kwargs
    assert kwargs["max_tokens"] == 16000  # bumped: Pro's thinking eats the budget
    assert kwargs["temperature"] == 0.2
    assert "BCG Extraction Prompt" in kwargs["system"]  # extraction prompt loaded
    assert "bot prompt body here" in kwargs["messages"][0]["content"]


# --- parent-directory creation ----------------------------------------------

@pytest.mark.asyncio
async def test_creates_missing_parent_directories(tmp_path):
    """mkdir(parents=True, exist_ok=True) so users don't have to pre-create
    nested output directories.
    """
    prompt = _write_prompt(tmp_path)
    out = tmp_path / "deep" / "nested" / "bcd.md"
    patcher, _ = _patch_llm_returning("# Barge Context — Bot\n\n## Modules\n1. x")
    with patcher:
        await generate_bcd(prompt, out, api_key="fake-key")
    assert out.exists()


# --- 6b: invalid LLM response shape ----------------------------------------

@pytest.mark.asyncio
async def test_invalid_llm_response_raises_value_error(tmp_path):
    """Defensive: if the LLM omits bcd_markdown, we raise a clear ValueError
    rather than writing a half-formed file.
    """
    prompt = _write_prompt(tmp_path)
    mock_client = AsyncMock()
    mock_client.provider = "gemini"
    mock_client.model = "gemini-2.5-pro"
    mock_client.complete_json = AsyncMock(return_value={"oops": "no bcd_markdown"})
    mock_client.last_usage = None
    with patch("judge_it.bcg.generator.get_client", return_value=mock_client):
        with pytest.raises(ValueError, match="bcd_markdown"):
            await generate_bcd(prompt, tmp_path / "bcd.md", api_key="fake-key")
    assert not (tmp_path / "bcd.md").exists()


# --- scenario_loader: BCD apply / reject / no-op ---------------------------

def _write_scenario(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "scenario.yaml"
    p.write_text(textwrap.dedent(body))
    return p


# --- 7: scenario_loader applies BCD ----------------------------------------

def test_scenario_loader_applies_bcd(tmp_path):
    bcd = tmp_path / "bargin-context.md"
    bcd.write_text("# Barge Context — Bot\n\n## Modules\n1. Greeting", encoding="utf-8")
    p = _write_scenario(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: "You are a caller."
        evaluation_prompt: ok
        barge_in:
          enabled: true
          mode: llm
          bot_context_path: "bargin-context.md"
        """,
    )
    scenario = load(p)
    assert BCD_DELIMITER in scenario.persona_rendered
    assert "## Modules" in scenario.persona_rendered
    assert "1. Greeting" in scenario.persona_rendered
    # Original persona content preserved
    assert "You are a caller." in scenario.persona_rendered


# --- 8: scenario_loader rejects missing BCD --------------------------------

def test_scenario_loader_rejects_missing_bcd(tmp_path):
    p = _write_scenario(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: hi
        evaluation_prompt: ok
        barge_in:
          enabled: true
          mode: llm
          bot_context_path: "does-not-exist.md"
        """,
    )
    with pytest.raises(ValueError, match="bot_context_path.*not found"):
        load(p)


# --- 9: no BCD → persona unchanged -----------------------------------------

def test_scenario_loader_no_bcd_does_not_change_persona(tmp_path):
    p = _write_scenario(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: "raw persona text"
        evaluation_prompt: ok
        """,
    )
    scenario = load(p)
    assert BCD_DELIMITER not in scenario.persona_rendered
    assert scenario.persona_rendered.startswith("raw persona text")


def test_scenario_loader_no_bcd_llm_mode_without_path(tmp_path):
    """mode: llm without bot_context_path is valid (BCD is optional). Persona
    must not gain the BCD delimiter.
    """
    p = _write_scenario(
        tmp_path,
        """
        name: t
        target: "+1"
        vars: {x: 1}
        persona: "raw"
        evaluation_prompt: ok
        barge_in:
          enabled: true
          mode: llm
        """,
    )
    scenario = load(p)
    assert BCD_DELIMITER not in scenario.persona_rendered


def test_scenario_loader_resolves_bcd_relative_to_scenario_file(tmp_path, monkeypatch):
    """bot_context_path must resolve relative to the scenario file's parent —
    NOT the current working directory.
    """
    subdir = tmp_path / "clients" / "acme"
    subdir.mkdir(parents=True)
    bcd = subdir / "bargin-context.md"
    bcd.write_text("# Barge Context — Acme", encoding="utf-8")
    p = subdir / "scenario.yaml"
    p.write_text(textwrap.dedent("""
        name: t
        target: "+1"
        vars: {x: 1}
        persona: "p"
        evaluation_prompt: ok
        barge_in:
          enabled: true
          mode: llm
          bot_context_path: "bargin-context.md"
    """))
    # Run from a different cwd to prove resolution doesn't depend on it.
    monkeypatch.chdir(tmp_path)
    scenario = load(p)
    assert BCD_DELIMITER in scenario.persona_rendered
    assert "# Barge Context — Acme" in scenario.persona_rendered


def test_scenario_loader_absolute_bot_context_path(tmp_path):
    bcd = tmp_path / "abs-bcd.md"
    bcd.write_text("# Absolute BCD", encoding="utf-8")
    p = _write_scenario(
        tmp_path,
        f"""
        name: t
        target: "+1"
        vars: {{x: 1}}
        persona: "p"
        evaluation_prompt: ok
        barge_in:
          enabled: true
          mode: llm
          bot_context_path: "{bcd}"
        """,
    )
    scenario = load(p)
    assert "# Absolute BCD" in scenario.persona_rendered
