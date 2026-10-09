"""Run admission (P3.4 + P3.7 + S2): the one path a run comes into existence by.

Order of operations is the safety design, so it is spelled out:

1. resolve variables (set → explicit set → overrides, provenance kept);
2. S4 credential scan over the resolved values and overrides;
3. S2 PII value guard on the resolved values (skipped only for a reviewed
   synthetic set with nothing layered on top — core/variables.py's rule);
4. sandbox-render the persona/evaluation prompt with the resolved values —
   a run that cannot render must not exist as a row;
5. pre-flight (P3.7): customer-data guard, S3 post-call refusal for text,
   S6 region compliance and trunk resolution — all BEFORE any row or dial;
6. idempotency claim + run row in ONE transaction — the claim and the row
   commit or roll back together, so there is never a claimed key without its
   run, and a duplicate POST returns the original run (P3.4);
7. the scenario's `first_run_at` latches in the same transaction — from this
   commit on, the scenario row is immutable and the run stays explicable.

Execution (BackgroundTasks → executor) happens strictly after the commit.
"""
from __future__ import annotations

import hashlib
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from judge_it.api.errors import api_error
from judge_it.api.schemas import RunIn
from judge_it.api.validation import reject_credentials
from judge_it.core import variables as varlib
from judge_it.core.config import Settings
from judge_it.core.pii_guard import inspect as pii_inspect, is_significant
from judge_it.core.region_profile import (
    RegionNotCompliant,
    RegionNotConfigured,
    resolve_profile,
)
from judge_it.core.rendering import RenderError, render_sandboxed
from judge_it.db.idempotency import claim
from judge_it.db.models import Run, Scenario, Target, VariableSet


# ------------------------------------------------------------------ preflight

def preflight_refusals(
    target: Optional[Target],
    *,
    transport: str,
    region: str,
    settings: Settings,
    acknowledge_customer_data: bool = False,
) -> list[dict]:
    """Every reason NOT to run against this target, as data.

    Returned (not raised) so the pre-flight endpoint can answer "would this be
    allowed?" and run admission can 422 with the same list. Empty list = go.
    """
    refusals: list[dict] = []
    if target is not None:
        if (
            target.account_count > 0
            and not target.allow_customer_data
            and not acknowledge_customer_data
        ):
            refusals.append(
                {
                    "error_type": "target_has_customer_data",
                    "message": (
                        f"target {target.phone_number} ({target.assistant_name or 'unnamed'}) "
                        f"carries {target.account_count} real accounts — imported clones "
                        f"copy the account table. Refused unless the target row sets "
                        f"allow_customer_data or the request acknowledges it."
                    ),
                }
            )
        if transport == "text" and target.post_call_enabled:
            refusals.append(
                {
                    "error_type": "post_call_events_enabled",
                    "message": (
                        "text-mode post-call processing fires CRM webhooks even with "
                        "is_test (S3) — this target has post-call events enabled, so a "
                        "text run would write to the client's production CRM. Disable "
                        "post-call on the target or use a target without it."
                    ),
                }
            )
        if target.region != region:
            refusals.append(
                {
                    "error_type": "target_region_mismatch",
                    "message": (
                        f"run says region '{region}' but the target is registered in "
                        f"'{target.region}' — assistants do not exist across regions, "
                        f"so this run would dial a number the region cannot resolve."
                    ),
                }
            )

    try:
        resolve_profile(
            region,
            settings,
            for_transport=transport if transport in ("sip", "pstn") else None,
        )
    except RegionNotCompliant as e:
        refusals.append({"error_type": "region_not_compliant", "message": str(e)})
    except RegionNotConfigured as e:
        refusals.append({"error_type": "region_not_configured", "message": str(e)})

    # P6.6 — the provider chooser IS the compliance rule for voice: a region
    # no configured telephony provider is allowed to carry is refused here,
    # named. Twilio declares (us, ca); India needs the LiveKit-SIP provider,
    # which activates once the org's India trunk coordinates are configured.
    if transport in ("sip", "pstn"):
        from judge_it.voice.provider import ProviderRefusal, configured_providers

        try:
            provider_for_region = configured_providers(settings)
            from judge_it.voice.provider import provider_for

            provider_for(region, provider_for_region)
        except ProviderRefusal as e:
            refusals.append({
                "error_type": "provider_not_allowed",
                "message": str(e),
                "rule": e.rule,
            })
    return refusals


# ----------------------------------------------------------------- run intake

def _load_variable_set(session: Session, vs_id) -> Optional[varlib.VariableSet]:
    if vs_id is None:
        return None
    row = session.get(VariableSet, vs_id)
    if row is None:
        raise api_error(404, "variable_set_not_found", f"variable set {vs_id} does not exist")
    return varlib.VariableSet(
        id=str(row.id), name=row.name, values=row.vars, is_synthetic=row.is_synthetic
    )


