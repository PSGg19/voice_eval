"""Unit tests for `_send_dtmf` REST/audio dispatch (Phase 3 Task 2)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from vjudge.core.models import CallResult
from vjudge.transport.stream import (
    DTMFReconnectTimeoutError,
    DTMFRedirectAbortedError,
    DTMFRedirectFailedError,
    MediaStreamSession,
)


def _make_session(
    *,
    transport: str = "rest",
    with_call_mgr: bool = True,
    timeout_s: float = 0.2,
):
    engine = MagicMock()
    engine.barge_mode = "off"
    engine.scenario = MagicMock()
    engine.scenario.barge_in = []
    engine.is_complete = False

    tts = MagicMock()
    tts.model = "aura-2-thalia-en"

    result = CallResult(flow_name="test", target="+10000000000")
    result.call_sid = "CA_test_sid"

    call_mgr = MagicMock() if with_call_mgr else None
    if call_mgr is not None:
        call_mgr.play_dtmf_and_reconnect = MagicMock(return_value=None)

    s = MediaStreamSession(
        engine=engine,
        deepgram_api_key="dg",
        tts=tts,
        result=result,
        streaming_tts=False,
        tts_transport="blocking",
        call_manager=call_mgr,
        session_id="sess_abc",
        reconnect_ws_url="wss://vjudge.example.com/ws/sess_abc",
        dtmf_transport=transport,
        dtmf_reconnect_timeout_s=timeout_s,
    )
    s._stt = MagicMock()
    s._stt.start = AsyncMock()
    s._stt.stop = AsyncMock()
    s._send_audio = AsyncMock()
    return s, call_mgr


# ----------------------------------------------------------------------
# REST path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_dtmf_rest_pauses_calls_twilio_and_waits():
    s, call_mgr = _make_session(transport="rest", timeout_s=5.0)

    # Pre-arm the reconnect signal so wait_for returns immediately
    # — simulates the server.py endpoint receiving the reconnect WS
    # and routing to reattach() which sets _reconnected.
    async def fire_reconnect_after_delay():
        await asyncio.sleep(0.01)
        # Mirror what attach_ws's reattach branch does:
        s._in_transition = False
        s._reconnected.set()

    asyncio.create_task(fire_reconnect_after_delay())

    await s._send_dtmf("4012000033330125")

    # Twilio REST was called with our params
    call_mgr.play_dtmf_and_reconnect.assert_called_once()
    kwargs = call_mgr.play_dtmf_and_reconnect.call_args.kwargs
    assert kwargs["call_sid"] == "CA_test_sid"
    assert kwargs["digits"] == "4012000033330125"
    assert kwargs["reconnect_ws_url"] == "wss://vjudge.example.com/ws/sess_abc"
    # State is back to normal after a successful round trip.
    assert s._in_transition is False
    # Audio path was NOT used.
    s._send_audio.assert_not_called()


@pytest.mark.asyncio
async def test_send_dtmf_rest_times_out_when_no_reconnect():
    s, _ = _make_session(transport="rest", timeout_s=0.05)
    # Never fire _reconnected — simulates the new WS never arriving.

    with pytest.raises(DTMFReconnectTimeoutError) as exc_info:
        await s._send_dtmf("4012000033330125")

    err = exc_info.value
    assert err.call_sid == "CA_test_sid"
    assert err.digit_count == 16
    assert err.elapsed_ms > 0


@pytest.mark.asyncio
async def test_send_dtmf_rest_rolls_back_pause_on_twilio_failure():
    s, call_mgr = _make_session(transport="rest")
    call_mgr.play_dtmf_and_reconnect.side_effect = RuntimeError("twilio 500")

    with pytest.raises(DTMFRedirectFailedError) as exc_info:
        await s._send_dtmf("1234")

    # The original cause is preserved for diagnostics.
    assert isinstance(exc_info.value.cause, RuntimeError)
    assert "twilio 500" in str(exc_info.value.cause)
    # Pause must be rolled back so the still-alive WS keeps working.
    assert s._in_transition is False


@pytest.mark.asyncio
async def test_send_dtmf_rest_validates_digits_first():
    s, call_mgr = _make_session(transport="rest")
    with pytest.raises(ValueError, match="invalid chars"):
        await s._send_dtmf("4012-0000")
    # Twilio must NOT have been called.
    call_mgr.play_dtmf_and_reconnect.assert_not_called()


@pytest.mark.asyncio
async def test_send_dtmf_rest_requires_call_sid():
    s, _ = _make_session(transport="rest")
    s.result.call_sid = ""  # not yet known
    with pytest.raises(RuntimeError, match="no call_sid"):
        await s._send_dtmf("1234")


# ----------------------------------------------------------------------
# Audio fallback path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_dtmf_audio_mode_falls_back():
    s, call_mgr = _make_session(transport="audio")
    await s._send_dtmf("1234")
    call_mgr.play_dtmf_and_reconnect.assert_not_called()
    s._send_audio.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_dtmf_falls_back_to_audio_when_no_call_manager():
    """If no CallManager wired in (e.g. test fixture), use the legacy
    audio path even if Settings says rest."""
    s, _ = _make_session(transport="rest", with_call_mgr=False)
    # Dtmf transport should auto-downgrade since call_manager is None
    assert s._dtmf_transport == "audio"
    await s._send_dtmf("1234")
    s._send_audio.assert_awaited_once()


# ----------------------------------------------------------------------
# Two redirects in same call
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Task 4 — abort + status-callback wiring
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_dtmf_rest_aborts_when_call_lifecycle_completed_mid_redirect():
    """If Twilio's status callback fires 'completed' while we're waiting
    for the new WS, we must raise DTMFRedirectAbortedError instead of
    hanging until the reconnect timeout."""
    s, _ = _make_session(transport="rest", timeout_s=5.0)

    async def fire_abort():
        await asyncio.sleep(0.01)
        s.on_call_lifecycle("completed")

    asyncio.create_task(fire_abort())

    with pytest.raises(DTMFRedirectAbortedError) as exc_info:
        await s._send_dtmf("1234")
    assert exc_info.value.call_sid == "CA_test_sid"
    assert exc_info.value.lifecycle_event == "completed"


@pytest.mark.asyncio
async def test_on_call_lifecycle_ignores_non_terminal_events():
    s, _ = _make_session(transport="rest", timeout_s=5.0)
    s._in_transition = True  # pretend we're paused

    # These events shouldn't abort.
    for event in ("initiated", "ringing", "answered", "in-progress"):
        s.on_call_lifecycle(event)
        assert s._aborted.is_set() is False, f"{event} wrongly aborted"


@pytest.mark.asyncio
async def test_on_call_lifecycle_noop_outside_transition():
    s, _ = _make_session(transport="rest")
    # Not in transition → terminal events shouldn't set _aborted.
    s.on_call_lifecycle("completed")
    assert s._aborted.is_set() is False


@pytest.mark.asyncio
async def test_dtmf_redirect_failed_carries_status_and_code():
    """When the underlying TwilioRestException has .status / .code, the
    typed wrapper exposes them for diagnostics."""
    s, call_mgr = _make_session(transport="rest")

    class FakeTwilioErr(Exception):
        status = 404
        code = 20404

    call_mgr.play_dtmf_and_reconnect.side_effect = FakeTwilioErr("call gone")

    with pytest.raises(DTMFRedirectFailedError) as exc_info:
        await s._send_dtmf("1234")

    assert exc_info.value.twilio_status == 404
    assert exc_info.value.twilio_code == 20404


@pytest.mark.asyncio
async def test_two_consecutive_rest_dtmf_turns_succeed():
    s, call_mgr = _make_session(transport="rest", timeout_s=5.0)

    async def fire_reconnect():
        await asyncio.sleep(0.01)
        s._in_transition = False
        s._reconnected.set()

    # First turn
    asyncio.create_task(fire_reconnect())
    await s._send_dtmf("4012000033330125")
    assert s._in_transition is False

    # Second turn — _reconnected was rebuilt by pause(), so a fresh
    # wait is needed.
    asyncio.create_task(fire_reconnect())
    await s._send_dtmf("1226")

    assert call_mgr.play_dtmf_and_reconnect.call_count == 2
    assert s._in_transition is False
