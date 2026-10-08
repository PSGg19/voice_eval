"""P2.1 — the frame vocabulary: one wire format between transports and the
conversation engine.

A frame is a fact about the conversation, emitted by whichever side observed
it. Transports (text HTTP, voice media stream) translate their native events
INTO bot frames and caller frames OUT into native actions; the conversation
engine speaks only frames. This module is deliberately inert: frozen
dataclasses, no methods, no I/O, no imports beyond the stdlib — the vocabulary
must be importable from anywhere (engine, adapters, observers, tests) without
dragging a transport along.

Naming note: these are FRAMES, not observer events. `observers/events.py`
(RunStarted, BotTurn, …) describes a finished run to telemetry; frames are the
live protocol a run is MADE of. The two vocabularies overlap in subject but
not in role, so they stay separate types.

Timing note: `ts_ms` is wall-progress within the conversation (milliseconds
since the transport's clock zero), matching `TranscriptEntry.timestamp_ms`.
Adapters stamp it; the engine treats it as opaque.

Latency note (divergence D3, resolved in P2.5): `BotTurnEnded` carries TWO
named latencies instead of the old single ambiguous `bot_response_latency`.
`reasoning_ms` is the bot's own thinking time (the text runtime reports it;
voice cannot observe it). `transport_ms` is the observed round-trip at the
transport boundary — HTTP request→response for text; end-of-caller-audio →
first-bot-audio for voice. Either may be None when the transport cannot
measure it; they are never summed or compared to each other.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union


# ------------------------------------------------------------- bot frames

@dataclass(frozen=True)
class BotPartialSpeech:
    """A fragment of the bot's current utterance, turn still open.

    Voice emits one per STT final segment; text never emits it (an HTTP
    response is already a complete turn)."""

    text: str
    ts_ms: float = 0.0


@dataclass(frozen=True)
class BotFinalSpeech:
    """The bot's complete utterance for one turn."""

    text: str
    ts_ms: float = 0.0


@dataclass(frozen=True)
class ModuleEntered:
    """The bot's flow moved to a module. Text observes this per response;
    voice cannot observe it live (it is recovered post-call from OSV)."""

    module: str
    ts_ms: float = 0.0


@dataclass(frozen=True)
class ToolCalled:
    """The bot invoked a tool. `payload` is the raw tool-call dict exactly as
    the runtime reported it — the engine stores, never interprets it."""

    name: str
    payload: dict = field(default_factory=dict)
    ts_ms: float = 0.0


@dataclass(frozen=True)
class BotTurnEnded:
    """The bot's turn is over: everything it had to say has been framed.

    `tag_only` marks a turn whose utterance was empty because the runtime
    stripped a marker tag (<LANG:…>/<HANGUP>) — indistinguishable from real
    silence at the transport, but a distinct defect. `message_type` is the
    text runtime's classification (prompt/…); empty where unobservable."""

    reasoning_ms: Optional[float] = None
    transport_ms: Optional[float] = None
    message_type: str = ""
    tag_only: bool = False
    ts_ms: float = 0.0


# ---------------------------------------------------------- caller frames

@dataclass(frozen=True)
class CallerSpeech:
    """What the test caller says next. `barge=True` means it was spoken over
    the bot's unfinished turn (voice only)."""

    text: str
    barge: bool = False
    ts_ms: float = 0.0


@dataclass(frozen=True)
class CallerDTMF:
    """Digits the caller keys in. Alphabet enforced upstream
    (engine sanitises LLM output before framing)."""

    digits: str
    ts_ms: float = 0.0


@dataclass(frozen=True)
class CallerHangup:
    """The caller ends the call. `reason` is one of the engine's termination
    reasons (see engine/conversation.py) — carried on the frame so the
    transport can log WHY without reaching back into the engine."""

    reason: str = ""
    ts_ms: float = 0.0


# --------------------------------------------------------- control frames

@dataclass(frozen=True)
class ControlStarted:
    """The transport is live: the call answered / the first HTTP turn landed.
    `interaction_uuid` is the bot side's id for this conversation, where the
    transport learns it up front (text does; voice learns it post-call)."""

    interaction_uuid: str = ""
    ts_ms: float = 0.0


@dataclass(frozen=True)
class ControlEnded:
    """The transport ended normally — remote hangup, flow completion. A
    graceful end: everything already framed was real and is kept."""

    reason: str = ""
    ts_ms: float = 0.0


@dataclass(frozen=True)
class ControlCancelled:
    """The run was cancelled from outside (operator stop, deadline enforcement
    above the engine). Not graceful: pending work is dropped, not drained."""

    reason: str = ""
    ts_ms: float = 0.0


BotFrame = Union[BotPartialSpeech, BotFinalSpeech, ModuleEntered, ToolCalled, BotTurnEnded]
CallerFrame = Union[CallerSpeech, CallerDTMF, CallerHangup]
ControlFrame = Union[ControlStarted, ControlEnded, ControlCancelled]
Frame = Union[BotFrame, CallerFrame, ControlFrame]
