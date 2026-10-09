"""P1.4 — Twilio off the event loop.

The PRD acceptance test, scoped to the boundary that was blocking: with 100
dials in flight against a Twilio SDK that BLOCKS 100ms per call, no event-loop
iteration exceeds 50ms. Bare (pre-P1.4) the very first dial alone would freeze
the loop for its full round trip.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from judge_it.core.config import Settings
from judge_it.transport.call import CallManager


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        twilio_account_sid="ACtest", twilio_auth_token="t",
        twilio_phone_number="+15550001111", deepgram_api_key="dg",
        call_transport="pstn", public_url="https://example.test",
    )


class BlockingFakeTwilio:
    """The sync SDK's worst behaviour, condensed: every REST call sleeps."""

    def __init__(self, block_s: float):
        self._block = block_s
        self.created = 0
        self.calls = self  # mgr.client.calls.create / mgr.client.calls(sid)

    def create(self, **kwargs):
        time.sleep(self._block)  # BLOCKING, like real twilio-python
        self.created += 1
        return SimpleNamespace(sid=f"CAfake{self.created:04d}", _properties={"sid": "x"})

    def __call__(self, call_sid):
        outer = self

        class _Call:
            def update(self, **kw):
                time.sleep(outer._block)
                return SimpleNamespace(sid=call_sid)

        return _Call()


@pytest.mark.asyncio
async def test_100_dials_in_flight_keep_loop_lag_under_50ms():
    mgr = CallManager(make_settings())
    mgr.client = BlockingFakeTwilio(block_s=0.1)

    lags: list[float] = []
    stop = asyncio.Event()

    async def lag_probe():
        interval = 0.005
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(interval)
            now = time.monotonic()
            lags.append(now - last - interval)
            last = now

    probe = asyncio.create_task(lag_probe())
    sids = await asyncio.gather(
        *(mgr.make_call_async("+15550009999", f"s{i:03d}") for i in range(100))
    )
    stop.set()
    await probe

    assert len(sids) == 100 and len(set(sids)) == 100
    assert mgr.client.created == 100
    worst = max(lags)
    assert worst < 0.05, f"event loop stalled {worst*1000:.0f}ms with dials in flight"


@pytest.mark.asyncio
async def test_hang_up_async_runs_off_loop_too():
    mgr = CallManager(make_settings())
    mgr.client = BlockingFakeTwilio(block_s=0.05)

    ticked = asyncio.Event()

    async def ticker():
        await asyncio.sleep(0.01)  # must fire DURING the blocking update
        ticked.set()

    t = asyncio.create_task(ticker())
    await mgr.hang_up_async("CAfake0001")
    await t
    assert ticked.is_set()
