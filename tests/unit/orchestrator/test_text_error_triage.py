"""Regression (found live, 2026-08-31 Veros probe): an errored text run must
keep its triage state — interaction_uuid, module path, partial transcript,
tool calls. The frame rewire briefly copied these only on the success path, so
a turn-N runtime hang shipped an ERROR row with modules_visited=[] while the
engine knew better.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from vjudge.core.models import CallStatus, Scenario
from vjudge.core.region_profile import RegionProfile
from vjudge.core.run_context import Limits, Ports, RunContext
from vjudge.llm.base import LLMResult
from vjudge.orchestrator.runner import TestRunner
from vjudge.transport.text import TextTransportError


class FakeBotTurn:
    def __init__(self, text: str, module: str):
        self.text = text
        self.current_module = module
        self.tool_calls = [{"name": "check_rmn_temp"}]
        self.reasoning_ms = 10.0
        self.message_type = "RECORD"


class HangThenDieTransport:
    """First turn works; the second POST dies like a hung runtime read."""

    def __init__(self, scenario: Scenario, session_id: str):
        self.scenario = scenario
        self.session_id = session_id
        self.interaction_uuid = "int-triage-0001"
        self.closed = False

    async def start(self) -> FakeBotTurn:
        return FakeBotTurn("Hello, may I know your name?", "GREETINGS")

    async def send(self, speech: str = "", dtmf: str = "") -> FakeBotTurn:
        raise TextTransportError("text runtime did not answer within 90s (turn 2)")

    async def close(self) -> None:
        self.closed = True


class ChattyLLM:
    provider = "fake"
    model = "fake-caller"

    async def complete_json(self, system, messages, **kw) -> LLMResult:
        return LLMResult(
            {"speech": "Casey Italia.", "dtmf": "", "hangup": False, "barge": False},
            {"input_tokens": 1, "output_tokens": 1},
        )


class Store:
    def __init__(self):
        self.saved = []

    async def save_result(self, result, *, scenario=None, transport=None):
        self.saved.append(result)


def test_errored_text_run_keeps_triage_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scenario = Scenario(
        name="triage", target="+15550009999", vars={},
        persona_rendered="p", evaluation_prompt_rendered="e",
        transport="text", max_turns=5,
    )
    ctx = RunContext(
        run_id="e" * 32, scenario=scenario, target=scenario.target,
        transport="text", region="us",
        profile=RegionProfile(region="us", osv_base_url="",
                              text_runtime_base_url="",
                              sip_destination_host="", sip_trunk_number=""),
        caller_model="fake", evaluator_model="fake",
        limits=Limits(max_turns=5, run_deadline_s=30),
        ports=Ports(caller_llm=ChattyLLM(), evaluator_llm=ChattyLLM(),
                    text_transport=HangThenDieTransport, result_store=Store()),
        skip_evaluation=True,
    )

    runner = TestRunner(settings=None)
    with patch.dict(os.environ, {}, clear=True):
        result = asyncio.run(runner.run_scenario(scenario, ctx))

    assert result.status == CallStatus.ERROR
    assert "text transport failed" in (result.error or "")
    # The triage state survived the mid-run failure:
    assert result.interaction_uuid == "int-triage-0001"
    assert result.modules_visited == ["GREETINGS"]
    assert [t["name"] for t in result.tool_calls] == ["check_rmn_temp"]
    speakers = [e.speaker for e in result.transcript]
    assert speakers == ["bot", "caller"]      # the partial transcript
