"""V3 routes (P3.3). Conventions throughout: bearer auth on every route,
`{detail:{error_type,…}}` errors, page/page_size pagination, and the resource
status IS the job status — there are no task ids to poll.
"""
from __future__ import annotations

import uuid as uuid_mod
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opkura.api.errors import api_error
from opkura.api.schemas import (
    BatchIn,
    PreflightIn,
    RunIn,
    ScenarioIn,
    ScenarioUpdate,
    TargetIn,
    VariableSetIn,
    VariableSetUpdate,
    batch_to_dict,
    page_envelope,
    run_detail_to_dict,
    run_to_dict,
    scenario_to_dict,
    target_to_dict,
    variable_set_to_dict,
)
from opkura.api.service import create_run, preflight_refusals
from opkura.api.state import V3State, get_session, get_state, require_auth
from opkura.api.validation import reject_credentials, validate_scenario_body
from opkura.db.models import Run, RunBatch, Scenario, Target, VariableSet

router = APIRouter(prefix="/v1", tags=["v3"], dependencies=[Depends(require_auth)])

PageParam = Query(default=1, ge=1)
PageSizeParam = Query(default=50, ge=1, le=200)


# ------------------------------------------------------------- variable sets

@router.post("/variable-sets", status_code=201)
def create_variable_set(
    payload: VariableSetIn, session: Session = Depends(get_session)
) -> dict:
    reject_credentials(payload.vars, where="vars")
    row = VariableSet(
        name=payload.name,
        client=payload.client,
        vars=payload.vars,
        provenance=payload.provenance,
        is_synthetic=payload.is_synthetic,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise api_error(
            409,
            "variable_set_exists",
            f"a variable set named {payload.name!r} already exists for client "
            f"{payload.client!r}",
        )
    return variable_set_to_dict(row)


@router.get("/variable-sets")
def list_variable_sets(
    client: Optional[str] = None,
    page: int = PageParam,
    page_size: int = PageSizeParam,
    session: Session = Depends(get_session),
) -> dict:
    q = select(VariableSet).order_by(VariableSet.created_at.desc())
    cq = select(func.count()).select_from(VariableSet)
    if client is not None:
        q = q.where(VariableSet.client == client)
        cq = cq.where(VariableSet.client == client)
    total = session.execute(cq).scalar_one()
    rows = session.execute(q.offset((page - 1) * page_size).limit(page_size)).scalars()
    return page_envelope([variable_set_to_dict(r) for r in rows], page, page_size, total)


@router.get("/variable-sets/{vs_id}")
def get_variable_set(vs_id: uuid_mod.UUID, session: Session = Depends(get_session)) -> dict:
    row = session.get(VariableSet, vs_id)
    if row is None:
        raise api_error(404, "variable_set_not_found", f"variable set {vs_id} does not exist")
    return variable_set_to_dict(row)


@router.put("/variable-sets/{vs_id}")
def update_variable_set(
    vs_id: uuid_mod.UUID,
    payload: VariableSetUpdate,
    session: Session = Depends(get_session),
) -> dict:
    row = session.get(VariableSet, vs_id)
    if row is None:
        raise api_error(404, "variable_set_not_found", f"variable set {vs_id} does not exist")
    if payload.vars is not None:
        reject_credentials(payload.vars, where="vars")
        row.vars = payload.vars
        # New values were not what the reviewer saw: trust does not survive an
        # edit unless the update explicitly re-asserts it.
        if payload.is_synthetic is None:
            row.is_synthetic = False
    if payload.provenance is not None:
        row.provenance = payload.provenance
    if payload.is_synthetic is not None:
        row.is_synthetic = payload.is_synthetic
    row.updated_at = func.now()
    session.commit()
    session.refresh(row)
    return variable_set_to_dict(row)


# ------------------------------------------------------------------- targets

@router.post("/targets", status_code=201)
def create_target(payload: TargetIn, session: Session = Depends(get_session)) -> dict:
    row = Target(**payload.model_dump())
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise api_error(
            409,
            "target_exists",
            f"target {payload.phone_number} already registered in region "
            f"{payload.region}",
        )
    return target_to_dict(row)


@router.get("/targets")
def list_targets(
    region: Optional[str] = None,
    page: int = PageParam,
    page_size: int = PageSizeParam,
    session: Session = Depends(get_session),
) -> dict:
    q = select(Target).order_by(Target.created_at.desc())
    cq = select(func.count()).select_from(Target)
    if region is not None:
        q = q.where(Target.region == region)
        cq = cq.where(Target.region == region)
    total = session.execute(cq).scalar_one()
    rows = session.execute(q.offset((page - 1) * page_size).limit(page_size)).scalars()
    return page_envelope([target_to_dict(r) for r in rows], page, page_size, total)


@router.get("/targets/{target_id}")
def get_target(target_id: uuid_mod.UUID, session: Session = Depends(get_session)) -> dict:
    row = session.get(Target, target_id)
    if row is None:
        raise api_error(404, "target_not_found", f"target {target_id} does not exist")
    return target_to_dict(row)


@router.post("/targets/{target_id}/preflight")
def preflight_target(
    target_id: uuid_mod.UUID,
    payload: PreflightIn,
    session: Session = Depends(get_session),
    state: V3State = Depends(get_state),
) -> dict:
    """Would a run against this target be admitted? Answers, never acts —
    hence 200 either way, with every refusal named (P3.7)."""
    row = session.get(Target, target_id)
    if row is None:
        raise api_error(404, "target_not_found", f"target {target_id} does not exist")
    refusals = preflight_refusals(
        row,
        transport=payload.transport,
        region=payload.region or row.region,
        settings=state.settings,
        acknowledge_customer_data=payload.acknowledge_customer_data,
    )
    return {"allowed": not refusals, "refusals": refusals}


# ----------------------------------------------------------------- scenarios

@router.post("/scenarios", status_code=201)
def create_scenario(
    payload: ScenarioIn, session: Session = Depends(get_session)
) -> dict:
    reject_credentials(payload.body, where="body")
    warnings = validate_scenario_body(payload.body)

    if payload.target_id is not None and session.get(Target, payload.target_id) is None:
        raise api_error(404, "target_not_found", f"target {payload.target_id} does not exist")
    if (
        payload.variable_set_id is not None
        and session.get(VariableSet, payload.variable_set_id) is None
    ):
        raise api_error(
            404, "variable_set_not_found",
            f"variable set {payload.variable_set_id} does not exist",
        )

    next_version = session.execute(
        select(func.coalesce(func.max(Scenario.version), 0) + 1).where(
            Scenario.key == payload.key
        )
    ).scalar_one()
    row = Scenario(
        key=payload.key,
        version=next_version,
        body=payload.body,
        target_id=payload.target_id,
        variable_set_id=payload.variable_set_id,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        # Two concurrent POSTs computed the same next version. Rare, retryable.
        raise api_error(
            409,
            "scenario_version_conflict",
            f"a concurrent request created {payload.key!r} v{next_version}; retry",
        )
    return {**scenario_to_dict(row), "lint_warnings": warnings}


@router.post("/scenarios/lint")
def lint_scenario_endpoint(payload: ScenarioIn) -> dict:
    """Lint without persisting — the same 422 the create would produce, or the
    warnings it would return."""
    reject_credentials(payload.body, where="body")
    warnings = validate_scenario_body(payload.body)
    return {"ok": True, "lint_warnings": warnings}


@router.get("/scenarios")
def list_scenarios(
    key: Optional[str] = None,
    page: int = PageParam,
    page_size: int = PageSizeParam,
    session: Session = Depends(get_session),
) -> dict:
    q = select(Scenario).order_by(Scenario.created_at.desc())
    cq = select(func.count()).select_from(Scenario)
    if key is not None:
        q = q.where(Scenario.key == key)
        cq = cq.where(Scenario.key == key)
    total = session.execute(cq).scalar_one()
    rows = session.execute(q.offset((page - 1) * page_size).limit(page_size)).scalars()
    return page_envelope(
        [scenario_to_dict(r, include_body=False) for r in rows], page, page_size, total
    )


@router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: uuid_mod.UUID, session: Session = Depends(get_session)) -> dict:
    row = session.get(Scenario, scenario_id)
    if row is None:
        raise api_error(404, "scenario_not_found", f"scenario {scenario_id} does not exist")
    return scenario_to_dict(row)


@router.put("/scenarios/{scenario_id}")
def update_scenario(
    scenario_id: uuid_mod.UUID,
    payload: ScenarioUpdate,
    session: Session = Depends(get_session),
) -> dict:
    row = session.get(Scenario, scenario_id)
    if row is None:
        raise api_error(404, "scenario_not_found", f"scenario {scenario_id} does not exist")
    if row.first_run_at is not None:
        raise api_error(
            409,
            "scenario_locked",
            f"scenario {row.key!r} v{row.version} has been run and is immutable "
            f"(first run {row.first_run_at.isoformat()}); POST a new version instead",
        )
    reject_credentials(payload.body, where="body")
    warnings = validate_scenario_body(payload.body)
    row.body = payload.body
    session.commit()
    session.refresh(row)
    return {**scenario_to_dict(row), "lint_warnings": warnings}


# ---------------------------------------------------------------------- runs

@router.post("/runs")
def create_run_endpoint(
    payload: RunIn,
    background: BackgroundTasks,
    response: Response,
    session: Session = Depends(get_session),
    state: V3State = Depends(get_state),
) -> dict:
    run, created = create_run(
        session, state.settings, payload, gate=state.admission_gate
    )
    if created:
        response.status_code = 201
        background.add_task(state.executor.execute, run.run_uuid)
    else:
        response.status_code = 200  # idempotent replay: the original run
    return run_to_dict(run)


@router.get("/runs")
def list_runs(
    batch_id: Optional[uuid_mod.UUID] = None,
    status: Optional[str] = None,
    target_id: Optional[uuid_mod.UUID] = None,
    scenario_key: Optional[str] = None,
    page: int = PageParam,
    page_size: int = PageSizeParam,
    session: Session = Depends(get_session),
) -> dict:
    q = select(Run).order_by(Run.created_at.desc())
    cq = select(func.count()).select_from(Run)
    for col, val in (
        (Run.batch_id, batch_id),
        (Run.status, status),
        (Run.target_id, target_id),
        (Run.scenario_key, scenario_key),
    ):
        if val is not None:
            q = q.where(col == val)
            cq = cq.where(col == val)
    total = session.execute(cq).scalar_one()
    rows = session.execute(q.offset((page - 1) * page_size).limit(page_size)).scalars()
    return page_envelope([run_to_dict(r) for r in rows], page, page_size, total)


@router.get("/runs/{run_uuid}")
def get_run(run_uuid: uuid_mod.UUID, session: Session = Depends(get_session)) -> dict:
    row = session.execute(
        select(Run).where(Run.run_uuid == run_uuid)
    ).scalar_one_or_none()
    if row is None:
        raise api_error(404, "run_not_found", f"run {run_uuid} does not exist")
    return run_detail_to_dict(row)


# ------------------------------------------------------------------- batches

@router.post("/run-batches", status_code=201)
def create_batch(
    payload: BatchIn,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    state: V3State = Depends(get_state),
) -> dict:
    batch = RunBatch(name=payload.name, config=payload.config, status="created")
    session.add(batch)
    # Committed BEFORE the loop: create_run manages its own transactions
    # (including a rollback on idempotent replay), and a still-pending batch
    # row would be swept away by the first of those rollbacks — leaving every
    # later run pointing at a batch that never existed.
    session.commit()
    batch_id = batch.id

    runs = []
    for i, run_payload in enumerate(payload.runs):
        # Admission is all-or-nothing per run, but the batch keeps going: one
        # refused run must not waste the other 499. Refusals come back in-line.
        try:
            run, created = create_run(
                session, state.settings, run_payload, batch_id=batch_id,
                gate=state.admission_gate,
            )
        except Exception as exc:
            session.rollback()  # clear whatever the refused admission left pending
            detail = getattr(exc, "detail", None)
            runs.append({"index": i, "accepted": False, "refusal": detail or str(exc)})
            continue
        if created:
            background.add_task(state.executor.execute, run.run_uuid)
        runs.append(
            {"index": i, "accepted": True, "created": created,
             "run_uuid": run.run_uuid, "status": run.status}
        )

    counts = _batch_counts(session, batch_id)
    batch = session.get(RunBatch, batch_id)
    return {**batch_to_dict(batch, counts), "runs": runs}


@router.get("/run-batches/{batch_id}")
def get_batch(batch_id: uuid_mod.UUID, session: Session = Depends(get_session)) -> dict:
    batch = session.get(RunBatch, batch_id)
    if batch is None:
        raise api_error(404, "batch_not_found", f"batch {batch_id} does not exist")
    return batch_to_dict(batch, _batch_counts(session, batch_id))


def _batch_counts(session: Session, batch_id) -> dict[str, int]:
    rows = session.execute(
        select(Run.status, func.count())
        .where(Run.batch_id == batch_id)
        .group_by(Run.status)
    ).all()
    return {status: count for status, count in rows}


# ------------------------------------------------------------------ gauges

@router.get("/queue/gauges")
async def queue_gauges(state: V3State = Depends(get_state)) -> dict:
    """P4 exit criterion: budgets observable. Live in-flight gauges per
    fleet/client/assistant, stage backlog depths, and the admission config in
    force. Best-effort by design — Redis trouble yields an `error` field, not
    a 500, because the endpoint exists precisely for the bad days."""
    from opkura.queue.admission import ExecutionBudget, load_config_async
    from opkura.queue.streams import LANES, STAGES, StageQueue, connect

    redis = connect(state.settings.redis_url)
    try:
        budget = ExecutionBudget(redis)
        out = await budget.snapshot()
        out["admission_config"] = (await load_config_async(redis)).__dict__
        queue = StageQueue(redis)
        depths: dict = {}
        try:
            for stage in STAGES:
                for lane in LANES:
                    depths[f"{stage}:{lane}"] = await queue.depth(stage, lane)
        except Exception as exc:
            depths = {"error": f"depths unreadable: {exc}"}
        out["depths"] = depths
        out["admission_enabled"] = state.settings.admission_enabled
        return out
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


# --------------------------------------------------------------- admission

class AdmissionConfigIn(BaseModel):
    """Partial update — only the fields present are written. The config lives
    in the `opkura:admission:config` Redis hash the AdmissionGate re-reads
    each cycle, so a change takes effect within one cycle, no restart."""

    kill_switch: Optional[bool] = None
    fleet_max_concurrency: Optional[int] = Field(default=None, ge=0)
    per_client_max_concurrency: Optional[int] = Field(default=None, ge=0)
    per_assistant_max_concurrency: Optional[int] = Field(default=None, ge=0)


@router.get("/admission")
async def get_admission(state: V3State = Depends(get_state)) -> dict:
    from opkura.queue.admission import load_config_async
    from opkura.queue.streams import connect

    redis = connect(state.settings.redis_url)
    try:
        cfg = await load_config_async(redis)
        return {
            "admission_enabled": state.settings.admission_enabled,
            **cfg.__dict__,
        }
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


@router.put("/admission")
async def put_admission(
    payload: AdmissionConfigIn, state: V3State = Depends(get_state)
) -> dict:
    from opkura.queue.admission import CONFIG_HASH, load_config_async
    from opkura.queue.streams import connect

    fields = payload.model_dump(exclude_none=True)
    if not fields:
        raise api_error(422, "empty_update", "no admission fields to set")
    mapping = {
        k: ("1" if v else "0") if isinstance(v, bool) else str(v)
        for k, v in fields.items()
    }
    redis = connect(state.settings.redis_url)
    try:
        await redis.hset(CONFIG_HASH, mapping=mapping)
        cfg = await load_config_async(redis)
        return {
            "admission_enabled": state.settings.admission_enabled,
            **cfg.__dict__,
        }
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass


# ----------------------------------------------------------------- workers

@router.get("/workers")
async def list_workers(state: V3State = Depends(get_state)) -> dict:
    """Live worker registry (Redis TTL rows — a worker that stops
    heartbeating drops out on its own)."""
    from opkura.queue.registry import WorkerRegistry
    from opkura.queue.streams import connect

    redis = connect(state.settings.redis_url)
    try:
        workers = await WorkerRegistry(redis).list_workers()
        return {"items": sorted(workers, key=lambda w: w.get("consumer", ""))}
    finally:
        try:
            await redis.aclose()
        except Exception:
            pass
