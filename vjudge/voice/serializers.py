"""Media serializers: wire format ↔ frames (the P6.6 port's media half).

Today the Twilio Media Streams wire format is welded into the stream session
at dozens of call sites; here it is ONE object with two pure halves:

* `decode(raw)`  — one WS text message → one chain frame (or None for chatter
  like 'connected'/'mark').
* `encode_media`/`encode_mark`/`chunk` — frames → wire messages, with the
  20ms/160-byte μ-law chunk invariant enforced in exactly one place.

A future LiveKit-SIP serializer (P6.7) implements the same surface; nothing
downstream of the serializer knows which vendor carried the bytes.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Iterator, Optional

from vjudge.voice.frames import CallEnded, CallStarted, InboundAudio

MULAW_SILENCE = b"\xff"   # one byte of μ-law silence
CHUNK_DURATION_S = 0.02   # 20ms per media chunk
CHUNK_SIZE = 160          # 160 bytes = 20ms at 8kHz μ-law


class TwilioSerializer:
    """Twilio Media Streams JSON, both directions. Stateless apart from the
    stream SID learned from the 'start' event."""

    def __init__(self) -> None:
        self.stream_sid: str = ""
        self.call_sid: str = ""

    # ---- inbound: wire → frames -------------------------------------------

    def decode(self, raw: str):
        """One WS text message → one frame, or None for protocol chatter."""
        data = json.loads(raw)
        event = data.get("event")
        if event == "media":
            return InboundAudio(payload=base64.b64decode(data["media"]["payload"]))
        if event == "start":
            meta = data.get("start", {})
            self.stream_sid = meta.get("streamSid", "")
            self.call_sid = meta.get("callSid", "")
            return CallStarted(stream_sid=self.stream_sid, call_sid=self.call_sid)
        if event == "stop":
            return CallEnded(reason="twilio stream stopped")
        # 'connected', 'mark' acks, anything future: chatter.
        return None

    # ---- outbound: frames → wire --------------------------------------------

    def chunk(self, audio: bytes) -> Iterator[bytes]:
        """Split μ-law audio into exact CHUNK_SIZE pieces, silence-padding the
        tail — the one place the chunk invariant lives."""
        for offset in range(0, len(audio), CHUNK_SIZE):
            piece = audio[offset:offset + CHUNK_SIZE]
            if len(piece) < CHUNK_SIZE:
                piece += MULAW_SILENCE * (CHUNK_SIZE - len(piece))
            yield piece

    def encode_media(self, chunk: bytes) -> str:
        return json.dumps({
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": base64.b64encode(chunk).decode("ascii")},
        })

    def encode_mark(self, name: Optional[str] = None) -> str:
        return json.dumps({
            "event": "mark",
            "streamSid": self.stream_sid,
            "mark": {"name": name or f"done_{time.time():.0f}"},
        })
