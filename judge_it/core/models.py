from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

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
    dtmf: Optional[str] = None
    barge: bool = False
    bot_partial_at_barge: Optional[str] = None
    bot_response_after_ms: Optional[float] = None


class CallResult(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    flow_name: str
    target: str
    call_sid: str = ""
    # The judge_it session id for this call — the same value injected as the
    # `X-JudgeIt-Session-Id` SIP header and stored on the bot's interaction
    # (see transport/sip.py). Surfaced here because it is the ONLY exact join
    # between an judge_it result and the OSV interaction it created; the runner
    # previously kept it as a local and threw it away. Empty for calls placed
    # before this existed, or over PSTN where the header cannot travel.
    session_id: str = ""
    # The bot-side interaction this run produced. On the text transport OSV
    # returns it on the first turn, so it is known immediately and exactly —
    # no SIP header, no post-call reconciliation. Empty on pstn/sip, where job
    # mode still correlates via session_id afterwards.
    interaction_uuid: str = ""
    # Text-transport extras. The text runtime reports which module the bot is in
    # and which tools it called, per turn — data the audio path cannot observe
    # at all. This is the substrate for deterministic assertions (must_visit,
    # tool_order) that don't depend on the LLM evaluator's judgement.
    modules_visited: list[str] = []
    tool_calls: list[dict] = []
    # Bot turns whose stored text was ONLY marker tags (`<LANG:en-US>`,
    # `<HANGUP>`, …). OSV strips those before filling `output_message`, so over
    # HTTP they are indistinguishable from the bot saying nothing — which is
    # exactly how the Veros IB 2.0 stall presented, and why diagnosing it needed
    # a database query. Populated after the run from the conversations API, so
    # it is empty unless `JUDGE_IT_OSV_ACCESS_TOKEN` is set. Each entry:
    # {"turn_index": int, "tags": [str], "raw": str}.
    tag_only_bot_turns: list[dict] = []
    # Whether the post-run OSV conversations fetch actually ran. Tool calls
    # exist ONLY there, so without this an empty `tool_calls` is ambiguous
    # between "called nothing" and "never looked" — and a tool assertion
    # would fail every run that simply had no OSV token configured.
    osv_enriched: bool = False
    # Outcomes of the scenario's `assert:` block. Plain dicts so the Pydantic
    # model and the JSON log writer need no custom encoder:
    # {"name": str, "status": "passed|failed|inconclusive", "detail": str}
    assertion_outcomes: list[dict] = []
    status: CallStatus = CallStatus.PENDING
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    steps: list[StepResult] = []
    transcript: list[TranscriptEntry] = []
    error: Optional[str] = None
    recording_paths: dict[str, str] = {}  # {"bot": "path.wav", "caller": "path.wav"}
    # Filesystem path of the per-call log folder (Task 4). Populated by the
    # V2 runner the moment CallLogWriter is constructed, BEFORE dialing — so
    # setup-error calls (missing API key, provider mismatch) can still surface
    # the folder in CLI output. recording_paths is populated only post-call.
    log_dir: Optional[str] = None
    # `evaluation` is a stdlib @dataclass (not Pydantic). Pydantic accepts it via
    # arbitrary_types_allowed but won't deep-validate or auto-serialize. Task 4
    # (per-call JSON logs) must call dataclasses.asdict(self.evaluation) when
    # serializing, or convert EvaluationResult/EvaluationCriterion to BaseModel.
    evaluation: Optional["EvaluationResult"] = None

    @property
    def passed(self) -> bool:
        # V2 (evaluation present): trust the evaluator's verdict.
        # V1 (no evaluation): require at least one step AND all steps passed.
        # v1 bugfix: previously `all([]) is True` reported pass for zero-step calls
        # (errored before any step ran). Now empty-steps → False.
        # A failed structural assertion vetoes the judge. Whether the bot
        # called a tool or entered a module is a fact about the run; the LLM can
        # only infer it from the transcript, and a convincing-sounding call that
        # skipped an auth gate must not pass. Inconclusive outcomes do NOT veto
        # (see engine/assertions.py) — unobserved is not the same as violated.
        if any(o.get("status") == "failed" for o in self.assertion_outcomes):
            return False
        if self.evaluation is not None:
            return self.evaluation.overall_passed
        return bool(self.steps) and all(
            s.status == StepStatus.PASSED for s in self.steps
        )

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


# --- V2 AI-driven scenario model ---


@dataclass
class LLMConfig:
    caller_model: Optional[str] = None
    evaluator_model: Optional[str] = None
    temperature: float = 0.3
    provider: Optional[str] = None


@dataclass
class STTConfig:
    # Deepgram's silence threshold before firing UtteranceEnd. Bots with long
    # multi-sentence turns and natural mid-sentence pauses (Resurgent's
    # Michelle) need a higher value than the 1500 SDK default — otherwise the
    # period after "...for this purpose." gets misread as turn end, the
    # caller responds, and the rest of the bot's utterance (account
    # presentation, menu options) is lost. 3500ms covers Resurgent Michelle's
    # observed ~3s sentence-boundary pauses. Per-scenario override available
    # via `stt.utterance_end_ms` in the YAML.
    utterance_end_ms: int = 3500
    # Silero VAD silence threshold (seconds). When use_silero_vad=true,
    # judge_it declares turn end after this many seconds of confirmed
    # audio-domain silence. Default 1.5s covers most bots' mid-sentence
    # pauses; bump to 2.0-2.5s for verbose bots like Resurgent's
    # Michelle, or drop to 0.8-1.0s for snappy bots with short turns.
    silence_threshold_s: float = 1.5


@dataclass
class TTSConfig:
    # Deepgram Aura voice for the CALLER's synthesized speech. None -> the
    # runner's default English voice (aura-2-thalia-en). For SPANISH scenarios
    # set a Spanish voice (e.g. "aura-2-javier-es") so the audio the bot hears
    # is intelligible — an English voice mispronouncing Spanish gets garbled by
    # the bot's STT ("mañana" -> "banana"), which breaks the whole call.
    model: Optional[str] = None


@dataclass
class ScheduledTrigger:
    at_turn: int
    say: str
    after_bot_words: int = 0


@dataclass
class BargeInConfig:
    enabled: bool = False
    mode: Literal["scheduled", "llm"] = "scheduled"
    triggers: list[ScheduledTrigger] = field(default_factory=list)
    decide_every_ms: int = 500
    bot_context_path: Optional[str] = None


# The declarable metric types. Lives in core beside MetricSpec, not in
# engine/metrics.py, so the scenario loader can validate against it without
# core having to import from engine — that inversion is how the two import
# cycles in this package started.
METRIC_TYPES = ("integer", "number", "string", "boolean", "array")


@dataclass
class AssertionSpec:
    """Structural claims about a run, checked without an LLM.

    Declared under `assert:` in the scenario YAML. **Not** `assertions:` — that
    name is a V1 legacy key that `lint.py` actively rejects, and the design note
    in docs/dual-transport.md proposed it by mistake.

    Every field is optional; an all-empty spec is a no-op. A failed assertion
    vetoes the evaluator's verdict via `CallResult.passed`, because whether a
    tool was called is a fact and the LLM judge can only infer it.
    """

    must_visit_modules: list[str] = field(default_factory=list)
    must_not_visit_modules: list[str] = field(default_factory=list)
    must_call_tools: list[str] = field(default_factory=list)
    must_not_call_tools: list[str] = field(default_factory=list)
    # Relative order, gaps allowed — real flows interleave other calls, so an
    # exact-sequence check would be unusable.
    tool_order: list[str] = field(default_factory=list)
    # A bot turn that is only marker tags reaches the caller as silence. 0 means
    # "the bot must never go silent".
    max_tag_only_turns: Optional[int] = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.must_visit_modules
            or self.must_not_visit_modules
            or self.must_call_tools
            or self.must_not_call_tools
            or self.tool_order
            or self.max_tag_only_turns is not None
        )

    @property
    def needs_modules(self) -> bool:
        return bool(self.must_visit_modules or self.must_not_visit_modules)

    @property
    def needs_tools(self) -> bool:
        return bool(
            self.must_call_tools or self.must_not_call_tools or self.tool_order
        )


