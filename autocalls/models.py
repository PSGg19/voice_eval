from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


class CallStatus(str, Enum):
    PENDING = "pending"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    ERROR = "error"


# --- Flow definition (loaded from YAML) ---


class ExpectConfig(BaseModel):
    contains_any: list[str] = []
    contains_all: list[str] = []
    not_contains: list[str] = []


class FlowStep(BaseModel):
    expect: Optional[ExpectConfig] = None
    say: Optional[str] = None
    dtmf: Optional[str] = None  # e.g. "1", "123", "1w2" (w = 0.5s pause)
    timeout_ms: int = 10000


class SuccessCriteria(BaseModel):
    all_steps_passed: bool = True
    max_latency_ms: int = 5000


class Flow(BaseModel):
    name: str
    target: str  # phone number to call
    schedule: Optional[str] = None  # cron expression
    steps: list[FlowStep]
    success_criteria: SuccessCriteria = SuccessCriteria()


# --- Results ---


class StepResult(BaseModel):
    step_index: int
    status: StepStatus
    bot_transcript: str = ""
    our_utterance: str = ""
    our_dtmf: str = ""
    latency_ms: float = 0
    expectations_met: dict = {}


class TranscriptEntry(BaseModel):
    speaker: str  # "bot" or "caller"
    text: str
    timestamp_ms: float = 0


class CallResult(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    flow_name: str
    target: str
    call_sid: str = ""
    status: CallStatus = CallStatus.PENDING
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    steps: list[StepResult] = []
    transcript: list[TranscriptEntry] = []
    error: Optional[str] = None
    recording_paths: dict[str, str] = {}  # {"bot": "path.wav", "caller": "path.wav"}

    @property
    def passed(self) -> bool:
        return all(s.status == StepStatus.PASSED for s in self.steps)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds() * 1000
        return None

    def finish(self, status: CallStatus, error: str | None = None):
        self.status = status
        self.ended_at = datetime.now(timezone.utc)
        if error:
            self.error = error


# --- Load testing ---


class LoadTestConfig(BaseModel):
    calls: int = 10  # total number of calls to make
    cps: float = 1.0  # calls per second launch rate
    max_concurrent: int = 0  # 0 = no limit


class LoadTestResult(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    flow_name: str
    target: str
    config: LoadTestConfig

    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    # Counts
    total_calls: int = 0
    passed_calls: int = 0
    failed_calls: int = 0
    error_calls: int = 0

    # Duration stats (ms)
    avg_duration_ms: float = 0
    min_duration_ms: float = 0
    max_duration_ms: float = 0
    p50_duration_ms: float = 0
    p95_duration_ms: float = 0

    # Individual call results
    call_results: list[CallResult] = []

    @property
    def pass_rate(self) -> float:
        return (self.passed_calls / self.total_calls * 100) if self.total_calls else 0

    @property
    def total_duration_ms(self) -> Optional[float]:
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds() * 1000
        return None
