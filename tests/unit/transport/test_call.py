"""Unit tests for `vjudge.transport.call` — REST DTMF redirect helper.

Phase 3 Task 2. The Twilio `Client` constructor is instantiated lazily
inside `CallManager.__init__`; we don't hit the network because the
tests use Pydantic-validated test creds and never actually call
`.calls.create` or `.calls.update` — those are mocked.

`build_dtmf_redirect_twiml` is pure and can be tested without any
mocking.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from vjudge.core.config import Settings
from vjudge.transport.call import (
    CallManager,
    build_dtmf_redirect_twiml,
)


def _make_settings() -> Settings:
    return Settings(
        # `_env_file=None` makes these tests hermetic. Without it pydantic also
        # reads the developer's `.env`, so a real `.env` carrying
        # VJUDGE_CALL_TRANSPORT=sip silently flipped the "pstn is the default"
        # assertions — a test failing because of local config, not code.
        _env_file=None,
        twilio_account_sid="AC_test",
        twilio_auth_token="token_test",
        twilio_phone_number="+10000000000",
        deepgram_api_key="dg_test",
        public_url="https://vjudge.example.com",
    )


# ----------------------------------------------------------------------
# build_dtmf_redirect_twiml — pure
# ----------------------------------------------------------------------


def test_build_twiml_canonical():
    twiml = build_dtmf_redirect_twiml(
        digits="4012000033330125",
        reconnect_ws_url="wss://vjudge.example.com/ws/abc123",
    )
    expected = (
        "<Response>"
        '<Play digits="4012000033330125"/>'
        "<Connect>"
        '<Stream url="wss://vjudge.example.com/ws/abc123"/>'
        "</Connect>"
        "</Response>"
    )
    assert twiml == expected


def test_build_twiml_supports_pause_and_symbols():
    twiml = build_dtmf_redirect_twiml(
        digits="w1234*#",
        reconnect_ws_url="wss://x/ws/s",
    )
    assert '<Play digits="w1234*#"/>' in twiml


def test_build_twiml_rejects_invalid_digits():
    with pytest.raises(ValueError, match="invalid chars"):
        build_dtmf_redirect_twiml(
            digits="4012 0000 3333 0125",  # spaces — not valid
            reconnect_ws_url="wss://x/ws/s",
        )


def test_build_twiml_rejects_empty_digits():
    with pytest.raises(ValueError, match="empty"):
        build_dtmf_redirect_twiml(digits="", reconnect_ws_url="wss://x/ws/s")


def test_build_twiml_rejects_injection_attempt():
    with pytest.raises(ValueError, match="invalid chars"):
        build_dtmf_redirect_twiml(
            digits='1"/><script>',
            reconnect_ws_url="wss://x/ws/s",
        )


def test_build_twiml_rejects_bad_url_scheme():
    with pytest.raises(ValueError, match="ws://"):
        build_dtmf_redirect_twiml(
            digits="1234",
            reconnect_ws_url="https://vjudge.example.com/ws/s",
        )


def test_build_twiml_rejects_non_string_digits():
    with pytest.raises(TypeError, match="must be str"):
        build_dtmf_redirect_twiml(
            digits=4012,  # int — type error
            reconnect_ws_url="wss://x/ws/s",
        )


# ----------------------------------------------------------------------
# CallManager.play_dtmf_and_reconnect — Twilio client mocked
# ----------------------------------------------------------------------


def test_play_dtmf_and_reconnect_calls_update_with_correct_twiml():
    with patch("vjudge.transport.call.Client") as TwClient:
        cm = CallManager(_make_settings())
        # The client we want to assert against is the one cm built.
        mock_call_resource = MagicMock()
        mock_call_resource.update = MagicMock(return_value="updated_call_obj")
        cm.client.calls = MagicMock(return_value=mock_call_resource)

        result = cm.play_dtmf_and_reconnect(
            call_sid="CA1234567890abcdef",
            digits="4012000033330125",
            reconnect_ws_url="wss://vjudge.example.com/ws/sess1",
        )

    assert result == "updated_call_obj"
    cm.client.calls.assert_called_once_with("CA1234567890abcdef")
    # Verify the actual TwiML body sent matches what we expect.
    update_kwargs = mock_call_resource.update.call_args.kwargs
    assert "twiml" in update_kwargs
    twiml = update_kwargs["twiml"]
    assert '<Play digits="4012000033330125"/>' in twiml
    assert (
        '<Stream url="wss://vjudge.example.com/ws/sess1"/>' in twiml
    )


def test_play_dtmf_and_reconnect_validates_digits_before_calling_twilio():
    """Bad digits must fail BEFORE we hit Twilio's API — saves a round trip
    and prevents bad TwiML from ever reaching their service."""
    with patch("vjudge.transport.call.Client"):
        cm = CallManager(_make_settings())
        cm.client.calls = MagicMock()

        with pytest.raises(ValueError, match="invalid chars"):
            cm.play_dtmf_and_reconnect(
                call_sid="CA1234",
                digits="abc",
                reconnect_ws_url="wss://x/ws/s",
            )
        cm.client.calls.assert_not_called()


# ----------------------------------------------------------------------
# CallManager.make_call — Twilio client mocked
# ----------------------------------------------------------------------


def _mock_created_call():
    """A stand-in for the Twilio Call resource returned by calls.create,
    carrying a populated `_properties` dict like the real SDK hydrates."""
    call = MagicMock()
    call.sid = "CA_created_123"
    call._properties = {
        "sid": "CA_created_123",
        "status": "queued",
        "direction": "outbound-api",
        "to": "+19998887777",
        "from": "+10000000000",
        "api_version": "2010-04-01",
        "price": None,
        "error_code": None,
    }
    return call


def test_make_call_returns_sid_and_passes_expected_params():
    with patch("vjudge.transport.call.Client"):
        cm = CallManager(_make_settings())
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())

        sid = cm.make_call(to="+19998887777", session_id="sess-xyz")

    assert sid == "CA_created_123"
    create_kwargs = cm.client.calls.create.call_args.kwargs
    assert create_kwargs["to"] == "+19998887777"
    assert create_kwargs["from_"] == "+10000000000"
    assert create_kwargs["url"] == "https://vjudge.example.com/twiml/sess-xyz"
    assert create_kwargs["status_callback"] == "https://vjudge.example.com/status/sess-xyz"


def test_make_call_logs_full_request_and_response_at_debug(caplog):
    """Debugging headers/params: DEBUG must carry the full request params
    we send to Twilio AND every attribute Twilio returned on the call."""
    import logging

    with patch("vjudge.transport.call.Client"):
        cm = CallManager(_make_settings())
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())

        with caplog.at_level(logging.DEBUG, logger="vjudge.transport.call"):
            cm.make_call(to="+19998887777", session_id="sess-xyz")

    log_text = " ".join(r.message for r in caplog.records)
    # Request side: params and the reconstructed twiml/status URLs.
    assert "Call request params" in log_text
    assert "sess-xyz" in log_text
    assert "https://vjudge.example.com/twiml/sess-xyz" in log_text
    assert "initiated" in log_text and "completed" in log_text
    # Response side: the full Twilio Call properties.
    assert "Call response (full)" in log_text
    assert "outbound-api" in log_text
    assert "api_version" in log_text


def test_make_call_survives_call_object_without_properties(caplog):
    """If the SDK ever stops exposing `_properties`, logging must fall back
    to hand-built attrs instead of raising and killing the call."""
    import logging

    call = MagicMock(spec=["sid", "status", "direction", "to", "from_", "date_created"])
    call.sid = "CA_noprops"
    call.status = "queued"

    with patch("vjudge.transport.call.Client"):
        cm = CallManager(_make_settings())
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=call)

        with caplog.at_level(logging.DEBUG, logger="vjudge.transport.call"):
            sid = cm.make_call(to="+19998887777", session_id="sess-noprops")

    assert sid == "CA_noprops"
    log_text = " ".join(r.message for r in caplog.records)
    assert "Call response (full)" in log_text
    assert "CA_noprops" in log_text


def test_play_dtmf_and_reconnect_logs_count_not_raw_digits(caplog):
    """Log lines must include the digit COUNT, never the raw digit
    string — credit-card-shaped strings shouldn't leak into logs."""
    import logging

    with patch("vjudge.transport.call.Client"):
        cm = CallManager(_make_settings())
        cm.client.calls = MagicMock()

        with caplog.at_level(logging.INFO, logger="vjudge.transport.call"):
            cm.play_dtmf_and_reconnect(
                call_sid="CA1234",
                digits="4012000033330125",
                reconnect_ws_url="wss://x/ws/s",
            )

    log_text = " ".join(r.message for r in caplog.records)
    assert "digit_count=16" in log_text
    assert "4012000033330125" not in log_text


