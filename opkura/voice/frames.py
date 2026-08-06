"""Voice-chain frames: what moves between processors.

Two vocabularies meet in the voice chain. The ENGINE vocabulary
(opkura/frames.py — BotPartialSpeech, CallerSpeech, …) crosses the chain at
the STT and TTS boundaries. THESE frames are chain plumbing: raw audio,
wire lifecycle, and speech-activity signals that never leave the voice
worker. Frozen dataclasses, no behaviour — same rules as the engine
vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InboundAudio:
    """One media chunk from the caller-facing wire (bot's voice). μ-law 8k."""

    payload: bytes
    ts_ms: float = 0.0


@dataclass(frozen=True)
class OutboundAudio:
    """One media chunk to put on the wire (our TTS / DTMF audio). μ-law 8k."""

    payload: bytes
    ts_ms: float = 0.0


@dataclass(frozen=True)
class CallStarted:
    """The wire is live (Twilio 'start' event / SIP answer)."""

    stream_sid: str = ""
    call_sid: str = ""
    ts_ms: float = 0.0


@dataclass(frozen=True)
class CallEnded:
    """The wire closed. Propagates down the chain so every processor drains
    and stops — the chain's EndOfStream."""

    reason: str = ""
    ts_ms: float = 0.0


@dataclass(frozen=True)
class SpeechStarted:
    """Audio-domain voice-activity onset (Deepgram VAD / Silero). Beats STT
    interim by 50–200ms; turn detection's grace period feeds on it."""

    ts_ms: float = 0.0


@dataclass(frozen=True)
class UtteranceBoundary:
    """STT-domain end-of-utterance signal (Deepgram UtteranceEnd). Not yet a
    turn end — turn detection applies the grace window on top."""

    ts_ms: float = 0.0
