"""The job pipeline: seed → generate → (ready) → run → correlate → report.

Both entry points are background tasks kicked off by `jobs/api.py`, and both
report progress through the callback. They never raise into the caller — a
failure becomes `status=failed` plus an `error`, delivered on the callback,
because by the time these run the HTTP response has already been sent.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from judge_it.core.config import Settings
from judge_it.jobs.callbacks import notify
from judge_it.jobs.generator import GeneratorError, generate_scenario, write_campaign
from judge_it.jobs.models import (
    Job,
    JobResultItem,
    JobStatus,
    SeededCall,
)
from judge_it.jobs.store import job_store
from judge_it.jobs.workspace import JobWorkspace
from judge_it.services.osv import (
    OSVError,
    client_for_region,
    osv_client,
    parse_interaction_ref,
)

logger = logging.getLogger(__name__)

# How far either side of the campaign window to look for our interactions.
CORRELATION_PAD = timedelta(minutes=3)

# `disposition` is written by the post-call worker AFTER the call ends, and
# correlation runs about a second after hangup — so the interaction row already
# exists and matches, while its disposition is still null. Observed live: session
# 59febbad… correlated immediately with disposition=None, and the very same
# interaction read back 'agent_unavailable_hangup' shortly after. Disposition is
# the clearest single piece of evidence in a report (a transfer bug reproduces as
# `bot_initiated_transfer`), so re-read the matched interaction a few times rather
# than reporting a null that looks like the bot did nothing.
DISPOSITION_ATTEMPTS = 5
DISPOSITION_WAIT = 5.0  # seconds between re-reads

LLM_STUDIO_BASE = "https://llm-studio.judge_it.dev"


def _studio_url(
    client_uuid: str | None,
    interaction_uuid: str | None,
    assistant_id: int | str | None = None,
    region: str = "us",
) -> Optional[str]:
    """The verified working llm-studio shape:
    /clients/<client>/conversations/<assistant_id>?region=<r>&preview=<interaction>
    (without an assistant id the old query-only form is the best we can do)."""
    if not client_uuid or not interaction_uuid:
        return None
    if assistant_id is not None:
        return (
            f"{LLM_STUDIO_BASE}/clients/{client_uuid}/conversations/{assistant_id}"
            f"?region={region}&preview={interaction_uuid}"
        )
    return (
        f"{LLM_STUDIO_BASE}/clients/{client_uuid}/conversations"
        f"?region={region}&preview={interaction_uuid}"
    )


def _one_line(text: str, limit: int = 240) -> str:
    """Collapse to a single truncated line, for log use."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _format_results(job: Job) -> str:
    """A readable results block for the log.

    Until this existed the results were reachable only via GET /v1/jobs/<id> or
    report.json, so whoever was watching the server saw "N results, M correlated"
    and nothing about what the run actually found. Built as ONE multi-line record
    rather than a line per field, so a concurrent worker's logging cannot
    interleave into the middle of it.
    """
    total = len(job.results)
    lines = [f"Job {job.job_id} results (eval={'on' if job.eval_enabled else 'off'}):"]
    for n, item in enumerate(job.results, start=1):
        verdict = ""
        if job.eval_enabled and item.passed is not None:
            verdict = f"  eval={'PASS' if item.passed else 'FAIL'}"
        lines.append(f"  [{n}/{total}] {item.scenario_id}  {item.title}")
        lines.append(
            f"      call={item.call_status or '?'}"
            f"  disposition={item.disposition or '-'}"
            f"  turns bot={item.bot_turns} caller={item.caller_turns}{verdict}"
        )
        if item.caller_was_silent:
            lines.append("      !! caller never spoke — nothing was tested")
        lines.append(f"      session={item.judge_it_session_id}")
        if item.source_interaction_uuid:
            lines.append(f"      broken call={item.source_interaction_uuid}")
        lines.append(f"      test call={item.call_link or 'NOT CORRELATED'}")
        if item.what_we_tested:
            lines.append(f"      tested: {_one_line(item.what_we_tested)}")
        if item.overall_reasoning:
            lines.append(f"      reasoning: {_one_line(item.overall_reasoning)}")
    for warning in job.warnings:
        lines.append(f"  warning: {_one_line(warning)}")
    return "\n".join(lines)


