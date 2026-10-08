"""Local audio-domain VAD using Silero. Replaces Deepgram's
text/silence-based UtteranceEnd for turn-end detection.

Silero runs entirely in-process: no network roundtrip, ~5ms per 32ms
frame on a single CPU thread. The model file (~1.8MB) is downloaded the
first time `load_silero_vad()` is called and cached under torch hub.

Why not Deepgram's UtteranceEnd alone:
  - Text-domain silence detection — fires after `utterance_end_ms` of
    silence in the transcript. Bot mid-sentence pauses (2-3s after a
    period) cross that threshold → false turn-end → caller barges.
  - Adding a grace period mitigates but adds latency on real turn ends.

What Silero gives us:
  - Audio-domain detection — classifies each 32ms frame as
    speech-or-not based on energy + spectral features.
  - We define "turn end" as ≥600ms of confirmed silence after the bot
    last made voice sound. Bot's mid-sentence breathing/pauses keep
    refreshing `_last_voice_t` so they don't trigger turn-end.

This module ONLY replaces the turn-end signal. Deepgram STT keeps
running for transcript text — Silero doesn't transcribe.
"""
from __future__ import annotations

import audioop  # mulaw → PCM int16 (stdlib, pure C). Deprecated in 3.13;
                # we're on 3.12. If/when we upgrade, swap for a small
                # custom decoder or `audioop-lts` shim.
import logging
import time

import numpy as np
import torch
from silero_vad import load_silero_vad

logger = logging.getLogger(__name__)

# Silero 8kHz model requires exactly 256-sample input frames (32ms at 8kHz).
SAMPLE_RATE = 8000
FRAME_SAMPLES = 256
FRAME_BYTES_PCM16 = FRAME_SAMPLES * 2  # 512 bytes per Silero frame
# Twilio sends 160-byte mulaw chunks (= 160 PCM int16 samples = 320 bytes
# PCM16) every 20ms. We buffer until ≥512 bytes accumulate.