# ----------------------------------------------------------------------
# make_call — SIP transport (call_transport="sip")
# ----------------------------------------------------------------------

LIVEKIT_HOST = "581mqcm8zvu.sip.livekit.cloud:5060"
TRUNK = "1205"


def _make_sip_settings(**overrides) -> Settings:
    """Settings for pure SIP-to-SIP dialing at the LiveKit host."""
    base = dict(
        _env_file=None,  # hermetic — see _make_settings
        twilio_account_sid="AC_test",
        twilio_auth_token="token_test",
        twilio_phone_number="+12202206062",
        deepgram_api_key="dg_test",
        public_url="https://vjudge.example.com",
        call_transport="sip",
        sip_destination_host=LIVEKIT_HOST,
        sip_trunk_number=TRUNK,
    )
    base.update(overrides)
    return Settings(**base)


def test_pstn_transport_is_the_default_and_dials_the_number_unchanged(monkeypatch):
    """Regression guard: existing scenarios must keep dialing E.164.

    The env var is cleared so a developer whose `.env` flips the default
    to sip still gets a truthful answer about what the DEFAULT is.
    """
    monkeypatch.delenv("VJUDGE_CALL_TRANSPORT", raising=False)
    settings = _make_settings()
    assert settings.call_transport == "pstn"
    with patch("vjudge.transport.call.Client"):
        cm = CallManager(settings)
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())
        cm.make_call(to="+18447331982", session_id="sess-xyz")

    kwargs = cm.client.calls.create.call_args.kwargs
    assert kwargs["to"] == "+18447331982"
    assert "sip_auth_username" not in kwargs


