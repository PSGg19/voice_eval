"""μ-law audio helpers (extracted from the stream monolith, P6)."""
from __future__ import annotations

import struct
import wave


def mulaw_decode(byte: int) -> int:
    """Decode a single mu-law byte to signed 16-bit PCM."""
    byte = ~byte & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = ((mantissa << 3) + 0x84) << exponent
    sample -= 0x84
    return -sample if sign else sample


def mulaw_to_wav(mulaw_data: bytes, path: str):
    """Convert raw mulaw 8kHz audio to a PCM 16-bit WAV file."""
    pcm_data = bytearray()
    for b in mulaw_data:
        sample = mulaw_decode(b)
        pcm_data.extend(struct.pack("<h", sample))

    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(8000)
        wf.writeframes(bytes(pcm_data))


# μ-law byte for silence (encode(0) == 0xFF).
MULAW_SILENCE = 0xFF


def mulaw_stereo_wav(left: bytes, right: bytes, path: str):
    """Two 8kHz μ-law tracks → one stereo PCM-16 WAV (left/right channels).

    The tracks must already share a timeline (the session pads its outbound
    buffer with silence against the wall clock); the shorter one is padded
    with μ-law silence so the file plays both sides in conversation order —
    the llm-studio-style dual-channel recording."""
    n = max(len(left), len(right))
    left = left + bytes([MULAW_SILENCE]) * (n - len(left))
    right = right + bytes([MULAW_SILENCE]) * (n - len(right))
    frames = bytearray()
    for lb, rb in zip(left, right):
        frames.extend(struct.pack("<hh", mulaw_decode(lb), mulaw_decode(rb)))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(bytes(frames))
