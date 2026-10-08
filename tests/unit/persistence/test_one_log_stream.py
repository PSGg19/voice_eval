"""P1.5 — one log stream.

Concurrent modes drop the per-run root-logger FileHandler (each one is a
filter EVERY log record in the process must traverse, times the number of
in-flight runs) and rely on the shared structured stream instead, where
`run_id` rides on every line via `call_id_var`. The per-run debug.log
survives only for the single-run CLI case.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging

import pytest

from vjudge.connectors.logs import _JsonFormatter
from vjudge.core.log_context import call_id_var
from vjudge.core.models import Campaign, CallResult, CallStatus, Scenario
from vjudge.orchestrator.campaign_runner import CampaignRunner
from vjudge.persistence.logwriter import CallLogWriter


def _scenario(name: str = "one-stream") -> Scenario:
    return Scenario(
        name=name, target="+15550009999", vars={},
        persona_rendered="p", evaluation_prompt_rendered="e",
    )


# ----------------------------------------------------- the writer-side switch

def test_concurrent_mode_attaches_no_root_handler(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)

    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="c" * 32,
                       capture_debug_log=False)
    try:
        assert root.handlers == before      # nothing attached, nothing to race
        pointer = (lw.dir / "debug.log").read_text(encoding="utf-8")
        assert f"run_id={'c' * 32}" in pointer  # triage knows where to look
    finally:
        lw.close()
    # close() must not clobber the pointer with the empty-file stub
    assert f"run_id={'c' * 32}" in (lw.dir / "debug.log").read_text(encoding="utf-8")
    assert root.handlers == before


def test_single_run_mode_still_captures_a_per_run_file(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)

    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="d" * 32)
    try:
        assert len(root.handlers) == len(before) + 1
        token = call_id_var.set("d" * 32)
        try:
            src = logging.getLogger("vjudge.test.p15")
            src.setLevel(logging.INFO)     # root defaults to WARNING in tests
            src.info("hello single-run")
        finally:
            call_id_var.reset(token)
    finally:
        lw.close()

    assert root.handlers == before          # detached on close
    assert "hello single-run" in (lw.dir / "debug.log").read_text(encoding="utf-8")


# ------------------------------------------------ campaign flips the switch

class _FlagRunner:
    """Minimal fake: records the flag value seen while scenarios ran."""

    def __init__(self):
        self.per_run_debug_log = True       # the TestRunner default
        self.seen: list[bool] = []

    async def run_scenario_file(self, path: str) -> CallResult:
        self.seen.append(self.per_run_debug_log)
        return CallResult(id="x", flow_name=path, target="+15550009999",
                          status=CallStatus.COMPLETED)


def _campaign(paths, max_concurrency):
    return Campaign(name="c", scenarios=paths, resolved_scenarios=paths,
                    max_concurrency=max_concurrency)


def test_campaign_workers_gt_1_disables_per_run_files():
    runner = _FlagRunner()
    asyncio.run(CampaignRunner(runner).run(_campaign(["a.yaml", "b.yaml"], 2)))
    assert runner.seen == [False, False]


def test_campaign_at_concurrency_1_is_the_single_run_case():
    runner = _FlagRunner()
    asyncio.run(CampaignRunner(runner).run(_campaign(["a.yaml"], 1)))
    assert runner.seen == [True]


# ---------------------------------------------------- the PRD acceptance test

@pytest.mark.asyncio
async def test_20_concurrent_runs_share_one_stream_without_corruption():
    """Concurrency 20: no interleaved-line corruption, every line carries
    the correct run_id — the Phase 1 acceptance wording, verbatim."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(_JsonFormatter())     # the LOG_SINK=json formatter
    stream_logger = logging.getLogger("vjudge.test.p15.stream")
    stream_logger.setLevel(logging.INFO)
    stream_logger.propagate = False            # keep test output clean
    stream_logger.addHandler(handler)

    lines_per_run = 25

    async def one_run(run_id: str):
        token = call_id_var.set(run_id)
        try:
            for i in range(lines_per_run):
                stream_logger.info("run %s line %d", run_id, i)
                await asyncio.sleep(0)         # force task interleaving
        finally:
            call_id_var.reset(token)

    try:
        await asyncio.gather(*(one_run(f"run-{i:02d}") for i in range(20)))
    finally:
        stream_logger.removeHandler(handler)

    lines = [line for line in buf.getvalue().splitlines() if line]
    assert len(lines) == 20 * lines_per_run
    for line in lines:
        record = json.loads(line)              # corruption would fail to parse
        # the run named INSIDE the message must match the run_id field —
        # a contextvar leak between tasks would break this pairing
        assert record["msg"].startswith(f"run {record['run_id']} line ")