# --------------------------------------------------------------------- seeding


async def _seed_one(job_id: str, ref: str, osv=osv_client) -> SeededCall:
    """Pull metadata + transcript for one faulty call.

    Order is forced: metadata first, because it is what yields `client_uuid`,
    which the transcript endpoint needs as a path parameter and which the
    correlation query needs as its index leader.
    """
    try:
        interaction_uuid = parse_interaction_ref(ref)
    except ValueError as exc:
        return SeededCall(interaction_uuid=ref, error=f"unparseable reference: {exc}")

    seed = SeededCall(interaction_uuid=interaction_uuid)
    try:
        meta = await osv.get_interaction_metadata(interaction_uuid)
    except (OSVError, Exception) as exc:  # noqa: B014 - OSVError is a RuntimeError
        seed.error = f"metadata fetch failed: {exc}"
        return seed

    seed.client_uuid = meta.get("client_uuid") or (meta.get("interaction") or {}).get(
        "client_uuid"
    )
    seed.assistant_id = meta.get("assistant_id")
    seed.assistant_name = meta.get("assistant_name")
    inner = meta.get("interaction_metadata") or meta.get("metadata") or {}
    seed.metadata = inner if isinstance(inner, dict) else {"raw": inner}
    # Kept ONLY as a prompt-cache discriminator, never sent to OSV as `version`.
    # A flow_config_id is not a version: `/assistants/1734/?version=15232`
    # returns 404 "Assistant not found" while the bare path returns 200. Conflate
    # them and prompt fetching silently breaks for every job.
    seed.flow_config_version = str(
        seed.metadata.get("flow_config_version") or meta.get("flow_config_id") or ""
    ) or None
    # Keep the envelope too — field names vary by OSV version and the generator
    # benefits from anything we didn't think to map.
    seed.metadata.setdefault("_osv_envelope", {k: v for k, v in meta.items() if k != "interaction_metadata"})

    if not seed.client_uuid:
        seed.error = "metadata had no client_uuid; cannot fetch transcript"
        return seed

    try:
        seed.transcript = await osv.get_conversations(
            seed.client_uuid, interaction_uuid
        )
    except Exception as exc:
        # A missing transcript is a degraded seed, not a dead one — the prompt
        # plus metadata is still enough to write a replication scenario.
        logger.warning("Transcript fetch failed for %s: %s", interaction_uuid, exc)
        seed.transcript = None

    await job_store.save_seed(job_id, seed)
    return seed


async def _bot_prompt(seed: SeededCall, osv=osv_client) -> str:
    """Fetch the assistant config, cached per client+assistant+version.

    Six faulty calls on one bot should cost one fetch — hence the cache key,
    which deliberately has nothing to do with the job or the call.
    """
    if seed.assistant_id is None or not seed.client_uuid:
        return ""
    cached = await job_store.get_prompt(
        seed.client_uuid, seed.assistant_id, seed.flow_config_version
    )
    if cached:
        logger.info("Prompt cache hit for assistant %s", seed.assistant_id)
        return cached
    try:
        # No `version` argument on purpose — see get_bot_data's docstring. We hold
        # a flow_config_id, which is not a version, and passing it 404s. The
        # production config is what a replication test should run against.
        bot = await osv.get_bot_data(seed.assistant_id)
    except Exception as exc:
        logger.warning("Bot config fetch failed for %s: %s", seed.assistant_id, exc)
        return ""
    import json

    payload = json.dumps(bot, indent=2, default=str, sort_keys=True)
    await job_store.set_prompt(
        seed.client_uuid, seed.assistant_id, seed.flow_config_version, payload
    )
    return payload


