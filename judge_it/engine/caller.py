"""LLM-driven caller engine: turn-taking (Task 2) and barge modes (Task 5)."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Optional

from judge_it.llm import LLMClient
from judge_it.core.models import Scenario

if TYPE_CHECKING:
    from judge_it.persistence.logwriter import CallLogWriter

logger = logging.getLogger(__name__)

DEFAULT_BOT_TURN_TIMEOUT_MS = 15_000
DEFAULT_MAX_TOKENS = 500
# Hang up after this many consecutive bot turns where STT received nothing.
# Three 15s timeouts ≈ 45 seconds of dead air — bot has clearly stopped
# responding (internal error, hung up from their side, network drop).
# Without this guard the conversation loop spins until max_duration fires
# (e.g. 405s for max_turns=25).
MAX_EMPTY_BOT_TURNS = 3
STRICT_RETRY_SUFFIX = (
    "\n\nRESPOND IN EXACTLY THIS JSON: "
    '{"speech":"...","dtmf":"...","hangup":false,"barge":false}. '
    "No other text."
)

# DTMF chars accepted by judge_it.transport.dtmf.generate_sequence: 0-9, A-D, *, #, w (pause), space.
_DTMF_ALLOWED_RE = re.compile(r"^[0-9A-Da-d*#w ]*$")


def _sanitize_dtmf(raw: str) -> str:
    """Drop the field entirely if the LLM hallucinated non-digit chars.
    Sending 'press 1' to generate_sequence raises ValueError mid-call.
    """
    if not raw:
        return ""
    if _DTMF_ALLOWED_RE.match(raw):
        return raw
    logger.warning("dropping invalid dtmf payload from LLM: %r", raw)
    return ""


@dataclass
class CallerTurn:
    speech: str = ""
    dtmf: str = ""
    hangup: bool = False
    barge: bool = False


@dataclass
class _TimeoutStub:
    timeout_ms: int


def _to_bool(v) -> bool:
    """Coerce LLM output to bool; treat 'false'/'no'/'0' strings as False.
    bool('false') is True in Python — that would flip a hangup/barge intent.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


def _coerce_turn(response: dict) -> CallerTurn:
    return CallerTurn(
        speech=str(response.get("speech") or ""),
        dtmf=_sanitize_dtmf(str(response.get("dtmf") or "")),
        hangup=_to_bool(response.get("hangup", False)),
        barge=_to_bool(response.get("barge", False)),
    )


