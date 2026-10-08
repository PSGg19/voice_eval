"""End-of-call evaluator: one LLM call returns structured pass/fail."""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Optional

from vjudge.llm import LLMClient
from vjudge.core.models import EvaluationCriterion, EvaluationResult, TranscriptEntry
from vjudge.core.models import MetricSpec
from vjudge.engine.metrics import (
    compute_metrics,
    merge_metrics,
    metrics_to_request,
    render_metric_spec,
    validate_metrics,
)

if TYPE_CHECKING:
    from vjudge.persistence.logwriter import CallLogWriter

logger = logging.getLogger(__name__)

# Gemini 2.5 Pro spends `max_output_tokens` on internal "thinking"
# BEFORE emitting any response text. With 2000 tokens, thinking ate the
# whole budget on barge scenarios (10+ criteria) and the response came
# back empty or truncated mid-string. 16000 gives Pro ~12k for thinking
# + ~4k for the criteria JSON. Same fix as bcg.generator. Cost is
# negligible — evaluator runs once per call, ~$0.05-0.15 per scenario.
DEFAULT_MAX_TOKENS = 16000
STRICT_RETRY_SUFFIX = (
    "\n\nIMPORTANT: respond with STRICT JSON only, no prose, no markdown. "
    "Required fields: overall_passed (bool), overall_reasoning (string), "
    "criteria (list of {id, name, passed, evidence}), metrics (dict)."
)