@dataclass
class MetricSpec:
    """One declared metric the evaluator must return.

    Replaces the hand-written `"metrics": {...}` example blob that each
    scenario's evaluation prompt carries. That blob had three problems:

    * **No types.** A metric meant as a yes/no came back as `"yes"`, `true`,
      `"likely"` or a sentence, depending on the run. Nothing checked.
    * **No descriptions.** `observed_gates_passed: "n/m"` tells the model the
      shape and nothing about the meaning, so it guessed.
    * **It asked for values judge_it already measures.** 1108 scenarios request
      `bot_turns`; the harness counts it exactly and overrides the answer. Asking
      anyway wastes a step and puts two conflicting instructions in one prompt.

    Declaring metrics instead means the prompt block is generated, the returned
    values are type-checked, and anything the harness measures is simply not
    asked for.
    """

    key: str
    description: str = ""
    # "integer" | "number" | "string" | "boolean" | "array"
    type: str = "string"
    # Shown to the model as the expected shape. Not a default — a metric with no
    # example just shows its type.
    example: Optional[Any] = None
    # False means "nice to have": a missing value is not flagged.
    required: bool = True


@dataclass
class Scenario:
    name: str
    target: str
    vars: dict
    persona_rendered: str
    evaluation_prompt_rendered: str
    llm: LLMConfig = field(default_factory=LLMConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    max_turns: int = 30
    # Declared metrics for the evaluator (`metrics:` in the YAML). Empty means
    # "use whatever the evaluation prompt's own example blob asks for", which is
    # every pre-existing scenario — so this is purely additive.
    metrics: list[MetricSpec] = field(default_factory=list)
    # Deterministic structural assertions (`assert:` in the YAML). A failure
    # overrides the LLM verdict — see CallResult.passed.
    assertions: Optional[AssertionSpec] = None
    barge_in: Optional[BargeInConfig] = None
    # Which transport carries this scenario: "pstn" | "sip" | "text". None means
    # "whatever the deployment is configured for" (Settings.call_transport), so
    # existing scenarios keep working untouched. Set it when the scenario only
    # makes sense on one transport — a barge scenario needs audio; a
    # flow/reasoning scenario is cheaper and faster on text. Validated by
    # core/lint.py, which also rejects impossible combinations (barge + text)
    # before anything is dialled.
    transport: Optional[str] = None
    # Marks the bot-side interaction as a test (text transport only). None means
    # "use the deployment default" (Settings.text_is_test, itself False).
    #
    # False is what makes the run visible in llm-studio — its conversations view
    # filters on is_test=false. True suppresses the compliance event and nothing
    # else: in-call tools and per-turn post-call CRM writes fire either way. So
    # set True only when you need that event suppressed, accepting that the run
    # then cannot be opened in the UI.
    is_test: Optional[bool] = None
    # WHICH BOT this scenario tests, and which version of its flow.
    #
    # These belong here, not in the environment. A test harness serves many
    # assistants across many clients: with these as env globals a single process
    # could only ever address one bot, a campaign could not span two, and
    # pinning a version per scenario was impossible. `target` was already
    # per-scenario; these are the rest of the same fact.
    #
    # None on either means "use the deployment default"
    # (Settings.text_assistant_uuid / text_fc_version), so existing scenarios
    # are untouched.
    #
    #   assistant_uuid — address the assistant explicitly. When absent, OSV
    #                    resolves it from `target` (the virtual number), which
    #                    keeps one YAML working on both voice and text.
    #   fc_version     — pin the flow-config version. 0/None means "whatever OSV
    #                    considers current", which is why an unpublished draft
    #                    silently does not get tested. Pin it for a regression
    #                    suite: an unpinned run changes meaning the moment
    #                    someone publishes.
    assistant_uuid: Optional[str] = None
    fc_version: Optional[int] = None
    # WHO is calling — and therefore WHICH customer account the bot loads.
    #
    # OSV's `get_account` matches this against `Account.phone_number`
    # (then phone_number_2..5, or `Account.email` when it contains "@"). Over
    # voice it is the caller's real number. On text judge_it must supply it, and
    # getting it wrong means "No account found": the bot runs with no account
    # variables, EXPLAIN_DUE renders against nothing, and the LLM returns empty.
    #
    # None = auto-detect a phone number from `vars` (any `phone`/`mobile`/`cell`
    # key with 10-15 digits). Set it explicitly when the vars are named oddly, or
    # to use an email.
    recipient_id: Optional[str] = None
    # WHICH REGION's runtime serves this scenario: "us" | "in" | "ca". The region
    # is a path segment in the text-runtime URL, and assistants do NOT exist
    # across regions — an India assistant asked of the US gateway returns
    # `404 No assistant found`. None means "use the deployment default".
    #
    # This belongs in the scenario for the same reason `assistant_uuid` does: an
    # SBI bot is only ever in India and a Veros bot only ever in the US, so the
    # scenario knows. Leaving it to an env var means one exported variable
    # silently misroutes a whole campaign — which happened on 2026-08-24.
    region: Optional[str] = None
    source_path: Optional[str] = None


@dataclass
class CampaignResult:
    """Aggregate outcome of one campaign run.

    Populated by `CampaignRunner.run()`. Holds every per-scenario
    `CallResult` plus rolled-up counters for reporting. The reporter
    (Task 4) renders this to markdown + JSON.
    """

    campaign_name: str
    source_path: Optional[str]
    started_at: datetime
    ended_at: datetime
    duration_s: float
    total_scenarios: int
    passed: int
    failed: int
    errored: int
    pass_rate: float  # 0.0 to 1.0
    call_results: list = field(default_factory=list)
    # True when fail_fast was enabled AND a failure caused the queue to
    # be drained early. Lets the reporter distinguish "stopped after
    # failure" from "all scenarios completed".
    fail_fast_triggered: bool = False
    # Scenarios that were queued but never processed (only possible
    # under fail_fast). Lets the reporter say "10 of 50 scenarios were
    # skipped after failure at scenario 5".
    skipped_paths: list[str] = field(default_factory=list)


@dataclass
class Campaign:
    """A Phase 2 campaign: a named set of scenarios to run together.

    The loader is responsible for expanding `scenarios` (which may
    contain glob patterns) into the absolute paths stored in
    `resolved_scenarios`. The runner pulls from `resolved_scenarios`
    only; the raw `scenarios` field is preserved for round-tripping
    and reporter output.
    """

    name: str
    scenarios: list[str]  # raw entries: explicit paths and/or glob patterns
    resolved_scenarios: list[str] = field(default_factory=list)
    description: str = ""
    # Number of scenarios to run in parallel. Capped at 50 in the
    # loader to avoid accidentally configuring something Twilio would
    # reject; production default in YAML is 10 (verified against our
    # Twilio account in Phase 2 Task 0).
    max_concurrency: int = 10
    # If true, stop pulling new work from the queue after the first
    # failed scenario. In-flight scenarios still complete. Default
    # false — run the full sweep so a single flaky scenario doesn't
    # mask issues with the rest.
    fail_fast: bool = False
    source_path: Optional[str] = None


@dataclass
class EvaluationCriterion:
    id: int
    name: str
    passed: bool
    evidence: str


@dataclass
class EvaluationResult:
    overall_passed: bool
    overall_reasoning: str
    criteria: list[EvaluationCriterion] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    raw_response: Optional[str] = None
    malformed: bool = False


# Resolve the forward reference in CallResult.evaluation
CallResult.model_rebuild()
