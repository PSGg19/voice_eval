"""P6.1 — TwilioSerializer (pure, both halves) and the AudioOut processor's
isolated frame-in/frame-out test with pacing asserted."""
from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from judge_it.voice.audio_out import AudioOut
from judge_it.voice.frames import CallEnded, CallStarted, InboundAudio, OutboundAudio
from judge_it.voice.serializers import CHUNK_SIZE, MULAW_SILENCE, TwilioSerializer

from tests.unit.voice.test_processor import run_processor


# --------------------------------------------------------------- serializer

def test_decode_media_start_stop_and_chatter():
    s = TwilioSerializer()
    start = s.decode(json.dumps({
        "event": "start",
        "start": {"streamSid": "MZ123", "callSid": "CA456"},
    }))
    assert start == CallStarted(stream_sid="MZ123", call_sid="CA456")
    assert s.stream_sid == "MZ123" and s.call_sid == "CA456"

    media = s.decode(json.dumps({
        "event": "media",
        "media": {"payload": base64.b64encode(b"\x01\x02").decode()},
    }))
    assert media == InboundAudio(payload=b"\x01\x02")

    stop = s.decode(json.dumps({"event": "stop"}))
    assert isinstance(stop, CallEnded)

    assert s.decode(json.dumps({"event": "connected"})) is None
    assert s.decode(json.dumps({"event": "mark", "mark": {"name": "x"}})) is None


def test_chunk_invariant_exact_size_with_silence_padding():
    s = TwilioSerializer()
    chunks = list(s.chunk(b"\x01" * (CHUNK_SIZE * 2 + 10)))
    assert [len(c) for c in chunks] == [CHUNK_SIZE] * 3
    assert chunks[2][10:] == MULAW_SILENCE * (CHUNK_SIZE - 10)
    assert list(s.chunk(b"")) == []


def test_encode_media_round_trips_through_decode():
    s = TwilioSerializer()
    s.stream_sid = "MZ999"
    wire = s.encode_media(b"\x0a" * CHUNK_SIZE)
    parsed = json.loads(wire)
    assert parsed["streamSid"] == "MZ999"
    back = TwilioSerializer().decode(wire)
    assert back == InboundAudio(payload=b"\x0a" * CHUNK_SIZE)


# ----------------------------------------------------------------- audio out

@pytest.mark.asyncio
async def test_audio_out_paces_chunks_and_pads_the_tail():
    sent: list[str] = []

    async def send_text(msg: str) -> None:
        sent.append(msg)

    s = TwilioSerializer()
    s.stream_sid = "MZ1"
    out = AudioOut(send_text, s)

    audio = b"\x05" * (CHUNK_SIZE * 5 + 20)   # 5 full chunks + a tail
    t0 = time.monotonic()
    emitted = await run_processor(out, [OutboundAudio(payload=audio)])
    elapsed = time.monotonic() - t0

    media = [m for m in sent if json.loads(m)["event"] == "media"]
    assert len(media) == 6
    for m in media:
        assert len(base64.b64decode(json.loads(m)["media"]["payload"])) == CHUNK_SIZE
    # 6 chunks at 20ms deadlines ≈ 120ms; unpaced this is microseconds.
    assert elapsed >= 0.08, f"chunks were not paced ({elapsed*1000:.0f}ms)"
    # the sentinel produced an end-of-speech mark
    assert any(json.loads(m)["event"] == "mark" for m in sent)
    assert out.bytes_sent == 6 * CHUNK_SIZE
    # non-audio frames were NOT swallowed
    assert any(isinstance(f, CallEnded) for f in emitted)


@pytest.mark.asyncio
async def test_non_audio_frames_pass_through():
    async def send_text(msg: str) -> None:  # pragma: no cover — not reached
        raise AssertionError("no audio was sent")

    out = AudioOut(send_text, TwilioSerializer())

    async def quiet_send(msg):   # the CallEnded mark still fires; allow it
        pass

    out._send_text = quiet_send
    emitted = await run_processor(out, [CallStarted(stream_sid="MZ")])
    assert emitted[0] == CallStarted(stream_sid="MZ")
