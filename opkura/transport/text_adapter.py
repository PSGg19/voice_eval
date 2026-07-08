"""P2.3 — the text frame adapter: one HTTP round trip, framed directly.

Wraps a `TextTransport` (or any object with its `start()/send()/close()`
surface returning `BotTurn`-shaped replies) as the `FrameTransport` the
conversation engine drives. There is no chain and no async queue: `deliver()`
performs the POST inline and stages the reply's frames in a plain list that
`receive()` pops. The transport's retry-before-delivery rule is untouched —
retries live inside `TextTransport._post`, below this adapter, and never
duplicate a delivered turn.

Divergence decisions this adapter declares (P2.5):

* **D4 — `nudge_on_silence = True`.** An HTTP response is a definitively
  finished turn, so silence here is real; the engine nudges like a caller
  ("hello?"), bounded.
* **D1 — the final line is POSTed.** When the caller turn carries both speech
  and hangup, the speech is sent so the bot-side interaction records the
  caller's goodbye (the old loop broke out before sending — the line existed
  only in opkura's transcript). The bot's reply to a goodbye is post-hangup
  noise: discarded from the conversation, logged for triage.
* **D5 — nothing to drain.** The one round trip happens inside `deliver()`,
  so `finish()` has no queued work either way; it only latches the adapter
  shut. Closing the underlying transport stays with the runner, which still
  needs it for post-run OSV enrichment.
"""
from __future__ import annotations

import logging
import time
from typing import Sequence

from opkura.frames import (
    BotFinalSpeech,
    BotTurnEnded,
    CallerDTMF,
    CallerFrame,
    CallerHangup,
    CallerSpeech,
    ControlEnded,
    ControlStarted,
    Frame,
    ModuleEntered,
    ToolCalled,
)

logger = logging.getLogger(__name__)


def _tool_name(payload: dict) -> str:
    """Best-effort name from the runtime's tool-call dict (OpenAI shape or flat)."""
    if not isinstance(payload, dict):
        return ""
    fn = payload.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return str(payload.get("name") or "")


class TextFrameAdapter:
    """FrameTransport over one `TextTransport`."""

    nudge_on_silence = True  # D4 — see module docstring

    def __init__(self, transport):
        self._t = transport
        self._buffer: list[Frame] = []
        self._started = False
        self._done = False
        self._clock0 = time.monotonic()

    def _now_ms(self) -> float:
        return (time.monotonic() - self._clock0) * 1000.0

    async def start(self) -> None:
        self._clock0 = time.monotonic()

    async def receive(self) -> Frame:
        if not self._buffer:
            if self._started or self._done:
                # Nothing staged and nothing owed: the conversation is over
                # from this side (the engine only re-receives after a deliver
                # that stages frames).
                return ControlEnded(reason="text conversation over", ts_ms=self._now_ms())
            self._started = True
            t0 = time.monotonic()
            turn = await self._t.start()
            rtt_ms = (time.monotonic() - t0) * 1000.0
            interaction = getattr(self._t, "interaction_uuid", None) or ""
            logger.info(
                "text conversation started: interaction_uuid=%s module=%s",
                interaction, getattr(turn, "current_module", None),
            )
            self._buffer.append(
                ControlStarted(interaction_uuid=interaction, ts_ms=self._now_ms())
            )
            self._buffer.extend(self._frames_for(turn, rtt_ms))
        return self._buffer.pop(0)

    async def deliver(self, frames: Sequence[CallerFrame]) -> None:
        if self._done:
            return
        speech, dtmf, hangup = "", "", False
        for f in frames:
            if isinstance(f, CallerSpeech):
                speech = f.text
            elif isinstance(f, CallerDTMF):
                dtmf = f.digits
            elif isinstance(f, CallerHangup):
                hangup = True

        if speech or dtmf:
            t0 = time.monotonic()
            turn = await self._t.send(speech=speech, dtmf=dtmf)
            rtt_ms = (time.monotonic() - t0) * 1000.0
            if hangup:
                # D1: the goodbye reached the bot; its reply is post-hangup
                # noise — a real caller is no longer on the line to hear it.
                logger.debug(
                    "discarding bot reply after caller hangup: %r",
                    (getattr(turn, "text", "") or "")[:120],
                )
            else:
                self._buffer.extend(self._frames_for(turn, rtt_ms))
        if hangup:
            self._done = True

    async def finish(self, *, graceful: bool) -> None:
        # D5: no queued work exists on this transport (the round trip is
        # synchronous inside deliver), so graceful and cancelled ends differ
        # only in what the ENGINE already did or didn't deliver.
        self._done = True

    def _frames_for(self, turn, rtt_ms: float) -> list[Frame]:
        """One BotTurn reply → its frame sequence."""
        now = self._now_ms()
        frames: list[Frame] = []
        module = getattr(turn, "current_module", None)
        if module:
            frames.append(ModuleEntered(module=module, ts_ms=now))
        for payload in getattr(turn, "tool_calls", None) or []:
            frames.append(ToolCalled(name=_tool_name(payload), payload=payload, ts_ms=now))
        text = getattr(turn, "text", "") or ""
        if text.strip():
            frames.append(BotFinalSpeech(text=text, ts_ms=now))
        frames.append(BotTurnEnded(
            reasoning_ms=getattr(turn, "reasoning_ms", None),
            transport_ms=rtt_ms,
            message_type=getattr(turn, "message_type", None) or "",
            tag_only=not text.strip(),
            ts_ms=now,
        ))
        return frames