class SileroBotVAD:
    """Audio-domain VAD for the inbound bot stream.

    Why this uses the model directly instead of `VADIterator`:

    VADIterator emits two events — `{start: t}` and `{end: t}` — based
    on its internal state. After an `end` event fires (100ms of silence),
    the iterator stays in "silent" state until a frame's probability
    crosses the threshold (default 0.5) and triggers `start`.

    Problem: telephony audio at sentence boundaries dips into the 0.2-0.5
    probability range for a few frames before re-asserting > 0.5. During
    those uncertain frames, VADIterator's state is "silent" and we
    weren't refreshing `_last_voice_t` — so vjudge thought silence had
    grown to 2s+ while the bot was actually speaking softly.

    Fix: skip VADIterator. Call the model directly per frame, get
    probability, use two thresholds:
      - voice_high (0.5): >= this flips _currently_speaking=True
      - voice_low (0.15): >= this refreshes _last_voice_t (even if
        below voice_high — catches the uncertain frames)
      - < voice_low: clear silence; don't refresh _last_voice_t

    This way, the borderline frames common in telephony count as "bot
    still active" instead of growing the silence-duration counter.

    Usage:
        vad = SileroBotVAD()
        # In MediaStreamSession._receive_loop, on each Twilio mulaw chunk:
        vad.feed_mulaw(audio_bytes)
        # In _wait_for_bot_turn:
        if vad.time_since_voice_s() > 2.0:
            # bot truly silent for 2s+ → declare turn end
            ...
    """

    def __init__(
        self,
        threshold: float = 0.5,
        voice_low_threshold: float = 0.15,
    ):
        self._model = load_silero_vad()
        # Two thresholds applied per frame (see class docstring for why).
        self._voice_high = threshold
        self._voice_low = voice_low_threshold
        self._pcm_buffer = bytearray()
        # Init to "now" so a fresh VAD doesn't immediately report
        # "silent for 5 minutes" before any audio has arrived.
        self._last_voice_t: float = time.monotonic()
        self._currently_speaking: bool = False
        # Sticky flag: True once Silero has seen ANY voice frame since
        # the last reset(). Used by _wait_for_bot_turn to know "has bot
        # actually started speaking yet?" without relying on Deepgram
        # callbacks. Cleared by reset().
        self._has_seen_voice: bool = False
        # Diagnostic counters since last reset(). Lets stream.py log
        # "what did Silero see this turn?" for debugging.
        self._frames_high: int = 0
        self._frames_low: int = 0
        self._frames_silent: int = 0

    def feed_mulaw(self, mulaw_chunk: bytes) -> None:
        """Append a Twilio mulaw chunk; classify any complete frames inside.

        Safe to call repeatedly with arbitrary-size mulaw byte strings —
        the internal buffer handles non-frame-aligned chunks (Twilio's
        160-byte chunks don't align to Silero's 256-sample frames).
        """
        pcm = audioop.ulaw2lin(mulaw_chunk, 2)  # 2 = output 16-bit PCM
        self._pcm_buffer.extend(pcm)
        while len(self._pcm_buffer) >= FRAME_BYTES_PCM16:
            frame_pcm = bytes(self._pcm_buffer[:FRAME_BYTES_PCM16])
            del self._pcm_buffer[:FRAME_BYTES_PCM16]
            samples = (
                np.frombuffer(frame_pcm, dtype=np.int16).astype(np.float32)
                / 32768.0
            )
            tensor = torch.from_numpy(samples)
            # Direct model call returns scalar probability per frame.
            # Silero maintains its own state across calls — no need to
            # pass state explicitly.
            prob = self._model(tensor, SAMPLE_RATE).item()
            now = time.monotonic()
            if prob >= self._voice_high:
                # Confident voice — flip speaking + refresh.
                self._currently_speaking = True
                self._last_voice_t = now
                self._has_seen_voice = True
                self._frames_high += 1
            elif prob >= self._voice_low:
                # Uncertain frame — bot is likely still speaking softly
                # (telephony band-limited; transitions dip below 0.5).
                # Refresh _last_voice_t so silence-duration doesn't grow,
                # but don't flip _currently_speaking — that should
                # require a confident voice frame.
                self._last_voice_t = now
                self._has_seen_voice = True
                self._frames_low += 1
            else:
                # Clear silence — don't refresh. Silence duration grows.
                self._currently_speaking = False
                self._frames_silent += 1

    def time_since_voice_s(self) -> float:
        """Seconds since the last frame classified as containing voice.

        Returns ~0 while bot is actively speaking, grows during silence.
        Callers use this to gate the turn-end decision: e.g. ">=2.0" means
        "bot has been silent for at least 2 seconds — caller may respond."
        """
        return time.monotonic() - self._last_voice_t

    def is_speaking(self) -> bool:
        """True iff Silero currently believes voice is active.

        Cheap snapshot of internal state. Distinct from
        time_since_voice_s, which encodes how long silence has lasted.
        """
        return self._currently_speaking

    def has_seen_voice(self) -> bool:
        """True iff ANY voice frame has been classified since the last
        reset(). Sticky — doesn't go False when bot pauses. Used by
        stream._wait_for_bot_turn to gate the silence-detection phase
        on "bot has actually started" without depending on Deepgram.
        """
        return self._has_seen_voice

    def frame_stats(self) -> dict:
        """Diagnostic snapshot of frames classified since last reset.

        Useful for `stream._wait_for_bot_turn` to log "what did Silero
        see this turn?" when declaring turn end.
        """
        total = self._frames_high + self._frames_low + self._frames_silent
        return {
            "high": self._frames_high,
            "low": self._frames_low,
            "silent": self._frames_silent,
            "total": total,
        }

    def reset(self) -> None:
        """Reset state between bot turns.

        Clears Silero's internal model state, the PCM buffer, the
        speaking/last-voice trackers, the has-seen-voice flag, and
        the diagnostic counters. Call this when starting a new bot
        turn so stale verdicts from the previous turn don't leak in.
        """
        self._model.reset_states()
        self._pcm_buffer.clear()
        self._currently_speaking = False
        self._has_seen_voice = False
        self._last_voice_t = time.monotonic()
        self._frames_high = 0
        self._frames_low = 0
        self._frames_silent = 0
