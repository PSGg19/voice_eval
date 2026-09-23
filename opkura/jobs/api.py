"""Job-mode HTTP API.

Two endpoints that matter, both async-with-callback:

    POST /v1/jobs                 create: seed from OSV, generate scenarios
    POST /v1/jobs/{job_id}/run    run the generated campaign

Create and run are separate on purpose. Generation is LLM output, and someone
may want to read it before it dials real phone numbers at real cost. Splitting
them also means a job can be re-run without regenerating.

A third endpoint, `GET /v1/jobs/{job_id}`, exists for debugging — the contract
is the callback, not polling.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException

from opkura.core.config import Settings
from opkura.jobs.models import (
    Job,
    JobCreateRequest,
    JobRunRequest,
    JobStatus,
)
from opkura.jobs.store import job_store, new_job_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

# Separate router: a callback sink is not scoped to a job (a caller may use one
# sink for several jobs), and mounting it under /v1/jobs/{job_id} would imply it
# is.
hooks_router = APIRouter(prefix="/v1/hooks", tags=["jobs"])

# Statuses from which a run is allowed. READY is the normal one; COMPLETED and
# FAILED are allowed so a job can be re-run without regenerating scenarios.
RUNNABLE = {JobStatus.READY, JobStatus.COMPLETED, JobStatus.FAILED}


def _require_correlatable_transport(
    settings: Settings, requested: str | None = None
) -> str:
    """Resolve the job's transport and reject one that cannot correlate.

    Job mode's whole value is joining each dialled call back to the OSV
    interaction it created, so a transport that cannot do that would make every
    report unreliable in a way that is hard to notice.

    * ``sip``  — correlates via the ``x-opkura-session-id`` header.
    * ``text`` — correlates for free: OSV returns the interaction_uuid on the
      first turn (see docs/dual-transport.md).
    * ``pstn`` — cannot carry the header, and post-hoc matching on
      (caller#, called#, ±time) is a guess. Rejected.

    `requested` is the per-job override from the payload; without it the
    deployment default applies. Returns the resolved transport.
    """
    transport = (requested or settings.call_transport or "").strip().lower()
    if transport in ("sip", "text"):
        return transport
    raise HTTPException(
        status_code=400,
        detail=(
            f"job mode cannot use transport {transport!r} — correlation requires "
            "'sip' (x-opkura-session-id header) or 'text' (interaction_uuid "
            "returned on turn 1). Set config.transport in the payload, or "
            "AUTOCALLS_CALL_TRANSPORT."
        ),
    )


@router.post("", status_code=202)
@router.post("/", status_code=202, include_in_schema=False)
async def create_job(req: JobCreateRequest, background: BackgroundTasks) -> dict:
    """Create a job: pull the faulty calls from OSV, generate scenarios.

    Returns 202 with the job id immediately. Progress arrives on the callback as
    `seeding` → `generating` → `ready` (or `failed`).
    """
    settings = Settings()
    _require_correlatable_transport(settings, req.config.transport)

    if not req.calls:
        raise HTTPException(status_code=400, detail="`calls` must not be empty")

    # Reject an unknown region here rather than in the background task: a typo'd
    # region would otherwise come back as "no call could be seeded", which reads
    # like the call ids are wrong.
    from opkura.services.osv import REGION_BASE_URLS, normalise_region

    if normalise_region(req.config.region) not in REGION_BASE_URLS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown region {req.config.region!r} — expected one of "
                f"{sorted(REGION_BASE_URLS)}"
            ),
        )

    job_id = req.job_id or new_job_id()
    if req.job_id and await job_store.exists(job_id):
        raise HTTPException(
            status_code=409,
            detail=f"job {job_id} already exists — omit job_id to create a new one",
        )

    job = Job(
        job_id=job_id,
        status=JobStatus.CREATED,
        config=req.config,
        calls=req.calls,
        callback_url=req.callback_url,
    )
    await job_store.save(job)
    logger.info(
        "Job created: job_id=%s calls=%d target=%s",
        job_id, len(req.calls), req.config.target,
    )

    # Imported here, not at module scope: the pipeline pulls in the OSV client
    # and the generator, and a bad env var there should not stop the API from
    # importing at all.
    from opkura.jobs.pipeline import seed_and_generate

    background.add_task(seed_and_generate, job_id)
    return job.ack()


@router.post("/{job_id}/run", status_code=202)
async def run_job(job_id: str, req: JobRunRequest, background: BackgroundTasks) -> dict:
    """Run a job's generated campaign.

    `eval: false` means "we dial, you judge" — the report comes back with the
    session ids, titles and call links, and the caller evaluates.
    """
    settings = Settings()

    job = await job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    # Re-check at run time against the job's OWN stored transport: the server's
    # env may have changed between create and run, and it is the job's choice
    # that decides whether its results can be correlated.
    _require_correlatable_transport(settings, job.config.transport)
    if job.status not in RUNNABLE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id} is {job.status.value}; runnable states are "
                f"{sorted(s.value for s in RUNNABLE)}"
            ),
        )
    if not job.campaign_path or not job.scenarios:
        raise HTTPException(
            status_code=409, detail=f"job {job_id} has no generated campaign to run"
        )

    job.eval_enabled = req.eval
    if req.callback_url:
        job.callback_url = req.callback_url
    job.results = []  # a re-run replaces the previous report
    job.touch(JobStatus.RUNNING)
    await job_store.save(job)
    logger.info(
        "Job run accepted: job_id=%s scenarios=%d eval=%s",
        job_id, len(job.scenarios), req.eval,
    )

    from opkura.jobs.pipeline import run_and_report

    background.add_task(run_and_report, job_id)
    return job.ack()


@router.get("", include_in_schema=False)
@router.get("/")
async def list_jobs() -> dict:
    """Job summaries, newest first — the fields a listing needs, not the
    full document (that's GET /v1/jobs/{job_id})."""
    jobs = await job_store.list_jobs()
    return {
        "items": [
            {
                "job_id": j.job_id,
                "status": j.status,
                "created_at": j.created_at,
                "updated_at": j.updated_at,
                "error": j.error,
                "eval_enabled": j.eval_enabled,
                "call_count": len(j.calls),
                "scenario_count": len(j.scenarios),
                "result_count": len(j.results),
                "target": j.config.target,
                "region": getattr(j.config, "region", "us"),
                "warnings": j.warnings,
            }
            for j in jobs
        ]
    }


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict:
    """Full job document. Poll this if you can't receive a callback."""
    job = await job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    return job.model_dump()


# Statuses a caller is usually waiting to reach. `wait` returns as soon as the
# job is at rest — there is nothing more to wait for from any of these.
SETTLED = {JobStatus.READY, JobStatus.COMPLETED, JobStatus.FAILED}


@router.get("/{job_id}/wait")
async def wait_for_job(job_id: str, timeout: float = 300.0, poll: float = 1.0) -> dict:
    """Long-poll until the job settles, then return it.

    This is the endpoint for a caller that cannot host a webhook — Claude Code,
    a shell script, anyone without a public URL. One blocking request replaces
    both a callback listener and a polling loop.

    Returns the job whether it succeeded or failed; `status` says which. Returns
    the job as-is on timeout too, rather than erroring — a slow campaign is not
    a client error, and the caller can simply wait again.
    """
    deadline = asyncio.get_running_loop().time() + max(timeout, 0.0)
    while True:
        job = await job_store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
        if job.status in SETTLED:
            return {"settled": True, **job.model_dump()}
        if asyncio.get_running_loop().time() >= deadline:
            return {"settled": False, **job.model_dump()}
        await asyncio.sleep(max(poll, 0.1))


# ---------------------------------------------------------------- callback sink


@hooks_router.post("/{sink_id}", status_code=202)
async def receive_callback(sink_id: str, payload: dict) -> dict:
    """Accept a callback and store it, so a caller without a server can read it.

    Point `callback_url` at `{public_url}/v1/hooks/{anything}` — using the job id
    is the obvious choice — then `GET /v1/hooks/{same}` to read what was
    delivered, in order.
    """
    count = await job_store.push_hook(sink_id, json.dumps(payload, default=str))
    logger.info("Callback sink %s received event #%d", sink_id, count)
    return {"sink_id": sink_id, "received": count}


@hooks_router.get("/{sink_id}")
async def read_callbacks(sink_id: str) -> dict:
    """Everything delivered to this sink, oldest first."""
    raw = await job_store.list_hooks(sink_id)
    events = []
    for item in raw:
        try:
            events.append(json.loads(item))
        except json.JSONDecodeError:
            events.append({"_unparseable": item})
    return {"sink_id": sink_id, "count": len(events), "events": events}


@hooks_router.delete("/{sink_id}")
async def clear_callbacks(sink_id: str) -> dict:
    await job_store.clear_hooks(sink_id)
    return {"sink_id": sink_id, "cleared": True}
