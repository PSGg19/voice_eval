"""P1.1 — RunContext: the PRD acceptance tests, verbatim.

* a run is constructible AND executable in a unit test with an EMPTY
  environment — no env vars, no containers, no Settings object anywhere;
* two RunContexts with different targets/providers execute concurrently in
  one process without sharing any mutable state, asserted via fake providers
  recording which config they were called with.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from opkura.core.models import CallStatus, Scenario
from opkura.core.region_profile import RegionProfile
from opkura.core.run_context import Limits, Ports, RunContext
from opkura.llm.base import LLMResult
from opkura.orchestrator.runner import TestRunner


# ------------------------------------------------------------------- fakes

class FakeBotTurn:
    def __init__(self, text: str, module: str = "greeting"):
        self.text = text
        self.current_module = module
        self.tool_calls: list = []
        self.reasoning_ms = 12.0
        self.message_type = "prompt"


class FakeTextTransport:
    """The TextTransport surface the conversation loop actually touches."""

    def __init__(self, scenario: Scenario, session_id: str):
        self.scenario = scenario
        self.session_id = session_id
        self.interaction_uuid = f"fake-int-{session_id[:6]}"
        self.sent: list[dict] = []
        self.closed = False

    async def start(self) -> FakeBotTurn:
        return FakeBotTurn(f"Hello from {self.scenario.target}")

    async def send(self, speech: str = "", dtmf: str = "") -> FakeBotTurn:
        self.sent.append({"speech": speech, "dtmf": dtmf})
        return FakeBotTurn("Anything else?")

    async def close(self) -> None:
        self.closed = True


class FakeLLM:
    """Records every call's system prompt so cross-contamination is provable."""

    provider = "fake"

    def __init__(self, model: str, reply: dict):
        self.model = model
        self.reply = reply
        self.calls: list[str] = []

    async def complete_json(self, system: str, messages: list, **kw) -> LLMResult:
        self.calls.append(system)
        await asyncio.sleep(0)  # force interleaving under gather
        return LLMResult(dict(self.reply), {"input_tokens": 1, "output_tokens": 1})


class FakeStore:
    def __init__(self):
        self.saved: list = []

    async def save_result(self, result, *, scenario=None, transport=None):
        self.saved.append((result, scenario, transport))


VERDICT = {"overall_passed": True, "overall_reasoning": "ok",
           "criteria": [], "metrics": {}}
CALLER_REPLY = {"speech": "Goodbye.", "dtmf": "", "hangup": True, "barge": False}


def make_scenario(name: str, target: str) -> Scenario:
    return Scenario(
        name=name, target=target,
        vars={"who": name},
        persona_rendered=f"You are the caller for {name}.",
        evaluation_prompt_rendered=f"Judge {name}.",
        transport="text", max_turns=4,
    )


def make_ctx(scenario: Scenario, run_id: str, ports: Ports) -> RunContext:
    """Direct construction — no Settings, no environment, everything explicit."""
    return RunContext(
        run_id=run_id,
        scenario=scenario,
        target=scenario.target,
        transport="text",
        region="us",
        profile=RegionProfile(
            region="us", osv_base_url="", text_runtime_base_url="",
            sip_destination_host="", sip_trunk_number="",
        ),
        caller_model="fake-caller",
        evaluator_model="fake-eval",
        vars=scenario.vars,
        limits=Limits(max_turns=scenario.max_turns),
        ports=ports,
    )


# --------------------------------------------------------------- acceptance

def test_run_executes_with_empty_environment(tmp_path, monkeypatch):
    """No env vars, no Settings, no containers — the context carries it all."""
    monkeypatch.chdir(tmp_path)  # the per-call log folder lands here

    scenario = make_scenario("empty-env", "+15550009999")
    caller = FakeLLM("fake-caller", CALLER_REPLY)
    judge = FakeLLM("fake-eval", VERDICT)
    store = FakeStore()
    ctx = make_ctx(scenario, run_id="a" * 32, ports=Ports(
        caller_llm=caller,
        evaluator_llm=judge,
        text_transport=FakeTextTransport,
        result_store=store,
    ))

    runner = TestRunner(settings=None)
    with patch.dict(os.environ, {}, clear=True):
        result = asyncio.run(runner.run_scenario(scenario, ctx))

    assert result.status == CallStatus.COMPLETED
    assert result.passed is True
    assert result.interaction_uuid.startswith("fake-int-")
    assert caller.calls and judge.calls        # both fakes actually drove it
    [(saved, saved_scenario, saved_transport)] = store.saved
    assert saved is result and saved_transport == "text"
    # And nothing heavyweight was ever built:
    assert runner._call_mgr is None and runner._tts is None and runner._store is None


def test_two_contexts_run_concurrently_without_shared_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    scen_a = make_scenario("ctx-A", "+15550000001")
    scen_b = make_scenario("ctx-B", "+15550000002")
    caller_a, caller_b = FakeLLM("m-a", CALLER_REPLY), FakeLLM("m-b", CALLER_REPLY)
    judge_a, judge_b = FakeLLM("e-a", VERDICT), FakeLLM("e-b", VERDICT)
    store_a, store_b = FakeStore(), FakeStore()
    transports: dict[str, FakeTextTransport] = {}

    def transport_factory(bucket):
        def make(scenario, session_id):
            t = FakeTextTransport(scenario, session_id)
            transports[bucket] = t
            return t
        return make

    ctx_a = make_ctx(scen_a, run_id="a" * 32, ports=Ports(
        caller_llm=caller_a, evaluator_llm=judge_a,
        text_transport=transport_factory("A"), result_store=store_a,
    ))
    ctx_b = make_ctx(scen_b, run_id="b" * 32, ports=Ports(
        caller_llm=caller_b, evaluator_llm=judge_b,
        text_transport=transport_factory("B"), result_store=store_b,
    ))

    runner = TestRunner(settings=None)  # ONE runner, two concurrent runs

    async def main():
        return await asyncio.gather(
            runner.run_scenario(scen_a, ctx_a),
            runner.run_scenario(scen_b, ctx_b),
        )

    res_a, res_b = asyncio.run(main())

    # Each run saw exactly its own config, provider and transport:
    assert res_a.target == "+15550000001" and res_b.target == "+15550000002"
    assert transports["A"].scenario is scen_a and transports["B"].scenario is scen_b
    assert all("ctx-A" in s for s in caller_a.calls)
    assert all("ctx-B" in s for s in caller_b.calls)
    assert store_a.saved[0][0] is res_a and store_b.saved[0][0] is res_b
    assert res_a.passed is True and res_b.passed is True


def test_context_variables_are_immutable():
    scenario = make_scenario("frozen", "+15550000003")
    ctx = make_ctx(scenario, run_id="c" * 32, ports=Ports())
    with pytest.raises(TypeError):
        ctx.vars["who"] = "mutated"  # type: ignore[index]


def test_runner_without_settings_names_the_missing_piece():
    runner = TestRunner(settings=None)
    with pytest.raises(RuntimeError) as exc:
        _ = runner.call_mgr
    assert "RunContext port" in str(exc.value)
