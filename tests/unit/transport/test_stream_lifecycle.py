"""Tests for Phase 3 Task 1 — session-lifecycle decoupling on
``MediaStreamSession``.

The refactor split the old monolithic ``handle(ws)`` into five
primitives:

  start_session() / attach_ws(ws) / pause(reason) / reattach(ws) / end_session()

These tests cover the state-machine invariants and the state
preservation guarantee across a pause/reattach cycle. They use
mocks for the WebSocket / STT / TTS / VAD so no network or
torch import is needed; the WebSocket loop in ``attach_ws`` is
NOT exercised here — that's covered by the integration test in
Task 5. State-machine correctness is what Task 1 is responsible
for.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from judge_it.core.models import CallResult, CallStatus, TranscriptEntry
from judge_it.transport.stream import MediaStreamSession


def _make_session() -> MediaStreamSession:
    """Build a MediaStreamSession with mocked I/O for state-machine tests.

    The flow engine, STT, TTS, and Silero VAD are all replaced with
    mocks so the constructor doesn't open network sockets or load
    torch. The receive / conversation loops are never actually run —
    every test interacts with the lifecycle methods directly.
    """
    engine = MagicMock()
    engine.barge_mode = "off"
    engine.scenario = MagicMock()
    engine.scenario.barge_in = []
    engine.is_complete = False

    tts = MagicMock()
    tts.model = "aura-2-thalia-en"

    result = CallResult(flow_name="test-flow", target="+10000000000")

    s = MediaStreamSession(
        engine=engine,
        deepgram_api_key="k-test",
        tts=tts,
        result=result,
        use_silero_vad=False,
        streaming_tts=False,
        tts_transport="blocking",
    )
    # Replace STT with a mock so start/stop don't try to open a
    # Deepgram WS.
    s._stt = MagicMock()
    s._stt.start = AsyncMock()
    s._stt.stop = AsyncMock()
    return s


def _make_ws_mock() -> MagicMock:
    """A WebSocket whose ``accept`` is awaitable."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    return ws


# ----------------------------------------------------------------------
# Initial state
# ----------------------------------------------------------------------


def test_initial_lifecycle_state():
    """Fresh session: not started, not in transition, attach_count=0."""
    s = _make_session()
    assert s._session_started is False
    assert s._session_ended is False
    assert s._in_transition is False
    assert s._attach_count == 0
    assert s._conv_task is None
    assert s._reconnected.is_set() is False


def test_start_session_idempotent():
    """start_session twice is a no-op on the second call."""
    s = _make_session()
    s.start_session()
    first_ts = s._start_time
    assert s._session_started is True
    assert s.result.status == CallStatus.IN_PROGRESS
    s.start_session()
    assert s._start_time == first_ts  # unchanged
    assert s._session_started is True


# ----------------------------------------------------------------------
# pause() invariants
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_sets_in_transition_and_rebuilds_event():
    """pause() flips _in_transition and gives us a fresh Event."""
    s = _make_session()
    original_event = s._reconnected
    original_event.set()  # Pretend a previous DTMF turn left it set.

    await s.pause(reason="dtmf_redirect")

    assert s._in_transition is True
    assert s._reconnected is not original_event
    assert s._reconnected.is_set() is False


@pytest.mark.asyncio
async def test_pause_during_paused_raises():
    """pause() refuses to run if already paused."""
    s = _make_session()
    await s.pause(reason="first")
    with pytest.raises(RuntimeError, match="already paused"):
        await s.pause(reason="second")


# ----------------------------------------------------------------------
# State preservation across pause
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_preserves_transcript():
    """Transcript list survives pause untouched."""
    s = _make_session()
    s.result.transcript.append(
        TranscriptEntry(
            turn=1, speaker="bot", text="Hello.", dtmf="", barge=False
        )
    )
    s.result.transcript.append(
        TranscriptEntry(
            turn=2, speaker="caller", text="Hi.", dtmf="", barge=False
        )
    )
    pre_len = len(s.result.transcript)
    pre_ids = [id(e) for e in s.result.transcript]

    await s.pause(reason="dtmf_redirect")

    assert len(s.result.transcript) == pre_len
    assert [id(e) for e in s.result.transcript] == pre_ids


@pytest.mark.asyncio
async def test_pause_preserves_flow_state():
    """Flow engine reference (and any state on it) survives pause."""
    s = _make_session()
    engine_before = s.flow
    s.flow.turn_count = 7
    s.flow.triggers_fired = {1, 2}

    await s.pause(reason="dtmf_redirect")

    assert s.flow is engine_before
    assert s.flow.turn_count == 7
    assert s.flow.triggers_fired == {1, 2}


@pytest.mark.asyncio
async def test_pause_preserves_audio_buffers():
    """Audio buffers are append-only and must survive pause."""
    s = _make_session()
    s._bot_audio_buffer.extend(b"\x00\x01\x02\x03")
    s._our_audio_buffer.extend(b"\xff\xfe")
    bot_before = bytes(s._bot_audio_buffer)
    our_before = bytes(s._our_audio_buffer)

    await s.pause(reason="dtmf_redirect")

    assert bytes(s._bot_audio_buffer) == bot_before
    assert bytes(s._our_audio_buffer) == our_before


