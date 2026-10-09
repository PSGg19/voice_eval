"""Deterministic metrics, computed from the run instead of asked of the LLM.

Every scenario's evaluation prompt ends with a `metrics` object, and the model
fills it in. For judgement calls (`required_gates_passed`) that is the right
division of labour. For **counts** it is not: `bot_turns` and `caller_turns`
appear in 1108 of the corpus's scenarios, and asking a language model to tally
turns is asking it to do arithmetic that the transcript already answers exactly.
It gets them wrong, and worse, it gets them *plausibly* wrong — a metric that
reads fine and is off by one is harder to catch than a missing one.

Observed on the 1740 CI OB probe before the harness supplied any facts:
`modules_visited: "Not provided in transcript"` — invented, because nothing had
been provided. Same run, same transcript, after: `"GREETINGS"`.

So: compute what is computable, hand it to the evaluator as input, and let the
computed value win on merge. The LLM's own number is kept and compared — a
disagreement is a measurement of evaluator reliability, which is otherwise
entirely unquantified (`docs/platform-evolution.md` §10.2).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from vjudge.core.models import METRIC_TYPES, MetricSpec, TranscriptEntry

logger = logging.getLogger(__name__)

# Metrics this module owns. On merge these override whatever the LLM reported,
# because they are measurements and its are guesses. Anything NOT listed here
# (required_gates_passed, observed_gates_passed, …) is genuinely a judgement and
# stays the model's to make.
COMPUTED_KEYS = (
    "bot_turns",
    "caller_turns",
    "total_turns",
    "modules_visited",
    "module_count",
    "tool_call_count",
    "tool_calls_in_order",
    "empty_or_tag_only_turns",
    "duplicated_text_seen",
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
# A repeated fragment is only interesting if it carries content. "Yes." or "OK."
# recur legitimately; a duplicated clause of this length does not.
_MIN_DUPLICATE_CHARS = 25


def _normalise(text: str) -> str:
    return " ".join(text.lower().split()).strip(" .!?,")


def find_duplicated_text(transcript: list[TranscriptEntry]) -> Optional[str]:
    """The first bot sentence that appears more than once, verbatim.

    This is a real, recurring OSV defect, not a hypothetical: `text_bots.py:72`
    accumulates `response_text` across turns, so a bot's reply arrives with
    earlier sentences repeated 2x/3x/5x/7x. Seen on two different Veros bots.
    Detecting it deterministically beats hoping the evaluator notices — and the
    evaluator frequently reported `duplicated_text_seen: "none"` on transcripts
    that plainly contained it.

    Counts repeats ACROSS bot turns as well as within one, because the
    accumulation bug spreads them across turns.
    """
    seen: dict[str, str] = {}
    for entry in transcript:
        if entry.speaker != "bot" or not entry.text:
            continue
        for sentence in _SENTENCE_SPLIT.split(entry.text):
            key = _normalise(sentence)
            if len(key) < _MIN_DUPLICATE_CHARS:
                continue
            if key in seen:
                return seen[key]
            seen[key] = sentence.strip()
    return None


def compute_metrics(
    transcript: list[TranscriptEntry],
    *,
    modules_visited: Optional[list[str]] = None,
    tool_calls: Optional[list[dict]] = None,
    tag_only_bot_turns: Optional[list[dict]] = None,
) -> dict[str, Any]:
    """Everything measurable about a run. Transport-agnostic.

    The text-only arguments are omitted from the result when None, so a voice
    run's metrics gain the turn counts without sprouting keys that would read as
    "zero modules visited" when the truth is "modules are unobservable here".
    """
    metrics: dict[str, Any] = {
        "bot_turns": sum(1 for e in transcript if e.speaker == "bot"),
        "caller_turns": sum(1 for e in transcript if e.speaker == "caller"),
        "total_turns": len(transcript),
    }

    duplicate = find_duplicated_text(transcript)
    metrics["duplicated_text_seen"] = duplicate if duplicate else "none"

    if modules_visited is not None:
        # A string, matching the shape the prompts already ask for
        # (`"modules_visited": "..."`), so this drops into existing scenarios.
        metrics["modules_visited"] = " -> ".join(modules_visited) or "none"
        metrics["module_count"] = len(modules_visited)

    if tool_calls is not None:
        names = []
        for call in tool_calls:
            fn = call.get("function")
            name = fn.get("name") if isinstance(fn, dict) else call.get("name")
            names.append(str(name) if name else "(unnamed)")
        metrics["tool_call_count"] = len(tool_calls)
        metrics["tool_calls_in_order"] = ", ".join(names) or "none"

    if tag_only_bot_turns is not None:
        metrics["empty_or_tag_only_turns"] = len(tag_only_bot_turns)

    return metrics


def merge_metrics(llm_metrics: dict, computed: dict) -> dict:
    """Computed wins; the LLM's disagreeing value is preserved and logged.

    Keeping the model's number under `<key>_reported_by_llm` rather than
    dropping it is deliberate: the evaluator's accuracy has never been measured,
    and every disagreement here is a free data point. Values are compared as
    strings because the model returns "3" as often as 3.
    """
    merged = dict(llm_metrics or {})
    for key, value in computed.items():
        if key in merged and str(merged[key]).strip() != str(value).strip():
            logger.warning(
                "evaluator metric disagreement on %r: LLM said %r, measured %r "
                "— using the measurement",
                key, merged[key], value,
            )
            merged[f"{key}_reported_by_llm"] = merged[key]
        merged[key] = value
    return merged


# --------------------------------------------------------------- declared metrics


_TRUTHY = ("true", "yes", "1", "y")
_FALSY = ("false", "no", "0", "n")


def render_metric_spec(specs: list["MetricSpec"]) -> str:
    """Render declared metrics as an explicit contract for the evaluator.

    The thing this replaces is a JSON example blob pasted into each scenario's
    prompt: `{"bot_turns": 0, "required_gates_passed": "n/m", ...}`. It conveyed
    shape but never meaning, so a metric like `observed_gates_passed` was
    answered by guesswork, and a yes/no metric came back as `true`, `"yes"`,
    `"likely"` or a sentence depending on the run.

    Metrics the harness measures are NOT rendered here — see
    `metrics_to_request`. Asking for a value we compute and then override put two
    conflicting instructions in one prompt.
    """
    if not specs:
        return ""
    lines = []
    for spec in specs:
        bits = [f"- {spec.key} ({spec.type}"]
        bits.append(", optional)" if not spec.required else ")")
        line = "".join(bits)
        if spec.description:
            line += f": {spec.description}"
        if spec.example is not None:
            line += f"  [e.g. {spec.example!r}]"
        lines.append(line)
    return (
        "\n--- METRICS TO REPORT ---\n"
        "Return exactly these keys in `metrics`, and no others. Match the stated\n"
        "type: a boolean is true/false (not \"yes\"), an integer is a number (not\n"
        '"3"), an array is a JSON list.\n'
        + "\n".join(lines)
        + "\n"
    )


def metrics_to_request(specs: list["MetricSpec"]) -> list["MetricSpec"]:
    """The declared metrics minus the ones the harness measures itself.

    This is the "remove it if it only confuses the model" rule, applied
    mechanically: a spec for `bot_turns` is dropped, because `compute_metrics`
    already counted it and `merge_metrics` would override whatever came back.
    Silently dropping is right here — the value still appears in the result, so
    nothing is lost, and lint warns the author separately.
    """
    return [s for s in specs if s.key not in COMPUTED_KEYS]


def _coerce(value: Any, declared: str) -> tuple[Any, Optional[str]]:
    """Coerce `value` to `declared`. Returns (value, problem-or-None).

    Coerces the near-misses that models actually produce ("3", "true") and
    reports the rest rather than silently accepting them — an unchecked metric
    that reads plausibly is worse than one flagged as wrong.
    """
    if value is None:
        return None, "missing"
    if declared == "boolean":
        if isinstance(value, bool):
            return value, None
        text = str(value).strip().lower()
        if text in _TRUTHY:
            return True, None
        if text in _FALSY:
            return False, None
        return value, f"expected boolean, got {value!r}"
    if declared in ("integer", "number"):
        if isinstance(value, bool):  # bool is an int subclass; not a count
            return value, f"expected {declared}, got boolean {value!r}"
        if isinstance(value, int) and declared == "integer":
            return value, None
        if isinstance(value, (int, float)):
            return (int(value) if declared == "integer" else float(value)), None
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return value, f"expected {declared}, got {value!r}"
        return (int(number) if declared == "integer" else number), None
    if declared == "array":
        if isinstance(value, (list, tuple)):
            return list(value), None
        return value, f"expected array, got {type(value).__name__} {value!r}"
    return (value if isinstance(value, str) else str(value)), None


def validate_metrics(
    specs: list["MetricSpec"], metrics: dict
) -> tuple[dict, list[str]]:
    """Type-check declared metrics. Returns (coerced, problems).

    Never drops a value and never raises: a bad metric is reported, not
    discarded. Extra keys the model volunteered are kept too — an unrequested
    metric is noise, not a failure, and deleting it would hide what the model
    actually said.
    """
    out = dict(metrics or {})
    problems: list[str] = []
    for spec in specs:
        if spec.key in COMPUTED_KEYS:
            continue  # measured; whatever the model said is about to be overridden
        if spec.key not in out:
            if spec.required:
                problems.append(f"{spec.key}: missing (declared {spec.type})")
            continue
        coerced, problem = _coerce(out[spec.key], spec.type)
        out[spec.key] = coerced
        if problem:
            problems.append(f"{spec.key}: {problem}")

    declared = {s.key for s in specs}
    extra = [k for k in out if k not in declared and k not in COMPUTED_KEYS
             and not k.endswith("_reported_by_llm")]
    if extra:
        problems.append(f"undeclared metrics returned: {', '.join(sorted(extra))}")

    for problem in problems:
        logger.warning("declared-metric problem — %s", problem)
    return out, problems
