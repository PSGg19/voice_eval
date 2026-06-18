"""Load YAML scenario files into Scenario dataclasses."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import yaml

from .models import (
    METRIC_TYPES, AssertionSpec, BargeInConfig, LLMConfig, MetricSpec, STTConfig,
    TTSConfig, Scenario,
    ScheduledTrigger,
)
from .region import ALLOWED_REGIONS as _REGION_LIST
from .rendering import render_sandboxed

REQUIRED_KEYS = {"name", "target", "vars", "persona", "evaluation_prompt"}
OPTIONAL_KEYS = {
    "llm", "stt", "tts", "max_turns", "barge_in",
    "transport", "is_test", "assistant_uuid", "fc_version", "recipient_id", "region",
    "metrics", "assert", "allow_customer_data",
}
ALLOWED_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS
STRING_KEYS = ("name", "target", "persona", "evaluation_prompt")
LLM_KEYS = {"caller_model", "evaluator_model", "temperature", "provider"}
STT_KEYS = {"utterance_end_ms", "silence_threshold_s"}
TTS_KEYS = {"model"}

BARGE_IN_TOP_KEYS = {
    "enabled",
    "mode",
    "triggers",
    "decide_every_ms",
    "bot_context_path",
}
TRIGGER_KEYS = {"at_turn", "say", "after_bot_words"}
SCHEDULED_ONLY_KEYS = {"triggers"}
LLM_ONLY_KEYS = {"decide_every_ms", "bot_context_path"}


# Rendering is delegated to core.rendering (P3.2): the same SandboxedEnvironment
# renders a template whether it arrived from a YAML file here or over HTTP in
# the V3 run API. RenderError subclasses ValueError, and its unresolved-variable
# message keeps the historical format, so callers of `load()` see no change.


def _parse_llm(raw: object) -> LLMConfig:
    if raw is None:
        return LLMConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"`llm` must be a mapping, got {type(raw).__name__}")
    unknown = sorted(raw.keys() - LLM_KEYS)
    if unknown:
        raise ValueError(f"unknown key(s) in `llm`: {', '.join(unknown)}")
    try:
        temperature = float(raw.get("temperature", 0.3))
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"`llm.temperature` must be a number, got {raw.get('temperature')!r}"
        ) from e
    return LLMConfig(
        caller_model=raw.get("caller_model"),
        evaluator_model=raw.get("evaluator_model"),
        temperature=temperature,
        provider=raw.get("provider"),
    )


def _parse_stt(raw: object) -> STTConfig:
    if raw is None:
        return STTConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"`stt` must be a mapping, got {type(raw).__name__}")
    unknown = sorted(raw.keys() - STT_KEYS)
    if unknown:
        raise ValueError(f"unknown key(s) in `stt`: {', '.join(unknown)}")
    raw_uem = raw.get("utterance_end_ms", 2500)
    try:
        utterance_end_ms = int(raw_uem)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"`stt.utterance_end_ms` must be an integer, got {raw_uem!r}"
        ) from e
    if utterance_end_ms < 500:
        raise ValueError(
            f"`stt.utterance_end_ms` must be >= 500, got {utterance_end_ms}"
        )
    raw_threshold = raw.get("silence_threshold_s", 1.5)
    try:
        silence_threshold_s = float(raw_threshold)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"`stt.silence_threshold_s` must be a number, got {raw_threshold!r}"
        ) from e
    if silence_threshold_s < 0.2:
        raise ValueError(
            f"`stt.silence_threshold_s` must be >= 0.2, got {silence_threshold_s}"
        )
    return STTConfig(
        utterance_end_ms=utterance_end_ms,
        silence_threshold_s=silence_threshold_s,
    )


ALLOWED_TRANSPORTS = ("pstn", "sip", "text")


def _parse_transport(raw: object) -> Optional[str]:
    """Validate the optional `transport:` key.

    None means "use the deployment default" (Settings.call_transport), which is
    what every pre-existing scenario gets. An explicit value pins the scenario
    to a transport, so a barge scenario cannot silently run on text and a
    cheap reasoning scenario need not burn a phone call.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"`transport` must be a string, got {type(raw).__name__}")
    value = raw.strip().lower()
    if value not in ALLOWED_TRANSPORTS:
        raise ValueError(
            f"`transport` must be one of {ALLOWED_TRANSPORTS}, got {raw!r}"
        )
    return value


