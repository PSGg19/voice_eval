"""Scenario + campaign generation.

Turns (prompt, transcript, metadata) into an vjudge V2 scenario that replicates
the reported issue, then a campaign tying them together.

**LLM transport is the OpenAI chat-completions schema over plain httpx.** No
`openai` SDK: it is one non-streaming POST, and OpenRouter speaks the identical
schema — so switching providers is `VJUDGE_LLM_BASE_URL`, not a rewrite. That
is `docs/platform-evolution.md` decision #5 ("LLM speaks the OpenAI schema")
arriving early, for free.

**Validation uses vjudge's own linter AND its loader**, never a hand-rolled
check: generate → normalise → validate → feed the errors back once → validate
again. Both are needed. The linter encodes the schema, but it passed a scenario
whose `persona` was a YAML list, which `load_scenario` then rejected — and since
the campaign runner calls the loader, that combination is a false green: the job
reports `ready` and only breaks when someone runs it.

Two things are also fixed deterministically rather than asked of the model,
because we already know the right answer and a wasted repair round-trip is
expensive: the dialled `target` (the model dropped its `+` and failed E.164), and
string-vs-list fields.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from collections.abc import Sequence
from typing import Any

import httpx
import yaml

from vjudge.core.lint import lint_scenario
from vjudge.jobs.models import GeneratedScenario, JobConfig, SeededCall
from vjudge.jobs.workspace import JobWorkspace

logger = logging.getLogger(__name__)

LLM_BASE_URL = os.getenv("VJUDGE_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.getenv("VJUDGE_JOB_GENERATOR_MODEL", "gpt-4.1")
LLM_TIMEOUT_S = float(os.getenv("VJUDGE_LLM_TIMEOUT_S", "180"))
LLM_MAX_TOKENS = int(os.getenv("VJUDGE_LLM_MAX_TOKENS", "8000"))

# Transcript/metadata can be enormous; keep the prompt inside a sane budget.
MAX_TRANSCRIPT_CHARS = int(os.getenv("VJUDGE_JOB_MAX_TRANSCRIPT_CHARS", "24000"))
MAX_PROMPT_CHARS = int(os.getenv("VJUDGE_JOB_MAX_PROMPT_CHARS", "24000"))

_FENCE = re.compile(r"```(?:ya?ml|json)?\s*(.*?)```", re.S)


class GeneratorError(RuntimeError):
    """The model could not produce a usable scenario."""


# A worked example in the house style of `clients/*/scenarios/*.yaml`. Showing one
# good scenario turned out to matter far more than describing rules: the prompt
# already SAID "a person, not a script", and the model still returned a
# `- description:/response:` step list whose first entry was
# `{"speech": ""}` — so the caller sat silent for a whole call.
#
# Synthetic data on purpose. The real hand-written scenarios carry live customer
# PII, which does not belong in source; the shape is what teaches, not the values.
EXAMPLE_SCENARIO = {
    "name": "Acme Card · closing must stay in English",
    "target": "+15550001111",
    "vars": {
        "name": "Dana Whitfield",
        "phone_number": "Five Five Five, Two Zero One, Nine Four Four Two",
        "ssn_last_4": "Six One Two Zero",
        "dob": "April ninth, nineteen eighty-eight",
    },
    "persona": (
        "You are {{ vars.name }}, a real person calling Acme Card about your\n"
        "account. Talk like a normal caller — short, natural, a little casual —\n"
        "and react to what the agent ACTUALLY just said. This is a real\n"
        "conversation, not a script: the agent's wording and order will vary, so\n"
        "listen and answer what it asked using the facts below.\n"
        "\n"
        "Facts about you:\n"
        "  - Name: {{ vars.name }}\n"
        "  - Phone on file: {{ vars.phone_number }}\n"
        "  - Last 4 of SSN: {{ vars.ssn_last_4 }}\n"
        "  - Date of birth: {{ vars.dob }}\n"
        "\n"
        "Getting verified: the agent won't recognise your number automatically, so\n"
        "it will ASK for the phone number registered with them — give\n"
        "{{ vars.phone_number }}. Then it may confirm your name (say yes) and ask\n"
        "your SSN last 4 ({{ vars.ssn_last_4 }}). Cooperate with whatever it uses.\n"
        "\n"
        "YOUR GOAL: once verified and you've heard the balance, you are NOT paying\n"
        "today. When it asks to pay, decline (\"No, not today.\"). When it asks if\n"
        "there's anything else, say casually: \"No, that'll be it.\" Then let it\n"
        "give its goodbye.\n"
        "\n"
        "Ground rules:\n"
        "  - \"Please wait while your call is being transferred\" is the phone\n"
        "    system, not the agent — reply with empty speech.\n"
        "  - You are the CALLER; never talk like the agent.\n"
        "  - Never actually pay: if it starts taking a card, bank details or an\n"
        "    amount, say you have to go and hang up.\n"
        "\n"
        "Reply each turn as JSON: {\"speech\": \"...\", \"dtmf\": \"\", \"hangup\": false}\n"
    ),
    "evaluation_prompt": (
        "PASS target: the caller spoke English throughout and, after \"No, that'll\n"
        "be it,\" the bot delivered its closing IN ENGLISH and ended the call.\n"
        "\n"
        "overall_reasoning MUST start with one of: \"PASS: \" / \"BOT FAILURE: \" /\n"
        "\"TEST/LLM FAILURE: \".\n"
        "\n"
        "GATES\n"
        "  1. reached_close — auth happened and the bot reached its closing.\n"
        "  2. english_closing — the closing is in English, not Spanish.\n"
        "  3. no_language_flip — the bot never switched language unprompted.\n"
        "\n"
        "overall_passed = reached_close AND english_closing AND no_language_flip.\n"
        "\n"
        "Return STRICT JSON:\n"
        "{\n"
        '  "overall_passed": true|false,\n'
        '  "overall_reasoning": "prefix — one sentence",\n'
        '  "criteria": [\n'
        '    {"id":1,"name":"reached_close","passed":true,"evidence":"quote"}\n'
        "  ]\n"
        "}\n"
    ),
    "max_turns": 30,
}


SYSTEM_PROMPT = """\
You write vjudge test scenarios. VJudge places a REAL phone call to a voice bot \
and plays the role of a caller, then an LLM judge decides pass/fail.

