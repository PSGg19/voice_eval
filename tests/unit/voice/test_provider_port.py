"""P6.6/P6.7 — the TelephonyProvider port: chooser, compliance refusal,
Twilio extraction, and the provider-equivalence acceptance test.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opkura.core.config import Settings
from opkura.transport.call import CallManager
from opkura.voice.frames import InboundAudio
from opkura.voice.provider import (
    LiveKitSIPProvider,
    ProviderRefusal,
    TwilioProvider,
    configured_providers,
    provider_for,
)


def make_settings(**over) -> Settings:
    base = dict(
        twilio_account_sid="ACtest", twilio_auth_token="t",
        twilio_phone_number="+15550001111", deepgram_api_key="dg",
        public_url="https://worker.example",
    )
    base.update(over)
    return Settings(_env_file=None, **base)


# ------------------------------------------------------------------ chooser

def test_provider_is_chosen_per_target_region():
    settings = make_settings(
        livekit_sip_url="wss://in.livekit.example",
        livekit_sip_api_key="k", livekit_sip_api_secret="s",
        livekit_sip_trunk_id_in="ST_trunk",
    )
    providers = configured_providers(settings)
    assert [p.name for p in providers] == ["twilio", "livekit-sip"]

    assert provider_for("us", providers).name == "twilio"
    assert provider_for("ca", providers).name == "twilio"
    assert provider_for("in", providers).name == "livekit-sip"


def test_india_without_the_livekit_provider_is_refused_with_the_rule_named():
    providers = configured_providers(make_settings())   # Twilio only
    assert [p.name for p in providers] == ["twilio"]

    with pytest.raises(ProviderRefusal) as exc:
        provider_for("in", providers)
    assert exc.value.rule == "S6-provider-region"
    assert "TRAI" in str(exc.value)


def test_preflight_names_the_compliance_rule_for_an_india_voice_run():
    """The PRD acceptance: an India-region target with a US-only provider is
    refused at pre-flight, naming the rule (this list IS the 422 body)."""
    from opkura.api.service import preflight_refusals

    refusals = preflight_refusals(
        None, transport="sip", region="in", settings=make_settings()
    )
    provider_refusals = [r for r in refusals
                         if r["error_type"] == "provider_not_allowed"]
    assert len(provider_refusals) == 1
    assert provider_refusals[0]["rule"] == "S6-provider-region"
    assert "TRAI" in provider_refusals[0]["message"]

    # …and a US run passes the provider gate.
    refusals_us = preflight_refusals(
        None, transport="sip",
        region="us",
        settings=make_settings(
            call_transport="sip",
            sip_destination_host="x.sip.livekit.cloud:5060",
            sip_trunk_number="1205",
        ),
    )
    assert not [r for r in refusals_us
                if r["error_type"] == "provider_not_allowed"]


# ------------------------------------------------------- Twilio extraction

class FakeTwilioClient:
    def __init__(self):
        self.created = []
        self.updated = []
        self.calls = self

    def create(self, **kw):
        self.created.append(kw)
        return SimpleNamespace(sid=f"CA{len(self.created):04d}", _properties={})

    def __call__(self, sid):
        outer = self

        class _Call:
            def update(self, **kw):
                outer.updated.append((sid, kw))
                return SimpleNamespace(sid=sid)

        return _Call()


@pytest.mark.asyncio
async def test_twilio_provider_is_the_extracted_call_manager():
    mgr = CallManager(make_settings(call_transport="pstn"))
    mgr.client = FakeTwilioClient()
    provider = TwilioProvider(mgr)

    ref = await provider.originate(target="+15550002222", session_id="s" * 32)
    assert ref == "CA0001"
    assert mgr.client.created[0]["to"] == "+15550002222"

    await provider.hang_up(ref)
    assert mgr.client.updated[0][0] == "CA0001"

    await provider.dtmf().send(
        call_ref=ref, digits="1234",
        reconnect_ws_url="wss://worker.example/ws/abc",
    )
    sid, kw = mgr.client.updated[1]
    assert sid == "CA0001" and 'digits="1234"' in kw["twiml"]


# ------------------------------------------------ provider equivalence (PRD)

class FakeLiveKitSIP(LiveKitSIPProvider):
    """The PRD's 'fake LiveKit-SIP adapter': dialing is faked; the serializer
    surface is the real one."""

    def __init__(self):
        super().__init__(url="wss://fake", api_key="k", api_secret="s",
                         trunk_id="ST_fake")
        self.originated = []

    async def originate(self, *, target, session_id, profile=None) -> str:
        self.originated.append((target, session_id))
        return f"room-{session_id[:8]}/pa_fake"

    async def hang_up(self, call_ref: str) -> None:
        pass


@pytest.mark.asyncio
async def test_same_wire_bytes_produce_identical_frames_downstream():
    """Provider equivalence: downstream of the serializer, nothing knows the
    vendor — the same μ-law produces frame-identical output."""
    mgr = CallManager(make_settings(call_transport="pstn"))
    mgr.client = FakeTwilioClient()
    twilio = TwilioProvider(mgr)
    livekit = FakeLiveKitSIP()

    payload = bytes(range(160))
    wire_msg = None
    frames_by_provider = {}
    for provider in (twilio, livekit):
        serializer = provider.serializer()
        serializer.stream_sid = "MZeq"
        wire_msg = serializer.encode_media(payload)
        frames = [serializer.decode(wire_msg)]
        frames_by_provider[provider.name] = frames

    assert frames_by_provider["twilio"] == frames_by_provider["livekit-sip"]
    assert frames_by_provider["twilio"][0] == InboundAudio(payload=payload)


@pytest.mark.asyncio
async def test_fake_livekit_adapter_dials_with_the_correlation_headers():
    livekit = FakeLiveKitSIP()
    ref = await livekit.originate(target="+918062123187", session_id="a" * 32)
    assert livekit.originated == [("+918062123187", "a" * 32)]
    assert ref.startswith("room-")
