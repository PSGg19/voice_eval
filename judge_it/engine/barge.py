"""Evaluator that fires scheduled barge triggers during a bot turn."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from judge_it.core.models import BargeInConfig, ScheduledTrigger

logger = logging.getLogger(__name__)


@dataclass
class TriggerFire:
    trigger_index: int
    trigger: ScheduledTrigger


class ScheduledTriggerEvaluator:
    """Walks the scenario's scheduled triggers and returns the first one whose
    (current_turn, bot_word_count) conditions are satisfied and which has not
    already fired this call. Caller is responsible for adding the returned
    trigger's index to `already_fired` to prevent a re-fire.

    IMPORTANT: mark the trigger as fired BEFORE any async hand-off. Two STT
    interim events arriving back-to-back can both observe a not-yet-claimed
    trigger if the caller defers `already_fired.add(...)` to the spawned task.
    """

    def __init__(self, config: BargeInConfig):
        if config.mode != "scheduled":
            raise ValueError(
                f"ScheduledTriggerEvaluator requires mode='scheduled', got {config.mode!r}"
            )
        self.config = config

    def check(
        self,
        *,
        current_turn: int,
        bot_word_count: int,
        already_fired: set[int],
    ) -> Optional[TriggerFire]:
        for idx, trigger in enumerate(self.config.triggers):
            if idx in already_fired:
                continue
            if trigger.at_turn != current_turn:
                continue
            if bot_word_count < trigger.after_bot_words:
                continue
            return TriggerFire(trigger_index=idx, trigger=trigger)
        return None
