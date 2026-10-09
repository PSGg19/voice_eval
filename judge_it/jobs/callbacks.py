"""Callback delivery.

Job create and run both return immediately and report progress by POSTing to a
caller-supplied `callback_url`. A campaign of six calls runs for minutes, so a
synchronous response would be a connection waiting to time out — and the caller
here is often Claude Code or a script, which wants to be told rather than poll.

Delivery is best-effort by design: a job must not fail because the caller's hook
is down. Failures are logged and swallowed.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from judge_it.jobs.models import Job

logger = logging.getLogger(__name__)

CALLBACK_TIMEOUT_S = 10.0


async def send_callback(
    url: str | None, payload: dict[str, Any], *, event: str = "status"
) -> bool:
    """POST `payload` to `url`. Returns whether it was delivered.

    Never raises — see the module docstring.
    """
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT_S) as http:
            resp = await http.post(url, json=payload)
        if resp.is_success:
            logger.info("Callback %s delivered to %s (%d)", event, url, resp.status_code)
            return True
        logger.warning(
            "Callback %s rejected by %s: %d %s", event, url, resp.status_code, resp.text[:200]
        )
    except httpx.HTTPError as exc:
        logger.warning("Callback %s to %s failed: %s", event, url, exc)
    return False


async def notify(job: Job, *, event: str = "status", full: bool = False) -> None:
    """Send the job's current state to its callback URL.

    `full=True` includes scenarios and results — used for terminal events. The
    progress pings stay small so a job with 50 results doesn't POST a megabyte
    on every transition.
    """
    payload: dict[str, Any] = {
        "event": event,
        "job_id": job.job_id,
        "status": job.status.value,
        "updated_at": job.updated_at,
    }
    if job.error:
        payload["error"] = job.error
    # Always included, on every event: a mismatch warning is useless if the
    # caller only sees it after the calls have already been placed.
    if job.warnings:
        payload["warnings"] = list(job.warnings)
    if full:
        payload["eval"] = job.eval_enabled
        payload["scenarios"] = [s.model_dump() for s in job.scenarios]
        payload["results"] = [r.model_dump() for r in job.results]
        payload["campaign_path"] = job.campaign_path
    else:
        payload["scenario_count"] = len(job.scenarios)
        payload["result_count"] = len(job.results)

    await send_callback(job.callback_url, payload, event=event)
