"""P6.1 — the audio-out processor: paced μ-law onto the wire.

Consumes `OutboundAudio` frames (any size), splits them through the
serializer's chunk invariant, and sends each 20ms piece against a wall-clock
deadline anchor — the same anti-drift pacing the monolith used: per-chunk
`sleep(20ms)` drifts 5–10ms per iteration on most platforms, which over a
5-second utterance is >1s of cumulative gap, audible as crackle. Anchoring
every chunk to `burst_start + idx*20ms` lets a fast chunk's spare time absorb
a slow chunk's delay instead of compounding.

A burst's anchor resets when the stream has been idle longer than one chunk —
a new utterance must not inherit an exhausted deadline and blast out
unpaced.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from vjudge.voice.frames import CallEnded, OutboundAudio
from vjudge.voice.processor import Processor
from vjudge.voice.serializers import CHUNK_DURATION_S, TwilioSerializer

logger = logging.getLogger(__name__)

# Idle gap after which the pacing anchor resets (one chunk of slack).
_ANCHOR_RESET_S = CHUNK_DURATION_S * 5


class AudioOut(Processor):
    """Terminal link: OutboundAudio → serializer → `send_text(str)`.

    `send_text` is the only wire dependency — a WS send in production, a list
    append in tests. Everything else it emits (non-audio frames) is forwarded
    so taps behind it still see the chain."""

    def __init__(
        self,
        send_text: Callable[[str], Awaitable[None]],
        serializer: TwilioSerializer,
        name: str = "audio-out",
        **kw,
    ):
        super().__init__(name, **kw)
        self._send_text = send_text
        self._serializer = serializer
        self._anchor: Optional[float] = None
        self._idx = 0
        self.bytes_sent = 0

    async def handle(self, frame) -> None:
        if not isinstance(frame, OutboundAudio):
            await self.emit(frame)
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        if (
            self._anchor is None
            or now > self._anchor + self._idx * CHUNK_DURATION_S + _ANCHOR_RESET_S
        ):
            self._anchor = now
            self._idx = 0
        for chunk in self._serializer.chunk(frame.payload):
            await self._send_text(self._serializer.encode_media(chunk))
            self.bytes_sent += len(chunk)
            self._idx += 1
            deadline = self._anchor + self._idx * CHUNK_DURATION_S
            remaining = deadline - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def on_call_ended(self, frame: CallEnded) -> None:
        # Best-effort end-of-speech mark; the wire may already be gone.
        try:
            await self._send_text(self._serializer.encode_mark())
        except Exception:
            pass
