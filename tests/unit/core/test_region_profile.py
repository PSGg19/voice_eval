"""P3.8/P3.9/P3.10 — region → config-bundle resolution.

PRD acceptance tests live here:
* a `region=in` run resolves the India OSV URL AND the India SIP trunk while a
  `region=us` run resolves the US pair, in one process;
* the resolver returns the right trunk for a known India DID's config
  (`livekit-vjudgetl-2`) and the text base URL for that region from one call;
* an unconfigured region is refused BEFORE any call is placed;
* PSTN into India is refused as non-compliant (S6), never dialled.
"""
from __future__ import annotations

import dataclasses

import pytest

from vjudge.core.config import Settings
from vjudge.core.region_profile import (
    PSTN_ALLOWED_REGIONS,
    RegionNotCompliant,
    RegionNotConfigured,
    default_region,
    resolve_profile,
)


def make_settings(**over) -> Settings:
    """Settings from explicit kwargs only — immune to whatever VJUDGE_*
    happens to be exported in the developer's shell."""
    base = dict(
        twilio_account_sid="ACtest",
        twilio_auth_token="token",
        twilio_phone_number="+15550001111",
        deepgram_api_key="dg",
        osv_base_url="https://llm-studio.vjudge.dev/us/api/interface",
        text_runtime_base_url="https://llm-studio.vjudge.dev/us/api/text",
        sip_destination_host="",
        sip_trunk_number="",
        sip_destination_host_us="",
        sip_trunk_number_us="",
        sip_provider_config_us="",
        sip_destination_host_in="",
        sip_trunk_number_in="",
        sip_provider_config_in="",
        sip_destination_host_ca="",
        sip_trunk_number_ca="",
        sip_provider_config_ca="",
    )
    base.update(over)
    return Settings(_env_file=None, **base)


# ------------------------------------------------------------ default region

@pytest.mark.parametrize("seg", ["us", "in", "ca"])
def test_default_region_inferred_from_osv_url(seg):
    s = make_settings(osv_base_url=f"https://llm-studio.vjudge.dev/{seg}/api/interface")
    assert default_region(s) == seg


def test_default_region_falls_back_to_us():
    s = make_settings(osv_base_url="http://localhost:9000/api")
    assert default_region(s) == "us"


# ------------------------------------------------------------- URL semantics

def test_none_region_leaves_urls_untouched():
    """region=None is 'deployment default': historical behaviour, byte-for-byte.
    In particular a region-less local URL must pass through without warnings."""
    s = make_settings(text_runtime_base_url="http://localhost:8001")
    p = resolve_profile(None, s)
    assert p.text_runtime_base_url == "http://localhost:8001"
    assert p.osv_base_url == "https://llm-studio.vjudge.dev/us/api/interface"
    assert p.region == "us"


def test_explicit_region_swaps_both_urls():
    p = resolve_profile("in", make_settings())
    assert p.osv_base_url == "https://llm-studio.vjudge.dev/in/api/interface"
    assert p.text_runtime_base_url == "https://llm-studio.vjudge.dev/in/api/text"
    assert p.region == "in"


def test_invalid_region_rejected():
    with pytest.raises(ValueError) as exc:
        resolve_profile("eu", make_settings())
    assert "us" in str(exc.value) and "eu" in str(exc.value)


# --------------------------------------------------------------- SIP trunks

def test_default_region_uses_base_sip_fields():
    s = make_settings(
        sip_destination_host="usproj.sip.livekit.cloud:5060",
        sip_trunk_number="1205",
    )
    p = resolve_profile(None, s, for_transport="sip")
    assert p.sip_destination_host == "usproj.sip.livekit.cloud:5060"
    assert p.sip_trunk_number == "1205"


def test_per_region_fields_win_over_base_for_default_region():
    s = make_settings(
        sip_destination_host="old.sip.livekit.cloud:5060",
        sip_trunk_number="1205",
        sip_destination_host_us="new.sip.livekit.cloud:5060",
        sip_trunk_number_us="9000",
    )
    p = resolve_profile("us", s, for_transport="sip")
    assert p.sip_destination_host == "new.sip.livekit.cloud:5060"
    assert p.sip_trunk_number == "9000"


