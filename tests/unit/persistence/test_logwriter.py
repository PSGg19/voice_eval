from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from opkura.persistence.logwriter import CallLogWriter, slugify
from opkura.core.models import (
    CallResult,
    CallStatus,
    EvaluationCriterion,
    EvaluationResult,
    LLMConfig,
    Scenario,
    TranscriptEntry,
)


def _scenario(name: str = "Resurgent · happy path · debit") -> Scenario:
    return Scenario(
        name=name,
        target="+18005551234",
        vars={"name": "Iris"},
        persona_rendered="You are Iris.",
        evaluation_prompt_rendered="Judge the call.",
        llm=LLMConfig(caller_model="gemini-2.5-flash", evaluator_model="gemini-2.5-pro"),
    )


def _transcript() -> list[TranscriptEntry]:
    return [
        TranscriptEntry(speaker="bot", text="Hello, Michelle here.", timestamp_ms=500),
        TranscriptEntry(speaker="caller", text="Hi, this is Iris.", timestamp_ms=2300),
        TranscriptEntry(speaker="caller", text="[DTMF: 1]", timestamp_ms=4800, dtmf="1"),
    ]


def test_slugify():
    assert slugify("Resurgent · happy path · debit") == "resurgent-happy-path-debit"
    assert slugify("  multiple   spaces ") == "multiple-spaces"
    assert slugify("Über@FOO!") == "uber-foo"
    assert slugify("") == ""


def test_folder_naming(tmp_path):
    lw = CallLogWriter(root=tmp_path, scenario=_scenario("Foo Bar"), call_id="abcdef1234567890")
    assert lw.dir.parent == tmp_path
    assert lw.dir.exists()
    # Second-resolution timestamp (HHMMSS) so same-minute reruns sort
    # chronologically by name. UUID suffix still tiebreaks same-second.
    assert re.match(r"call-foo-bar-\d{4}-\d{2}-\d{2}T\d{6}Z-[a-f0-9]{8}", lw.dir.name)
    assert lw.dir.name.endswith("-abcdef12")
    lw.close()


def test_folder_naming_disambiguates_same_minute_calls(tmp_path):
    """Two calls with the same scenario in the same UTC minute must NOT
    collide (run_repeated, load tests, manual retries).
    """
    lw1 = CallLogWriter(root=tmp_path, scenario=_scenario("foo"), call_id="aaaa" * 8)
    lw2 = CallLogWriter(root=tmp_path, scenario=_scenario("foo"), call_id="bbbb" * 8)
    assert lw1.dir != lw2.dir
    lw1.close()
    lw2.close()


def test_snapshots_written(tmp_path):
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    assert (lw.dir / "scenario.yaml").exists()
    assert (lw.dir / "rendered_persona.txt").read_text() == "You are Iris."
    assert (lw.dir / "rendered_eval.txt").read_text() == "Judge the call."
    assert (lw.dir / "debug.log").exists()
    lw.close()


def test_record_llm_call_accumulates(tmp_path):
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    for i in range(3):
        lw.record_llm_call(
            turn=i + 1,
            role="caller",
            input_messages=[{"role": "user", "content": f"turn {i+1}"}],
            output={"speech": f"reply {i+1}"},
            latency_ms=100.0 * (i + 1),
            input_tokens=10,
            output_tokens=5,
        )
    lw.close()

    entries = json.loads((lw.dir / "llm_calls.json").read_text())
    assert len(entries) == 3
    assert entries[0]["turn"] == 1
    assert entries[2]["latency_ms"] == 300.0
    assert entries[0]["input_tokens"] == 10
    assert entries[0]["role"] == "caller"


def test_write_transcript_json_and_txt(tmp_path):
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    lw.write_transcript(_transcript())
    lw.close()

    entries = json.loads((lw.dir / "transcript.json").read_text())
    assert len(entries) == 3
    assert entries[0]["speaker"] == "bot"
    assert entries[2]["dtmf"] == "1"

    txt = (lw.dir / "transcript.txt").read_text().strip().splitlines()
    assert len(txt) == 3
    assert txt[0].startswith("[+0.5s] BOT:")
    assert txt[1].startswith("[+2.3s] CALLER:")
    assert "Iris" in txt[1]


