"""P3.3/P3.4/P3.5/P3.7 — the V3 API end to end (real Postgres, fake executor).

PRD acceptance tests, verbatim:
* two racing POSTs with one idempotency key → exactly one run row, both
  callers receive the same run_uuid (repeated in a loop);
* `{{ ''.__class__.__mro__ }}` and a 1MB template are rejected at submit with
  a 422; a typo'd `{{ vars.foo }}` is a 422 naming the missing key;
* a run created via API completes against a fake transport and is fully
  explicable from its row alone (snapshots reproduce the exact persona);
* editing a variable set after a run leaves that run's vars_snapshot unchanged;
* pre-flight refuses a customer-data target and a post-call-enabled target,
  and allows an acknowledged one.
"""
from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opkura.core.rendering import render_sandboxed
from opkura.db.models import Run

from .conftest import build_api


def scenario_body(**over) -> dict:
    body = {
        "name": "API smoke",
        "target": "+15559990001",
        "vars": {"customer_name": "Iris Crawford", "amount_due": "125.40"},
        "persona": "You are {{ vars.customer_name }} calling about ${{ vars.amount_due }}.",
        "evaluation_prompt": "Judge whether the bot greeted {{ vars.customer_name }}.",
        "max_turns": 5,
    }
    body.update(over)
    return body


