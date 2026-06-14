"""Conversation flow engine — walks through steps, evaluates bot speech."""

from __future__ import annotations

import logging
from typing import Optional

from opkura.core.models import ExpectConfig, Flow, FlowStep, StepResult, StepStatus

logger = logging.getLogger(__name__)


class FlowEngine:
    """State machine that walks a Flow's steps.

    Lifecycle per step:
      1. Bot speaks → call ``accumulate_bot_speech(text)``
      2. Bot goes silent → call ``end_bot_turn()``
         - Evaluates expectations against accumulated text
         - Returns our response text (``step.say``) or None if flow is done
      3. We speak the response via TTS
      4. Repeat from 1 for next step
    """

    def __init__(self, flow: Flow):
        self.flow = flow
        self.current_step = 0
        self.step_results: list[StepResult] = []
        self._bot_buffer: str = ""

    # -- public state --

    @property
    def is_complete(self) -> bool:
        return self.current_step >= len(self.flow.steps)

    @property
    def current_flow_step(self) -> Optional[FlowStep]:
        if not self.is_complete:
            return self.flow.steps[self.current_step]
        return None

    # -- callbacks --

    def accumulate_bot_speech(self, text: str):
        """Append a final transcript chunk from the bot."""
        if text:
            self._bot_buffer = (self._bot_buffer + " " + text).strip()

    def end_bot_turn(self) -> Optional[FlowStep]:
        """Finalize the bot's turn.

        Returns:
            The current FlowStep (containing .say and/or .dtmf) so the caller
            can speak and/or send DTMF tones.  None if the flow is done.
        """
        if self.is_complete:
            return None

        step = self.flow.steps[self.current_step]
        bot_text = self._bot_buffer.strip()
        logger.info(
            "Step %d | bot said: %s", self.current_step, bot_text[:120] or "(silence)"
        )

        result = self._evaluate(step, bot_text)
        self.step_results.append(result)
        logger.info(
            "Step %d | %s | expectations: %s",
            self.current_step,
            result.status.value,
            result.expectations_met,
        )

        self._bot_buffer = ""
        self.current_step += 1

        return step

    # -- evaluation --

    def _evaluate(self, step: FlowStep, bot_text: str) -> StepResult:
        status = StepStatus.PASSED
        expectations: dict[str, bool] = {}

        if step.expect:
            bot_lower = bot_text.lower()

            if step.expect.contains_any:
                met = any(kw.lower() in bot_lower for kw in step.expect.contains_any)
                expectations["contains_any"] = met
                if not met:
                    status = StepStatus.FAILED

            if step.expect.contains_all:
                for kw in step.expect.contains_all:
                    key = f"contains:{kw}"
                    met = kw.lower() in bot_lower
                    expectations[key] = met
                    if not met:
                        status = StepStatus.FAILED

            if step.expect.not_contains:
                for kw in step.expect.not_contains:
                    key = f"not_contains:{kw}"
                    met = kw.lower() not in bot_lower
                    expectations[key] = met
                    if not met:
                        status = StepStatus.FAILED

        if not bot_text:
            status = StepStatus.TIMEOUT

        return StepResult(
            step_index=self.current_step,
            status=status,
            bot_transcript=bot_text,
            our_utterance=step.say or "",
            our_dtmf=step.dtmf or "",
            expectations_met=expectations,
        )