def _parse_is_test(raw: object) -> Optional[bool]:
    """Validate the optional `is_test:` key (text transport only).

    None means "use the deployment default" (Settings.text_is_test). Strings are
    rejected rather than coerced: YAML already gives us real booleans for
    `true`/`false`, so a quoted `"false"` is a mistake worth catching — the truthy
    coercion would silently flip the flag to the opposite of what was written.
    """
    if raw is None:
        return None
    if not isinstance(raw, bool):
        raise ValueError(
            f"`is_test` must be a boolean (true/false, unquoted), got "
            f"{type(raw).__name__} {raw!r}"
        )
    return raw



def _parse_assistant_uuid(raw: object) -> Optional[str]:
    """Validate the optional `assistant_uuid:` key.

    None means "use the deployment default" (Settings.text_assistant_uuid), and
    if that is blank too, OSV resolves the assistant from `target`. Put it in the
    scenario when the target number is not registered as a virtual number in the
    environment under test, or when you want the scenario to name its bot
    unambiguously rather than depend on number routing.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(
            f"`assistant_uuid` must be a string, got {type(raw).__name__}"
        )
    value = raw.strip()
    if not value:
        raise ValueError("`assistant_uuid` must not be empty — omit the key instead")
    return value


def _parse_fc_version(raw: object) -> Optional[int]:
    """Validate the optional `fc_version:` key.

    None means "use the deployment default"; 0 means "whatever OSV considers
    current". Pin a real version for a regression suite — an unpinned scenario
    changes meaning the moment someone publishes a new flow config, and an
    unpublished draft never gets tested at all.

    `bool` is rejected explicitly because it is an int subclass in Python, so
    `fc_version: true` would otherwise sail through as version 1.
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(
            f"`fc_version` must be an integer, got {type(raw).__name__} {raw!r}"
        )
    if raw < 0:
        raise ValueError(f"`fc_version` must be >= 0, got {raw}")
    return raw


def _parse_recipient_id(raw: object) -> Optional[str]:
    """Validate the optional `recipient_id:` key.

    None means "auto-detect from vars". This is OSV's account lookup key, not a
    label — see transport/text.py::resolve_recipient_id.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(
            f"`recipient_id` must be a string, got {type(raw).__name__}"
        )
    value = raw.strip()
    if not value:
        raise ValueError("`recipient_id` must not be empty — omit the key instead")
    return value


# Single source of truth — `apply_region` rewrites exactly these segments, so a
# region the loader accepts but the rewriter doesn't recognise would silently
# leave the URL pointing at the wrong gateway.
ALLOWED_REGIONS = _REGION_LIST



METRIC_KEYS = {"key", "description", "type", "example", "required"}



ASSERT_LIST_KEYS = (
    "must_visit_modules",
    "must_not_visit_modules",
    "must_call_tools",
    "must_not_call_tools",
    "tool_order",
)
ASSERT_KEYS = set(ASSERT_LIST_KEYS) | {"max_tag_only_turns"}


def _parse_assert(raw: object) -> Optional[AssertionSpec]:
    """Validate the optional `assert:` key.

    Named `assert`, not `assertions` — the latter is a V1 legacy key that lint
    rejects outright, so a scenario using it fails before it runs. (The design
    note in docs/dual-transport.md proposed `assertions:`; that was wrong.)
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"`assert` must be a mapping, got {type(raw).__name__}"
        )
    unknown = sorted(set(raw) - ASSERT_KEYS)
    if unknown:
        raise ValueError(
            f"`assert` has unknown key(s) {unknown}; allowed: {sorted(ASSERT_KEYS)}"
        )

    kwargs: dict = {}
    for key in ASSERT_LIST_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, list) or not all(
            isinstance(v, str) and v.strip() for v in value
        ):
            raise ValueError(
                f"`assert.{key}` must be a list of non-empty strings, got {value!r}"
            )
        kwargs[key] = [v.strip() for v in value]

    if "max_tag_only_turns" in raw:
        value = raw["max_tag_only_turns"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"`assert.max_tag_only_turns` must be a non-negative integer, "
                f"got {value!r}"
            )
        kwargs["max_tag_only_turns"] = value

    spec = AssertionSpec(**kwargs)
    return None if spec.is_empty else spec