def test_other_region_does_not_inherit_base_sip():
    """The 31 Aug failure class: the US trunk must never leak into an India
    run just because it is what the global .env holds."""
    s = make_settings(
        sip_destination_host="usproj.sip.livekit.cloud:5060",
        sip_trunk_number="1205",
    )
    p = resolve_profile("in", s)  # no for_transport: resolution itself is fine
    assert not p.sip_configured
    assert p.sip_destination_host == ""


def test_sip_into_unconfigured_region_refused_before_dial():
    s = make_settings(
        sip_destination_host="usproj.sip.livekit.cloud:5060",
        sip_trunk_number="1205",
    )
    with pytest.raises(RegionNotConfigured) as exc:
        resolve_profile("in", s, for_transport="sip")
    msg = str(exc.value)
    assert "VJUDGE_SIP_DESTINATION_HOST_IN" in msg
    assert "VJUDGE_SIP_TRUNK_NUMBER_IN" in msg
    assert "livekit-vjudgetl-2" in msg          # names where the answer lives
    assert "No call was placed" in msg


def test_text_transport_tolerates_unconfigured_sip():
    p = resolve_profile("in", make_settings(), for_transport="text")
    assert p.region == "in"


# ---------------------------------------------------------------- S6 / PSTN

def test_pstn_into_india_refused_as_non_compliant():
    with pytest.raises(RegionNotCompliant) as exc:
        resolve_profile("in", make_settings(), for_transport="pstn")
    msg = str(exc.value)
    assert "pstn" in msg and "sip" in msg    # refuses AND names the fix


@pytest.mark.parametrize("region", PSTN_ALLOWED_REGIONS)
def test_pstn_allowed_regions_pass(region):
    p = resolve_profile(region, make_settings(), for_transport="pstn")
    assert p.region == region


def test_pstn_needs_no_sip_trunk():
    # PSTN dials the E.164 directly — an empty trunk must not block it.
    p = resolve_profile("us", make_settings(), for_transport="pstn")
    assert not p.sip_configured


# ------------------------------------------------- PRD acceptance: two regions

def test_us_and_india_resolve_concurrently_in_one_process():
    """A region=in run resolves the India bundle while a region=us run
    resolves the US pair — same Settings object, no re-sourcing, no shared
    mutable state (profiles are frozen)."""
    s = make_settings(
        sip_destination_host_us="usproj.sip.livekit.cloud:5060",
        sip_trunk_number_us="1205",
        sip_destination_host_in="inproj.sip.livekit.cloud:5060",
        sip_trunk_number_in="2001",
        sip_provider_config_in="livekit-vjudgetl-2",
    )
    us = resolve_profile("us", s, for_transport="sip")
    india = resolve_profile("in", s, for_transport="sip")

    assert us.sip_destination_host == "usproj.sip.livekit.cloud:5060"
    assert us.osv_base_url.endswith("/us/api/interface")
    assert india.sip_destination_host == "inproj.sip.livekit.cloud:5060"
    assert india.sip_trunk_number == "2001"
    assert india.osv_base_url.endswith("/in/api/interface")
    assert india.text_runtime_base_url.endswith("/in/api/text")

    with pytest.raises(dataclasses.FrozenInstanceError):
        us.sip_destination_host = "mutated"  # type: ignore[misc]


def test_india_profile_carries_vjudgetl2_provider_config():
    """PRD acceptance: the resolver returns the right trunk config for the
    known India DID's project (`+918062123187` → livekit-vjudgetl-2) and the
    text base URL for that region, from one call."""
    s = make_settings(
        sip_destination_host_in="inproj.sip.livekit.cloud:5060",
        sip_trunk_number_in="2001",
        sip_provider_config_in="livekit-vjudgetl-2",
    )
    p = resolve_profile("in", s, for_transport="sip")
    assert p.provider_config == "livekit-vjudgetl-2"    # P3.10 hook
    assert p.text_runtime_base_url.endswith("/in/api/text")
    assert p.sip_configured


# ---------------------------------------------------- settings-level guards

def test_half_configured_region_trunk_refused_at_load():
    with pytest.raises(Exception) as exc:
        make_settings(
            call_transport="sip",
            sip_destination_host="usproj.sip.livekit.cloud:5060",
            sip_trunk_number="1205",
            sip_destination_host_in="inproj.sip.livekit.cloud:5060",
            # trunk number missing
        )
    assert "half-configured" in str(exc.value)
