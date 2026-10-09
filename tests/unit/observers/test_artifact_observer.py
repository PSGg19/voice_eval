"""P5.3 — the artifact observer: accumulate, write once at settle."""
from __future__ import annotations

import json

import pytest

from judge_it.observers.artifact_observer import (
    ArtifactObserver,
    run_summary_key,
    transcript_key,
)
from judge_it.observers.events import (
    BotTurn,
    CallerTurn,
    Generation,
    RunFinished,
    RunStarted,
)


class DictStore:
    def __init__(self):
        self.data = {}
        self.puts = 0

    def put_bytes(self, key, data, *, content_type="application/json"):
        self.puts += 1
        self.data[key] = data
        return key

    def get_bytes(self, key):
        return self.data[key]


def drive(obs, *, passed=True):
    obs.on_event(RunStarted(run_id="r42", scenario_name="s", target="+1",
                            region="in", transport="sip", at_ns=100))
    obs.on_event(BotTurn(turn_index=1, text="hello", module="greet",
                         latency_ms=3.0, at_ns=200))
    obs.on_event(CallerTurn(turn_index=1, text="hi", at_ns=300))
    obs.on_event(Generation(role="caller", model="m", prompt_tokens=5,
                            completion_tokens=2, started_ns=210, ended_ns=290))
    obs.on_event(RunFinished(status="completed", passed=passed,
                             interaction_uuid="iu", at_ns=400))


def test_writes_exactly_two_documents_at_settle():
    store = DictStore()
    obs = ArtifactObserver(store)
    obs.on_event(RunStarted(run_id="r42", scenario_name="s", target="+1",
                            region="in", transport="sip", at_ns=100))
    obs.on_event(BotTurn(turn_index=1, text="hello", at_ns=200))
    assert store.puts == 0          # nothing until the run settles
    obs.on_event(RunFinished(status="completed", passed=True, at_ns=300))
    assert store.puts == 2


def test_transcript_and_summary_content():
    store = DictStore()
    drive(ArtifactObserver(store))

    turns = json.loads(store.get_bytes(transcript_key("r42")))
    assert [(t["speaker"], t["text"]) for t in turns] == [
        ("bot", "hello"), ("caller", "hi"),
    ]
    assert turns[0]["module"] == "greet"

    summary = json.loads(store.get_bytes(run_summary_key("r42")))
    assert summary["status"] == "completed" and summary["passed"] is True
    assert summary["region"] == "in" and summary["transport"] == "sip"
    assert summary["turns"] == 2
    [gen] = summary["llm_calls"]
    assert gen["model"] == "m" and gen["prompt_tokens"] == 5
    assert gen["duration_ms"] == pytest.approx((290 - 210) / 1e6)  # ns → ms, exact


def test_keys_are_deterministic_from_run_id():
    assert transcript_key("abc") == "runs/abc/transcript.json"
    assert run_summary_key("abc") == "runs/abc/run.json"


def test_next_run_does_not_inherit_previous_turns():
    store = DictStore()
    obs = ArtifactObserver(store)
    drive(obs)
    obs.on_event(RunStarted(run_id="r43", scenario_name="s2", target="+1",
                            region="us", transport="text", at_ns=500))
    obs.on_event(RunFinished(status="failed", passed=False, at_ns=600))
    turns = json.loads(store.get_bytes(transcript_key("r43")))
    assert turns == []