def test_write_evaluation(tmp_path):
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    ev = EvaluationResult(
        overall_passed=True,
        overall_reasoning="all good",
        criteria=[EvaluationCriterion(id=1, name="greeting", passed=True, evidence="turn 1")],
        metrics={"bot_turns": 5},
    )
    lw.write_evaluation(ev)
    lw.close()

    payload = json.loads((lw.dir / "evaluation.json").read_text())
    assert payload["overall_passed"] is True
    assert payload["overall_reasoning"] == "all good"
    assert payload["criteria"][0]["name"] == "greeting"
    assert payload["metrics"] == {"bot_turns": 5}


def test_metadata_has_required_keys(tmp_path):
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    lw.record_llm_call(
        turn=1, role="caller", input_messages=[], output={}, latency_ms=120.0,
        input_tokens=10, output_tokens=20,
    )
    lw.record_llm_call(
        turn=2, role="caller", input_messages=[], output={}, latency_ms=200.0,
        input_tokens=15, output_tokens=25,
    )
    result = CallResult(
        flow_name="t",
        target="+1",
        status=CallStatus.COMPLETED,
        call_sid="CA123",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        evaluation=EvaluationResult(overall_passed=True, overall_reasoning="ok"),
    )
    lw.write_metadata(
        result=result,
        llm_models={"caller": "gemini-2.5-flash", "evaluator": "gemini-2.5-pro"},
        duration_ms=5500.0,
    )
    lw.close()

    meta = json.loads((lw.dir / "metadata.json").read_text())
    for key in (
        "call_sid", "status", "started_at", "ended_at", "overall_passed",
        "duration_ms", "llm_models", "llm_tokens_used",
        "p50_caller_llm_ms", "p95_caller_llm_ms",
        "max_caller_llm_ms",
        "bot_response_latencies_ms",
        "bot_response_p50_ms", "bot_response_p95_ms", "bot_response_max_ms",
    ):
        assert key in meta, f"missing key {key}"
    assert meta["call_sid"] == "CA123"
    assert meta["status"] == "completed"
    assert meta["overall_passed"] is True
    assert meta["llm_models"] == {"caller": "gemini-2.5-flash", "evaluator": "gemini-2.5-pro"}
    assert meta["llm_tokens_used"] == 10 + 20 + 15 + 25
    assert meta["max_caller_llm_ms"] == 200.0


def test_latency_aggregates_null_when_no_caller_calls(tmp_path):
    """Task 7 / F8: per-CHECKLIST, the p50/p95/max keys must be `null` when
    no caller LLM calls were recorded (e.g. setup error before dialing).
    """
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    failed = CallResult(
        flow_name="t",
        target="+1",
        status=CallStatus.ERROR,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
    )
    lw.write_metadata(
        result=failed,
        llm_models={"caller": "gemini-2.5-flash"},
        duration_ms=10.0,
    )
    lw.close()
    meta = json.loads((lw.dir / "metadata.json").read_text())
    assert meta["p50_caller_llm_ms"] is None
    assert meta["p95_caller_llm_ms"] is None
    assert meta["max_caller_llm_ms"] is None


def test_close_idempotent(tmp_path):
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    lw.close()
    lw.close()  # must not raise
    assert (lw.dir / "llm_calls.json").exists()


def test_failed_call_still_writes_folder(tmp_path):
    """A call that errored before talking still produces the required scaffolding."""
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")

    # Simulate runner finalization on a failed call: empty transcript, error status.
    lw.write_transcript([])
    failed_result = CallResult(
        flow_name="t",
        target="+1",
        status=CallStatus.ERROR,
        error="Twilio dial failed",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
    )
    lw.write_metadata(
        result=failed_result,
        llm_models={"caller": "gemini-2.5-flash", "evaluator": "gemini-2.5-pro"},
        duration_ms=120.0,
    )
    lw.close()

    # All required files (per spec) must exist — close() stubs any that
    # weren't written explicitly. bot.wav / caller.wav are excluded (empty
    # bytes aren't valid WAV).
    for required in (
        "scenario.yaml",
        "rendered_persona.txt",
        "rendered_eval.txt",
        "metadata.json",
        "transcript.json",
        "transcript.txt",
        "evaluation.json",   # stubbed by close() since write_evaluation was never called
        "llm_calls.json",
        "debug.log",
    ):
        assert (lw.dir / required).exists(), f"missing {required}"

    transcript = json.loads((lw.dir / "transcript.json").read_text())
    assert transcript == []

    meta = json.loads((lw.dir / "metadata.json").read_text())
    assert meta["status"] == "error"
    assert meta["overall_passed"] is None

    # Stubbed evaluation.json is parseable as JSON.
    assert json.loads((lw.dir / "evaluation.json").read_text()) == {}