def _parse_metrics(raw: object) -> list[MetricSpec]:
    """Validate the optional `metrics:` key — a list of declared metrics.

    Replaces the JSON example blob hand-written into each evaluation prompt,
    which stated shape but never meaning or type. Absent means "keep using the
    prompt's own blob", so every existing scenario is unaffected.

    Shape:

        metrics:
          - key: required_gates_passed
            description: required gates the bot passed, out of the total
            type: string
            example: "4/4"
          - key: caller_had_to_repeat
            description: did the caller repeat because the bot missed it
            type: boolean
            required: false
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"`metrics` must be a list of metric definitions, got "
            f"{type(raw).__name__}"
        )
    specs: list[MetricSpec] = []
    seen: set[str] = set()
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(
                f"`metrics[{i}]` must be a mapping with a `key`, got "
                f"{type(item).__name__}"
            )
        unknown = set(item) - METRIC_KEYS
        if unknown:
            raise ValueError(
                f"`metrics[{i}]` has unknown field(s) "
                f"{sorted(unknown)}; allowed: {sorted(METRIC_KEYS)}"
            )
        key = item.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"`metrics[{i}].key` must be a non-empty string")
        key = key.strip()
        if key in seen:
            raise ValueError(f"`metrics` declares {key!r} twice")
        seen.add(key)

        mtype = item.get("type", "string")
        if not isinstance(mtype, str) or mtype.strip().lower() not in METRIC_TYPES:
            raise ValueError(
                f"`metrics[{i}].type` must be one of {sorted(METRIC_TYPES)}, "
                f"got {mtype!r}"
            )
        description = item.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"`metrics[{i}].description` must be a string")
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise ValueError(
                f"`metrics[{i}].required` must be true or false, got {required!r}"
            )
        specs.append(
            MetricSpec(
                key=key,
                description=description.strip(),
                type=mtype.strip().lower(),
                example=item.get("example"),
                required=required,
            )
        )
    return specs


def _parse_region(raw: object) -> Optional[str]:
    """Validate the optional `region:` key.

    None means "use the deployment default" (whatever region the configured
    text-runtime URL points at). Case-insensitive; the canonical values are the
    URL path segments themselves, so they stay honest.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"`region` must be a string, got {type(raw).__name__}")
    value = raw.strip().lower()
    if value not in ALLOWED_REGIONS:
        raise ValueError(
            f"`region` must be one of {ALLOWED_REGIONS}, got {raw!r}"
        )
    return value


def _parse_tts(raw: object) -> TTSConfig:
    if raw is None:
        return TTSConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"`tts` must be a mapping, got {type(raw).__name__}")
    unknown = sorted(raw.keys() - TTS_KEYS)
    if unknown:
        raise ValueError(f"unknown key(s) in `tts`: {', '.join(unknown)}")
    raw_model = raw.get("model")
    if raw_model is not None and not isinstance(raw_model, str):
        raise ValueError(f"`tts.model` must be a string, got {raw_model!r}")
    return TTSConfig(model=raw_model)


def _parse_trigger(raw: object, index: int) -> ScheduledTrigger:
    if not isinstance(raw, dict):
        raise ValueError(
            f"`barge_in.triggers[{index}]` must be a mapping, got {type(raw).__name__}"
        )
    unknown = sorted(raw.keys() - TRIGGER_KEYS)
    if unknown:
        raise ValueError(
            f"unknown key(s) in `barge_in.triggers[{index}]`: {', '.join(unknown)}"
        )
    if "at_turn" not in raw:
        raise ValueError(f"`barge_in.triggers[{index}].at_turn` is required")
    if "say" not in raw:
        raise ValueError(f"`barge_in.triggers[{index}].say` is required")
    try:
        at_turn = int(raw["at_turn"])
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"`barge_in.triggers[{index}].at_turn` must be an integer, got {raw['at_turn']!r}"
        ) from e
    if at_turn < 1:
        raise ValueError(
            f"`barge_in.triggers[{index}].at_turn` must be >= 1, got {at_turn}"
        )
    say = raw["say"]
    if not isinstance(say, str) or not say.strip():
        raise ValueError(
            f"`barge_in.triggers[{index}].say` must be a non-empty string"
        )
    try:
        after_bot_words = int(raw.get("after_bot_words", 0))
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"`barge_in.triggers[{index}].after_bot_words` must be an integer, "
            f"got {raw.get('after_bot_words')!r}"
        ) from e
    if after_bot_words < 0:
        raise ValueError(
            f"`barge_in.triggers[{index}].after_bot_words` must be >= 0, got {after_bot_words}"
        )
    return ScheduledTrigger(at_turn=at_turn, say=say, after_bot_words=after_bot_words)


