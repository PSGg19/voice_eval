"""P1.6 — wall-clock cap on text runs.

The PRD acceptance test: a text run against a hanging fake server terminates
at the deadline with status ERROR — not never. Before this, only voice had an
overall deadline; a wedged text run held its worker slot forever (the 90s
per-request read timeout bounds one turn, not a run that trickles).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from vjudge.core.config import Settings
from vjudge.core.models import CallStatus, Scenario
from vjudge.core.region_profile import RegionProfile
from vjudge.core.run_context import Limits, Ports, RunContext
from vjudge.engine.caller import DEFAULT_BOT_TURN_TIMEOUT_MS
from vjudge.llm.base import LLMResult
from vjudge.orchestrator.runner import TestRunner


class _Turn:
    def __init__(self, text: str):
        self.text = text
        self.current_module = "greeting"
        self.tool_calls: list = []
        self.reasoning_ms = 1.0
        self.message_type = "prompt"


class HangingTextTransport:
    """A server that answers the first turn, then never again."""

    def __init__(self, scenario: Scenario, session_id: str):
        self.scenario = scenario
        self.session_id = session_id
        self.interaction_uuid = "hang-int-000001"
        self.closed = False

    async def start(self) -> _Turn:
        return _Turn("Hello, how can I help?")

    async def send(self, speech: str = "", dtmf: str = "") -> _Turn:
        await asyncio.sleep(3600)          # the hang under test
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


class _KeepTalkingLLM:
    """Caller that never hangs up — the run can only end via the deadline."""

    provider = "fake"
    model = "fake-caller"

    async def complete_json(self, system: str, messages: list, **kw) -> LLMResult:
        return LLMResult(
            {"speech": "Tell me more.", "dtmf": "", "hangup": False, "barge": False},
            {"input_tokens": 1, "output_tokens": 1},
        )


class _Store:
    def __init__(self):
        self.saved: list = []

    async def save_result(self, result, *, scenario=None, transport=None):
        self.saved.append(result)


def _scenario() -> Scenario:
    return Scenario(
        name="hang", target="+15550009999", vars={},
        persona_rendered="p", evaluation_prompt_rendered="e",
        transport="text", max_turns=5,
    )


def _ctx(scenario: Scenario, deadline_s: float, store: _Store) -> RunContext:
    return RunContext(
        run_id="f" * 32,
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
        limits=Limits(max_turns=scenario.max_turns, run_deadline_s=deadline_s),
        ports=Ports(
            caller_llm=_KeepTalkingLLM(),
            evaluator_llm=_KeepTalkingLLM(),   # never reached; deadline fires first
            text_transport=HangingTextTransport,
            result_store=store,
        ),
        skip_evaluation=True,
    )


def test_hanging_text_run_dies_at_the_deadline_with_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)               # log folder lands here
    scenario = _scenario()
    store = _Store()
    ctx = _ctx(scenario, deadline_s=0.3, store=store)
    runner = TestRunner(settings=None)

    t0 = time.monotonic()
    result = asyncio.run(runner.run_scenario(scenario, ctx))
    elapsed = time.monotonic() - t0

    assert result.status == CallStatus.ERROR
    assert "deadline" in (result.error or "").lower()
    assert elapsed < 5.0                       # at the deadline, not at 3600s
    assert store.saved and store.saved[0] is result   # still persisted
    # NOTE: transport.close() still ran — the finally block survives the
    # wait_for cancellation — but the factory-made instance isn't reachable
    # here; the persisted ERROR result is the observable contract.


def test_from_settings_resolves_the_voice_deadline_formula():
    settings = Settings(
        _env_file=None,
        twilio_account_sid="ACtest", twilio_auth_token="t",
        twilio_phone_number="+15550001111", deepgram_api_key="dg",
    )
    ctx = RunContext.from_settings(_scenario(), settings)
    expected = 5 * (DEFAULT_BOT_TURN_TIMEOUT_MS / 1000) + 30
    assert ctx.limits.run_deadline_s == expected
