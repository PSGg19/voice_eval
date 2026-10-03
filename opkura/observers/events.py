"""The event vocabulary observers consume (P5.1).

These are the LIFECYCLE events a run emits today. When Phase 2 lands its
frame vocabulary, frames map onto these (a bot-final-speech frame becomes a
BotTurn, a control-started frame a RunStarted) — observers won't change.

Every event carries its own wall-clock nanosecond timestamps. That is the
P5.2 rule made structural: an observer records what happened WHEN it
happened; the time it took the observer to hear about it appears nowhere.

Frozen dataclasses, no behaviour, JSON-safe fields only.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


def now_ns() -> int:
    return time.time_ns()


@dataclass(frozen=True)
class RunStarted:
    run_id: str
    scenario_name: str
    target: str
    region: str
    transport: str
    at_ns: int = field(default_factory=now_ns)


@dataclass(frozen=True)
class BotTurn:
    turn_index: int
    text: str
    module: str = ""
    # The bot's own reasoning latency (text runtime's number). The span is
    # reconstructed as [at_ns - latency, at_ns].
    latency_ms: Optional[float] = None
    tag_only: bool = False
    at_ns: int = field(default_factory=now_ns)


@dataclass(frozen=True)
class CallerTurn:
    turn_index: int
    text: str
    dtmf: str = ""
    at_ns: int = field(default_factory=now_ns)


@dataclass(frozen=True)
class Generation:
    """One LLM call, measured by whoever made it.

    prompt_text / completion_text stay EMPTY unless TRACE_CAPTURE_LLM_IO is
    set — prompts embed resolved variables, and traces must not become a
    PII side channel by default. A local debugging stack opts in."""

    role: str  # "caller" | "evaluator"
    model: str
    provider: str = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    error: Optional[str] = None
    started_ns: int = 0
    ended_ns: int = 0
    prompt_text: str = ""
    completion_text: str = ""


@dataclass(frozen=True)
class RunFinished:
    status: str
    passed: Optional[bool] = None
    error: str = ""
    interaction_uuid: str = ""
    at_ns: int = field(default_factory=now_ns)


Event = object  # any of the above; observers switch on type