class AICallerEngine:
    def __init__(self, scenario: Scenario, llm: LLMClient):
        self.scenario = scenario
        self.llm = llm
        self.history: list[dict] = []
        self.turn_count: int = 0
        self.hangup: bool = False
        self._bot_speech_buffer: list[str] = []
        # Joined bot utterance from the most recent end_bot_turn() — stream.py
        # reads this to surface bot turns into result.transcript (V2 path has
        # no step_results, so this is the only handle).
        self.last_bot_utterance: str = ""
        # Consecutive empty-utterance count for abandoned-call detection.
        self._consecutive_empty_bot_turns: int = 0
        self._logwriter: Optional["CallLogWriter"] = None

        # Barge state (Task 5). barge_mode derived once at construction so
        # stream.py and tests can branch cheaply without re-reading the config.
        self.barge_mode: str = self._derive_barge_mode(scenario)
        self.bot_word_count_in_current_turn: int = 0
        self.triggers_fired: set[int] = set()

    @staticmethod
    def _derive_barge_mode(scenario: Scenario) -> str:
        cfg = scenario.barge_in
        if cfg is None or not cfg.enabled:
            return "off"
        return cfg.mode

    def reset_bot_turn_counters(self) -> None:
        self.bot_word_count_in_current_turn = 0

    def attach_logwriter(self, lw: "CallLogWriter") -> None:
        self._logwriter = lw

    def record_bot_response_latency(self, latency_ms: float) -> None:
        """Forward bot-response latency to logwriter if attached (called by stream.py)."""
        if self._logwriter is not None:
            self._logwriter.record_bot_response_latency(latency_ms)

    def accumulate_bot_speech(self, text: str) -> None:
        if text:
            self._bot_speech_buffer.append(text)

    async def end_bot_turn(self) -> Optional[CallerTurn]:
        bot_utterance = " ".join(self._bot_speech_buffer).strip()
        self._bot_speech_buffer.clear()
        # Set even on empty utterance so stream.py never re-records a stale value.
        self.last_bot_utterance = bot_utterance

        if not bot_utterance:
            self._consecutive_empty_bot_turns += 1
            if self._consecutive_empty_bot_turns >= MAX_EMPTY_BOT_TURNS:
                logger.warning(
                    "%d consecutive empty bot turns — bot stopped responding, hanging up",
                    self._consecutive_empty_bot_turns,
                )
                self.hangup = True
            else:
                logger.warning(
                    "end_bot_turn called with empty bot utterance — skipping (%d/%d)",
                    self._consecutive_empty_bot_turns, MAX_EMPTY_BOT_TURNS,
                )
            return None

        # Bot is responsive again — reset the abandonment counter.
        self._consecutive_empty_bot_turns = 0

        self.history.append({"role": "user", "content": bot_utterance})

        turn = await self._invoke_llm(self.scenario.persona_rendered, self.history)

        if turn.barge:
            # End-of-turn replies are never barges by definition — the bot has
            # already stopped. Clear the flag in every mode. Only warn in "off"
            # mode where the LLM shouldn't be emitting barge=true at all.
            if self.barge_mode == "off":
                logger.warning("barge=true ignored in turn-taking mode")
            turn = replace(turn, barge=False)

        self.history.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "speech": turn.speech,
                        "dtmf": turn.dtmf,
                        "hangup": turn.hangup,
                        "barge": turn.barge,
                    }
                ),
            }
        )

        self.turn_count += 1

        if turn.hangup or self.turn_count >= self.scenario.max_turns:
            self.hangup = True

        return turn

    async def _invoke_llm(self, system: str, messages: list[dict]) -> CallerTurn:
        # thinking_budget=0 disables Gemini's internal "thinking" for caller
        # turns — saves 5-10s of latency on every reply. Caller LLM returns
        # short structured JSON (speech + flags), not chain-of-reasoning, so
        # thinking buys nothing here. Evaluator keeps thinking on for grading.
        response: Optional[dict] = None
        start = time.monotonic()
        try:
            response = await self.llm.complete_json(
                system=system,
                messages=messages,
                max_tokens=DEFAULT_MAX_TOKENS,
                temperature=self.scenario.llm.temperature,
                thinking_budget=0,
            )
            self._record_llm_call(messages, response, start)
            return _coerce_turn(response)
        except Exception as first_exc:
            logger.warning("LLM call/parse failed: %s — retrying once", first_exc)
            self._record_llm_call(messages, response, start, error=first_exc)

        start = time.monotonic()
        response = None
        try:
            response = await self.llm.complete_json(
                system=system + STRICT_RETRY_SUFFIX,
                messages=messages,
                max_tokens=DEFAULT_MAX_TOKENS,
                temperature=0.0,
                thinking_budget=0,
            )
            self._record_llm_call(messages, response, start)
            return _coerce_turn(response)
        except Exception as exc:
            logger.exception("LLM retry also failed — ending call")
            self._record_llm_call(messages, response, start, error=exc)
            self.hangup = True
            return CallerTurn(speech="", dtmf="", hangup=True, barge=False)

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
        # Usage rides IN the result (LLMResult, P1.2) — call-local by
        # construction, so a shared client cannot cross-attribute tokens.
        # Defensive getattr: a failed call passes response=None, and test
        # doubles may return plain dicts.
        usage = getattr(response, "usage", None) or {}
        self._logwriter.record_llm_call(
            turn=self.turn_count + 1,
            role="caller",
            input_messages=list(messages),
            output=response,
            latency_ms=latency_ms,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            error=repr(error) if error is not None else None,
        )

    async def maybe_barge(self, partial_bot_transcript: str) -> Optional[CallerTurn]:
        """LLM-mode periodic decision: given the partial bot transcript so far,
        ask the caller LLM whether to barge. Returns the CallerTurn iff the LLM
        decided to barge AND emitted speech or DTMF. The partial is NOT
        persisted to self.history — decisions are provisional. When a barge
        actually fires, stream.py calls `record_barge` to commit it.

        Lifecycle invariant: this method must never end the call. _invoke_llm's
        double-failure path sets self.hangup=True (correct for end_bot_turn,
        wrong for a polling blip during bot speech), so we snapshot+restore
        around it. A transient LLM failure during a decision-check degrades to
        "skip this poll" instead of killing the scenario mid-utterance — the
        next end_bot_turn will hangup naturally if the LLM stays unreachable.
        """
        if not partial_bot_transcript.strip():
            return None
        # Tag poll messages so the LLM can distinguish a mid-turn barge
        # decision from an end-of-turn response. Without this, the LLM
        # treats every partial like a finished turn and pre-answers
        # questions it predicts the bot is about to ask. The marker is
        # opt-in: personas that reference it apply the BARGE RULE; legacy
        # personas still receive a coherent user message and behave as
        # before.
        poll_content = (
            "[BARGE POLL — the bot is STILL SPEAKING. The text below is "
            "the partial transcript of the bot's CURRENT turn so far, "
            "NOT a finished question. Apply your persona's BARGE RULE "
            "to decide whether to barge. If the rule's conditions aren't "
            "met, return barge=false with empty speech — do NOT answer "
            "as if the bot had finished speaking.]\n\n"
            f"{partial_bot_transcript}"
        )
        messages = list(self.history) + [
            {"role": "user", "content": poll_content}
        ]
        hangup_before = self.hangup
        turn = await self._invoke_llm(self.scenario.persona_rendered, messages)
        self.hangup = hangup_before
        if turn.barge and (turn.speech or turn.dtmf):
            return turn
        return None

    def record_barge(self, speech: str, dtmf: str, bot_partial: str) -> None:
        """Commit a fired barge into caller history so subsequent end_bot_turn
        invocations see what was said mid-utterance. Used by stream.py for both
        scheduled and LLM modes. Pairs bot_partial (user role) with the barge
        speech (assistant role) to preserve the strict user/assistant
        alternation Gemini expects.
        """
        if bot_partial:
            self.history.append({"role": "user", "content": bot_partial})
        self.history.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "speech": speech,
                        "dtmf": dtmf,
                        "hangup": False,
                        "barge": True,
                    }
                ),
            }
        )

    @property
    def is_complete(self) -> bool:
        return self.hangup or self.turn_count >= self.scenario.max_turns

    @property
    def current_flow_step(self) -> Optional[_TimeoutStub]:
        if self.is_complete:
            return None
        return _TimeoutStub(timeout_ms=DEFAULT_BOT_TURN_TIMEOUT_MS)

    @property
    def step_results(self) -> list:
        return []