def _parse_barge_in(raw: object) -> BargeInConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"`barge_in` must be a mapping when present, got {type(raw).__name__}"
        )
    unknown = sorted(raw.keys() - BARGE_IN_TOP_KEYS)
    if unknown:
        raise ValueError(f"unknown key(s) in `barge_in`: {', '.join(unknown)}")

    enabled = bool(raw.get("enabled", False))
    if not enabled:
        return BargeInConfig(enabled=False)

    mode = raw.get("mode")
    if mode not in ("scheduled", "llm"):
        raise ValueError(
            f"`barge_in.mode` must be 'scheduled' or 'llm', got {mode!r}"
        )

    if mode == "scheduled":
        rejected = sorted(raw.keys() & LLM_ONLY_KEYS)
        if rejected:
            raise ValueError(
                f"`barge_in.mode: scheduled` does not allow: {', '.join(rejected)}"
            )
        triggers_raw = raw.get("triggers")
        if not isinstance(triggers_raw, list) or not triggers_raw:
            raise ValueError(
                "`barge_in.mode: scheduled` requires a non-empty `triggers` list"
            )
        triggers = [_parse_trigger(t, i) for i, t in enumerate(triggers_raw)]
        seen_turns: dict[int, int] = {}
        for i, t in enumerate(triggers):
            if t.at_turn in seen_turns:
                raise ValueError(
                    f"`barge_in.triggers` has duplicate at_turn={t.at_turn} "
                    f"(indices {seen_turns[t.at_turn]} and {i}); each trigger must target a unique turn"
                )
            seen_turns[t.at_turn] = i
        return BargeInConfig(enabled=True, mode="scheduled", triggers=triggers)

    rejected = sorted(raw.keys() & SCHEDULED_ONLY_KEYS)
    if rejected:
        raise ValueError(
            f"`barge_in.mode: llm` does not allow: {', '.join(rejected)}"
        )
    try:
        decide_every_ms = int(raw.get("decide_every_ms", 500))
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"`barge_in.decide_every_ms` must be an integer, got {raw.get('decide_every_ms')!r}"
        ) from e
    if decide_every_ms < 1:
        raise ValueError(
            f"`barge_in.decide_every_ms` must be >= 1, got {decide_every_ms}"
        )
    bot_context_path = raw.get("bot_context_path")
    if bot_context_path is not None and not isinstance(bot_context_path, str):
        raise ValueError("`barge_in.bot_context_path` must be a string when present")
    return BargeInConfig(
        enabled=True,
        mode="llm",
        decide_every_ms=decide_every_ms,
        bot_context_path=bot_context_path,
    )


BCD_DELIMITER = "──────── Bot Context Document ────────"

# Strong "reference only" guard wrapped around the appended BCD content.
# Without this, the caller LLM treated Michelle's phrases (modules,
# transitions, sample utterances) as instructions and started copying
# them into its own `speech` field — observed in the 2026-05-18 16:50
# A-001-barge-llm run, where the caller said "To verify your identity,
# can you please tell me your date of birth?" (Michelle's exact question)
# instead of staying in character as Iris.
_BCD_PREAMBLE = """
══════════════════ REFERENCE ONLY — DO NOT SPEAK THESE PHRASES ══════════════════

The block below describes how the BOT (the other party on the call) behaves:
its modules, transition phrases, tool-call moments, and example utterances.

THIS IS PURELY FOR YOUR REFERENCE so you can identify which phase the bot is
in and time your barges at semantic boundaries. You are still the CALLER —
your persona, identity, and goal are defined ABOVE.

HARD RULES:
  1. NEVER copy a phrase from the block below into your `speech` output.
  2. NEVER role-play as the bot. You are the caller, not the agent.
  3. Use the block below ONLY to recognize what the bot is doing right now
     (greeting / verifying / disclosing / listing / asking) and time when
     to set `barge: true`.
  4. Your replies must remain in your caller persona's voice — short,
     conversational, never reciting the bot's wording.

═════════════════════════════════════════════════════════════════════════════════
"""