Your job: given a bot's prompt, ONE faulty production call's transcript, and that \
call's interaction metadata, write a scenario that REPLICATES the reported issue \
so it can be reproduced on demand.

Rules that matter:

1. FIDELITY. Use the REAL values from the metadata (name, account number, DOB, \
SSN last 4, registered phone, amounts, due dates, vehicle). The caller must be \
able to authenticate, because the bug is usually AFTER authentication. Never \
invent an identity when the metadata supplies one.
2. The persona is a PERSON, not a script. It must react to what the agent \
actually says, since wording and ordering vary between runs. Give it facts and a \
goal, not lines to recite.
3. NEVER settle a payment. If the flow reaches card/bank collection, an amount, \
or a <dtmf:...> prompt, the persona says it has to go and hangs up.
4. The evaluation_prompt must define concrete, quotable GATES and demand STRICT \
JSON with "overall_passed", "overall_reasoning" and "criteria".
5. Phone-system noise ("please wait while your call is transferred") is not the \
agent; the persona replies with empty speech.
6. Each caller turn is JSON: {"speech": "...", "dtmf": "", "hangup": false}
7. If the caller speaks a language other than English, set \
`tts.model` to that language's Aura voice — Spanish is `aura-2-javier-es`. The \
default voice is English and mispronounces other languages badly enough that the \
BOT's speech recognition garbles the caller. We also enforce this ourselves, so \
omitting it is recoverable, but set it.

Return ONE JSON object, no prose, no code fence:

