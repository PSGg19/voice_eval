"""P6.4 — worker registration and the calls-land-where-they-dialed proof.

The addressing contract: a voice worker dials with ITS OWN public URL (its
Settings), so Twilio's TwiML fetch / status callbacks / media WS all land on
the process holding the session state. The fake-Twilio correlation test pins
that the dial's callback URLs come from the dialing worker's settings, never
from anything ambient or shared.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeaioredis

from opkura.core.config import Settings
from opkura.queue.registry import WorkerRegistry
from opkura.transport.call import CallManager


@pytest_asyncio.fixture
async def redis():
    r = fakeaioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


# ---------------------------------------------------------------- registry

@pytest.mark.asyncio
async def test_register_heartbeat_expire_deregister(redis):
    reg = WorkerRegistry(redis, ttl_s=0.08)
    await reg.register(consumer="w1", public_url="https://w1.example",
                       stages=("converse-voice",))
    row = await reg.get("w1")
    assert row["public_url"] == "https://w1.example"
    assert row["stages"] == ["converse-voice"]

    assert await reg.heartbeat("w1") is True
    await asyncio.sleep(0.12)                 # TTL lapses without heartbeat
    assert await reg.get("w1") is None        # a dead worker's row expires
    assert await reg.heartbeat("w1") is False # caller knows to re-register

    await reg.register(consumer="w1", public_url="https://w1.example",
                       stages=("converse-voice",))
    await reg.deregister("w1")
    assert await reg.get("w1") is None


@pytest.mark.asyncio
async def test_list_workers_is_the_live_fleet_view(redis):
    reg = WorkerRegistry(redis, ttl_s=5)
    await reg.register(consumer="w1", public_url="https://w1", stages=("a",))
    await reg.register(consumer="w2", public_url="https://w2", stages=("b",))
    fleet = {w["consumer"]: w["public_url"] for w in await reg.list_workers()}
    assert fleet == {"w1": "https://w1", "w2": "https://w2"}


# ------------------------------------------------- correlation (fake Twilio)

class FakeTwilio:
    """Records where each dial pointed its callbacks."""

    def __init__(self, deliveries: list):
        self.deliveries = deliveries
        self.calls = self
        self._n = 0

    def create(self, **kwargs):
        self._n += 1
        self.deliveries.append({
            "twiml_url": kwargs["url"],
            "status_url": kwargs["status_callback"],
        })
        return SimpleNamespace(sid=f"CA{self._n:04d}", _properties={})


def worker_settings(public_url: str) -> Settings:
    return Settings(
        _env_file=None,
        twilio_account_sid="ACtest", twilio_auth_token="t",
        twilio_phone_number="+15550001111", deepgram_api_key="dg",
        call_transport="pstn", public_url=public_url,
    )


@pytest.mark.asyncio
async def test_calls_land_on_the_worker_that_dialed_them(redis):
    """Two workers, each with its own callback URL: every callback of every
    call points at the worker that placed it."""
    reg = WorkerRegistry(redis, ttl_s=5)
    await reg.register(consumer="w1", public_url="https://w1.example",
                       stages=("converse-voice",))
    await reg.register(consumer="w2", public_url="https://w2.example",
                       stages=("converse-voice",))

    deliveries_by_worker: dict[str, list] = {"w1": [], "w2": []}
    managers = {}
    for consumer in ("w1", "w2"):
        row = await reg.get(consumer)
        mgr = CallManager(worker_settings(row["public_url"]))
        mgr.client = FakeTwilio(deliveries_by_worker[consumer])
        managers[consumer] = mgr

    for i in range(5):
        await managers["w1"].make_call_async("+15550002222", f"s1-{i:02d}")
        await managers["w2"].make_call_async("+15550003333", f"s2-{i:02d}")

    for consumer, url in (("w1", "https://w1.example"), ("w2", "https://w2.example")):
        assert len(deliveries_by_worker[consumer]) == 5
        for d in deliveries_by_worker[consumer]:
            assert d["twiml_url"].startswith(url + "/twiml/"), d
            assert d["status_url"].startswith(url + "/status/"), d