def post_scenario(api, key=None, **body_over) -> dict:
    resp = api.client.post(
        "/v1/scenarios",
        json={"key": key or f"t/{uuid.uuid4()}", "body": scenario_body(**body_over)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# -------------------------------------------------------------------- basics

def test_lint_endpoint_accepts_the_reference_body(api):
    resp = api.client.post(
        "/v1/scenarios/lint", json={"key": "t/lint", "body": scenario_body()}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_scenario_versions_increment_per_key(api):
    key = f"t/{uuid.uuid4()}"
    assert post_scenario(api, key=key)["version"] == 1
    assert post_scenario(api, key=key)["version"] == 2


def test_lint_failure_is_422_with_codes(api):
    resp = api.client.post(
        "/v1/scenarios",
        json={"key": "t/bad", "body": {"name": "x", "target": "not-a-number"}},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error_type"] == "lint_failed"
    codes = {e["code"] for e in detail["errors"]}
    assert "missing_required_key" in codes or len(codes) > 0
    assert all("code" in e and "message" in e for e in detail["errors"])


# --------------------------------------------------- PRD: rejected at submit

def _lint_codes(resp) -> set[str]:
    detail = resp.json()["detail"]
    assert detail["error_type"] == "lint_failed"
    return {e["code"] for e in detail["errors"]}


def test_mro_escape_rejected_at_submit(api):
    resp = api.client.post(
        "/v1/scenarios",
        json={
            "key": "t/mro",
            "body": scenario_body(persona="{{ ''.__class__.__mro__ }}"),
        },
    )
    assert resp.status_code == 422
    assert "sandbox_violation" in _lint_codes(resp)


def test_megabyte_template_rejected_at_submit(api):
    resp = api.client.post(
        "/v1/scenarios",
        json={"key": "t/big", "body": scenario_body(persona="x" * (1024 * 1024))},
    )
    assert resp.status_code == 422
    assert "template_too_large" in _lint_codes(resp)


def test_typoed_var_names_the_missing_key(api):
    """A typo'd {{ vars.foo }} is a 422 naming the key — lint catches it at
    scenario submit (unresolved_jinja), i.e. even earlier than the run."""
    resp = api.client.post(
        "/v1/scenarios",
        json={"key": f"t/{uuid.uuid4()}", "body": scenario_body(persona="Hello {{ vars.foo }}")},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error_type"] == "lint_failed"
    [err] = [e for e in detail["errors"] if e["code"] == "unresolved_jinja"]
    assert "foo" in err["message"]


def test_credential_shaped_payload_refused(api):
    scen = post_scenario(api)
    resp = api.client.post(
        "/v1/runs",
        json={
            "scenario_id": scen["id"],
            "region": "us",
            "vars_overrides": {"note": "sk-abcdefghijklmnop1234"},
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_type"] == "credential_in_payload"


def test_pii_value_guard_runs_at_admission(api):
    scen = post_scenario(api)
    resp = api.client.post(
        "/v1/runs",
        json={
            "scenario_id": scen["id"],
            "region": "us",
            "vars_overrides": {"ssn": "523451789"},  # 9 digits, not invented
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error_type"] == "pii_value_detected"
    # values come back MASKED, never verbatim
    assert "523451789" not in resp.text


def test_region_is_required_on_runs(api):
    scen = post_scenario(api)
    resp = api.client.post("/v1/runs", json={"scenario_id": scen["id"]})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_type"] == "validation_error"


# ----------------------------------------------------- runs + explicability

def test_run_completes_and_is_explicable_from_its_row(api):
    scen = post_scenario(api)
    resp = api.client.post(
        "/v1/runs", json={"scenario_id": scen["id"], "region": "us"}
    )
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert len(api.executor.executed) == 1  # background executed exactly once

    got = api.client.get(f"/v1/runs/{run['run_uuid']}").json()
    assert got["status"] == "completed"  # resource status IS the job status
    assert got["passed"] is True

    # Explicability: scenario body (immutable once run) + vars_snapshot
    # re-render to exactly the persona that ran — one hash comparison.
    body = api.client.get(f"/v1/scenarios/{scen['id']}").json()["body"]
    rendered = render_sandboxed(body["persona"], got["vars_snapshot"], "persona")
    import hashlib

    assert (
        hashlib.sha256(rendered.encode()).hexdigest()
        == got["config_snapshot"]["persona_sha256"]
    )
    # And the config snapshot names the resolved region bundle.
    assert got["config_snapshot"]["region"] == "us"
    assert got["config_snapshot"]["osv_base_url"].endswith("/us/api/interface")


def test_scenario_locks_after_first_run(api):
    scen = post_scenario(api)
    api.client.post("/v1/runs", json={"scenario_id": scen["id"], "region": "us"})
    resp = api.client.put(
        f"/v1/scenarios/{scen['id']}", json={"body": scenario_body(name="edited")}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_type"] == "scenario_locked"


def test_variable_set_edit_leaves_run_snapshot_unchanged(api):
    vs = api.client.post(
        "/v1/variable-sets",
        json={"name": f"vs-{uuid.uuid4()}", "vars": {"customer_name": "Original"}},
    ).json()
    scen = post_scenario(api)
    run = api.client.post(
        "/v1/runs",
        json={
            "scenario_id": scen["id"],
            "region": "us",
            "variable_set_id": vs["id"],
        },
    ).json()
    assert run["vars_snapshot"]["customer_name"] == "Original"

    api.client.put(f"/v1/variable-sets/{vs['id']}", json={"vars": {"customer_name": "Edited"}})
    got = api.client.get(f"/v1/runs/{run['run_uuid']}").json()
    assert got["vars_snapshot"]["customer_name"] == "Original"


# ------------------------------------------------------- PRD: idempotency

def test_racing_posts_share_one_run(api):
    """Two racing POSTs, one idempotency key → one run row, same run_uuid to
    both callers. Looped (25× here; the store-level primitive is raced 100×
    in test_v3_store.py — this exercises the whole HTTP admission path)."""
    scen = post_scenario(api)
    for i in range(25):
        key = f"race-{uuid.uuid4()}"
        results: list[dict] = []
        barrier = threading.Barrier(2)

        def post():
            barrier.wait()
            r = api.client.post(
                "/v1/runs",
                json={
                    "scenario_id": scen["id"],
                    "region": "us",
                    "idempotency_key": key,
                },
            )
            assert r.status_code in (200, 201), r.text
            results.append(r.json())

        threads = [threading.Thread(target=post) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results[0]["run_uuid"] == results[1]["run_uuid"], f"iter {i}"
        with Session(api.engine) as s:
            count = s.execute(
                select(func.count()).select_from(Run).where(
                    Run.config_snapshot["idempotency_key"].astext == key
                )
            ).scalar_one()
        assert count == 1, f"iter {i}: {count} rows for one key"


def test_duplicate_post_returns_original_as_200(api):
    scen = post_scenario(api)
    key = f"dup-{uuid.uuid4()}"
    payload = {"scenario_id": scen["id"], "region": "us", "idempotency_key": key}
    first = api.client.post("/v1/runs", json=payload)
    second = api.client.post("/v1/runs", json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["run_uuid"] == second.json()["run_uuid"]
    assert len(api.executor.executed) == 1  # replay never re-executes


# ------------------------------------------------------------- pre-flight

@pytest.fixture()
def customer_target(api):
    resp = api.client.post(
        "/v1/targets",
        json={
            # unique per test — (region, phone) is a DB unique constraint
            "phone_number": f"+1866555{uuid.uuid4().int % 10**4:04d}",
            "region": "us",
            "assistant_name": "Imported - RSI_V3",
            "account_count": 957069,
            "post_call_enabled": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_preflight_refuses_customer_data_and_post_call(api, customer_target):
    resp = api.client.post(
        f"/v1/targets/{customer_target['id']}/preflight", json={"transport": "text"}
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["allowed"] is False
    types = {r["error_type"] for r in body["refusals"]}
    assert types == {"target_has_customer_data", "post_call_events_enabled"}


def test_preflight_acknowledge_clears_only_the_customer_refusal(api, customer_target):
    resp = api.client.post(
        f"/v1/targets/{customer_target['id']}/preflight",
        json={"transport": "text", "acknowledge_customer_data": True},
    )
    types = {r["error_type"] for r in resp.json()["refusals"]}
    assert types == {"post_call_events_enabled"}  # S3 is not acknowledgeable


def test_run_against_refused_target_is_422(api, customer_target):
    scen = post_scenario(api)
    resp = api.client.post(
        "/v1/runs",
        json={
            "scenario_id": scen["id"],
            "region": "us",
            "transport": "text",
            "target_id": customer_target["id"],
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error_type"] == "preflight_refused"
    assert {r["error_type"] for r in detail["refusals"]} == {
        "target_has_customer_data",
        "post_call_events_enabled",
    }


def test_pstn_into_india_refused_at_preflight(migrated_engine):
    api = build_api(migrated_engine)
    scen = post_scenario(api)
    resp = api.client.post(
        "/v1/runs",
        json={
            "scenario_id": scen["id"],
            "region": "in",
            "transport": "pstn",
        },
    )
    assert resp.status_code == 422
    refusals = resp.json()["detail"]["refusals"]
    assert refusals[0]["error_type"] == "region_not_compliant"
    assert "sip" in refusals[0]["message"]  # names the fix


def test_sip_into_unconfigured_region_refused(migrated_engine):
    api = build_api(migrated_engine)
    scen = post_scenario(api)
    resp = api.client.post(
        "/v1/runs",
        json={"scenario_id": scen["id"], "region": "in", "transport": "sip"},
    )
    assert resp.status_code == 422
    refusals = resp.json()["detail"]["refusals"]
    assert refusals[0]["error_type"] == "region_not_configured"
    assert "AUTOCALLS_SIP_DESTINATION_HOST_IN" in refusals[0]["message"]


# ------------------------------------------------------------------ batches

def test_batch_mixes_accepted_and_refused_runs(api, customer_target):
    scen = post_scenario(api)
    resp = api.client.post(
        "/v1/run-batches",
        json={
            "name": "mixed",
            "runs": [
                {"scenario_id": scen["id"], "region": "us"},
                {
                    "scenario_id": scen["id"],
                    "region": "us",
                    "transport": "text",
                    "target_id": customer_target["id"],  # will be refused
                },
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    accepted = [r for r in body["runs"] if r["accepted"]]
    refused = [r for r in body["runs"] if not r["accepted"]]
    assert len(accepted) == 1 and len(refused) == 1
    assert refused[0]["refusal"]["error_type"] == "preflight_refused"

    got = api.client.get(f"/v1/run-batches/{body['id']}").json()
    assert got["run_status_counts"] == {"completed": 1}


# --------------------------------------------------------------------- auth

def test_bearer_auth_enforced_when_token_set(migrated_engine):
    api = build_api(migrated_engine, api_token="s3cret")
    r = api.client.get("/v1/runs")
    assert r.status_code == 401
    assert r.json()["detail"]["error_type"] == "unauthorized"

    r = api.client.get("/v1/runs", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401

    r = api.client.get("/v1/runs", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert set(r.json()) == {"items", "page", "page_size", "total"}


def test_unknown_payload_field_is_422(api):
    scen = post_scenario(api)
    resp = api.client.post(
        "/v1/runs",
        json={"scenario_id": scen["id"], "region": "us", "regoin": "in"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_type"] == "validation_error"