def test_sip_transport_dials_trunk_uri_with_target_in_virtualnumber_header():
    """The trunk id is the user part; the YAML target rides in
    X-VirtualNumber, which LiveKit's headersToAttributes maps to the
    `virtualNumber` attribute. Caller ID stays the Twilio DID."""
    with patch("vjudge.transport.call.Client"):
        cm = CallManager(_make_sip_settings())
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())
        sid = cm.make_call(to="+18447331982", session_id="sess-xyz")

    assert sid == "CA_created_123"
    kwargs = cm.client.calls.create.call_args.kwargs
    assert kwargs["to"] == (
        f"sip:1205@{LIVEKIT_HOST}"
        "?X-Is-Test=0&X-VJudge-Session-Id=sess-xyz&X-VirtualNumber=%2B18447331982"
    )
    assert kwargs["from_"] == "+12202206062"
    # TwiML/status callbacks are transport-agnostic and must not change.
    assert kwargs["url"] == "https://vjudge.example.com/twiml/sess-xyz"
    assert kwargs["status_callback"] == "https://vjudge.example.com/status/sess-xyz"


def test_sip_target_never_lands_in_the_user_part():
    """Regression guard for the addressing bug: putting the DID in the
    user part reaches the trunk but leaves virtualNumber unset."""
    with patch("vjudge.transport.call.Client"):
        cm = CallManager(_make_sip_settings())
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())
        cm.make_call(to="+18447331982", session_id="sess-xyz")

    to = cm.client.calls.create.call_args.kwargs["to"]
    assert to.startswith(f"sip:1205@{LIVEKIT_HOST}?")
    assert "+18447331982@" not in to


def test_sip_virtual_number_header_name_is_configurable():
    """Must track whatever key the trunk's headersToAttributes uses."""
    with patch("vjudge.transport.call.Client"):
        cm = CallManager(_make_sip_settings(sip_virtual_number_header="X-Bot-Did"))
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())
        cm.make_call(to="+18447331982", session_id="sess-xyz")

    to = cm.client.calls.create.call_args.kwargs["to"]
    assert "X-Bot-Did=%2B18447331982" in to
    assert "X-VirtualNumber" not in to


def test_sip_default_config_sends_no_transport_param():
    with patch("vjudge.transport.call.Client"):
        cm = CallManager(_make_sip_settings())
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())
        cm.make_call(to="+18447331982", session_id="sess-xyz")

    assert ";transport" not in cm.client.calls.create.call_args.kwargs["to"]