def _to_bool(v) -> bool:
    """Coerce LLM output to bool, treating 'false'/'no'/'0' as False.
    bool('false') is True in Python — that flips a 'failed' verdict to 'passed'.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


_DIGITS = re.compile(r"\d+")


def _to_criterion_id(v, fallback: int) -> int:
    """Coerce an LLM-supplied criterion id to int, never raising.

    The id is a *label* — it does not affect pass/fail. But a bare
    `int(c["id"])` raises on anything non-numeric ("C1", "criterion_3",
    "auth-gate", null), and `_parse`'s caller treats any exception as
    "evaluator output malformed": the whole verdict is discarded, including a
    perfectly good `overall_passed`, a retry LLM call is burned, and the run is
    reported malformed. Measured on live Veros runs twice. So: pull the first
    integer out of whatever arrived, and fall back to the criterion's 1-based
    position when there is no digit at all.
    """
    if isinstance(v, bool):  # bool is an int subclass — not an id
        return fallback
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        m = _DIGITS.search(v)
        if m:
            return int(m.group())
    return fallback


class Evaluator:
    def __init__(
        self,
        evaluation_prompt: str,
        llm: LLMClient,
        temperature: float = 0.3,
        metric_spec: Optional[list[MetricSpec]] = None,
    ):
        self.evaluation_prompt = evaluation_prompt
        self.llm = llm
        self.temperature = temperature
        # Declared metrics (scenario `metrics:`). Empty means the evaluation
        # prompt's own example blob governs, i.e. every pre-existing scenario.
        self.metric_spec = list(metric_spec or [])
        self._logwriter: Optional["CallLogWriter"] = None

    def attach_logwriter(self, lw: "CallLogWriter") -> None:
        self._logwriter = lw

    def _record_llm_call(
        self,
        messages: list[dict],
        response: Optional[dict],
        start: float,
        error: Optional[BaseException] = None,
    ) -> None:
        if self._logwriter is None:
            return
        latency_ms = (time.monotonic() - start) * 1000.0
        # Usage rides IN the result (LLMResult, P1.2); None/plain-dict safe.
        usage = getattr(response, "usage", None) or {}
        # turn=0 marks end-of-call entries; caller turns are 1-indexed.
        self._logwriter.record_llm_call(
            turn=0,
            role="evaluator",
            input_messages=list(messages),
            output=response,
            latency_ms=latency_ms,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            error=repr(error) if error is not None else None,
        )

    async def evaluate(
        self,
        transcript: list[TranscriptEntry],
        *,
        modules_visited: Optional[list[str]] = None,
        tool_calls: Optional[list[dict]] = None,
        tag_only_bot_turns: Optional[list[dict]] = None,
    ) -> EvaluationResult:
        """Judge one call.

        The keyword args are text-transport observations the audio path cannot
        produce. They are passed explicitly rather than read off a CallResult so
        this stays testable without one, and default to None so every existing
        caller is unaffected.

        Handing these over is not a nicety. Measured on the 1740 CI OB probe:
        with the transcript alone the evaluator passed a `flow_depth` criterion
        on the evidence *"modules_visited was not provided, but the flow
        progressed as expected"* — i.e. it guessed, and reported the guess as a
        PASS. The data existed; it just never reached here.
        """
        # Optimization (not in PRD): short-circuit empty transcripts to a clean
        # verdict instead of sending an empty user message to the LLM.
        if not transcript:
            return EvaluationResult(
                overall_passed=False,
                overall_reasoning="empty transcript — call never reached the conversation stage",
                criteria=[],
                metrics={},
                raw_response="empty transcript, evaluator not invoked",
                malformed=False,
            )

        computed = compute_metrics(
            transcript,
            modules_visited=modules_visited,
            tool_calls=tool_calls,
            tag_only_bot_turns=tag_only_bot_turns,
        )
        formatted = (
            self._format_transcript(transcript)
            + self._format_observations(
                modules_visited=modules_visited,
                tool_calls=tool_calls,
                tag_only_bot_turns=tag_only_bot_turns,
            )
            + self._format_computed_metrics(computed)
            + render_metric_spec(metrics_to_request(self.metric_spec))
        )
        messages = [{"role": "user", "content": formatted}]

        first_raw: Optional[str] = None
        first_response: Optional[dict] = None
        first_error: Optional[BaseException] = None
        start = time.monotonic()
        try:
            first_response = await self.llm.complete_json(
                system=self.evaluation_prompt,
                messages=messages,
                max_tokens=DEFAULT_MAX_TOKENS,
                temperature=self.temperature,
            )
        except Exception as exc:
            first_error = exc
        # Record once per LLM attempt (success or call-level failure). A
        # subsequent parse failure does not produce a second entry — the
        # recorded response is the audit trail.
        self._record_llm_call(messages, first_response, start, error=first_error)

        if first_response is not None:
            try:
                first_raw = repr(first_response)
                return self._parse(first_response, raw=first_raw, computed=computed)
            except Exception as parse_exc:
                logger.warning("Evaluator first parse failed: %s — retrying once", parse_exc)
        else:
            logger.warning("Evaluator first call failed: %s — retrying once", first_error)
            first_raw = f"<exception: {first_error!r}>"

        second_raw: Optional[str] = None
        second_response: Optional[dict] = None
        second_error: Optional[BaseException] = None
        start = time.monotonic()
        try:
            second_response = await self.llm.complete_json(
                system=self.evaluation_prompt + STRICT_RETRY_SUFFIX,
                messages=messages,
                max_tokens=DEFAULT_MAX_TOKENS,
                temperature=0.0,
            )
        except Exception as exc:
            second_error = exc
        self._record_llm_call(messages, second_response, start, error=second_error)

        if second_response is not None:
            try:
                second_raw = repr(second_response)
                return self._parse(second_response, raw=second_raw, computed=computed)
            except Exception:
                logger.exception("Evaluator retry parse failed — producing malformed verdict")
        else:
            # v1 bugfix: logger.exception outside an active except block produces
            # no traceback (sys.exc_info() was cleared when the except above exited).
            logger.error(
                "Evaluator retry call failed: %s — producing malformed verdict",
                second_error,
            )
            second_raw = f"<exception: {second_error!r}>"

        return EvaluationResult(
            overall_passed=False,
            overall_reasoning="evaluator output malformed",
            criteria=[],
            # The measurements are still valid — the model's verdict is what
            # failed, not the transcript. Dropping them here would lose the turn
            # counts on exactly the runs that most need explaining.
            metrics=computed,
            raw_response=f"first={first_raw} second={second_raw}",
            malformed=True,
        )

    def _format_transcript(self, transcript: list[TranscriptEntry]) -> str:
        """Render the transcript for the evaluator LLM.

        Surfaces barge metadata (`barge`, `bot_partial_at_barge`,
        `bot_response_after_ms`) inline so barge-aware evaluation
        prompts can reliably count barges and read latencies.
        Previously these fields were dropped and the evaluator
        hallucinated "0 barges" / made-up latency numbers on barge
        scenarios — observed in the 2026-05-18 A-001-barge-llm runs.
        """
        lines = []
        for i, entry in enumerate(transcript, start=1):
            seconds = (entry.timestamp_ms or 0) / 1000.0
            extras: list[str] = []
            dtmf = getattr(entry, "dtmf", None)
            if dtmf:
                extras.append(f"DTMF: {dtmf}")
            tag = f", {', '.join(extras)}" if extras else ""

            # Detect barge entries by the field that's only ever non-None
            # for an actual fired barge. `barge: false` entries (the
            # default for regular turns) and `None` both skip the tag.
            is_barge = bool(getattr(entry, "barge", False))
            barge_prefix = "[BARGE] " if is_barge else ""

            line = f"Turn {i} ({entry.speaker}, {seconds:.1f}s{tag}): {barge_prefix}{entry.text}"

            # Barge metadata as bullet sub-lines so the evaluator can
            # parse them turn-by-turn without confusing field names
            # across entries. Only emitted when the value is set.
            if is_barge:
                bot_partial = getattr(entry, "bot_partial_at_barge", None)
                if bot_partial:
                    line += f"\n    - bot was saying: {bot_partial!r}"
                response_ms = getattr(entry, "bot_response_after_ms", None)
                if response_ms is not None:
                    line += f"\n    - bot reacted in: {response_ms:.0f}ms"

            lines.append(line)

        # Header summarising barge counts so the evaluator can sanity-check
        # its own counting at a glance. Cheap signal that catches "0 barges
        # detected" hallucinations on the spot.
        barge_count = sum(
            1 for e in transcript if bool(getattr(e, "barge", False))
        )
        header = (
            f"[TRANSCRIPT SUMMARY] "
            f"{len(transcript)} total turns, {barge_count} barge entry(ies). "
            f"Barge entries are flagged with `[BARGE]` and include the "
            f"bot's partial transcript and reaction latency on sub-lines.\n"
        )
        return header + "\n".join(lines)

    def _format_observations(
        self,
        *,
        modules_visited: Optional[list[str]],
        tool_calls: Optional[list[dict]],
        tag_only_bot_turns: Optional[list[dict]],
    ) -> str:
        """Render the structured, non-transcript facts about the run.

        Only emitted for the transport that can observe them, and each section
        is emitted only when there is something to say — except tag-only turns,
        where **zero is a meaningful, assertable fact** and silence would invite
        the model to invent a number. `empty_or_tag_only_turns` is a metric the
        Veros eval prompts already ask for.

        Phrased as OBSERVED FACTS with an explicit instruction not to contradict
        them, because the failure being fixed is the model narrating around
        missing data rather than admitting it.
        """
        if modules_visited is None and tool_calls is None and tag_only_bot_turns is None:
            return ""

        parts: list[str] = [
            "\n\n--- OBSERVED FACTS (recorded by the harness, not inferred from "
            "the transcript) ---",
            "These are measurements, not claims. Do not contradict them, and do "
            "not report a value as unavailable when it is given below.",
        ]

        if modules_visited is not None:
            parts.append(
                f"modules_visited ({len(modules_visited)}, in order): "
                + (" -> ".join(modules_visited) if modules_visited else "(none recorded)")
            )
        if tool_calls is not None:
            names = []
            for call in tool_calls:
                fn = call.get("function")
                name = fn.get("name") if isinstance(fn, dict) else call.get("name")
                turn = call.get("_turn_index")
                names.append(
                    f"{name or '(unnamed)'}" + (f" @turn {turn}" if turn is not None else "")
                )
            parts.append(
                f"tool_calls ({len(tool_calls)}, in order): "
                + (", ".join(names) if names else "(none)")
            )
        if tag_only_bot_turns is not None:
            n = len(tag_only_bot_turns)
            if n:
                detail = "; ".join(
                    f"turn {t.get('turn_index')}: {' '.join(t.get('tags') or [])}"
                    for t in tag_only_bot_turns
                )
                parts.append(
                    f"empty_or_tag_only_turns: {n} ({detail}). Each of these "
                    f"reached the caller as SILENCE — the bot emitted only marker "
                    f"tags, which are stripped before delivery. This is a "
                    f"bot-flow defect, not a harness defect."
                )
            else:
                parts.append("empty_or_tag_only_turns: 0")

        return "\n".join(parts) + "\n"

    def _format_computed_metrics(self, computed: dict) -> str:
        """Give the model the counts instead of making it tally them.

        `bot_turns` / `caller_turns` are requested by 1108 of the corpus's
        scenarios. Handing them over removes an arithmetic step that has no
        upside and a real error rate, and it means the `metrics` block the model
        returns agrees with the one vjudge reports — they used to be able to
        diverge silently.
        """
        if not computed:
            return ""
        lines = "\n".join(f"{k}: {v}" for k, v in computed.items())
        return (
            "\n--- MEASURED METRICS (already counted for you — copy these into "
            "`metrics` verbatim) ---\n"
            f"{lines}\n"
            "Do not recount them. Fill in only the metrics NOT listed above "
            "(e.g. gate tallies), which are yours to judge.\n"
        )

    def _parse(
        self,
        response: dict,
        raw: Optional[str] = None,
        computed: Optional[dict] = None,
    ) -> EvaluationResult:
        overall_passed = _to_bool(response["overall_passed"])
        overall_reasoning = str(response.get("overall_reasoning", ""))

        criteria: list[EvaluationCriterion] = []
        for pos, c in enumerate(response.get("criteria", []) or [], start=1):
            # `passed` stays strict — a missing verdict really is malformed
            # output and the strict retry is the right answer. `id` and `name`
            # are labels, so they must never cost us the verdict.
            criteria.append(
                EvaluationCriterion(
                    id=_to_criterion_id(c.get("id"), pos),
                    name=str(c.get("name") or f"criterion {pos}"),
                    passed=_to_bool(c["passed"]),
                    evidence=str(c.get("evidence", "")),
                )
            )

        # Measured values override reported ones; a disagreement is logged and
        # the model's number kept as `<key>_reported_by_llm`.
        metrics = merge_metrics(response.get("metrics") or {}, computed or {})
        if self.metric_spec:
            # Type-check what the model returned against the declaration. Never
            # drops a value — a wrong metric is reported, not hidden.
            metrics, _problems = validate_metrics(self.metric_spec, metrics)

        return EvaluationResult(
            overall_passed=overall_passed,
            overall_reasoning=overall_reasoning,
            criteria=criteria,
            metrics=metrics,
            raw_response=raw,
            malformed=False,
        )
