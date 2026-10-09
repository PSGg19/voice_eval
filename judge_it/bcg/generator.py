"""Barge Context Generator — reads a bot's system prompt, emits a BCD markdown file."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from judge_it.llm import get_client

logger = logging.getLogger(__name__)

BCG_VERSION = "0.1.0"
# Prompt template lives under engine/prompts/ post-Task-9 restructure;
# bcg.generator is one level deeper so we go up to judge_it/ then into engine.
PROMPT_TEMPLATE_PATH = (
    Path(__file__).parent.parent / "engine" / "prompts" / "bcg_extract.md"
)
DEFAULT_MODEL = "gemini-2.5-pro"
# Gemini 2.5 Pro consumes `max_output_tokens` on internal "thinking"
# BEFORE emitting any response text. With 4000 tokens, thinking ate
# the whole budget on real Resurgent prompts and the API returned an
# empty string. 16000 gives Pro plenty of headroom for ~6000 tokens
# of thinking + 4-6k tokens of BCD markdown output. One-shot per
# client so cost is negligible (~$0.10-0.20 per regenerate).
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TEMPERATURE = 0.2


def _build_header(bot_h1: str, prompt_path: Path, model: str) -> str:
    """Compose the BCD header from the LLM's H1 line + traceability metadata.
    Format matches PRD §6.11: H1 first, then metadata lines, then a blank line.
    """
    # PRD §6.11 example uses the `Z` suffix form; isoformat() emits `+00:00`.
    generated_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return (
        f"{bot_h1}\n\n"
        f"Generated from: {prompt_path}\n"
        f"Generated at: {generated_at}\n"
        f"Generator: bcg v{BCG_VERSION} ({model})\n"
    )


def _split_llm_markdown(bcd_markdown: str) -> tuple[str, str]:
    """Return (h1_line, rest_of_body). If the LLM didn't lead with an H1,
    synthesize a generic one so the output stays parseable.
    """
    stripped = bcd_markdown.lstrip()
    if stripped.startswith("# "):
        first_line, _, rest = stripped.partition("\n")
        return first_line.rstrip(), rest.lstrip("\n")
    return "# Barge Context", stripped


async def generate_bcd(
    prompt_path: Union[str, Path],
    output_path: Union[str, Path],
    model: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
    regenerate: bool = False,
) -> Path:
    """Read a bot's system prompt, ask the LLM to extract a BCD, write markdown.

    Returns the output path on success. Raises:
      - FileNotFoundError: prompt_path doesn't exist.
      - FileExistsError: output_path exists and regenerate is False (protects
        hand-edits to a previously generated BCD).
      - ValueError: no api_key arg and JUDGE_IT_GEMINI_API_KEY unset; or LLM
        response missing the bcd_markdown field.
    """
    prompt_path = Path(prompt_path)
    output_path = Path(output_path)

    if not prompt_path.exists():
        raise FileNotFoundError(f"bot prompt file not found: {prompt_path}")
    # Validate api_key before the overwrite check so users hit one error trip
    # ("missing key") instead of two (FileExists → regenerate → missing key).
    # Use judge_it's Settings so the JUDGE_IT_GEMINI_API_KEY comes from `.env`
    # the same way `judge_it run` / `judge_it lint` resolve it — raw os.getenv
    # didn't read `.env` and made bcg the odd-one-out command.
    if not api_key:
        from judge_it.core.config import Settings

        try:
            api_key = Settings().gemini_api_key
        except Exception:
            # Settings() requires other fields too (Twilio creds etc); if it
            # bombs, fall back to a focused error so the user sees the right cause.
            api_key = ""
    if not api_key:
        raise ValueError(
            "no API key provided and JUDGE_IT_GEMINI_API_KEY is not set"
        )
    if output_path.exists() and not regenerate:
        raise FileExistsError(
            f"BCD already exists at {output_path}; pass --regenerate to overwrite"
        )

    bot_prompt_content = prompt_path.read_text(encoding="utf-8")
    extraction_prompt = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")

    llm = get_client(model, api_key)
    response = await llm.complete_json(
        system=extraction_prompt,
        messages=[
            {
                "role": "user",
                "content": f"Bot system prompt to analyze:\n\n{bot_prompt_content}",
            }
        ],
        max_tokens=DEFAULT_MAX_TOKENS,
        temperature=DEFAULT_TEMPERATURE,
    )

    bcd_markdown = response.get("bcd_markdown") if isinstance(response, dict) else None
    if not isinstance(bcd_markdown, str) or not bcd_markdown.strip():
        raise ValueError(
            "LLM response missing or empty `bcd_markdown` field; "
            f"got keys: {list(response) if isinstance(response, dict) else type(response).__name__}"
        )

    h1_line, body = _split_llm_markdown(bcd_markdown)
    header = _build_header(h1_line, prompt_path, model)
    output = header + "\n" + body
    if not output.endswith("\n"):
        output += "\n"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")
    logger.info("BCD written to %s", output_path)
    return output_path
