"""P3.9 at the dial site — CallManager honours the run's RegionProfile.

PRD acceptance test, verbatim: a SIP-to-SIP origination for any region puts
the target DID in X-VirtualNumber and never in the dialled address (asserted
on the built SIP URI).
"""
from __future__ import annotations

from opkura.core.config import Settings
from opkura.core.region_profile import resolve_profile
from opkura.transport.call import CallManager


def make_settings(**over) -> Settings:
    base = dict(
        twilio_account_sid="ACtest",
        twilio_auth_token="token",
        twilio_phone_number="+15550001111",
        deepgram_api_key="dg",
        call_transport="sip",
        osv_base_url="https://llm-studio.skit.ai/us/api/interface",
        sip_destination_host="usproj.sip.livekit.cloud:5060",
        sip_trunk_number="1205",
        sip_destination_host_in="inproj.sip.livekit.cloud:5060",
        sip_trunk_number_in="2001",
        sip_provider_config_in="livekit-skittl-2",
    )
    base.update(over)
    return Settings(_env_file=None, **base)


DID = "+918062123187"


def test_india_profile_selects_india_ingress_and_did_stays_a_header():
    s = make_settings()
    mgr = CallManager(s)
    profile = resolve_profile("in", s, for_transport="sip")

    dial_to, extra = mgr._resolve_destination(DID, "sess123", profile)

    # The dialled address is India's trunk at India's LiveKit project…
    assert dial_to.startswith("sip:2001@inproj.sip.livekit.cloud:5060")
    # …and the DID appears ONLY as the X-VirtualNumber header (URL-encoded in
    # the URI's header section), never in the dialled user@host part.
    addr = dial_to.split("?")[0]
    assert "91806" not in addr
    assert "X-VirtualNumber=%2B918062123187" in dial_to
    # Correlation + test marking still ride along.
    assert "X-Opkura-Session-Id=sess123" in dial_to
    assert "X-Is-Test=1" in dial_to
    assert extra == {}


def test_no_profile_keeps_deployment_default_trunk():
    """Back-compat: every existing caller that passes no profile dials exactly
    what it dialled before this change."""
    s = make_settings()
    mgr = CallManager(s)
    dial_to, _ = mgr._resolve_destination(DID, "sess123")
    assert dial_to.startswith("sip:1205@usproj.sip.livekit.cloud:5060")
    assert "X-VirtualNumber=%2B918062123187" in dial_to


def test_us_and_india_dials_coexist_in_one_manager():
    """Two runs, two regions, one process — neither leaks into the other."""
    s = make_settings()
    mgr = CallManager(s)
    us = resolve_profile("us", s, for_transport="sip")
    india = resolve_profile("in", s, for_transport="sip")

    us_to, _ = mgr._resolve_destination("+18447331995", "s-us", us)
    in_to, _ = mgr._resolve_destination(DID, "s-in", india)

    assert us_to.startswith("sip:1205@usproj.sip.livekit.cloud:5060")
    assert in_to.startswith("sip:2001@inproj.sip.livekit.cloud:5060")


def test_pstn_mode_ignores_profile_and_dials_e164():
    s = make_settings(
        call_transport="pstn",
        sip_destination_host="",
        sip_trunk_number="",
        sip_destination_host_in="",
        sip_trunk_number_in="",
        sip_provider_config_in="",
    )
    mgr = CallManager(s)
    dial_to, extra = mgr._resolve_destination("+18447331995", "sess123", None)
    assert dial_to == "+18447331995"
    assert extra == {}
