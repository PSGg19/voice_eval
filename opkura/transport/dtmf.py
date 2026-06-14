"""DTMF tone generator — produces mulaw 8kHz audio for Twilio media streams.

Phase 3 (Tasks 2+) moved the production DTMF transport to Twilio REST
``<Play digits=>``, which produces proper RFC 2833 events on the
outbound SIP leg (the only thing LiveKit-based bots decode as DTMF
input). The mulaw generator below is retained as a debug fallback
behind ``Settings.dtmf_transport == "audio"``.

``validate_dtmf_string`` is the shared digit-alphabet guard used by
both transports — same allowed character set as Twilio's
``<Play digits=>``: 0-9, A-D, `*`, `#`, and `w` (Twilio's 0.5s
pause convention).
"""

from __future__ import annotations

import math
import re

# DTMF frequency pairs: digit → (row_freq, col_freq)
DTMF_FREQS: dict[str, tuple[int, int]] = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "A": (697, 1633),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477), "B": (770, 1633),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "C": (852, 1633),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477), "D": (941, 1633),
}

SAMPLE_RATE = 8000
# Per-digit timing: aligned to standard DTMF spec (Twilio's <Play digits=>
# uses ~100ms tones, every PCI-validated carrier-grade STT reliably
# detects ≥80ms). Previously 250ms/100ms which added ~5.5s of playback
# for a 16-digit card and raced the bot's no-input timer; 100ms/50ms
# cuts a 16-digit card to ~2.35s of playback.
TONE_DURATION_S = 0.1   # 100ms per digit
PAUSE_DURATION_S = 0.05 # 50ms silence between digits
LONG_PAUSE_S = 0.5  # 'w' pause (Twilio convention)

# mulaw silence byte
_SILENCE = 0xFF

# Twilio's `<Play digits>` accepts 0-9, A-D, *, # and the convention
# 'w' for a 0.5s pause. Anything outside this alphabet is either bad
# TwiML or a potential injection vector — reject at the boundary.
_DTMF_ALPHABET_RE = re.compile(r"^[0-9A-Dw*#]+$")


def validate_dtmf_string(digits: str) -> None:
    """Raise ValueError if `digits` is not a valid Twilio DTMF string.

    Allowed alphabet:
      * 0-9   — standard DTMF digits
      * A-D   — extended DTMF (rarely used but valid)
      * `*` and `#` — symbols
      * `w`   — Twilio convention for a 0.5s pause

    Anything else (whitespace, hyphens, quotes, alpha noise) raises.
    Empty strings raise.
    """
    if not isinstance(digits, str):
        raise TypeError(f"DTMF string must be str, got {type(digits).__name__}")
    if not digits:
        raise ValueError("DTMF string is empty")
    if not _DTMF_ALPHABET_RE.match(digits):
        raise ValueError(
            f"DTMF string contains invalid chars: {digits!r}. "
            f"Allowed alphabet: [0-9A-Dw*#]"
        )


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
        # Two equal-amplitude sine waves, each at ~25% of full scale so the
        # summed peak is ~50% of int16 range (~16384 of 32767). Previously
        # 16000.0 per sine, which summed to ~32000 — 97% of range and right
        # at the μ-law clip ceiling. The resulting tone harmonics confused
        # the bot's DTMF decoder ("Sorry I wasn't able to get that" even
        # when audio reached Twilio cleanly).
        pcm = int(
            8000.0 * (math.sin(two_pi * f1 * t) + math.sin(two_pi * f2 * t))
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
