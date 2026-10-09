"""Job-mode data model.

A **job** is the third way to run judge_it, alongside `run` (one scenario) and
`campaign` (a list of scenarios). The difference is who writes the scenarios:
for `run` and `campaign` a human does, and for a job **judge_it does**, from a
list of faulty production calls.

This is the automation of `clients/veros/campaigns/ib2-qa-replication.yaml`,
whose header describes the manual version: nine flagged calls, each replicated
using the real account data from that call's `interaction_metadata`. A job does
that from call links, in one request.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStatus(str, Enum):
    """Lifecycle. Terminal states are READY (created, not yet run),
    COMPLETED and FAILED."""

    CREATED = "created"
    SEEDING = "seeding"        # pulling metadata/transcript/prompt from OSV
    GENERATING = "generating"  # LLM writing scenarios + campaign
    READY = "ready"            # scenarios on disk, lint-clean, not yet dialled
    RUNNING = "running"
    CORRELATING = "correlating"  # matching our calls back to OSV interactions
    COMPLETED = "completed"
    FAILED = "failed"


class JobConfig(BaseModel):
    """The minimal config a caller must supply.

    Deliberately small: everything else (account numbers, DOB, names, language
    actually used) is lifted from the faulty call's metadata, because the caller
    has no test credentials — that is the whole point of seeding from a real
    call.
    """

    target: str                                   # E.164, where judge_it dials
    languages: list[str] = Field(default_factory=lambda: ["en-US"])
    max_concurrency: int = 2
    notes: Optional[str] = None                   # free-text steer for the generator
    # Which OSV region the faulty calls live in: us | in | ca. It must come from
    # the caller, because the same interaction uuid does not exist across
    # regions and guessing wrong produces a "call not found" that looks like a
    # bad id. The `region=` param in an llm-studio URL is frequently wrong, so
    # it is deliberately NOT parsed from the link.
    region: str = "us"
    # Which transport carries every scenario in this job: "sip" (default —
    # correlation via the x-judge_it-session-id header) or "text" (OSV's text
    # runtime; correlation is free because the interaction_uuid comes back on
    # turn 1). Stamped onto each generated scenario, so it travels with the
    # scenario rather than depending on the server's env. "pstn" is rejected at
    # admission: it cannot correlate. None means "use the deployment default".
    transport: Optional[str] = None


class JobCreateRequest(BaseModel):
    job_id: Optional[str] = None                  # omitted -> we mint and return one
    callback_url: Optional[str] = None
    calls: list[str]                              # llm-studio links or bare uuids
    config: JobConfig


class JobRunRequest(BaseModel):
    callback_url: Optional[str] = None
    # eval=False means "we dial, you judge" — the caller (Claude via MCP, or a
    # human) evaluates. Maps onto the existing JUDGE_IT_SKIP_EVALUATION path
    # rather than a second skip mechanism.
    eval: bool = True


class SeededCall(BaseModel):
    """Everything OSV knows about one faulty call."""

    interaction_uuid: str
    client_uuid: Optional[str] = None
    assistant_id: Optional[int] = None
    assistant_name: Optional[str] = None
    flow_config_version: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    transcript: Any = None
    error: Optional[str] = None                   # seeding failed for this one call

    @property
    def ok(self) -> bool:
        return self.error is None and self.client_uuid is not None


class GeneratedScenario(BaseModel):
    """One generated scenario, plus the generator's own account of it.

    `title` / `description` / `what_we_tested` come from the model at generation
    time — it knows what it wrote the scenario to prove, so asking for it then is
    more faithful than reverse-engineering it from the YAML afterwards. They are
    also exactly the fields the eval:false report needs.
    """

    scenario_id: str
    source_interaction_uuid: str
    path: str
    title: str
    description: str = ""
    what_we_tested: str = ""
    lint_ok: bool = False
    lint_errors: list[str] = Field(default_factory=list)


class JobResultItem(BaseModel):
    """One dialled call, correlated back to OSV."""

    scenario_id: str
    judge_it_session_id: str
    source_interaction_uuid: str
    title: str
    description: str = ""
    what_we_tested: str = ""
    call_link: Optional[str] = None
    interaction_uuid: Optional[str] = None
    call_status: Optional[str] = None
    disposition: Optional[str] = None
    # Turn counts, so a call where nothing was said cannot be mistaken for a
    # success. Observed live: the caller returned empty speech on every turn, the
    # bot timed out and hung up, and the job still reported passed=True — because
    # with eval:false `passed` derives from call STATUS, and the call did connect.
    # `caller_turns == 0` is the tell, and it should be visible in the report
    # rather than only in a wav file nobody opens.
    bot_turns: int = 0
    caller_turns: int = 0
    # populated only when eval is on
    passed: Optional[bool] = None
    overall_reasoning: Optional[str] = None

    @property
    def caller_was_silent(self) -> bool:
        return self.caller_turns == 0 and self.bot_turns > 0


class Job(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.CREATED
    config: JobConfig
    calls: list[str] = Field(default_factory=list)
    callback_url: Optional[str] = None
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    error: Optional[str] = None

    scenarios: list[GeneratedScenario] = Field(default_factory=list)
    campaign_path: Optional[str] = None
    results: list[JobResultItem] = Field(default_factory=list)
    eval_enabled: bool = True
    # Non-fatal problems the caller should see. A job can succeed mechanically
    # and still not have tested what the caller believed it was testing — the
    # target/source-bot mismatch below being the case that cost a real call.
    warnings: list[str] = Field(default_factory=list)

    def touch(self, status: JobStatus | None = None, error: str | None = None) -> "Job":
        if status is not None:
            self.status = status
        if error is not None:
            self.error = error
        self.updated_at = _now()
        return self

    def ack(self) -> dict[str, Any]:
        """The immediate HTTP response — small, and never the full job.

        Create and run both return this straight away; everything else arrives
        on the callback. A campaign of six calls takes minutes, so a synchronous
        response would just be a connection waiting to time out.
        """
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "scenario_count": len(self.scenarios),
        }