async def seed_and_generate(job_id: str) -> None:
    """Background task for `POST /v1/jobs`."""
    job = await job_store.get(job_id)
    if job is None:
        logger.error("seed_and_generate: unknown job %s", job_id)
        return

    settings = Settings()
    workspace = JobWorkspace(job_id).create()
    workspace.write_json("job.json", job.model_dump())

    try:
        # The caller told us which region these calls live in; an interaction uuid
        # does not exist across regions, so reading the wrong one yields a
        # "not found" that looks like a bad id.
        osv = client_for_region(job.config.region)

        # ---- seed
        job.touch(JobStatus.SEEDING)
        await job_store.save(job)
        await notify(job, event="seeding")

        seeds = await asyncio.gather(
            *(_seed_one(job_id, ref, osv) for ref in job.calls),
            return_exceptions=False,
        )
        for seed in seeds:
            workspace.write_seed(seed.interaction_uuid, seed.model_dump())

        usable = [s for s in seeds if s.ok]
        logger.info("Job %s seeded %d/%d calls", job_id, len(usable), len(seeds))

        # Note, don't scold: dialling a DIFFERENT number than the faulty call is
        # the NORMAL case. Tests run against an imported clone of the live bot,
        # precisely so production is never phoned. An earlier version of this
        # warned about the mismatch and told the operator to dial the source DID —
        # which was advice to call the live bot, exactly backwards.
        # Kept as an informational note because a genuinely wrong number looks the
        # same, and the clone's flow can drift from the original it was cloned
        # from, which would make the replication test something subtly different.
        for seed in usable:
            source_did = (seed.metadata.get("_osv_envelope") or {}).get("virtual_id")
            if source_did and source_did != job.config.target:
                note = (
                    f"call {seed.interaction_uuid[:8]} came in on {source_did} "
                    f"(assistant {seed.assistant_id} {seed.assistant_name!r}) and "
                    f"this job dials {job.config.target}. Expected when testing an "
                    f"imported clone. Correlation is unaffected — it matches on "
                    f"client + caller id, not assistant. Worth confirming only "
                    f"that the clone's flow still matches the bot that broke."
                )
                logger.info("Job %s: %s", job_id, note)
                job.warnings.append(note)
        if not usable:
            details = "; ".join(f"{s.interaction_uuid}: {s.error}" for s in seeds)
            raise GeneratorError(f"no call could be seeded from OSV — {details}")

        # ---- generate
        job.touch(JobStatus.GENERATING)
        await job_store.save(job)
        await notify(job, event="generating")

        api_key = settings.openai_api_key
        generated = []
        for index, seed in enumerate(usable, start=1):
            scenario_id = f"S{index:02d}-{seed.interaction_uuid[:8]}"
            prompt = await _bot_prompt(seed, osv)
            try:
                gen = await generate_scenario(
                    seed=seed,
                    config=job.config,
                    bot_prompt=prompt,
                    scenario_id=scenario_id,
                    workspace=workspace,
                    api_key=api_key,
                )
            except Exception as exc:
                logger.exception("Scenario generation failed for %s", scenario_id)
                # One bad scenario shouldn't sink the job; record and continue.
                from judge_it.jobs.models import GeneratedScenario

                gen = GeneratedScenario(
                    scenario_id=scenario_id,
                    source_interaction_uuid=seed.interaction_uuid,
                    path="",
                    title=f"{scenario_id} (generation failed)",
                    lint_ok=False,
                    lint_errors=[f"{type(exc).__name__}: {exc}"],
                )
            generated.append(gen)
            job.scenarios = generated
            await job_store.save(job)

        clean = [g for g in generated if g.lint_ok]
        if not clean:
            errs = "; ".join(e for g in generated for e in g.lint_errors[:2])
            raise GeneratorError(f"no scenario passed lint — {errs}")

        campaign_path = write_campaign(workspace, generated, job.config, job_id)
        job.campaign_path = str(campaign_path)
        job.touch(JobStatus.READY)
        await job_store.save(job)
        logger.info(
            "Job %s ready: %d/%d scenarios lint-clean, campaign=%s",
            job_id, len(clean), len(generated), campaign_path,
        )
        await notify(job, event="ready", full=True)

    except Exception as exc:
        logger.exception("Job %s failed during seed/generate", job_id)
        job.touch(JobStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
        await job_store.save(job)
        await notify(job, event="failed", full=True)


# ----------------------------------------------------------------- correlation


async def find_interaction_by_session_id(
    client_uuid: str,
    session_id: str,
    assistant_id: int | None,
    window_start: datetime,
    window_end: datetime,
    caller_id: str | None = None,
    osv=osv_client,
) -> Optional[dict[str, Any]]:
    """Resolve one judge_it session id to the OSV interaction it created.

    Two hops, both existing endpoints — no new OSV work, no Metabase, no DB:

      1. `GET /client/{client_uuid}/interactions/` narrowed by assistant and the
         campaign's own time window. `client_uuid` is a PATH parameter, which is
         what guarantees the DB uses `ix_interactions_client_assistant_created`
         (it leads on client_uuid) — a query filter can be forgotten, a path
         segment cannot.
      2. `GET /accounts/interaction/{uuid}` per candidate, because the list
         response does NOT return the real metadata: the view rebuilds
         `interaction_metadata` as a single `is_awaiting_reply` key. Hop 2 is
         where `x-judge_it-session-id` actually becomes visible.

    The match is exact — string equality on a key we minted — not the
    (caller#, called#, ±time) heuristic that platform-evolution §5b describes.
    """
    try:
        listing = await osv.list_client_interactions(
            client_uuid=client_uuid,
            # assistant_id is deliberately NOT filtered on. Tests run against an
            # IMPORTED CLONE of the live bot — that is the standard practice, and
            # the whole point is not to dial production. The clone is a different
            # assistant, so filtering by the SOURCE call's assistant_id excluded
            # the very interaction we were looking for: our call landed on
            # assistant 1840 ("Imported - Claude Skill- Test") while the faulty
            # call came from 721 ("Arcadia - 3P IB"), same client.
            # client_uuid (a path param) plus recipient_id is precise enough —
            # it returned 3 rows, all ours — and hop 2 confirms identity exactly.
            assistant_id=None,
            # judge_it's caller ID. Only judge_it dials from it, so this is the
            # single strongest narrowing available — verified to return exactly
            # our calls and nothing else.
            recipient_id=caller_id,
            from_date=window_start.isoformat(),
            to_date=window_end.isoformat(),
            page_size=100,
        )
    except Exception as exc:
        logger.warning("Correlation hop 1 failed for %s: %s", session_id, exc)
        return None

    # The gateway returns rows under `interactions`; the other names are kept as
    # fallbacks in case a direct-to-OSV deployment shapes it differently.
    rows = (
        listing.get("interactions")
        or listing.get("items")
        or listing.get("results")
        or listing.get("data")
        or []
    )
    if isinstance(rows, dict):
        rows = rows.get("interactions") or rows.get("items") or []

    candidates: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # `interaction_uuid` first: that is what the list rows actually use.
        iuuid = row.get("interaction_uuid") or row.get("uuid")
        if iuuid:
            candidates.append(str(iuuid))

    logger.info(
        "Correlation: %d candidate interactions for session %s", len(candidates), session_id
    )

    for iuuid in candidates:
        try:
            meta = await osv.get_interaction_metadata(iuuid)
        except Exception:
            continue
        inner = meta.get("interaction_metadata") or {}
        if not isinstance(inner, dict):
            continue
        if inner.get("x-judge_it-session-id") == session_id:
            return meta
    return None


async def await_disposition(meta: dict[str, Any], osv=osv_client) -> dict[str, Any]:
    """Re-read one matched interaction until its disposition is written.

    Only hop 2 is repeated — the interaction is already identified, so re-listing
    would be wasted work. Returns the freshest metadata either way; a disposition
    that never arrives is reported as absent rather than held for.
    """
    iuuid = str(meta.get("uuid") or "")
    if not iuuid or str(meta.get("disposition") or "").strip():
        return meta

    for attempt in range(1, DISPOSITION_ATTEMPTS + 1):
        await asyncio.sleep(DISPOSITION_WAIT)
        try:
            fresh = await osv.get_interaction_metadata(iuuid)
        except Exception as exc:
            logger.warning("Disposition re-read failed for %s: %s", iuuid, exc)
            return meta
        meta = fresh
        if str(fresh.get("disposition") or "").strip():
            logger.info(
                "Disposition for %s arrived after %.0fs: %s",
                iuuid, attempt * DISPOSITION_WAIT, fresh.get("disposition"),
            )
            return fresh

    logger.info(
        "Disposition for %s still unwritten after %.0fs — reporting without it",
        iuuid, DISPOSITION_ATTEMPTS * DISPOSITION_WAIT,
    )
    return meta


# ------------------------------------------------------------------ run + report


async def run_and_report(job_id: str) -> None:
    """Background task for `POST /v1/jobs/{id}/run`."""
    job = await job_store.get(job_id)
    if job is None:
        logger.error("run_and_report: unknown job %s", job_id)
        return

    try:
        from judge_it.core.campaign_loader import load as load_campaign
        from judge_it.orchestrator.campaign_runner import CampaignRunner
        from judge_it.orchestrator.runner import TestRunner

        # Correlation must read the same region the calls were seeded from.
        osv = client_for_region(job.config.region)

        # eval=False reuses the EXISTING skip-evaluation path rather than adding a
        # second one: the call still runs and `passed` derives from call status.
        settings = Settings(skip_evaluation=not job.eval_enabled)

        campaign = load_campaign(job.campaign_path)
        runner = TestRunner(settings)
        started = datetime.now(timezone.utc)

        logger.info(
            "Job %s running campaign %s (eval=%s)", job_id, job.campaign_path, job.eval_enabled
        )
        campaign_result = await CampaignRunner(runner).run(campaign)
        finished = datetime.now(timezone.utc)

        # `call_results`, NOT `results` — see core/models.CampaignResult. This was
        # `getattr(campaign_result, "results", [])`, whose default silently
        # returned nothing: the campaign logged "1/1 passed" while the job
        # reported "0 results, 0 correlated". A defensive default on an attribute
        # we control turned a typo into a silent empty report, so it is gone —
        # and a mismatch between what ran and what we collected now shouts.
        call_results = list(campaign_result.call_results or [])
        expected = getattr(campaign_result, "total_scenarios", None)
        if expected and len(call_results) != expected:
            logger.warning(
                "Job %s: campaign ran %s scenario(s) but %d result(s) came back",
                job_id, expected, len(call_results),
            )

        # ---- correlate
        job.touch(JobStatus.CORRELATING)
        await job_store.save(job)
        await notify(job, event="correlating")

        seeds = {s.interaction_uuid: s for s in await job_store.list_seeds(job_id)}
        by_scenario = {s.scenario_id: s for s in job.scenarios}
        # Every generated scenario's `name` is stamped with its scenario_id (see
        # generator.stamp_scenario_id), and CallResult reports it as `flow_name`.
        # That is the exact join. Previously this matched the model's free-text
        # `title` against `flow_name` — different strings — and fell back to
        # comparing the id against a LOWERCASED log-dir path, so both missed and
        # every result came back with scenario_id '?'.
        def _match(call) -> Optional[Any]:
            flow_name = str(getattr(call, "flow_name", "") or "")
            for gen in job.scenarios:
                if flow_name.startswith(gen.scenario_id):
                    return gen
            # Case-insensitive path match, for scenarios generated before the
            # stamp existed.
            log_dir = str(getattr(call, "log_dir", "") or "").lower()
            for gen in job.scenarios:
                if gen.scenario_id.lower() in log_dir:
                    return gen
            # Single-scenario job: there is only one thing it can be.
            if len(job.scenarios) == 1:
                return job.scenarios[0]
            return None

        # Correlation needs a client_uuid and a session id — NOT a successful
        # scenario match. Gating it on `seed` meant one failed lookup silently
        # skipped the OSV call entirely and reported "0 correlated", which read
        # like an OSV problem when the lookup itself was fine. Every seed in a job
        # has shared the same client so far, so any of them will do as a fallback.
        fallback_client = next(
            (s.client_uuid for s in seeds.values() if s.client_uuid), None
        )

        items: list[JobResultItem] = []
        for call in call_results:
            session_id = getattr(call, "session_id", "") or getattr(call, "id", "")
            gen = _match(call)
            if gen is None:
                logger.warning(
                    "Job %s: could not map result %r back to a generated scenario",
                    job_id, getattr(call, "flow_name", ""),
                )
            seed = seeds.get(gen.source_interaction_uuid) if gen else None

            # Count who actually said something. A call can connect, run 49
            # seconds and be marked passed while the caller never spoke a word —
            # which is exactly what happened when a generated persona was a
            # script. caller_turns=0 makes that visible in the report.
            transcript = list(getattr(call, "transcript", None) or [])
            bot_turns = sum(
                1 for t in transcript
                if getattr(t, "speaker", "") == "bot" and (getattr(t, "text", "") or "").strip()
            )
            caller_turns = sum(
                1 for t in transcript
                if getattr(t, "speaker", "") == "caller"
                and ((getattr(t, "text", "") or "").strip() or (getattr(t, "dtmf", "") or "").strip())
            )

            item = JobResultItem(
                bot_turns=bot_turns,
                caller_turns=caller_turns,
                scenario_id=gen.scenario_id if gen else "?",
                judge_it_session_id=session_id,
                source_interaction_uuid=gen.source_interaction_uuid if gen else "",
                title=gen.title if gen else getattr(call, "flow_name", ""),
                description=gen.description if gen else "",
                what_we_tested=gen.what_we_tested if gen else "",
                call_status=getattr(getattr(call, "status", None), "value", None)
                or str(getattr(call, "status", "")),
                passed=getattr(call, "passed", None) if job.eval_enabled else None,
            )

            client_uuid = (seed.client_uuid if seed else None) or fallback_client
            if client_uuid and session_id:
                match = await find_interaction_by_session_id(
                    client_uuid=client_uuid,
                    session_id=session_id,
                    assistant_id=None,  # the clone is a different assistant
                    window_start=started - CORRELATION_PAD,
                    window_end=finished + CORRELATION_PAD,
                    caller_id=settings.twilio_phone_number,
                    osv=osv,
                )
                if match:
                    match = await await_disposition(match, osv)
                    item.interaction_uuid = str(match.get("uuid") or "")
                    item.disposition = match.get("disposition")
                    # Link against the interaction's OWN client, which is the
                    # clone's — not the source call's, though they have matched
                    # so far.
                    item.call_link = _studio_url(
                        str(match.get("client_uuid") or client_uuid),
                        item.interaction_uuid,
                        assistant_id=match.get("assistant_id"),
                        region=getattr(job.config, "region", "us") or "us",
                    )
                else:
                    logger.warning(
                        "Job %s: no OSV interaction found for session %s "
                        "(client=%s, window %s..%s)",
                        job_id, session_id, client_uuid,
                        (started - CORRELATION_PAD).isoformat(),
                        (finished + CORRELATION_PAD).isoformat(),
                    )

            evaluation = getattr(call, "evaluation", None)
            if job.eval_enabled and evaluation is not None:
                item.overall_reasoning = getattr(evaluation, "overall_reasoning", None)

            items.append(item)

        job.results = items
        job.touch(JobStatus.COMPLETED)
        await job_store.save(job)

        report_path = JobWorkspace(job_id).write_json(
            "report.json",
            {
                "job_id": job_id,
                "status": job.status.value,
                "eval": job.eval_enabled,
                "results": [i.model_dump() for i in items],
                "warnings": list(job.warnings),
            },
        )
        matched = sum(1 for i in items if i.interaction_uuid)
        silent = [i.scenario_id for i in items if i.caller_was_silent]
        logger.info(
            "Job %s completed: %d results, %d correlated to OSV (report: %s)",
            job_id, len(items), matched, report_path,
        )
        logger.info("%s", _format_results(job))
        if silent:
            # Loud, because `passed` does not catch it: with eval:false `passed`
            # comes from call status, and a call where the caller said nothing
            # still completes successfully.
            logger.warning(
                "Job %s: caller NEVER SPOKE on %d scenario(s) %s — the call "
                "connected but tested nothing; check the persona",
                job_id, len(silent), silent,
            )
        await notify(job, event="completed", full=True)

    except Exception as exc:
        logger.exception("Job %s failed during run", job_id)
        job.touch(JobStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
        await job_store.save(job)
        await notify(job, event="failed", full=True)