{
  "title": "<8 words max, the issue under test>",
  "description": "<one sentence: what went wrong in the source call>",
  "what_we_tested": "<one or two sentences: the path this scenario drives, incl. how it authenticates>",
  "scenario": {
    "name": "<short scenario name>",
    "target": "<the E.164 number given to you>",
    "vars": { "<key>": "<value>", "...": "..." },
    "persona": "<the whole persona as ONE string, newlines and all>",
    "evaluation_prompt": "<the whole evaluation prompt as ONE string>",
    "max_turns": 30
  }
}

`scenario` is a JSON OBJECT, not a YAML string. Do not emit YAML — we generate \
it from your JSON. `persona` and `evaluation_prompt` are each a single JSON \
string (use \\n for line breaks), never an array. `vars` is a flat object of \
strings.

CRITICAL — the persona is the caller LLM's ENTIRE system prompt, and it has no \
idea which turn it is on. So it must read as a CHARACTER BRIEF in prose: who they \
are, the facts they know, what they want, and the ground rules. It must NOT be a \
numbered script or a list of description/response pairs — given a script the \
caller replays step one forever and the call dies in silence.

Below is a COMPLETE, GOOD example. Match its shape, its use of \
`{{ vars.* }}` placeholders, and its level of detail. Change the content to suit \
the bug you are reproducing; do not copy its facts.

EXAMPLE:
"""


def _system_prompt() -> str:
    """The rules, followed by one worked example.

    The example is serialised from EXAMPLE_SCENARIO rather than pasted as text so
    it is guaranteed to be exactly the JSON shape we ask for — an example that
    drifted from the schema would teach the wrong thing.
    """
    return SYSTEM_PROMPT + json.dumps({"scenario": EXAMPLE_SCENARIO}, indent=2)


def _truncate(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    logger.info("Truncated %s from %d to %d chars for the generator", label, len(text), limit)
    return text[:limit] + f"\n…[{label} truncated]…"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, default=str, sort_keys=True)


def _extract_json(raw: str) -> dict[str, Any]:
    """Parse the model's reply, tolerating a code fence or leading prose."""
    raw = (raw or "").strip()
    for candidate in (raw, *(m.group(1) for m in _FENCE.finditer(raw))):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    # Last resort: the outermost {...}
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise GeneratorError(f"model reply was not JSON: {raw[:300]}")


def _llm_config() -> tuple[str, str]:
    """(base_url, model), preferring Settings over os.getenv.

    `.env` is read by pydantic-settings and never exported to the process
    environment, so os.getenv alone silently ignores anything configured there.
    """
    try:
        from vjudge.core.config import Settings

        cfg = Settings()
        return (
            (cfg.llm_base_url or LLM_BASE_URL).rstrip("/"),
            cfg.job_generator_model or LLM_MODEL,
        )
    except Exception:  # pragma: no cover - env-dependent
        return LLM_BASE_URL, LLM_MODEL


async def _chat(messages: list[dict[str, str]], api_key: str) -> str:
    """One OpenAI-schema chat completion. Works against OpenRouter unchanged."""
    if not api_key:
        raise GeneratorError(
            "no LLM API key — set VJUDGE_OPENAI_API_KEY (or point "
            "VJUDGE_LLM_BASE_URL at OpenRouter and use its key)"
        )
    base_url, model = _llm_config()
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as http:
        resp = await http.post(f"{base_url}/chat/completions", json=payload, headers=headers)
    if not resp.is_success:
        raise GeneratorError(f"LLM {resp.status_code}: {resp.text[:400]}")
    body = resp.json()
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise GeneratorError(f"unexpected LLM response shape: {str(body)[:300]}") from exc


