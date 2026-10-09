"""Phase 2 Task 2 — verify concurrent-writer log isolation.

Without the `_CallIdFilter`, every per-call `FileHandler` attached to
root would capture every concurrent call's log records, corrupting
each `debug.log` with the others' noise. These tests prove that
under N concurrent `CallLogWriter` instances, each `debug.log`
contains ONLY records emitted in its own `call_id_var` context.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from vjudge.core.log_context import call_id_var
from vjudge.core.models import LLMConfig, STTConfig, Scenario
from vjudge.persistence.logwriter import CallLogWriter

# Module-level logger so spawned tasks share the same logger object —
# mimicking how real vjudge modules emit (`logging.getLogger(__name__)`).
# Set DEBUG explicitly so info() records are actually emitted — in
# production this is done via logging.basicConfig(level=INFO) in
# cli/main.py, but the test process has no such global config.
_TEST_LOGGER = logging.getLogger("vjudge.test.logwriter_concurrent")
_TEST_LOGGER.setLevel(logging.DEBUG)


def _make_scenario(name: str) -> Scenario:
    return Scenario(
        name=name,
        target="+15555550100",
        vars={},
        persona_rendered="test persona",
        evaluation_prompt_rendered="test eval",
        llm=LLMConfig(),
        stt=STTConfig(),
    )


@pytest.mark.asyncio
async def test_concurrent_writers_isolate_log_records(tmp_path: Path):
    """3 concurrent writers + 3 concurrent log-emitting tasks: each
    debug.log must contain ONLY its own call's records."""
    writers: list[CallLogWriter] = []
    try:
        # Build writers BEFORE the tasks so the handlers are attached
        # and the filter is in place when records start flowing.
        for i in range(3):
            scenario = _make_scenario(f"call-{i}")
            writer = CallLogWriter(
                root=tmp_path / f"root-{i}",
                scenario=scenario,
                call_id=f"calluuid-{i}",
            )
            writers.append(writer)

        async def emit_records(call_id: str, idx: int, n: int = 5):
            """Set the contextvar, emit N records, then leave."""
            token = call_id_var.set(call_id)
            try:
                for k in range(n):
                    _TEST_LOGGER.info("call=%s msg=%d-%d", call_id, idx, k)
                    # Yield so other tasks interleave.
                    await asyncio.sleep(0)
            finally:
                call_id_var.reset(token)

        # Spawn 3 emit tasks in parallel — they interleave via the
        # asyncio.sleep(0) yields.
        await asyncio.gather(
            *[
                emit_records(f"calluuid-{i}", idx=i, n=10)
                for i in range(3)
            ]
        )

        # Force handlers to flush before reading.
        for w in writers:
            if w._log_handler:
                w._log_handler.flush()

        # Each debug.log must contain ONLY its own call's records.
        for i, writer in enumerate(writers):
            content = (writer.dir / "debug.log").read_text()
            own_marker = f"call=calluuid-{i}"
            assert own_marker in content, (
                f"writer {i} debug.log missing its own records.\n"
                f"--- content ---\n{content}"
            )
            for j in range(3):
                if j == i:
                    continue
                other_marker = f"call=calluuid-{j}"
                assert other_marker not in content, (
                    f"writer {i} debug.log leaked records from call-{j}.\n"
                    f"--- content ---\n{content}"
                )
    finally:
        for w in writers:
            w.close()


@pytest.mark.asyncio
async def test_records_emitted_outside_call_context_dont_leak(tmp_path: Path):
    """When `call_id_var` is None (e.g., uvicorn startup, test setup),
    no per-call debug.log should capture the record."""
    scenario = _make_scenario("ctx-test")
    writer = CallLogWriter(
        root=tmp_path / "root",
        scenario=scenario,
        call_id="calluuid-X",
    )
    try:
        # No call_id_var.set() here — emission happens in default context.
        _TEST_LOGGER.info("orphan record not belonging to any call")

        # Flush + read.
        if writer._log_handler:
            writer._log_handler.flush()
        content = (writer.dir / "debug.log").read_text()
        assert "orphan record" not in content, (
            f"writer captured a record emitted outside any call context:\n{content}"
        )
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_records_from_spawned_subtask_inherit_call_id(tmp_path: Path):
    """asyncio.create_task copies the current context — subtasks
    spawned within a call's scope must inherit the call_id_var so
    their log records still land in the right debug.log."""
    scenario = _make_scenario("subtask-test")
    writer = CallLogWriter(
        root=tmp_path / "root",
        scenario=scenario,
        call_id="calluuid-S",
    )
    try:

        async def subtask_emit():
            # Note: this task does NOT call call_id_var.set() — it
            # relies on inheriting from the parent's context at
            # create_task() time.
            _TEST_LOGGER.info("subtask record under inherited context")
            await asyncio.sleep(0)

        async def parent():
            token = call_id_var.set("calluuid-S")
            try:
                # Spawn the subtask within this call's context.
                task = asyncio.create_task(subtask_emit())
                await task
            finally:
                call_id_var.reset(token)

        await parent()

        if writer._log_handler:
            writer._log_handler.flush()
        content = (writer.dir / "debug.log").read_text()
        assert "subtask record under inherited context" in content, (
            f"subtask record was rejected despite inherited context:\n{content}"
        )
    finally:
        writer.close()