def create_run(
    session: Session, settings: Settings, payload: RunIn, *, batch_id=None, gate=None
) -> tuple[Run, bool]:
    """Admit one run. Returns (run_row, created) — created=False means the
    idempotency key already owned a run and that run is being returned.

    `gate` (P4.4, optional) is the AdmissionGate; when the env gate is on it
    is consulted BEFORE any row exists. A kill-switch refusal is a 503
    (operator intent, don't retry blindly); a budget refusal is a 429."""
    scenario = session.get(Scenario, payload.scenario_id)
    if scenario is None:
        raise api_error(
            404, "scenario_not_found", f"scenario {payload.scenario_id} does not exist"
        )
    body = scenario.body

    if gate is not None:
        from judge_it.queue.admission import budget_keys

        client, assistant = budget_keys(dict(body))
        decision = gate.check(client=client, assistant=assistant)
        if not decision.admitted:
            raise api_error(
                429 if decision.retryable else 503,
                "admission_refused",
                decision.reason,
            )

    transport = payload.transport or body.get("transport") or settings.call_transport

    # (1) variables: scenario's own inline vars are the floor; the scenario's
    # default set, an explicitly chosen set, and inline overrides layer on
    # with provenance, exactly as core/variables.py defines.
    default_set = _load_variable_set(session, scenario.variable_set_id)
    explicit_set = _load_variable_set(session, payload.variable_set_id)
    resolution = varlib.resolve(
        default_set=default_set,
        explicit_set=explicit_set,
        overrides=payload.vars_overrides or None,
    )
    inline_vars = dict(body.get("vars") or {})
    effective_vars = {**inline_vars, **resolution.values}
    provenance = {
        **{k: "scenario" for k in inline_vars},
        **resolution.provenance,
    }
    # Trust holds only when the reviewed set covers everything that will
    # actually render — scenario-inline additions were never reviewed with it.
    fully_covered = set(effective_vars) == set(resolution.values)
    must_check_values = resolution.needs_value_check or not fully_covered

    # (2) S4 — refuse credential-shaped values before anything is stored.
    reject_credentials(payload.vars_overrides, where="vars_overrides")
    reject_credentials(effective_vars, where="resolved vars")

    # (3) S2 — the PII value guard runs at every admission.
    if must_check_values:
        findings = pii_inspect(effective_vars)
        if is_significant(findings):
            raise api_error(
                422,
                "pii_value_detected",
                "resolved variables look like real customer data; runs must use "
                "synthetic identities (mark a reviewed set is_synthetic to attest "
                "that). Findings name the fields, values masked.",
                findings=[
                    {"field": f.field, "reason": f.reason, "masked": f.masked}
                    for f in findings
                ],
            )

    # (4) render with what will actually run — a run that cannot render is a
    # 422 at submit, never an ERROR row discovered later.
    rendered: dict[str, str] = {}
    for field_name in ("persona", "evaluation_prompt"):
        template = body.get(field_name)
        if isinstance(template, str):
            try:
                rendered[field_name] = render_sandboxed(
                    template, effective_vars, field_name
                )
            except RenderError as e:
                raise api_error(422, e.error_type, str(e), field=field_name) from e

    # (5) pre-flight — same function the /preflight endpoint answers with.
    target = None
    target_id = payload.target_id or scenario.target_id
    if target_id is not None:
        target = session.get(Target, target_id)
        if target is None:
            raise api_error(404, "target_not_found", f"target {target_id} does not exist")
    refusals = preflight_refusals(
        target,
        transport=transport,
        region=payload.region,
        settings=settings,
        acknowledge_customer_data=payload.acknowledge_customer_data,
    )
    if refusals:
        raise api_error(
            422,
            "preflight_refused",
            "pre-flight refused this run; every reason listed in `refusals`",
            refusals=refusals,
        )

    profile = resolve_profile(payload.region, settings)

    # (6) idempotency claim + run row, one transaction (see module docstring).
    key = payload.idempotency_key or f"auto-{uuid_mod.uuid4()}"
    run_uuid = uuid_mod.uuid4()
    owner_uuid, won = claim(session.connection(), key, run_uuid)
    if not won:
        existing = session.execute(
            select(Run).where(Run.run_uuid == owner_uuid)
        ).scalar_one_or_none()
        if existing is None:
            # Claim exists but its run doesn't: the claimer's transaction is
            # still in flight or was lost mid-commit. Tell the caller to retry
            # rather than minting a second run for the same key.
            raise api_error(
                409,
                "idempotency_key_in_flight",
                f"idempotency key {key!r} is claimed by a run that is not yet "
                f"visible; retry shortly",
            )
        session.rollback()  # our claim attempt changed nothing; drop the tx
        return existing, False

    run = Run(
        run_uuid=run_uuid,
        batch_id=batch_id,
        scenario_id=scenario.id,
        scenario_key=scenario.key,
        scenario_version=scenario.version,
        target_id=target_id,
        region=payload.region,
        transport=transport,
        status="created",
        vars_snapshot=effective_vars,
        config_snapshot={
            "idempotency_key": key,
            "region": payload.region,
            "transport": transport,
            "osv_base_url": profile.osv_base_url,
            "text_runtime_base_url": profile.text_runtime_base_url,
            "sip_destination_host": profile.sip_destination_host,
            "sip_trunk_number": profile.sip_trunk_number,
            "provider_config": profile.provider_config,
            "vars_provenance": provenance,
            "variable_set_id": str(resolution.source_set_id)
            if resolution.source_set_id
            else None,
            "verified_synthetic": resolution.verified_synthetic,
            # Deterministic fingerprints: body is immutable once run (below),
            # so body + vars_snapshot re-render to exactly these — the
            # explicability check a month later is one hash comparison.
            "persona_sha256": _sha(rendered.get("persona", "")),
            "evaluation_prompt_sha256": _sha(rendered.get("evaluation_prompt", "")),
        },
    )
    session.add(run)

    # (7) immutability latch — same commit as the run that depends on it.
    if scenario.first_run_at is None:
        scenario.first_run_at = datetime.now(timezone.utc)

    session.commit()
    return run, True


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