def _user_prompt(
    seed: SeededCall, config: JobConfig, bot_prompt: str, scenario_id: str
) -> str:
    return f"""\
Write scenario `{scenario_id}`.

TARGET NUMBER TO DIAL (use verbatim as `target`): {config.target}
LANGUAGES IN SCOPE: {", ".join(config.languages)}
SOURCE CALL: {seed.interaction_uuid}
BOT: {seed.assistant_name or seed.assistant_id}
{f"EXTRA INSTRUCTIONS FROM THE OPERATOR: {config.notes}" if config.notes else ""}

=== BOT PROMPT / FLOW CONFIG ===
{_truncate(bot_prompt, MAX_PROMPT_CHARS, "bot prompt")}

=== FAULTY CALL TRANSCRIPT ===
{_truncate(_as_text(seed.transcript), MAX_TRANSCRIPT_CHARS, "transcript")}

=== INTERACTION METADATA (the real account data — use it) ===
{_truncate(_as_text(seed.metadata), MAX_TRANSCRIPT_CHARS, "metadata")}
"""


def repair_indentation(yaml_text: str) -> str:
    """Fix the "everything after line 1 is indented" quirk, or return as-is.

    Observed twice in a row from the model:

        name: Dispute balance after authentication
         target: "+18883286945"
         vars:
           ...

    A block mapping requires every key at the same indent, so that whole
    document is invalid. De-indenting lines 2+ by their common leading
    whitespace recovers it. Only returned if the result actually parses — never
    guess when the guess can't be checked.
    """
    lines = yaml_text.splitlines()
    if len(lines) < 2:
        return yaml_text
    rest = [ln for ln in lines[1:] if ln.strip()]
    if not rest:
        return yaml_text
    common = min(len(ln) - len(ln.lstrip(" ")) for ln in rest)
    if common == 0:
        return yaml_text
    candidate = "\n".join(
        [lines[0]] + [ln[common:] if ln.strip() else ln for ln in lines[1:]]
    )
    try:
        if isinstance(yaml.safe_load(candidate), dict):
            logger.info("Repaired a scenario indented by %d space(s) after line 1", common)
            return candidate
    except yaml.YAMLError:
        pass
    return yaml_text


def _block_str_representer(dumper, data):
    """Emit multi-line strings as `|` block scalars so files stay readable."""
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


class _ReadableDumper(yaml.SafeDumper):
    pass


_ReadableDumper.add_representer(str, _block_str_representer)

# Fields the loader requires to be strings. The model likes returning `persona`
# as a LIST of instruction lines, which lint happily accepts and the loader then
# rejects with "`persona` must be a string" — a false green that only surfaces
# when the campaign runs. Join them instead of burning a repair round-trip.
_MUST_BE_STRING = ("name", "target", "persona", "evaluation_prompt")


# The caller engine passes `persona` as the LLM's ENTIRE system prompt
# (`engine/caller.py:152` → `_invoke_llm(scenario.persona_rendered, …)`). Nothing
# else tells the model to answer in the turn format, so a persona that omits it
# makes every turn fail its first parse and fall back to STRICT_RETRY_SUFFIX —
# two LLM calls per turn instead of one, for the whole call. Append it if absent.
TURN_CONTRACT = (
    '\n\nReply to every turn as JSON and nothing else: '
    '{"speech": "...", "dtmf": "", "hangup": false}\n'
)


# The caller's TTS voice is a language decision, and the default is English.
# A Spanish persona spoken by aura-2-thalia-en is mispronounced badly enough that
# the BOT's own STT garbles it — so the test does not fail, it becomes
# meaningless, which is worse. Observed live on job_4ccab2353f57: the caller said
# "mi número es <redacted>" in fluent Spanish through the English voice, and the
# bot asked for the number a second time.
#
# Every hand-written Spanish scenario under clients/veros/ already sets this (see
# CB-01-dob-month-day-swap.yaml, which explains it in a comment); job mode was the
# only path that forgot. Only Spanish is proven on these bots — an unknown
# language keeps the English default rather than guessing at a voice name.
_VOICE_BY_LANGUAGE = {"es": "aura-2-javier-es"}


def voice_for_languages(languages: Sequence[str] | None) -> str | None:
    """The caller voice a language set requires, or None to keep the default.

    Any Spanish locale in scope selects the Spanish voice, including a bilingual
    en+es job: those scenarios switch language mid-call, and the hand-written
    bilingual ones (LIVE-18-spanish-switch-and-language-stability.yaml) use the
    Spanish voice throughout for exactly that reason.
    """
    for language in languages or ():
        prefix = str(language).strip().lower().split("-")[0]
        voice = _VOICE_BY_LANGUAGE.get(prefix)
        if voice:
            return voice
    return None


