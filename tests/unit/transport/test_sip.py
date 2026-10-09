"""Unit tests for `judge_it.transport.sip` — SIP URI construction.

Everything here is pure: no Twilio client, no network. The URI these
functions render is the entire contract with Twilio's outbound SIP
interface, so the tests pin the exact string rather than substrings
wherever the shape matters (parameter-before-query ordering, encoding).
"""
from __future__ import annotations

import pytest

from judge_it.transport.sip import (
    SIP_HEADER_BUDGET,
    build_sip_uri,
    encode_header_value,
    parse_header_spec,
    validate_header_name,
    validate_sip_host,
    validate_trunk_number,
)

LIVEKIT_HOST = "581mqcm8zvu.sip.livekit.cloud:5060"
TRUNK = "1205"


# ----------------------------------------------------------------------
# build_sip_uri
# ----------------------------------------------------------------------


def test_build_uri_bare():
    assert build_sip_uri(user=TRUNK, host=LIVEKIT_HOST) == f"sip:1205@{LIVEKIT_HOST}"


def test_build_uri_canonical_shape_judge_it_dials():
    """Trunk id in the user part, bot number in X-VirtualNumber."""
    uri = build_sip_uri(
        user=TRUNK,
        host=LIVEKIT_HOST,
        headers={
            "X-VirtualNumber": "+18447331982",
            "X-JudgeIt-Session-Id": "sess-xyz",
            "X-Is-Test": "1",
        },
    )
    assert uri == (
        f"sip:1205@{LIVEKIT_HOST}"
        "?X-Is-Test=1&X-JudgeIt-Session-Id=sess-xyz&X-VirtualNumber=%2B18447331982"
    )


def test_blank_or_none_transport_omits_the_parameter():
    """Default config sends no ;transport= so Twilio picks its own."""
    for t in (None, ""):
        uri = build_sip_uri(user=TRUNK, host=LIVEKIT_HOST, transport=t)
        assert ";transport" not in uri
        assert uri == f"sip:1205@{LIVEKIT_HOST}"


def test_plus_in_header_value_is_escaped_not_left_literal():
    """A literal `+` in a URI query reads as a space, which would corrupt
    the DID. It must go on the wire as %2B for the far end to decode."""
    uri = build_sip_uri(
        user=TRUNK, host=LIVEKIT_HOST, headers={"X-VirtualNumber": "+18447331982"}
    )
    assert "X-VirtualNumber=%2B18447331982" in uri
    assert "+18447331982" not in uri


def test_uri_params_precede_header_query():
    """RFC 3261 ordering: `;transport=` before `?headers`. Reversed, Twilio
    accepts the URI and then misroutes it — pin the ordering explicitly."""
    uri = build_sip_uri(
        user=TRUNK,
        host=LIVEKIT_HOST,
        transport="tls",
        headers={"X-A": "1"},
    )
    assert uri.index(";transport=tls") < uri.index("?X-A=1")


def test_headers_render_in_sorted_order_for_stable_logs():
    first = build_sip_uri(
        user=TRUNK, host=LIVEKIT_HOST, headers={"X-B": "2", "X-A": "1"}
    )
    second = build_sip_uri(
        user=TRUNK, host=LIVEKIT_HOST, headers={"X-A": "1", "X-B": "2"}
    )
    assert first == second == f"sip:1205@{LIVEKIT_HOST}?X-A=1&X-B=2"


def test_extension_user_part_allowed():
    assert build_sip_uri(user="vera-bot", host=LIVEKIT_HOST) == (
        f"sip:vera-bot@{LIVEKIT_HOST}"
    )


# ----------------------------------------------------------------------
# validate_trunk_number
# ----------------------------------------------------------------------


def test_validate_trunk_number_accepts_livekit_identifier():
    validate_trunk_number("1205")


def test_validate_trunk_number_empty_names_the_env_var():
    with pytest.raises(ValueError, match="JUDGE_IT_SIP_TRUNK_NUMBER"):
        validate_trunk_number("")


@pytest.mark.parametrize("bad", ["12 05", "sip:1205", "1205@host"])
def test_validate_trunk_number_rejects_malformed(bad):
    with pytest.raises(ValueError, match="bare identifier"):
        validate_trunk_number(bad)


