"""Request/response shapes for the V3 API.

Requests are pydantic models (FastAPI validates them into the envelope via
errors.py); responses are built by the small `*_to_dict` helpers so the wire
shape is explicit in one place rather than implied by ORM attributes.

`region` on RunIn is REQUIRED (P3.8): the run names its region, the resolver
turns it into the config bundle, and "whatever the .env happened to hold" is
no longer a thing a run can mean.
"""
from __future__ import annotations

import uuid
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Region = Literal["us", "in", "ca"]
Transport = Literal["text", "sip", "pstn"]

_MODEL_CONFIG = ConfigDict(extra="forbid")  # a typo'd field is a 422, not a no-op


class VariableSetIn(BaseModel):
    model_config = _MODEL_CONFIG
    name: str = Field(min_length=1, max_length=200)
    client: str = ""
    vars: dict = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)
    is_synthetic: bool = False


class VariableSetUpdate(BaseModel):
    model_config = _MODEL_CONFIG
    vars: Optional[dict] = None
    provenance: Optional[dict] = None
    is_synthetic: Optional[bool] = None


class TargetIn(BaseModel):
    model_config = _MODEL_CONFIG
    phone_number: str = Field(pattern=r"^\+[1-9]\d{1,14}$")
    region: Region
    assistant_uuid: str = ""
    assistant_id: Optional[int] = None
    assistant_name: str = ""
    client_uuid: str = ""
    provider_config: str = ""
    account_count: int = Field(default=0, ge=0)
    post_call_enabled: bool = True
    allow_customer_data: bool = False
    notes: str = ""


class ScenarioIn(BaseModel):
    model_config = _MODEL_CONFIG
    key: str = Field(min_length=1, max_length=300)
    body: dict
    target_id: Optional[uuid.UUID] = None
    variable_set_id: Optional[uuid.UUID] = None


class ScenarioUpdate(BaseModel):
    model_config = _MODEL_CONFIG
    body: dict


class RunIn(BaseModel):
    model_config = _MODEL_CONFIG
    scenario_id: uuid.UUID
    region: Region  # required — P3.8
    transport: Optional[Transport] = None  # None → scenario's, then deployment
    variable_set_id: Optional[uuid.UUID] = None
    vars_overrides: dict = Field(default_factory=dict)
    target_id: Optional[uuid.UUID] = None
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=200)
    acknowledge_customer_data: bool = False


class BatchIn(BaseModel):
    model_config = _MODEL_CONFIG
    name: str = ""
    config: dict = Field(default_factory=dict)
    runs: list[RunIn] = Field(min_length=1, max_length=500)


class PreflightIn(BaseModel):
    model_config = _MODEL_CONFIG
    transport: Transport = "text"
    region: Optional[Region] = None  # None → the target's own region
    acknowledge_customer_data: bool = False


# ------------------------------------------------------------- serializers

def variable_set_to_dict(vs) -> dict:
    return {
        "id": vs.id,
        "name": vs.name,
        "client": vs.client,
        "vars": vs.vars,
        "provenance": vs.provenance,
        "is_synthetic": vs.is_synthetic,
        "created_at": vs.created_at,
        "updated_at": vs.updated_at,
    }


def target_to_dict(t) -> dict:
    return {
        "id": t.id,
        "phone_number": t.phone_number,
        "region": t.region,
        "assistant_uuid": t.assistant_uuid,
        "assistant_id": t.assistant_id,
        "assistant_name": t.assistant_name,
        "client_uuid": t.client_uuid,
        "provider_config": t.provider_config,
        "account_count": t.account_count,
        "post_call_enabled": t.post_call_enabled,
        "allow_customer_data": t.allow_customer_data,
        "notes": t.notes,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


def scenario_to_dict(s, *, include_body: bool = True) -> dict:
    out = {
        "id": s.id,
        "key": s.key,
        "version": s.version,
        "target_id": s.target_id,
        "variable_set_id": s.variable_set_id,
        "source_path": s.source_path,
        "first_run_at": s.first_run_at,
        "created_at": s.created_at,
        "locked": s.first_run_at is not None,
    }
    if include_body:
        out["body"] = s.body
    return out


def run_to_dict(r) -> dict:
    return {
        "run_uuid": r.run_uuid,
        "status": r.status,  # the resource status IS the job status — no task ids
        "passed": r.passed,
        "batch_id": r.batch_id,
        "scenario_id": r.scenario_id,
        "scenario_key": r.scenario_key,
        "scenario_version": r.scenario_version,
        "target_id": r.target_id,
        "region": r.region,
        "transport": r.transport,
        "vars_snapshot": r.vars_snapshot,
        "config_snapshot": r.config_snapshot,
        "verdict": r.verdict,
        "error": r.error,
        "interaction_uuid": r.interaction_uuid,
        "call_sid": r.call_sid,
        "artifact_keys": r.artifact_keys,
        "created_at": r.created_at,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
    }


def run_detail_to_dict(r) -> dict:
    """The single-run view: everything the list view has, plus the payloads
    that are too heavy for a list page — transcript, observations, judge
    policy. List endpoints stay on run_to_dict deliberately."""
    return {
        **run_to_dict(r),
        "transcript": r.transcript,
        "observations": r.observations,
        "judge_policy": r.judge_policy,
    }


def batch_to_dict(b, status_counts: dict[str, int]) -> dict:
    return {
        "id": b.id,
        "name": b.name,
        "config": b.config,
        "status": b.status,
        "created_at": b.created_at,
        "finished_at": b.finished_at,
        "run_status_counts": status_counts,
    }


def page_envelope(items: list, page: int, page_size: int, total: int) -> dict:
    return {"items": items, "page": page, "page_size": page_size, "total": total}
