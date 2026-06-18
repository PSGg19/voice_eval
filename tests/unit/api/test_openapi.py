"""P3.3 exit — the OpenAPI schema generates without a database and carries
every route family. The TS-client compile check lives in
scripts/generate_ts_client.sh (needs node); this pins what it consumes."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "export_openapi", Path(__file__).parents[3] / "scripts" / "export_openapi.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_schema_generates_with_all_route_families():
    schema = _mod.build_schema()
    paths = set(schema["paths"])
    assert {
        "/v1/runs",
        "/v1/runs/{run_uuid}",
        "/v1/run-batches",
        "/v1/scenarios",
        "/v1/scenarios/lint",
        "/v1/variable-sets",
        "/v1/targets",
        "/v1/targets/{target_id}/preflight",
    } <= paths


def test_run_creation_is_documented_with_required_region():
    schema = _mod.build_schema()
    run_in = schema["components"]["schemas"]["RunIn"]
    assert "region" in run_in["required"]          # P3.8, visible in the contract
    assert run_in["additionalProperties"] is False  # typo'd fields are 422s
