"""P2.6 — the legacy V1 decision, pinned so it is decided, not rediscovered.

Decision: the sync FlowEngine is RETAINED AND FROZEN for `vjudge load` /
`vjudge watch`; a V1 `steps:` file run conversationally still works but warns
that it is deprecated. FlowEngine never learns frames.
"""
from __future__ import annotations

import logging

import pytest

from vjudge.orchestrator.runner import TestRunner, detect_schema


V1_FLOW = """
name: legacy-check
target: "+15550009999"
steps:
  - id: greet
    expect:
      any_of: ["hello"]
    say: "hi"
    timeout_ms: 1000
"""


def test_v1_schema_still_detected(tmp_path):
    p = tmp_path / "flow.yaml"
    p.write_text(V1_FLOW, encoding="utf-8")
    assert detect_schema(p) == "v1"


@pytest.mark.asyncio
async def test_v1_conversational_run_warns_deprecated(tmp_path, caplog, monkeypatch):
    p = tmp_path / "flow.yaml"
    p.write_text(V1_FLOW, encoding="utf-8")

    runner = TestRunner(settings=None)

    async def fake_run_flow_file(path):
        return "ran-v1"

    monkeypatch.setattr(runner, "run_flow_file", fake_run_flow_file)
    with caplog.at_level(logging.WARNING, logger="vjudge.orchestrator.runner"):
        result = await runner.run_scenario_file(p)

    assert result == "ran-v1"                      # V1 still runs (the shim)
    assert any("DEPRECATED" in r.message for r in caplog.records)


def test_flow_engine_has_not_learned_frames():
    """FROZEN means frozen: the legacy engine must not import the frame
    vocabulary or the conversation engine — its removal path is the load-test
    migration, not a quiet frame integration."""
    import subprocess
    import sys

    code = (
        "import sys; import vjudge.legacy.flow; "
        "bad = [m for m in sys.modules if m in "
        "('vjudge.frames', 'vjudge.engine.conversation')]; "
        "assert not bad, f'legacy FlowEngine now imports {bad}'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
