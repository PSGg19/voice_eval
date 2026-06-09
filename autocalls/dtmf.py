"""DTMF tone generator — produces mulaw 8kHz audio for Twilio media streams."""

from __future__ import annotations

import math

# DTMF frequency pairs: digit → (row_freq, col_freq)
DTMF_FREQS: dict[str, tuple[int, int]] = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "A": (697, 1633),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477), "B": (770, 1633),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "C": (852, 1633),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477), "D": (941, 1633),
}

SAMPLE_RATE = 8000
TONE_DURATION_S = 0.25  # 250ms per digit
PAUSE_DURATION_S = 0.1  # 100ms silence between digits
LONG_PAUSE_S = 0.5  # 'w' pause (Twilio convention)

# mulaw silence byte
_SILENCE = 0xFF


def _pcm16_to_mulaw(sample: int) -> int:
    """Compress a signed 16-bit PCM sample to 8-bit mu-law (ITU-T G.711)."""
    BIAS = 132
    CLIP = 32635
    sign = 0

    if sample < 0:
        sign = 0x80
        sample = -sample

    sample = min(sample, CLIP)
    sample += BIAS

    # Find the segment (exponent)
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1

    # Extract mantissa
    mantissa = (sample >> (exponent + 3)) & 0x0F

    # Combine sign, exponent, mantissa and invert
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def generate_tone(digit: str, duration_s: float = TONE_DURATION_S) -> bytes:
    """Generate mulaw 8kHz audio for a single DTMF digit."""
    if digit.upper() not in DTMF_FREQS:
        raise ValueError(f"Invalid DTMF digit: {digit!r}")

    f1, f2 = DTMF_FREQS[digit.upper()]
    num_samples = int(SAMPLE_RATE * duration_s)
    two_pi = 2.0 * math.pi

    buf = bytearray(num_samples)
    for i in range(num_samples):
        t = i / SAMPLE_RATE
        # Two equal-amplitude sine waves, ~70% of full scale
        pcm = int(
            16000.0 * (math.sin(two_pi * f1 * t) + math.sin(two_pi * f2 * t))
        )
        buf[i] = _pcm16_to_mulaw(pcm)

    return bytes(buf)


def generate_silence(duration_s: float) -> bytes:
    """Generate mulaw silence for the given duration."""
    return bytes([_SILENCE] * int(SAMPLE_RATE * duration_s))


def generate_sequence(digits: str) -> bytes:
    """Generate mulaw audio for a string of DTMF digits.

    Supports:
        0-9, A-D, *, #  — standard DTMF tones
        w                — 0.5 second pause (Twilio convention)

    Example: "1w2" → tone(1), 0.5s pause, tone(2)
    """
    parts: list[bytes] = []

    for i, ch in enumerate(digits):
        if ch == "w":
            parts.append(generate_silence(LONG_PAUSE_S))
        elif ch.upper() in DTMF_FREQS:
            if parts:  # inter-digit pause
                parts.append(generate_silence(PAUSE_DURATION_S))
            parts.append(generate_tone(ch))
        elif ch == " ":
            continue  # ignore spaces
        else:
            raise ValueError(f"Unsupported character in DTMF sequence: {ch!r}")

    return b"".join(parts)
