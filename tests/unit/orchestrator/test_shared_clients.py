"""P1.3 — one LLM client and one pooled AsyncClient per worker.

Safe only because P1.2 landed first: usage travels in the LLMResult, so a
shared client has nothing mutable for two runs to race on.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from vjudge.core.config import Settings
from vjudge.core.models import Scenario
from vjudge.orchestrator.runner import TestRunner
from vjudge.transport.text import TextTransport


def make_settings(**over) -> Settings:
    base = dict(
        twilio_account_sid="ACtest", twilio_auth_token="t",
        twilio_phone_number="+15550001111", deepgram_api_key="dg",
        gemini_api_key="g",
    )
    base.update(over)
    return Settings(_env_file=None, **base)


def make_scenario(**over) -> Scenario:
    base = dict(
        name="s", target="+15550009999", vars={},
        persona_rendered="p", evaluation_prompt_rendered="e", transport="text",
        recipient_id="2015550137",  # NANP fictional range, per transport docs
    )
    base.update(over)
    return Scenario(**base)


# -------------------------------------------------------------- LLM sharing

def test_same_model_yields_the_same_client_instance(monkeypatch):
    built = []

    def fake_get_client(model, api_key, provider=None):
        built.append(model)
        return object()

    monkeypatch.setattr("vjudge.orchestrator.runner.get_client", fake_get_client)
    runner = TestRunner(make_settings())

    a = runner._shared_llm("gemini-2.5-flash", None)
    b = runner._shared_llm("gemini-2.5-flash", None)
    c = runner._shared_llm("gemini-2.5-pro", None)

    assert a is b and a is not c          # one construction per (model, provider)
    assert built == ["gemini-2.5-flash", "gemini-2.5-pro"]


# ------------------------------------------------------------- HTTP sharing

def test_worker_pool_has_explicit_limits():
    runner = TestRunner(make_settings())
    pool = runner.shared_http
    assert pool is runner.shared_http     # one per worker
    # explicit, not library-default — the PRD's requirement
    assert pool._transport._pool._max_connections == 100

    asyncio.run(runner.close())


def test_transport_never_closes_an_injected_pool():
    settings = make_settings()
    shared = httpx.AsyncClient()

    async def main():
        t1 = TextTransport(settings=settings, scenario=make_scenario(),
                           session_id="a" * 32, http=shared)
        t2 = TextTransport(settings=settings, scenario=make_scenario(),
                           session_id="b" * 32, http=shared)
        assert await t1._ensure_client() is shared
        assert await t2._ensure_client() is shared
        await t1.close()
        await t2.close()
        assert not shared.is_closed       # the pool outlives its borrowers
        await shared.aclose()

    asyncio.run(main())


def test_transport_still_closes_a_client_it_owns():
    async def main():
        t = TextTransport(settings=make_settings(), scenario=make_scenario(),
                          session_id="c" * 32)
        owned = await t._ensure_client()
        await t.close()
        assert owned.is_closed

    asyncio.run(main())


def test_runner_close_disposes_the_pool_only_if_built():
    runner = TestRunner(make_settings())
    asyncio.run(runner.close())           # nothing built → nothing to close
    assert runner._shared_http is None

    pool = runner.shared_http
    asyncio.run(runner.close())
    assert pool.is_closed