def normalise_doc(
    doc: dict,
    target: str | None = None,
    languages: Sequence[str] | None = None,
    transport: str | None = None,
) -> tuple[dict, bool]:
    """Apply every known correction to a parsed scenario. Returns (doc, changed)."""
    changed = False

    voice = voice_for_languages(languages)
    if voice:
        tts = doc.get("tts")
        if not isinstance(tts, dict):
            tts = {}
        # Never override a voice the model chose deliberately.
        if not str(tts.get("model") or "").strip():
            tts["model"] = voice
            doc["tts"] = tts
            changed = True
            logger.info(
                "Set the caller TTS voice to %s for languages %s",
                voice, ",".join(str(x) for x in languages or ()),
            )

    for key in _MUST_BE_STRING:
        value = doc.get(key)
        if isinstance(value, list):
            doc[key] = "\n".join(str(v) for v in value)
            changed = True
        elif value is not None and not isinstance(value, str):
            doc[key] = str(value)
            changed = True

    if target and doc.get("target") != target:
        doc["target"] = target
        changed = True

    # Stamp the job's transport onto the scenario so the generated file records
    # what it actually ran on, rather than depending on the server's env at run
    # time. The runner honours `transport:` over the deployment default.
    if transport and doc.get("transport") != transport:
        doc["transport"] = transport
        changed = True

    persona = doc.get("persona")
    if isinstance(persona, str) and '"speech"' not in persona:
        doc["persona"] = persona.rstrip() + TURN_CONTRACT
        changed = True
        logger.info("Appended the JSON turn contract to a persona that omitted it")

    return doc, changed


def dump_scenario(doc: dict) -> str:
    """Serialise a scenario dict to YAML that vjudge's loader accepts.

    This is the reason the model is asked for JSON rather than YAML text: when we
    emit the YAML, a YAML syntax error is structurally impossible. Four separate
    model quirks had to be repaired while it was producing YAML itself — a
    one-space indent on every line after the first, `persona` as a list, a target
    missing its `+`, and an unquoted `name: Dispute flow: no balance` whose second
    colon started a nested mapping. Each one cost the single repair attempt, and
    losing that attempt is what turned a recoverable slip into a failed job.
    """
    return yaml.dump(
        doc, Dumper=_ReadableDumper, sort_keys=False, allow_unicode=True, width=100
    )


def normalise_scenario(
    yaml_text: str,
    target: str | None = None,
    languages: Sequence[str] | None = None,
    transport: str | None = None,
) -> str:
    """Coerce known model quirks into what the runner needs.

    Works on the PARSED document, never on text lines. An earlier line-surgery
    version matched `^target:` at column 0 only, so when the model indented the
    document it missed the real key, appended a second one at column 0, and
    turned a fixable indentation error into a duplicate-key error — which then
    became the repair prompt, hiding the actual problem.

    Returns the text unchanged when nothing needs fixing, so a well-formed
    scenario keeps the model's own formatting.
    """
    text = yaml_text
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        text = repair_indentation(text)
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            # Unparseable and unrecoverable: touch nothing. The validator will
            # report the model's real error, which is what the repair pass needs.
            return yaml_text
    if not isinstance(doc, dict):
        return yaml_text

    repaired_text = text is not yaml_text
    doc, changed = normalise_doc(doc, target, languages, transport)

    if not (changed or repaired_text):
        return yaml_text
    logger.info("Normalised generated scenario")
    return dump_scenario(doc)


# A persona written as `- description: … / response: {"speech": …}` pairs. The
# caller engine hands the persona to the LLM as its ENTIRE system prompt, with no
# step counter, so a script is read as "turn 1 = this" forever. Observed live:
# the first entry was `response: {"speech": ""}`, the caller stayed silent for the
# whole call, the bot timed out three times and hung up — and the job still
# reported passed=True, because with eval:false `passed` only means "connected".
_SCRIPT_DESCRIPTION = re.compile(r"^\s*-\s*description\s*:", re.M)
_SCRIPT_RESPONSE = re.compile(r"^\s*response\s*:", re.M)


