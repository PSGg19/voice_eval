"""Scenario YAML linter — validates without making real calls."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import yaml

from judge_it.llm.base import detect_provider
from .live_assistants import lookup as lookup_live_assistant
from .models import METRIC_TYPES
from .scenario_loader import ASSERT_KEYS, ASSERT_LIST_KEYS, METRIC_KEYS
# COMPUTED_KEYS names the metrics the harness measures. Imported lazily
# inside the check to keep lint free of an engine dependency at import time.

logger = logging.getLogger(__name__)

REQUIRED_KEYS = {"name", "target", "vars", "persona", "evaluation_prompt"}
LEGACY_KEYS = {"vars_ref", "persona_ref", "assertions", "goals"}
ALLOWED_BARGE_MODES = ("scheduled", "llm")
ALLOWED_TRANSPORTS = ("pstn", "sip", "text")
# Mirrors the providers wired in judge_it/llm/base.py::get_client. A YAML
# that sets llm.provider to anything outside this set would fail at runtime
# but currently passes lint because the model-prefix check is skipped when
# any explicit provider is given. Catch the misconfig here at load-time.
KNOWN_PROVIDERS = ("gemini", "anthropic", "openai")
E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


@dataclass
class LintError:
    code: str
    message: str
    path: Path
    field: Optional[str] = None


@dataclass
class LintResult:
    path: Path
    errors: list[LintError] = field(default_factory=list)
    warnings: list[LintError] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0


def _err(code: str, message: str, path: Path, field: Optional[str] = None) -> LintError:
    return LintError(code=code, message=message, path=path, field=field)


def _check_required_keys(data: dict, path: Path) -> list[LintError]:
    errs = []
    for k in sorted(REQUIRED_KEYS - data.keys()):
        errs.append(_err("missing_key", f"missing required key: {k}", path, field=k))
    return errs


def _check_legacy_keys(data: dict, path: Path) -> list[LintError]:
    errs = []
    for k in sorted(LEGACY_KEYS & data.keys()):
        errs.append(
            _err(
                "legacy_key",
                f"legacy key `{k}` is no longer supported in V2 scenarios",
                path,
                field=k,
            )
        )
    return errs


def _check_jinja_references(data: dict, path: Path) -> list[LintError]:
    """Render each template with the scenario's own vars, in the SANDBOX.

    Two fixes over the original raw-Environment version, both real:
    * a malformed template (`{% if x: %}`) raised TemplateSyntaxError out of
      lint itself — a linter must report bad input, never crash on it;
    * lint executed untrusted templates unsandboxed, so linting a hostile
      scenario ran `{{ ''.__class__.__mro__ }}` for real. Same renderer as
      the loader and the V3 API now (P3.2): one rule set, one sandbox.

    Codes: the historical `unresolved_jinja` is kept; everything else carries
    the RenderError's own type (template_syntax, sandbox_violation,
    template_too_large, output_too_large, …).
    """
    from .rendering import RenderError, render_sandboxed

    errs = []
    vars_data = data.get("vars") if isinstance(data.get("vars"), dict) else {}
    for fld in ("persona", "evaluation_prompt"):
        template_str = data.get(fld)
        if not isinstance(template_str, str):
            continue
        try:
            render_sandboxed(template_str, vars_data, fld)
        except RenderError as e:
            code = (
                "unresolved_jinja"
                if e.error_type == "unresolved_variable"
                else e.error_type
            )
            errs.append(_err(code, str(e), path, field=fld))
    return errs


def _check_persona_mentions_output_schema(data: dict, path: Path) -> list[LintError]:
    # Skip when persona is missing — already reported by _check_required_keys;
    # a second "missing schema hint" error here would just be noise.
    if "persona" not in data:
        return []
    persona = data.get("persona", "")
    if not isinstance(persona, str):
        return []
    if '"speech"' in persona and '"dtmf"' in persona:
        return []
    return [
        _err(
            "persona_missing_schema_hint",
            'persona should describe the caller JSON output schema '
            '(expected to mention `"speech"` and `"dtmf"`)',
            path,
            field="persona",
        )
    ]


def _check_evaluation_mentions_structured_output(
    data: dict, path: Path
) -> list[LintError]:
    # Skip when evaluation_prompt is missing — already reported by required-keys.
    if "evaluation_prompt" not in data:
        return []
    eval_prompt = data.get("evaluation_prompt", "")
    if not isinstance(eval_prompt, str):
        return []
    if '"overall_passed"' in eval_prompt and '"criteria"' in eval_prompt:
        return []
    return [
        _err(
            "evaluation_missing_schema_hint",
            "evaluation_prompt should request structured JSON "
            '(expected to mention `"overall_passed"` and `"criteria"`)',
            path,
            field="evaluation_prompt",
        )
    ]


def _check_target_is_e164(data: dict, path: Path) -> list[LintError]:
    # Skip when target is missing — already reported by _check_required_keys.
    if "target" not in data:
        return []
    target = data.get("target")
    if not isinstance(target, str) or not E164_RE.match(target):
        return [
            _err(
                "invalid_e164",
                f"`target` must be E.164 format (+<country><number>), got {target!r}",
                path,
                field="target",
            )
        ]
    return []


def _check_llm_model_prefixes(data: dict, path: Path) -> list[LintError]:
    llm = data.get("llm") or {}
    if not isinstance(llm, dict):
        return []
    explicit_provider = llm.get("provider")
    errs = []
    if explicit_provider is not None and explicit_provider not in KNOWN_PROVIDERS:
        errs.append(
            _err(
                "unknown_provider",
                f"`llm.provider` must be one of {KNOWN_PROVIDERS}, "
                f"got {explicit_provider!r}",
                path,
                field="llm.provider",
            )
        )
    for role in ("caller_model", "evaluator_model"):
        model = llm.get(role)
        if model is None:
            continue
        if explicit_provider:
            continue
        try:
            detect_provider(model)
        except ValueError:
            errs.append(
                _err(
                    "unknown_model_prefix",
                    f"`llm.{role}` has unknown provider prefix: {model!r}; "
                    "set `llm.provider` explicitly to override",
                    path,
                    field=f"llm.{role}",
                )
            )
    return errs


def _resolve_bcd_path(scenario_path: Path, bot_context_path: str) -> Path:
    bcd_path = Path(bot_context_path)
    if not bcd_path.is_absolute():
        bcd_path = (scenario_path.parent / bcd_path).resolve()
    return bcd_path


def _check_barge_in(data: dict, path: Path) -> list[LintError]:
    """Mirrors scenario_loader._parse_barge_in but accumulates errors instead
    of stopping at the first failure — that's the whole point of lint.
    """
    raw = data.get("barge_in")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [
            _err(
                "invalid_barge_in",
                f"`barge_in` must be a mapping, got {type(raw).__name__}",
                path,
                field="barge_in",
            )
        ]

    errs: list[LintError] = []
    enabled = raw.get("enabled")
    if enabled is None:
        errs.append(
            _err(
                "missing_enabled",
                "`barge_in.enabled` is required (set to true/false)",
                path,
                field="barge_in.enabled",
            )
        )
    if not enabled:
        # If disabled, nothing else needs validation — but still surface a
        # missing-enabled error if it was None vs explicit False.
        return errs

    mode = raw.get("mode")
    if mode not in ALLOWED_BARGE_MODES:
        errs.append(
            _err(
                "invalid_mode",
                f"`barge_in.mode` must be 'scheduled' or 'llm', got {mode!r}",
                path,
                field="barge_in.mode",
            )
        )
        # Without a valid mode the conflict-rules below don't apply.
        return errs

    raw_max_turns = data.get("max_turns", 30)
    try:
        max_turns = int(raw_max_turns)
    except (TypeError, ValueError):
        max_turns = 30  # if invalid, fall back so we can still range-check

    if mode == "scheduled":
        if "decide_every_ms" in raw:
            errs.append(
                _err(
                    "scheduled_rejects_decide_every_ms",
                    "`barge_in.mode: scheduled` does not allow `decide_every_ms`",
                    path,
                    field="barge_in.decide_every_ms",
                )
            )
        if "bot_context_path" in raw:
            errs.append(
                _err(
                    "scheduled_rejects_bot_context_path",
                    "`barge_in.mode: scheduled` does not allow `bot_context_path`",
                    path,
                    field="barge_in.bot_context_path",
                )
            )
        triggers_raw = raw.get("triggers")
        if not isinstance(triggers_raw, list) or not triggers_raw:
            errs.append(
                _err(
                    "missing_triggers",
                    "`barge_in.mode: scheduled` requires a non-empty `triggers` list",
                    path,
                    field="barge_in.triggers",
                )
            )
        else:
            seen_turns: dict[int, int] = {}
            for i, trig in enumerate(triggers_raw):
                fld = f"barge_in.triggers[{i}]"
                if not isinstance(trig, dict):
                    errs.append(
                        _err(
                            "invalid_trigger",
                            f"trigger must be a mapping, got {type(trig).__name__}",
                            path,
                            field=fld,
                        )
                    )
                    continue
                at_turn = trig.get("at_turn")
                if at_turn is None:
                    errs.append(
                        _err(
                            "missing_at_turn",
                            "`at_turn` is required",
                            path,
                            field=f"{fld}.at_turn",
                        )
                    )
                elif not isinstance(at_turn, int) or isinstance(at_turn, bool) or at_turn < 1:
                    errs.append(
                        _err(
                            "invalid_at_turn",
                            f"`at_turn` must be an integer >= 1, got {at_turn!r}",
                            path,
                            field=f"{fld}.at_turn",
                        )
                    )
                elif at_turn > max_turns:
                    errs.append(
                        _err(
                            "at_turn_out_of_range",
                            f"`at_turn={at_turn}` exceeds max_turns={max_turns}",
                            path,
                            field=f"{fld}.at_turn",
                        )
                    )
                elif at_turn in seen_turns:
                    errs.append(
                        _err(
                            "duplicate_at_turn",
                            f"`at_turn={at_turn}` already used by triggers[{seen_turns[at_turn]}]",
                            path,
                            field=f"{fld}.at_turn",
                        )
                    )
                else:
                    seen_turns[at_turn] = i
                say = trig.get("say")
                if not isinstance(say, str) or not say.strip():
                    errs.append(
                        _err(
                            "missing_say",
                            "`say` must be a non-empty string",
                            path,
                            field=f"{fld}.say",
                        )
                    )
                if "after_bot_words" in trig:
                    abw = trig["after_bot_words"]
                    if not isinstance(abw, int) or isinstance(abw, bool) or abw < 0:
                        errs.append(
                            _err(
                                "invalid_after_bot_words",
                                f"`after_bot_words` must be a non-negative integer, got {abw!r}",
                                path,
                                field=f"{fld}.after_bot_words",
                            )
                        )
    else:  # mode == "llm"
        if "triggers" in raw:
            errs.append(
                _err(
                    "llm_rejects_triggers",
                    "`barge_in.mode: llm` does not allow `triggers`",
                    path,
                    field="barge_in.triggers",
                )
            )
        if "decide_every_ms" not in raw:
            errs.append(
                _err(
                    "llm_missing_decide_every_ms",
                    "`barge_in.mode: llm` requires `decide_every_ms`",
                    path,
                    field="barge_in.decide_every_ms",
                )
            )
        bcp = raw.get("bot_context_path")
        if bcp is not None:
            if not isinstance(bcp, str):
                errs.append(
                    _err(
                        "invalid_bot_context_path",
                        f"`bot_context_path` must be a string, got {type(bcp).__name__}",
                        path,
                        field="barge_in.bot_context_path",
                    )
                )
            else:
                bcd_path = _resolve_bcd_path(path, bcp)
                if not bcd_path.exists():
                    errs.append(
                        _err(
                            "missing_bcd",
                            f"`bot_context_path` not found on disk: {bcd_path} "
                            f"(resolved from {bcp!r})",
                            path,
                            field="barge_in.bot_context_path",
                        )
                    )
    return errs


def _check_transport(data: dict, path: Path) -> list[LintError]:
    """Validate `transport:` and reject transport/feature combinations.

    The capability check is the point: the text transport drives the bot over
    HTTP turns, so there is no audio to interrupt and barge-in is meaningless
    there. Catching that at lint time turns a confusing mid-run surprise into a
    one-line message before anything is dialled or spent.
    """
    errs: list[LintError] = []
    raw = data.get("transport")
    if raw is None:
        # Absent is fine — the deployment default (Settings.call_transport) wins.
        return errs

    if not isinstance(raw, str) or raw.strip().lower() not in ALLOWED_TRANSPORTS:
        errs.append(
            _err(
                "invalid_transport",
                f"`transport` must be one of {ALLOWED_TRANSPORTS}, got {raw!r}",
                path,
                field="transport",
            )
        )
        return errs

    transport = raw.strip().lower()
    barge = data.get("barge_in")
    barge_enabled = isinstance(barge, dict) and bool(barge.get("enabled"))
    if transport == "text" and barge_enabled:
        errs.append(
            _err(
                "transport_capability",
                "`transport: text` cannot run `barge_in.enabled: true` — the text "
                "transport exchanges whole turns over HTTP, so there is no bot "
                "audio to interrupt. Use transport: sip (or pstn) for barge "
                "scenarios, or drop barge_in.",
                path,
                field="transport",
            )
        )
    return errs


def _check_is_test(data: dict, path: Path) -> list[LintError]:
    """Validate `is_test:`, and flag it on a transport that cannot honour it.

    `is_test` is a request field on the text runtime only. A voice call carries
    the intent as the `X-Is-Test` SIP header, which judge_it sets independently, so
    writing `is_test:` in a sip/pstn scenario reads as if it does something and
    does not. Better to say so at lint time than to leave a false assumption in
    the YAML.
    """
    errs: list[LintError] = []
    if "is_test" not in data:
        return errs

    raw = data["is_test"]
    if not isinstance(raw, bool):
        errs.append(
            _err(
                "invalid_is_test",
                f"`is_test` must be a boolean (true/false, unquoted), got "
                f"{type(raw).__name__} {raw!r}. A quoted \"false\" is truthy and "
                f"would set the opposite of what is written.",
                path,
                field="is_test",
            )
        )
        return errs

    transport = data.get("transport")
    if isinstance(transport, str) and transport.strip().lower() != "text":
        errs.append(
            _err(
                "transport_capability",
                f"`is_test` only affects the text transport, but this scenario "
                f"pins `transport: {transport.strip().lower()}`. On a voice call "
                f"the flag travels as the X-Is-Test SIP header, which judge_it sets "
                f"itself — this key would be silently ignored. Remove it, or "
                f"switch to transport: text.",
                path,
                field="is_test",
            )
        )
    return errs


def _check_bot_selection(data: dict, path: Path) -> list[LintError]:
    """Validate `assistant_uuid:` / `fc_version:`.

    Both are request fields on the text runtime only. On a voice call the
    assistant is whatever answers the dialled number, so writing them into a
    sip/pstn scenario reads as if it selects the bot and does not — the same
    trap as `is_test`.
    """
    errs: list[LintError] = []
    transport = data.get("transport")
    voice = isinstance(transport, str) and transport.strip().lower() != "text"

    if "assistant_uuid" in data:
        raw = data["assistant_uuid"]
        if not isinstance(raw, str) or not raw.strip():
            errs.append(
                _err(
                    "invalid_assistant_uuid",
                    f"`assistant_uuid` must be a non-empty string, got {raw!r}",
                    path,
                    field="assistant_uuid",
                )
            )
        elif voice:
            errs.append(
                _err(
                    "transport_capability",
                    f"`assistant_uuid` only applies to the text transport, but "
                    f"this scenario pins `transport: "
                    f"{transport.strip().lower()}`. Over voice the assistant is "
                    f"whichever bot answers `target`, so this key would be "
                    f"silently ignored. Remove it, or use transport: text.",
                    path,
                    field="assistant_uuid",
                )
            )

    if "fc_version" in data:
        raw = data["fc_version"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            errs.append(
                _err(
                    "invalid_fc_version",
                    f"`fc_version` must be an integer >= 0, got "
                    f"{type(raw).__name__} {raw!r}. 0 means \"whatever OSV "
                    f"considers current\"; pin a real version for a regression "
                    f"suite.",
                    path,
                    field="fc_version",
                )
            )
        elif voice:
            errs.append(
                _err(
                    "transport_capability",
                    f"`fc_version` only applies to the text transport, but this "
                    f"scenario pins `transport: {transport.strip().lower()}`. A "
                    f"voice call runs whatever version the bot has published, so "
                    f"this key would be silently ignored.",
                    path,
                    field="fc_version",
                )
            )
    return errs


def _check_assistant_customer_data(data: dict, path: Path) -> tuple[list[LintError], list[LintError]]:
    """Refuse to point a test at an assistant that holds real customer accounts.

    Returns (errors, warnings).

    This is the check that was missing on 2026-08-24, when a scenario was aimed
    at Veros CI OB (1160, 13,399 accounts), authenticated, and the post-call
    integration POSTed a real application number to the client's production
    workflow API. `is_test` did not help: it suppresses only the compliance
    event, while post-call processing runs after every turn.

    Note the rule is the ACCOUNT COUNT, not the assistant's name — an
    `Imported - ` clone copies the account table and can carry hundreds of
    thousands of real customers.
    """
    raw = data.get("assistant_uuid")
    if not isinstance(raw, str) or not raw.strip():
        # No `assistant_uuid` means the bot comes from
        # JUDGE_IT_TEXT_ASSISTANT_UUID, which lint cannot see — so the guard is
        # blind, and silence would read as approval. Only worth saying on text,
        # where the key exists at all.
        transport = data.get("transport")
        if isinstance(transport, str) and transport.strip().lower() == "text":
            return [], [_err(
                "assistant_not_pinned",
                "no `assistant_uuid`, so the bot comes from "
                "JUDGE_IT_TEXT_ASSISTANT_UUID and judge_it cannot check whether "
                "it holds real customer accounts. Pin it in the scenario to get "
                "that check — and so the run does not silently follow whatever "
                "was last exported.",
                path, "assistant_uuid",
            )]
        return [], []

    live = lookup_live_assistant(raw)
    if live is None:
        return [], []

    allowed = data.get("allow_customer_data") is True
    message = (
        f"`assistant_uuid` points at {live.describe()}. A test that "
        f"authenticates against a real account writes to the client's "
        f"production CRM — post-call processing runs after EVERY turn, and "
        f"`is_test` suppresses only the compliance event. Use an assistant with "
        f"zero accounts, or set `allow_customer_data: true` if you know these "
        f"accounts are not real customers."
    )
    err = _err("assistant_has_customer_data", message, path, "assistant_uuid")
    return ([], [err]) if allowed else ([err], [])


def _check_vars_pii(data: dict, path: Path) -> list[LintError]:
    """Warn when `vars` values look like they identify a real person.

    The customer-data guard above refuses a *bot* that holds real accounts. This
    is the other half, and the half that was missing on 2026-08-24: that run used
    a correct assistant and still leaked, because the scenario's own `vars`
    carried a real application number which the bot POSTed onward.

    A warning, not an error, because this is a heuristic — it cannot know that a
    plausible-looking number is real. It is silent on values that are
    recognisably invented (555 range, repdigits, sequential runs, the Veros test
    account) so the deliberately-seeded test identities produce no noise, and
    `allow_customer_data: true` acknowledges it the same way the assistant guard
    is acknowledged. Findings name the field and mask the value: a lint message
    ends up in tickets and chat, and a guard that restates the identifier is the
    leak it exists to prevent.
    """
    raw = data.get("vars")
    if not isinstance(raw, dict) or data.get("allow_customer_data") is True:
        return []

    from judge_it.core.pii_guard import inspect as inspect_pii, is_significant

    findings = inspect_pii(raw)
    # One unpatterned 4-digit number is not evidence; correlated identifiers are.
    if not findings or not is_significant(findings):
        return []
    detail = "; ".join(f.describe() for f in findings[:6])
    more = f" (+{len(findings) - 6} more)" if len(findings) > 6 else ""
    return [_err(
        "vars_look_like_real_pii",
        f"`vars` contains {len(findings)} value(s) that look like real customer "
        f"data: {detail}{more}. These reach the bot and can be written onward to "
        f"the client's CRM. Use an invented identity (555-01XX phones, "
        f"example.com emails) or set `allow_customer_data: true` to acknowledge.",
        path, "vars",
    )]


def _check_assert(data: dict, path: Path) -> list[LintError]:
    """Validate the optional `assert:` block, and reject it where it cannot work.

    Module and tool observations exist only on the text transport — an audio call
    reveals neither — so a structural assertion on a sip/pstn scenario would sit
    there looking meaningful and never check anything. That is worse than an
    error at lint time.
    """
    errs: list[LintError] = []
    if "assert" not in data:
        return errs

    raw = data["assert"]
    if not isinstance(raw, dict):
        return [_err(
            "invalid_assert",
            f"`assert` must be a mapping of assertion names to values, got "
            f"{type(raw).__name__}. Allowed keys: {sorted(ASSERT_KEYS)}.",
            path, "assert",
        )]

    unknown = sorted(set(raw) - ASSERT_KEYS)
    if unknown:
        errs.append(_err(
            "invalid_assert",
            f"`assert` has unknown key(s) {unknown}. Allowed: "
            f"{sorted(ASSERT_KEYS)}.",
            path, "assert",
        ))

    for key in ASSERT_LIST_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, list):
            errs.append(_err(
                "invalid_assert",
                f"`assert.{key}` must be a list of names, got "
                f"{type(value).__name__}.",
                path, f"assert.{key}",
            ))
            continue
        if not value:
            errs.append(_err(
                "invalid_assert",
                f"`assert.{key}` is an empty list, which asserts nothing. Remove "
                f"the key or give it names.",
                path, f"assert.{key}",
            ))
        bad = [v for v in value if not isinstance(v, str) or not v.strip()]
        if bad:
            errs.append(_err(
                "invalid_assert",
                f"`assert.{key}` entries must be non-empty strings; got {bad!r}.",
                path, f"assert.{key}",
            ))

    if "max_tag_only_turns" in raw:
        value = raw["max_tag_only_turns"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errs.append(_err(
                "invalid_assert",
                f"`assert.max_tag_only_turns` must be a non-negative integer, got "
                f"{value!r}.",
                path, "assert.max_tag_only_turns",
            ))

    transport = data.get("transport")
    if isinstance(transport, str) and transport.strip().lower() in ("sip", "pstn"):
        needs = sorted(k for k in raw if k in ASSERT_KEYS)
        if needs:
            errs.append(_err(
                "transport_capability",
                f"`assert` needs observations only the text transport produces "
                f"({', '.join(needs)}), but this scenario pins "
                f"transport: {transport!r}. A phone call reveals neither the "
                f"bot's current module nor its tool calls, so these assertions "
                f"could never be checked. Drop them, or use transport: \"text\".",
                path, "assert",
            ))
    return errs


def _check_assert_needs_osv_token(data: dict, path: Path) -> list[LintError]:
    """Warn that tool assertions depend on the post-run OSV fetch.

    The text runtime does not return `tool_calls` — judge_it recovers them from the
    conversations API afterwards, which needs a token. Without one those
    assertions report INCONCLUSIVE rather than failing, which is correct but easy
    to mistake for a pass.
    """
    raw = data.get("assert")
    if not isinstance(raw, dict):
        return []
    tool_keys = sorted(
        k for k in ("must_call_tools", "must_not_call_tools", "tool_order")
        if raw.get(k)
    )
    if not tool_keys:
        return []
    return [_err(
        "assert_needs_osv_token",
        f"{', '.join(tool_keys)} depend on tool calls, which the text runtime "
        f"does not return — judge_it fetches them from the conversations API after "
        f"the run. Without JUDGE_IT_OSV_ACCESS_TOKEN they report INCONCLUSIVE "
        f"(not a pass, and they will not fail the run either).",
        path, "assert",
    )]


def _check_metrics(data: dict, path: Path) -> list[LintError]:
    """Validate the optional declared-`metrics:` block.

    Structural errors mirror `_parse_metrics` so `judge_it lint` reports them
    before a run rather than at load time.
    """
    errs: list[LintError] = []
    if "metrics" not in data:
        return errs

    raw = data["metrics"]
    if not isinstance(raw, list):
        return [
            _err(
                "invalid_metrics",
                f"`metrics` must be a list of metric definitions, got "
                f"{type(raw).__name__}. Each entry needs a `key`; `description`, "
                f"`type`, `example` and `required` are optional.",
                path, "metrics",
            )
        ]

    seen: set[str] = set()
    for i, item in enumerate(raw, start=1):
        where = f"metrics[{i}]"
        if not isinstance(item, dict):
            errs.append(_err(
                "invalid_metrics",
                f"`{where}` must be a mapping with a `key`, got "
                f"{type(item).__name__}.",
                path, where,
            ))
            continue

        unknown = sorted(set(item) - METRIC_KEYS)
        if unknown:
            errs.append(_err(
                "invalid_metrics",
                f"`{where}` has unknown field(s) {unknown}. Allowed: "
                f"{sorted(METRIC_KEYS)}.",
                path, where,
            ))

        key = item.get("key")
        if not isinstance(key, str) or not key.strip():
            errs.append(_err(
                "invalid_metrics",
                f"`{where}.key` must be a non-empty string.",
                path, where,
            ))
        else:
            key = key.strip()
            if key in seen:
                errs.append(_err(
                    "duplicate_metric",
                    f"`metrics` declares {key!r} twice. Metric keys become JSON "
                    f"object keys, so the second silently wins.",
                    path, where,
                ))
            seen.add(key)

        mtype = item.get("type", "string")
        if not isinstance(mtype, str) or mtype.strip().lower() not in METRIC_TYPES:
            errs.append(_err(
                "invalid_metrics",
                f"`{where}.type` must be one of {sorted(METRIC_TYPES)}, got "
                f"{mtype!r}.",
                path, where,
            ))

        if not isinstance(item.get("required", True), bool):
            errs.append(_err(
                "invalid_metrics",
                f"`{where}.required` must be true or false, got "
                f"{item.get('required')!r}.",
                path, where,
            ))

        if "description" in item and not isinstance(item["description"], str):
            errs.append(_err(
                "invalid_metrics",
                f"`{where}.description` must be a string.",
                path, where,
            ))

    return errs


def _check_metrics_not_measured(data: dict, path: Path) -> list[LintError]:
    """Warn when a declared metric is one the harness already measures.

    Not an error — the run works, and `metrics_to_request` drops the key from
    the prompt so the model is never asked. But the declaration is dead weight
    and reads as though the model decides the value when it does not, so say so
    once at lint time.
    """
    raw = data.get("metrics")
    if not isinstance(raw, list):
        return []
    from judge_it.engine.metrics import COMPUTED_KEYS

    measured = [
        item["key"].strip()
        for item in raw
        if isinstance(item, dict)
        and isinstance(item.get("key"), str)
        and item["key"].strip() in COMPUTED_KEYS
    ]
    if not measured:
        return []
    return [
        _err(
            "metric_already_measured",
            f"judge_it measures {', '.join(sorted(measured))} itself and overrides "
            f"whatever the evaluator returns, so declaring "
            f"{'them' if len(measured) > 1 else 'it'} has no effect on the "
            f"result. The value still appears in `metrics` — drop the "
            f"declaration to keep the prompt honest about what the model decides.",
            path, "metrics",
        )
    ]


def _check_text_requires_recipient_id(data: dict, path: Path) -> list[LintError]:
    """A text scenario MUST state its `recipient_id`.

    OSV assigns it to the bot's `phone_number` and uses it as the account lookup
    key, so it reaches the client's integrations. judge_it therefore never invents
    one — but a missing key would otherwise only surface once the run started, so
    catch it here.

    Only enforced when the scenario PINS `transport: text`. An unpinned scenario
    may be intended for voice, where the key does not apply.
    """
    transport = data.get("transport")
    if not (isinstance(transport, str) and transport.strip().lower() == "text"):
        return []
    if data.get("recipient_id"):
        return []
    return [
        _err(
            "missing_recipient_id",
            "`transport: text` requires `recipient_id` — it becomes the bot's "
            "`phone_number` and is its account lookup key, so it reaches the "
            "client's integrations and is never generated for you. Use bare "
            "digits, no +1: the seeded test account's number if the flow must "
            'authenticate, else the NANP fictional range, e.g. recipient_id: '
            '"2015550137"',
            path,
            field="recipient_id",
        )
    ]


def lint_scenario(path: Union[str, Path]) -> LintResult:
    """Validate a scenario YAML and return all findings. Collects errors
    without short-circuiting so users see every issue in one pass.
    """
    path = Path(path)
    result = LintResult(path=path)

    if not path.exists():
        result.errors.append(
            _err("missing_file", f"scenario file not found: {path}", path)
        )
        return result

    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        result.errors.append(
            _err("invalid_yaml", f"failed to parse YAML: {e}", path)
        )
        return result
    except OSError as e:
        result.errors.append(_err("io_error", f"failed to read file: {e}", path))
        return result

    if not isinstance(data, dict):
        result.errors.append(
            _err(
                "invalid_root",
                "scenario file must contain a YAML mapping at top level",
                path,
            )
        )
        return result

    return lint_scenario_data(data, path)


# Sentinel path for scenarios that never were a file — API payloads (P3.5).
API_PATH = Path("<api>")


def lint_scenario_data(data: dict, path: Union[str, Path] = API_PATH) -> LintResult:
    """Every scenario rule, on a plain mapping.

    This is what makes P3.5 "lint as validation" one rule set rather than two:
    `lint_scenario` (files) and the V3 API's 422 path both land here, so a rule
    added for one door exists at the other by construction. `path` is only for
    the report — API callers get the `<api>` sentinel.
    """
    path = Path(path)
    result = LintResult(path=path)

    # Schema checks — order matters only for readability of the printed report.
    result.errors.extend(_check_required_keys(data, path))
    result.errors.extend(_check_legacy_keys(data, path))
    result.errors.extend(_check_jinja_references(data, path))
    result.errors.extend(_check_target_is_e164(data, path))
    result.errors.extend(_check_llm_model_prefixes(data, path))
    result.errors.extend(_check_barge_in(data, path))
    result.errors.extend(_check_transport(data, path))
    result.errors.extend(_check_is_test(data, path))
    result.errors.extend(_check_bot_selection(data, path))
    result.errors.extend(_check_text_requires_recipient_id(data, path))
    result.errors.extend(_check_metrics(data, path))
    result.errors.extend(_check_assert(data, path))
    _cd_errors, _cd_warnings = _check_assistant_customer_data(data, path)
    result.errors.extend(_cd_errors)
    result.warnings.extend(_cd_warnings)

    # Heuristic / advisory checks land in warnings.
    result.warnings.extend(_check_metrics_not_measured(data, path))
    result.warnings.extend(_check_assert_needs_osv_token(data, path))
    result.warnings.extend(_check_vars_pii(data, path))
    result.warnings.extend(_check_persona_mentions_output_schema(data, path))
    result.warnings.extend(
        _check_evaluation_mentions_structured_output(data, path)
    )

    return result


def detect_file_kind(path: Union[str, Path]) -> str:
    """Return 'campaign', 'scenario', or 'unknown' based on top-level YAML keys.

    Heuristic-based but unambiguous in practice: campaigns have a
    `scenarios` list, scenarios have a `persona` string. A file with
    both would be malformed (we treat as scenario and let lint_scenario
    flag the issue).
    """
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return "unknown"
    if not isinstance(data, dict):
        return "unknown"
    if "persona" in data:
        return "scenario"
    if "scenarios" in data and isinstance(data.get("scenarios"), list):
        return "campaign"
    return "unknown"


def lint_campaign(path: Union[str, Path]) -> LintResult:
    """Validate a campaign YAML and return all findings.

    Wraps `campaign_loader.load()` and converts any ValueError into a
    LintError. Does NOT recurse into the referenced scenarios — the
    CLI dispatches `lint_scenario` per resolved path so each scenario's
    findings appear under its own filename in the report.
    """
    from .campaign_loader import load as load_campaign

    path = Path(path)
    result = LintResult(path=path)

    if not path.exists():
        result.errors.append(
            _err("missing_file", f"campaign file not found: {path}", path)
        )
        return result

    try:
        load_campaign(path)
    except ValueError as e:
        result.errors.append(_err("invalid_campaign", str(e), path))
    except Exception as e:
        # Unexpected failure mode — surface to user so they have a
        # filename + traceback to dig into rather than silent ignore.
        result.errors.append(
            _err("campaign_load_failed", f"unexpected failure: {e}", path)
        )

    return result


async def probe_persona(path: Union[str, Path], api_key: str) -> list[LintError]:
    """Optional probe: dry-run the caller LLM against a one-line bot utterance
    and verify it returns a parseable JSON payload. Costs ~$0.01/scenario.
    Returns a list (possibly empty) of LintErrors for triage.
    """
    from .scenario_loader import load
    from judge_it.llm import get_client

    path = Path(path)
    try:
        scenario = load(path)
    except Exception as e:
        return [_err("probe_load_failed", f"could not load scenario: {e}", path)]

    model = scenario.llm.caller_model or "gemini-2.5-flash"
    try:
        client = get_client(model, api_key, provider=scenario.llm.provider)
    except Exception as e:
        return [_err("probe_client_failed", f"could not init LLM client: {e}", path)]

    try:
        response = await client.complete_json(
            system=scenario.persona_rendered,
            messages=[
                {"role": "user", "content": "Hello, this is the bot calling."}
            ],
            max_tokens=200,
            temperature=0.0,
        )
    except Exception as e:
        return [_err("probe_failed", f"LLM probe call failed: {e}", path)]

    if not isinstance(response, dict):
        return [
            _err(
                "probe_invalid_response",
                f"LLM probe returned non-dict: {type(response).__name__}",
                path,
            )
        ]
    # Defensive: caller schema requires at least "speech" or "dtmf" to be a string.
    if "speech" not in response and "dtmf" not in response:
        return [
            _err(
                "probe_invalid_response",
                f"LLM probe response missing both `speech` and `dtmf`: keys={list(response)}",
                path,
            )
        ]
    return []