@pytest.mark.asyncio
async def test_pause_preserves_latency_anchor():
    """_we_finished_at must survive so post-DTMF latency calculations stay accurate."""
    s = _make_session()
    s._we_finished_at = 12345.6789
    s._partial_bot_transcript = "Hello there"

    await s.pause(reason="dtmf_redirect")

    assert s._we_finished_at == 12345.6789
    assert s._partial_bot_transcript == "Hello there"


# ----------------------------------------------------------------------
# reattach() guards
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reattach_without_pause_raises():
    """reattach() on a non-transition session is a programmer error."""
    s = _make_session()
    ws = _make_ws_mock()
    with pytest.raises(RuntimeError, match="not in transition"):
        await s.reattach(ws)


# ----------------------------------------------------------------------
# attach_ws() guards
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_ws_refuses_clobber_when_active():
    """attach_ws() refuses to clobber an active WS unless we're paused."""
    s = _make_session()
    s._ws = MagicMock()  # pretend a WS is currently bound
    s._in_transition = False
    fresh = _make_ws_mock()
    with pytest.raises(RuntimeError, match="active WS"):
        await s.attach_ws(fresh)


# ----------------------------------------------------------------------
# Silero VAD reset on reattach
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_count_increments():
    """Each attach_ws bumps the counter even when the inner loop bails."""
    s = _make_session()
    s._stt.start = AsyncMock(side_effect=RuntimeError("bail"))

    ws1 = _make_ws_mock()
    with pytest.raises(RuntimeError):
        await s.attach_ws(ws1)
    assert s._attach_count == 1

    # Pretend a pause happened; reattach increments again.
    s._in_transition = True
    s._conv_task = MagicMock()
    ws2 = _make_ws_mock()
    with pytest.raises(RuntimeError):
        await s.attach_ws(ws2)
    assert s._attach_count == 2


# ----------------------------------------------------------------------
# end_session idempotency
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_session_idempotent():
    """Calling end_session twice is safe."""
    s = _make_session()
    s.start_session()
    await s.end_session()
    assert s._session_ended is True
    assert s.result.status == CallStatus.COMPLETED
    # Second call: no exception, no double-finalisation.
    await s.end_session()
    assert s._session_ended is True
    assert s.result.status == CallStatus.COMPLETED


@pytest.mark.asyncio
async def test_end_session_finalises_status_only_when_in_progress():
    """If the result is already ERROR, end_session doesn't downgrade it."""
    s = _make_session()
    s.start_session()
    s.result.finish(CallStatus.ERROR, "fake error")
    await s.end_session()
    assert s.result.status == CallStatus.ERROR


# ----------------------------------------------------------------------
# Reconnected Event lifecycle
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_wrapper_does_not_end_session_during_transition():
    """Regression guard for the post-Task-5 live-test bug: ``handle()``
    is a thin wrapper around ``attach_ws()``. When ``attach_ws``
    returned mid-transition (DTMF redirect in flight), the wrapper
    was unconditionally calling ``end_session()`` in its finally —
    which finalised the result, fired the completion event, and the
    runner immediately hung up the Twilio call before all 16 DTMF
    tones had finished playing.

    Confirmed behaviour: handle() must skip end_session() when
    _in_transition is True. The session stays alive for the next
    reattach() call to complete the lifecycle.
    """
    s = _make_session()
    ws = _make_ws_mock()

    async def fake_attach(_ws):
        # Simulate attach_ws returning mid-transition (DTMF redirect
        # initiated, WS torn down by Twilio, _in_transition set by
        # _send_dtmf via pause()).
        s._in_transition = True
        s._conv_task = MagicMock()  # bypass reattach guard

    s.attach_ws = fake_attach  # type: ignore[assignment]

    await s.handle(ws)

    # Critical assertion — session MUST NOT be ended.
    assert s._session_ended is False, (
        "handle() called end_session() during transition — would kill "
        "the call mid-DTMF-playback"
    )
    # And the in-transition flag is preserved so the next WS can route
    # to reattach.
    assert s._in_transition is True


@pytest.mark.asyncio
async def test_handle_wrapper_does_end_session_on_clean_close():
    """Sanity check: when attach_ws returns cleanly (no transition),
    handle() MUST still call end_session() so the runner gets its
    completion signal and the call gets hung up normally."""
    s = _make_session()
    ws = _make_ws_mock()

    async def fake_attach(_ws):
        # Clean close — no transition flag set.
        s.result.status = CallStatus.IN_PROGRESS

    s.attach_ws = fake_attach  # type: ignore[assignment]

    await s.handle(ws)

    assert s._session_ended is True


@pytest.mark.asyncio
async def test_reconnected_event_fresh_after_each_pause():
    """Two pause() calls in sequence produce two distinct Event objects."""
    s = _make_session()
    await s.pause(reason="first")
    first_event = s._reconnected

    # End the first transition manually (simulating attach_ws's
    # reattach branch).
    s._in_transition = False

    await s.pause(reason="second")
    second_event = s._reconnected

    assert first_event is not second_event
    assert second_event.is_set() is False