def persona_problems(persona: str) -> list[str]:
    """Behavioural checks the linter and loader cannot make.

    A structurally valid scenario can still be dead on arrival. These are the
    failure modes worth catching before spending a phone call to discover them.
    """
    problems: list[str] = []
    if not isinstance(persona, str) or not persona.strip():
        return ["persona is empty"]

    if _SCRIPT_DESCRIPTION.search(persona) and _SCRIPT_RESPONSE.search(persona):
        problems.append(
            "persona is a step-by-step script of description/response pairs. It "
            "must be PROSE describing a person — who they are, the facts they "
            "know, and what they want — because the caller LLM receives it as its "
            "whole system prompt with no notion of which step it is on, so it "
            "replays the first entry forever. Rewrite it as paragraphs."
        )

    # One occurrence is the turn-format contract. Several means canned lines.
    if persona.count('{"speech"') > 2:
        problems.append(
            f'persona contains {persona.count(chr(34) + "speech" + chr(34))} '
            'literal {"speech": …} objects — it is scripting replies instead of '
            "describing a person. State the turn format once, at the end."
        )

    return problems


def _validate(path: Path) -> tuple[bool, list[str]]:
    """Validate with BOTH the linter and the real loader.

    The linter alone is not enough: it passed a scenario whose `persona` was a
    list, which `load_scenario` then rejected. Since the campaign runner calls
    the loader, the loader is the binding contract — a scenario that lints but
    won't load is worse than one that fails outright, because the job reports
    `ready` and only breaks when someone runs it.
    """
    errors: list[str] = []
    try:
        result = lint_scenario(str(path))
        if not result.passed:
            errors += [f"{e.code} ({e.field or '-'}): {e.message}" for e in result.errors]
    except Exception as exc:  # a malformed file can raise before linting
        errors.append(f"lint {type(exc).__name__}: {exc}")

    try:
        from vjudge.core.scenario_loader import load as load_scenario

        scenario = load_scenario(str(path))
    except Exception as exc:
        errors.append(f"load {type(exc).__name__}: {exc}")
    else:
        # Structural validity is not enough — see persona_problems. Checked on
        # the RENDERED persona, since that is what the caller LLM receives.
        errors += [f"persona: {p}" for p in persona_problems(scenario.persona_rendered)]

    return (not errors), errors


# Control characters the model sometimes emits inside `name` (a real one arrived
# as 'S01-… \x183P Arcadia \x183P IB'). They survive into the log-folder name and
# any report that echoes it.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def stamp_scenario_id(doc: dict, scenario_id: str) -> dict:
    """Make `name` start with `scenario_id`, and strip control characters.

    This is the join key between a generated scenario and its CallResult. The
    campaign runner reports results with `flow_name` — the scenario's `name` — and
    nothing else that identifies which scenario ran: the completion hook receives
    `(call_result, completed, total)`, not the path. Matching on the model's
    free-text title failed (title and name are different strings), and the
    fallback compared the scenario id against a LOWERCASED log-dir path, so it
    failed too — the result came back with scenario_id '?' and no correlation.
    Prefixing the name with an id we chose makes the match exact.
    """
    name = _CONTROL_CHARS.sub(" ", str(doc.get("name") or "")).strip()
    name = re.sub(r"\s{2,}", " ", name)
    if not name.startswith(scenario_id):
        name = f"{scenario_id} {name}".strip()
    doc["name"] = name
    return doc


