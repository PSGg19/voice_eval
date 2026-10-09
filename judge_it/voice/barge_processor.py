"""P6.2 — barge as a processor: watches partials, emits early caller turns.

One processor, two configurations (the scenario's `barge_in.mode`):

* **scheduled** — every `BotPartialSpeech` updates the turn's word-count
  high-water mark (Deepgram interims occasionally regress on
  re-segmentation; a regression must not un-fire a threshold) and asks the
  ScheduledTriggerEvaluator; a fire claims the trigger index SYNCHRONOUSLY —
  two back-to-back partials must not both see it unfired — then emits.
* **llm** — a spawned poll loop asks `caller.maybe_barge(partial)` every
  `decide_every_ms` with the newest partial (deduplicated); a barge=true
  reply emits, at most once per bot turn.
* **off** — pure passthrough.

Emission is `CallerSpeech(text, barge=True)` (and/or `CallerDTMF`) straight
downstream — the same TTS → audio-out links speak it, so a barge utterance
can never interleave with a normal turn's audio: the queues serialize what
the monolith needed a send-lock and a drain for. The barge is committed into
the caller engine's history (`record_barge`) so the next end-of-turn reply
sees what was said mid-utterance — exactly the monolith's contract.

No-op on text BY CONSTRUCTION: the text adapter never produces
BotPartialSpeech frames, so neither mode ever has anything to react to.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from judge_it.engine.barge import ScheduledTriggerEvaluator
from judge_it.frames import BotPartialSpeech, BotTurnEnded, CallerDTMF, CallerSpeech
from judge_it.voice.frames import CallEnded
from judge_it.voice.processor import Processor

logger = logging.getLogger(__name__)


class BargeProcessor(Processor):
    """`caller` is the run's AICallerEngine — the same instance the engine
    bridge drives, so barge history and turn replies share one ledger."""

    def __init__(self, caller, name: str = "barge", **kw):
        super().__init__(name, **kw)
        self.caller = caller
        self.mode: str = getattr(caller, "barge_mode", "off")
        self._evaluator: Optional[ScheduledTriggerEvaluator] = None
        if self.mode == "scheduled":
            self._evaluator = ScheduledTriggerEvaluator(caller.scenario.barge_in)
        self._partial: str = ""
        self._barged_this_turn = False
        self._poll_task: Optional[asyncio.Task] = None
        self.fired: list[str] = []           # what was barged, for tests/log

    async def start(self) -> None:
        if self.mode == "llm":
            self._poll_task = self.spawn(self._llm_poll(), name="llm-poll")

    # ---- the frame path ------------------------------------------------------

    async def handle(self, frame) -> None:
        if isinstance(frame, BotPartialSpeech) and self.mode != "off":
            await self._on_partial(frame.text)
        elif isinstance(frame, BotTurnEnded):
            # New turn: fresh word count, fresh one-barge-per-turn budget.
            self._partial = ""
            self._barged_this_turn = False
            self.caller.reset_bot_turn_counters()
        await self.emit(frame)

    async def _on_partial(self, text: str) -> None:
        if not text:
            return
        self._partial = text
        if self.mode != "scheduled" or self._evaluator is None:
            return
        if self._barged_this_turn:
            return
        word_count = len(text.split())
        self.caller.bot_word_count_in_current_turn = max(
            self.caller.bot_word_count_in_current_turn, word_count
        )
        fire = self._evaluator.check(
            # turn_count increments at END of bot turn; while the bot is
            # speaking its "Nth turn" is turn_count + 1 — monolith semantics.
            current_turn=self.caller.turn_count + 1,
            bot_word_count=self.caller.bot_word_count_in_current_turn,
            already_fired=self.caller.triggers_fired,
        )
        if fire is not None:
            # Claim synchronously — before any await — so the next partial
            # cannot double-fire the same trigger.
            self.caller.triggers_fired.add(fire.trigger_index)
            await self._emit_barge(speech=fire.trigger.say, dtmf="",
                                   bot_partial=text)

    # ---- llm mode ---------------------------------------------------------------

    async def _llm_poll(self) -> None:
        cfg = self.caller.scenario.barge_in
        interval_s = max(getattr(cfg, "decide_every_ms", 500), 1) / 1000.0
        last_seen = ""
        while True:
            await asyncio.sleep(interval_s)
            partial = self._partial
            if self._barged_this_turn or not partial.strip() or partial == last_seen:
                continue
            last_seen = partial
            try:
                turn = await self.caller.maybe_barge(partial)
            except Exception as exc:
                logger.warning("barge decision LLM failed: %s", exc)
                continue
            if turn is None:
                continue
            await self._emit_barge(speech=turn.speech, dtmf=turn.dtmf,
                                   bot_partial=partial)

    # ---- emission -----------------------------------------------------------------

    async def _emit_barge(self, *, speech: str, dtmf: str, bot_partial: str) -> None:
        if not speech and not dtmf:
            return
        self._barged_this_turn = True
        self.fired.append(speech or f"[DTMF: {dtmf}]")
        logger.info("CALLER (barge): %s", speech or f"[DTMF: {dtmf}]")
        # One ledger: the next end_bot_turn reply must see the interruption.
        self.caller.record_barge(speech=speech, dtmf=dtmf, bot_partial=bot_partial)
        if speech:
            await self.emit(CallerSpeech(text=speech, barge=True))
        if dtmf:
            await self.emit(CallerDTMF(digits=dtmf))

    async def on_call_ended(self, frame: CallEnded) -> None:
        return None