def test_record_llm_call_flushes_incrementally(tmp_path):
    """llm_calls.json must be readable on disk after each record_llm_call,
    not only after close() — otherwise a runner crash loses the history.
    """
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    lw.record_llm_call(
        turn=1, role="caller", input_messages=[], output={"x": 1}, latency_ms=10.0,
    )
    entries = json.loads((lw.dir / "llm_calls.json").read_text())
    assert len(entries) == 1
    lw.record_llm_call(
        turn=2, role="caller", input_messages=[], output={"x": 2}, latency_ms=20.0,
    )
    entries = json.loads((lw.dir / "llm_calls.json").read_text())
    assert len(entries) == 2
    lw.close()


def test_bot_response_latency_aggregated_in_metadata(tmp_path):
    """bot_response_latencies_ms must be summarized as p50/p95/max in
    metadata.json — same shape as caller-LLM latency.
    """
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    for ms in (100.0, 200.0, 300.0, 400.0, 500.0):
        lw.record_bot_response_latency(ms)

    result = CallResult(
        flow_name="t",
        target="+1",
        status=CallStatus.COMPLETED,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
    )
    lw.write_metadata(
        result=result,
        llm_models={"caller": "x", "evaluator": "y"},
        duration_ms=1000.0,
    )
    lw.close()
    meta = json.loads((lw.dir / "metadata.json").read_text())
    assert meta["bot_response_max_ms"] == 500.0
    assert meta["bot_response_p50_ms"] is not None
    assert meta["bot_response_p95_ms"] is not None


def test_write_evaluation_none_is_noop(tmp_path):
    """Defensive: write_evaluation(None) doesn't crash and doesn't write a file."""
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    lw.write_evaluation(None)  # type: ignore[arg-type]
    # close() will then stub evaluation.json with {} since none was written.
    lw.close()
    assert json.loads((lw.dir / "evaluation.json").read_text()) == {}


def test_close_stubs_list_files_as_arrays(tmp_path):
    """transcript.json and llm_calls.json must be stubbed as [] (not {}),
    so downstream consumers iterating with entries[0] don't crash on a dict.
    """
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    # Don't call write_transcript or record_llm_call — let close() stub them.
    lw.close()
    assert json.loads((lw.dir / "transcript.json").read_text()) == []
    assert json.loads((lw.dir / "llm_calls.json").read_text()) == []
    # Object-typed files stay {}.
    assert json.loads((lw.dir / "evaluation.json").read_text()) == {}
    assert json.loads((lw.dir / "metadata.json").read_text()) == {}


def test_record_llm_call_with_error_field(tmp_path):
    """Failed LLM calls record an `error` field separate from output;
    output stays None instead of being polluted by a synthetic {'error': ...}.
    """
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    lw.record_llm_call(
        turn=1,
        role="caller",
        input_messages=[{"role": "user", "content": "x"}],
        output=None,
        latency_ms=50.0,
        error="ValueError('non-JSON')",
    )
    lw.close()
    entry = json.loads((lw.dir / "llm_calls.json").read_text())[0]
    assert entry["output"] is None
    assert entry["error"] == "ValueError('non-JSON')"


def test_record_bot_response_latency(tmp_path):
    lw = CallLogWriter(root=tmp_path, scenario=_scenario(), call_id="abc")
    lw.record_bot_response_latency(450.0)
    lw.record_bot_response_latency(800.0)
    result = CallResult(
        flow_name="t",
        target="+1",
        status=CallStatus.COMPLETED,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
    )
    lw.write_metadata(
        result=result,
        llm_models={"caller": "x", "evaluator": "y"},
        duration_ms=1000.0,
    )
    lw.close()
    meta = json.loads((lw.dir / "metadata.json").read_text())
    assert meta["bot_response_latencies_ms"] == [450.0, 800.0]