def _apply_bcd(scenario: Scenario) -> Scenario:
    """If the scenario's barge_in carries a bot_context_path, read the BCD file
    and append it to persona_rendered. Paths resolve relative to the scenario
    file's parent for portability (so `clients/foo/bargin-context.md` works
    regardless of cwd).

    The BCD content is wrapped with a strong "REFERENCE ONLY" preamble
    (`_BCD_PREAMBLE`) so the caller LLM doesn't confuse the bot's example
    phrases with its own instructions — see preamble docstring for the
    production incident that motivated this guard.
    """
    if scenario.barge_in is None or not scenario.barge_in.bot_context_path:
        return scenario

    bcd_path = Path(scenario.barge_in.bot_context_path)
    if not bcd_path.is_absolute() and scenario.source_path:
        bcd_path = (Path(scenario.source_path).parent / bcd_path).resolve()

    if not bcd_path.exists():
        raise ValueError(
            f"`barge_in.bot_context_path` not found: {bcd_path} "
            f"(resolved from {scenario.barge_in.bot_context_path!r})"
        )

    bcd_content = bcd_path.read_text(encoding="utf-8")
    scenario.persona_rendered = (
        f"{scenario.persona_rendered}"
        f"\n\n{_BCD_PREAMBLE}"
        f"\n\n{BCD_DELIMITER}\n\n{bcd_content}"
    )
    return scenario


def load(path: Union[str, Path]) -> Scenario:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"failed to parse YAML in {path}: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"scenario file {path} must contain a YAML mapping at top level")

    return parse_scenario_data(data, source_path=str(path))


def parse_scenario_data(
    data: dict, *, source_path: Optional[str] = None
) -> Scenario:
    """Everything `load()` does after the YAML parse, on a plain mapping.

    Split out (P3.3) because the V3 run API receives the same mapping as a
    JSON payload / a `scenarios.body` row — one parser, whichever door the
    scenario came in through. `source_path` is provenance only, plus the base
    for resolving a relative `barge_in.bot_context_path`.
    """
    missing = sorted(REQUIRED_KEYS - data.keys())
    if missing:
        raise ValueError(f"missing required key(s): {', '.join(missing)}")

    unknown = sorted(data.keys() - ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"unknown top-level key(s): {', '.join(unknown)}")

    for k in STRING_KEYS:
        if not isinstance(data[k], str):
            raise ValueError(
                f"`{k}` must be a string, got {type(data[k]).__name__}"
            )

    vars_data = data["vars"]
    if not isinstance(vars_data, dict):
        raise ValueError("`vars` must be a mapping")

    raw_max_turns = data.get("max_turns", 30)
    try:
        max_turns = int(raw_max_turns)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"`max_turns` must be an integer, got {raw_max_turns!r}"
        ) from e
    if max_turns < 1:
        raise ValueError(f"`max_turns` must be >= 1, got {max_turns}")

    persona_rendered = render_sandboxed(data["persona"], vars_data, "persona")
    evaluation_prompt_rendered = render_sandboxed(
        data["evaluation_prompt"], vars_data, "evaluation_prompt"
    )

    barge_in = _parse_barge_in(data.get("barge_in"))

    scenario = Scenario(
        name=data["name"],
        target=data["target"],
        vars=vars_data,
        persona_rendered=persona_rendered,
        evaluation_prompt_rendered=evaluation_prompt_rendered,
        llm=_parse_llm(data.get("llm")),
        stt=_parse_stt(data.get("stt")),
        tts=_parse_tts(data.get("tts")),
        max_turns=max_turns,
        barge_in=barge_in,
        transport=_parse_transport(data.get("transport")),
        is_test=_parse_is_test(data.get("is_test")),
        assistant_uuid=_parse_assistant_uuid(data.get("assistant_uuid")),
        fc_version=_parse_fc_version(data.get("fc_version")),
        recipient_id=_parse_recipient_id(data.get("recipient_id")),
        region=_parse_region(data.get("region")),
        metrics=_parse_metrics(data.get("metrics")),
        assertions=_parse_assert(data.get("assert")),
        source_path=source_path,
    )
    return _apply_bcd(scenario)

def peek_transport(scenario_path) -> Optional[str]:
    """Read just `transport:` from a scenario file, tolerating anything malformed.

    Lives here rather than in the CLI because both `opkura run` and
    `opkura campaign` need it, and importing one CLI module from the other
    creates a cycle (run -> main -> campaign -> run).

    Never raises. Its only job is deciding whether to start the media server;
    the runner is what properly loads, validates and reports on the scenario, so
    a parse failure here must not pre-empt that error message.
    """
    try:
        with open(scenario_path) as fh:
            raw = yaml.safe_load(fh)
        if isinstance(raw, dict):
            value = raw.get("transport")
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    except Exception:
        pass
    return None