def _scenario_text(
    reply: dict[str, Any],
    target: str,
    scenario_id: str,
    languages: Sequence[str] | None = None,
    transport: str | None = None,
) -> str:
    """Turn the model's reply into scenario YAML.

    Preferred path: `reply["scenario"]` is a JSON object, and WE emit the YAML —
    so a YAML syntax error cannot happen. The `scenario_yaml` string path is kept
    as a fallback for a model that ignores the instruction, and still runs the
    text repairs.
    """
    scenario = reply.get("scenario")
    if isinstance(scenario, dict) and scenario:
        doc, _ = normalise_doc(dict(scenario), target, languages, transport)
        return dump_scenario(stamp_scenario_id(doc, scenario_id))

    yaml_text = reply.get("scenario_yaml") or ""
    if isinstance(yaml_text, str) and yaml_text.strip():
        logger.info("%s: model returned scenario_yaml; using the text path", scenario_id)
        text = normalise_scenario(yaml_text, target, languages, transport)
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            return text  # unparseable; the validator will say so
        if isinstance(doc, dict):
            return dump_scenario(stamp_scenario_id(doc, scenario_id))
        return text

    raise GeneratorError(f"{scenario_id}: model returned no `scenario` object")


async def generate_scenario(
    seed: SeededCall,
    config: JobConfig,
    bot_prompt: str,
    scenario_id: str,
    workspace: JobWorkspace,
    api_key: str,
) -> GeneratedScenario:
    """Generate one scenario, lint it, and repair once if the lint fails."""
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": _user_prompt(seed, config, bot_prompt, scenario_id)},
    ]

    reply = _extract_json(await _chat(messages, api_key))
    path = workspace.write_scenario(
        scenario_id,
        _scenario_text(
            reply, config.target, scenario_id, config.languages, config.transport
        ),
    )
    ok, errors = _validate(path)

    if not ok:
        logger.warning(
            "%s failed validation, attempting one repair: %s", scenario_id, errors[:3]
        )
        messages += [
            {"role": "assistant", "content": json.dumps(reply)},
            {
                "role": "user",
                "content": (
                    "That scenario failed vjudge's validation with the errors below. "
                    "Return the SAME JSON object with `scenario` fixed. Change "
                    "nothing else, and fix every error.\n\n"
                    + "\n".join(f"- {e}" for e in errors)
                ),
            },
        ]
        repaired = _extract_json(await _chat(messages, api_key))
        if repaired.get("scenario") or repaired.get("scenario_yaml"):
            reply = repaired
            path = workspace.write_scenario(
                scenario_id,
                _scenario_text(
                    repaired, config.target, scenario_id,
                    config.languages, config.transport,
                ),
            )
            ok, errors = _validate(path)

    return GeneratedScenario(
        scenario_id=scenario_id,
        source_interaction_uuid=seed.interaction_uuid,
        path=str(path),
        title=(reply.get("title") or scenario_id)[:200],
        description=reply.get("description") or "",
        what_we_tested=reply.get("what_we_tested") or "",
        lint_ok=ok,
        lint_errors=errors,
    )


def write_campaign(
    workspace: JobWorkspace,
    scenarios: list[GeneratedScenario],
    config: JobConfig,
    job_id: str,
) -> Path:
    """Write the campaign that ties the lint-clean scenarios together.

    Scenario paths are relative to the campaign file, matching how the
    hand-written campaigns in `clients/*/campaigns/` reference `../scenarios/…`.
    Only lint-clean scenarios are included — a campaign that references a broken
    scenario fails at load and takes the whole run with it.
    """
    usable = [s for s in scenarios if s.lint_ok]
    doc = {
        "name": f"Job {job_id}",
        "description": (
            f"Auto-generated from {len(scenarios)} faulty call(s). "
            f"target={config.target} languages={','.join(config.languages)}"
        ),
        "max_concurrency": config.max_concurrency,
        "scenarios": [f"./scenarios/{Path(s.path).name}" for s in usable],
    }
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
    header = (
        f"# Auto-generated by vjudge job mode — job_id={job_id}\n"
        f"# Seeded from real production interactions; treat as sensitive.\n"
        f"# Excluded {len(scenarios) - len(usable)} scenario(s) that failed lint.\n"
    )
    return workspace.write_campaign(header + text)