def test_sip_target_number_is_logged_even_though_it_is_header_borne(caplog):
    """In SIP mode `to` is the same trunk URI for every scenario, so the
    log must name the target separately or runs become indistinguishable."""
    import logging

    with patch("vjudge.transport.call.Client"):
        cm = CallManager(_make_sip_settings())
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())

        with caplog.at_level(logging.INFO, logger="vjudge.transport.call"):
            cm.make_call(to="+18447331982", session_id="sess-xyz")

    log_text = " ".join(r.message for r in caplog.records)
    assert "target=+18447331982" in log_text


def test_sip_session_id_header_is_the_correlation_key():
    """The Twilio Call SID can't be a header (it's minted by this very
    request), so session_id is what crosses to the bot — and vjudge maps
    session_id → the returned SID locally."""
    with patch("vjudge.transport.call.Client"):
        cm = CallManager(_make_sip_settings())
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())
        sid = cm.make_call(to="+18447331982", session_id="sess-corr-1")

    assert "X-VJudge-Session-Id=sess-corr-1" in cm.client.calls.create.call_args.kwargs["to"]
    assert sid == "CA_created_123"


def test_sip_extra_headers_merge_and_vjudge_headers_win():
    with patch("vjudge.transport.call.Client"):
        cm = CallManager(
            _make_sip_settings(
                sip_extra_headers="X-VJudge-Suite=veros-ib2,X-Is-Test=1"
            )
        )
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())
        cm.make_call(to="+18447331982", session_id="sess-xyz")

    to = cm.client.calls.create.call_args.kwargs["to"]
    assert "X-VJudge-Suite=veros-ib2" in to
    # vjudge's own header wins over the extra: voice_is_test defaults to
    # False (visible in llm-studio), so the extra's "1" must lose.
    assert "X-Is-Test=0" in to and "X-Is-Test=1" not in to


def test_sip_transport_override_is_applied():
    with patch("vjudge.transport.call.Client"):
        cm = CallManager(_make_sip_settings(sip_transport="tls"))
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())
        cm.make_call(to="+18447331982", session_id="sess-xyz")

    assert ";transport=tls" in cm.client.calls.create.call_args.kwargs["to"]


def test_sip_auth_passed_only_when_username_configured():
    with patch("vjudge.transport.call.Client"):
        cm = CallManager(
            _make_sip_settings(sip_auth_username="vjudge", sip_auth_password="s3cret")
        )
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())
        cm.make_call(to="+18447331982", session_id="sess-xyz")

    kwargs = cm.client.calls.create.call_args.kwargs
    assert kwargs["sip_auth_username"] == "vjudge"
    assert kwargs["sip_auth_password"] == "s3cret"


def test_sip_auth_password_never_reaches_the_logs(caplog):
    """request_params is dumped at DEBUG into the retained per-call
    debug.log — the password must be redacted there."""
    import logging

    with patch("vjudge.transport.call.Client"):
        cm = CallManager(
            _make_sip_settings(sip_auth_username="vjudge", sip_auth_password="s3cret")
        )
        cm.client.calls = MagicMock()
        cm.client.calls.create = MagicMock(return_value=_mock_created_call())

        with caplog.at_level(logging.DEBUG, logger="vjudge.transport.call"):
            cm.make_call(to="+18447331982", session_id="sess-xyz")

    log_text = " ".join(r.message for r in caplog.records)
    assert "s3cret" not in log_text
    assert "redacted" in log_text
    # The dialed SIP URI itself should be visible for debugging.
    assert LIVEKIT_HOST in log_text
    assert "call_transport" in log_text


def test_sip_settings_require_a_trunk_number():
    """Misconfiguration fails at settings load, not mid-scenario."""
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="VJUDGE_SIP_TRUNK_NUMBER"):
        _make_sip_settings(sip_trunk_number="")


def test_sip_settings_reject_non_x_virtual_number_header():
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="X-<token>"):
        _make_sip_settings(sip_virtual_number_header="VirtualNumber")


def test_sip_settings_require_a_destination_host():
    """Misconfiguration fails at settings load, not mid-scenario."""
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="VJUDGE_SIP_DESTINATION_HOST"):
        _make_sip_settings(sip_destination_host="")


def test_sip_settings_reject_malformed_extra_headers():
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="X-<token>"):
        _make_sip_settings(sip_extra_headers="Suite=veros")


def test_sip_settings_reject_password_without_username():
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="VJUDGE_SIP_AUTH_USERNAME"):
        _make_sip_settings(sip_auth_password="s3cret")
