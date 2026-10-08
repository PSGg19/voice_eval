"""Unit tests for SileroBotVAD — local audio-domain VAD."""
from __future__ import annotations

import audioop
import time

import numpy as np
import pytest

from vjudge.transport.vad import (
    FRAME_BYTES_PCM16,
    SAMPLE_RATE,
    SileroBotVAD,
)


# These tests load the Silero model (~1.8MB download on first run, cached
# under torch hub afterward). Skip them if silero-vad or torch isn't
# installed — keeps the unit suite green for devs who haven't run SETUP.md.
silero_vad = pytest.importorskip("silero_vad")
torch = pytest.importorskip("torch")


def _gen_mulaw_silence(duration_s: float) -> bytes:
    """Generate `duration_s` of digital silence as mulaw bytes."""
    n_samples = int(SAMPLE_RATE * duration_s)
    pcm = np.zeros(n_samples, dtype=np.int16).tobytes()
    return audioop.lin2ulaw(pcm, 2)


def _gen_mulaw_tone(duration_s: float, freq: int = 440) -> bytes:
    """Generate `duration_s` of a sine tone as mulaw bytes."""
    n_samples = int(SAMPLE_RATE * duration_s)
    t = np.arange(n_samples) / SAMPLE_RATE
    pcm = (np.sin(2 * np.pi * freq * t) * 16000).astype(np.int16).tobytes()
    return audioop.lin2ulaw(pcm, 2)


def test_constructs_and_initial_state():
    vad = SileroBotVAD()
    # Fresh VAD should report "not speaking" and time_since_voice ~0.
    assert vad.is_speaking() is False
    # last_voice_t is initialized to time.monotonic() in __init__,
    # so a freshly-constructed VAD reports near-zero silence duration.
    assert vad.time_since_voice_s() < 0.1


def test_pure_silence_keeps_not_speaking():
    """Feeding 1s of digital silence must not flip speaking state."""
    vad = SileroBotVAD()
    # 1 second of silence = 50 Twilio chunks of 160 bytes each.
    silence = _gen_mulaw_silence(1.0)
    vad.feed_mulaw(silence)
    assert vad.is_speaking() is False
    # Initial _last_voice_t is set at construction, so after 1s of
    # feeding (which itself takes near-zero time), time_since_voice
    # is still small. The key invariant: feeding silence didn't
    # refresh _last_voice_t.
    # We can't easily test "> X seconds" because feed_mulaw is
    # near-instant; just verify state didn't flip to speaking.


def test_chunk_buffering_handles_non_aligned_sizes():
    """Twilio chunks are 160 bytes mulaw = 320 bytes PCM. Silero needs
    512-byte (256-sample) frames. Verify buffering math doesn't drop
    samples — 10 chunks (10 * 160 = 1600 mulaw → 3200 PCM bytes) should
    consume 6 complete Silero frames (3200 / 512 = 6.25) and leave a
    128-byte remainder in the buffer.
    """
    vad = SileroBotVAD()
    one_chunk = b"\xff" * 160  # mulaw "silence" byte, 160 = one Twilio chunk
    for _ in range(10):
        vad.feed_mulaw(one_chunk)
    # 10 * 160 = 1600 mulaw bytes → 1600 PCM samples → 3200 PCM bytes
    # 3200 / FRAME_BYTES_PCM16 (512) = 6 frames + 128 byte remainder.
    assert len(vad._pcm_buffer) == 3200 % FRAME_BYTES_PCM16
    assert len(vad._pcm_buffer) == 128


def test_buffering_processes_exactly_one_frame_at_a_time():
    """Two consecutive feeds should yield the same buffer state as one
    combined feed (verifies internal loop processes frames greedily)."""
    vad_a = SileroBotVAD()
    vad_b = SileroBotVAD()
    chunk = b"\xff" * 160
    # Feed 5 chunks split vs combined
    for _ in range(5):
        vad_a.feed_mulaw(chunk)
    vad_b.feed_mulaw(chunk * 5)
    # Same remainder buffer length (state independent of feed boundaries)
    assert len(vad_a._pcm_buffer) == len(vad_b._pcm_buffer)


def test_reset_clears_state():
    """After reset, all internal state must be cleared/reset."""
    vad = SileroBotVAD()
    # Feed enough mulaw to leave a buffer remainder
    vad.feed_mulaw(b"\xff" * 200)
    assert len(vad._pcm_buffer) > 0
    # Manually mark as speaking and force last_voice_t into the past
    vad._currently_speaking = True
    vad._last_voice_t = time.monotonic() - 5.0

    vad.reset()

    assert vad.is_speaking() is False
    assert vad._pcm_buffer == bytearray()
    # last_voice_t reset to ~now
    assert vad.time_since_voice_s() < 0.1


def test_time_since_voice_grows_with_real_time():
    """Without any speech, time_since_voice_s should grow over wall-clock."""
    vad = SileroBotVAD()
    initial = vad.time_since_voice_s()
    time.sleep(0.2)
    later = vad.time_since_voice_s()
    assert later > initial + 0.15  # at least ~150ms grew (allow scheduling jitter)


def test_speaking_state_flip_via_internal_state():
    """Direct state manipulation should reflect in is_speaking().

    NOTE: We don't test the actual Silero model's classification of real
    speech here because synthetic sine tones and white noise produce
    unstable verdicts (Silero is trained on human speech, not synthetic).
    Real-call verification happens via the CHECKLIST.md live test.
    """
    vad = SileroBotVAD()
    assert vad.is_speaking() is False
    vad._currently_speaking = True
    assert vad.is_speaking() is True
    vad.reset()
    assert vad.is_speaking() is False
