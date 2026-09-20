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