@pytest.mark.parametrize("bad_user", ["", "+1 844 733", "a@b", "sip:+1844", "+1844?x=1"])
def test_build_uri_rejects_malformed_user(bad_user):
    with pytest.raises(ValueError, match="user part"):
        build_sip_uri(user=bad_user, host=LIVEKIT_HOST)


def test_build_uri_rejects_bad_transport():
    with pytest.raises(ValueError, match="transport"):
        build_sip_uri(user=TRUNK, host=LIVEKIT_HOST, transport="sctp")


def test_build_uri_rejects_non_x_header():
    """Twilio drops non-X- headers silently, so we must not emit them."""
    with pytest.raises(ValueError, match="X-<token>"):
        build_sip_uri(
            user=TRUNK, host=LIVEKIT_HOST, headers={"Session-Id": "sess-xyz"}
        )


def test_build_uri_enforces_twilio_header_budget():
    oversized = {"X-Big": "v" * (SIP_HEADER_BUDGET + 10)}
    with pytest.raises(ValueError, match="over Twilio's"):
        build_sip_uri(user=TRUNK, host=LIVEKIT_HOST, headers=oversized)


def test_header_values_are_percent_encoded():
    """A value carrying URI delimiters must not break out of the query."""
    uri = build_sip_uri(
        user=TRUNK,
        host=LIVEKIT_HOST,
        headers={"X-Note": "a b&c=d?e;f@g"},
    )
    assert uri == f"sip:1205@{LIVEKIT_HOST}?X-Note=a%20b%26c%3Dd%3Fe%3Bf%40g"
    # Exactly one `?` and no stray `&` — the URI has a single header.
    assert uri.count("?") == 1
    assert uri.count("&") == 0


def test_empty_headers_dict_emits_no_query():
    assert "?" not in build_sip_uri(user=TRUNK, host=LIVEKIT_HOST, headers={})


# ----------------------------------------------------------------------
# validate_sip_host
# ----------------------------------------------------------------------


def test_validate_host_accepts_hostname_and_port():
    validate_sip_host("581mqcm8zvu.sip.livekit.cloud")
    validate_sip_host(LIVEKIT_HOST)  # …:5060
    validate_sip_host("581mqcm8zvu.sip.livekit.cloud:5061")


def test_validate_host_empty_names_the_env_var():
    with pytest.raises(ValueError, match="JUDGE_IT_SIP_DESTINATION_HOST"):
        validate_sip_host("")


@pytest.mark.parametrize(
    "bad_host",
    [
        f"sip:{LIVEKIT_HOST}",
        f"1205@{LIVEKIT_HOST}",
        f"{LIVEKIT_HOST};transport=tcp",
        "has space.com",
    ],
)
def test_validate_host_rejects_scheme_userinfo_params_spaces(bad_host):
    with pytest.raises(ValueError, match="bare hostname"):
        validate_sip_host(bad_host)


# ----------------------------------------------------------------------
# header name / value primitives
# ----------------------------------------------------------------------


def test_validate_header_name_accepts_x_prefixed():
    validate_header_name("X-JudgeIt-Session-Id")
    validate_header_name("X-Is-Test")


@pytest.mark.parametrize("bad", ["", "Session-Id", "x_judge_it", "X-", "X JudgeIt"])
def test_validate_header_name_rejects_others(bad):
    with pytest.raises(ValueError):
        validate_header_name(bad)


def test_encode_header_value_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        encode_header_value("")


# ----------------------------------------------------------------------
# parse_header_spec
# ----------------------------------------------------------------------


def test_parse_header_spec_blank_is_empty():
    assert parse_header_spec("") == {}
    assert parse_header_spec("   ") == {}


def test_parse_header_spec_multiple_entries_and_whitespace():
    assert parse_header_spec(" X-A=1 , X-B=two ") == {"X-A": "1", "X-B": "two"}


def test_parse_header_spec_rejects_missing_equals():
    with pytest.raises(ValueError, match="NAME=VALUE"):
        parse_header_spec("X-A")


def test_parse_header_spec_rejects_non_x_name():
    with pytest.raises(ValueError, match="X-<token>"):
        parse_header_spec("Suite=veros")


def test_parse_header_spec_rejects_empty_value():
    with pytest.raises(ValueError, match="empty value"):
        parse_header_spec("X-A=")
