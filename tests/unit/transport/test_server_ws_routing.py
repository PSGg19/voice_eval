"""Tests for Phase 3 Task 3 — the WS endpoint's reconnect-routing logic
in `vjudge.transport.server.media_stream_ws`.

The endpoint has three branches:
  (a) session._in_transition is True   → call session.reattach(ws)
  (b) session._ws is None              → call session.handle(ws)
  (c) session._ws is not None AND not in_transition
                                       → close the new ws + warn

These tests poke `media_stream_ws` directly with mocked sessions /
mocked WebSockets, avoiding the FastAPI test client and any real
Twilio plumbing.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from vjudge.transport import server as srv_mod


@pytest.fixture(autouse=True)
def _isolate_session_registry(monkeypatch):
    """Each test gets a fresh _sessions dict so registrations don't leak."""
    monkeypatch.setattr(srv_mod, "_sessions", {})
    yield


def _make_mock_session(*, in_transition=False, has_ws=False, session_ended=False):
    """Build a MediaStreamSession-shaped mock for the endpoint to dispatch on."""
    s = MagicMock()
    s._in_transition = in_transition
    s._ws = MagicMock() if has_ws else None
    s._attach_count = 1 if (has_ws or in_transition) else 0
    s._session_ended = session_ended
    s.result = MagicMock()
    s.handle = AsyncMock()
    s.reattach = AsyncMock()
    s.attach_completion_event = MagicMock()
    return s


def _make_mock_ws():
    ws = MagicMock()
    ws.close = AsyncMock()
    return ws


def _register(session_id: str, stream_session, *, end_after=False):
    """Mimic register_session() with the minimum fields the endpoint reads."""
    completion_event = MagicMock()
    completion_loop = MagicMock()
    completion_loop.call_soon_threadsafe = MagicMock()

    if end_after:
        async def mark_ended(*a, **kw):
            stream_session._session_ended = True

        stream_session.handle.side_effect = mark_ended
        stream_session.reattach.side_effect = mark_ended

    srv_mod._sessions[session_id] = {
        "settings": MagicMock(),
        "stream_session": stream_session,
        "completion_event": completion_event,
        "completion_loop": completion_loop,
        "result": stream_session.result,
    }
    return completion_event, completion_loop


# ----------------------------------------------------------------------
# (a) reattach branch
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_transition_session_routes_to_reattach():
    s = _make_mock_session(in_transition=True)
    _register("sess_a", s, end_after=True)
    ws = _make_mock_ws()

    await srv_mod.media_stream_ws(ws, "sess_a")

    s.reattach.assert_awaited_once_with(ws)
    s.handle.assert_not_called()


# ----------------------------------------------------------------------
# (b) first-attach branch
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_attach_routes_to_handle():
    s = _make_mock_session(in_transition=False, has_ws=False)
    _register("sess_b", s, end_after=True)
    ws = _make_mock_ws()

    await srv_mod.media_stream_ws(ws, "sess_b")

    s.handle.assert_awaited_once_with(ws)
    s.reattach.assert_not_called()


# ----------------------------------------------------------------------
# (c) duplicate WS rejection
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_ws_when_not_in_transition_rejected_and_closed():
    s = _make_mock_session(in_transition=False, has_ws=True)
    _register("sess_c", s, end_after=False)
    ws = _make_mock_ws()

    await srv_mod.media_stream_ws(ws, "sess_c")

    # The new (duplicate) WS got closed.
    ws.close.assert_awaited_once()
    # Neither handle nor reattach should have been invoked.
    s.handle.assert_not_called()
    s.reattach.assert_not_called()
    # Original session is still registered — it's still in flight.
    assert "sess_c" in srv_mod._sessions


# ----------------------------------------------------------------------
# Unknown session
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_session_id_closes_ws():
    ws = _make_mock_ws()
    await srv_mod.media_stream_ws(ws, "nonexistent")
    ws.close.assert_awaited_once()


# ----------------------------------------------------------------------
# Session deregistration
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_deregistered_when_ended():
    s = _make_mock_session(in_transition=False, has_ws=False)
    _register("sess_d", s, end_after=True)
    ws = _make_mock_ws()

    await srv_mod.media_stream_ws(ws, "sess_d")

    # End signalled inside handle.side_effect → session removed.
    assert "sess_d" not in srv_mod._sessions


@pytest.mark.asyncio
async def test_session_kept_registered_when_transition_close_pending():
    """attach_ws returned mid-transition (waiting for reattach) — the
    session must stay registered so the next WS for this session_id
    can find it."""
    s = _make_mock_session(in_transition=False, has_ws=False)
    _register("sess_e", s, end_after=False)
    ws = _make_mock_ws()

    # handle() returns without marking the session ended — simulates
    # attach_ws returning during a transition.
    s.handle.return_value = None

    await srv_mod.media_stream_ws(ws, "sess_e")

    # Session must still be there for the reconnect to find it.
    assert "sess_e" in srv_mod._sessions


# ----------------------------------------------------------------------
# Completion signal always fires
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_event_signalled_when_session_ends():
    """When handle() finishes and the session is fully ended, the
    runner gets its completion signal so it can call Twilio hangup."""
    s = _make_mock_session(in_transition=False, has_ws=False)
    completion_event, completion_loop = _register("sess_f", s, end_after=True)
    ws = _make_mock_ws()

    await srv_mod.media_stream_ws(ws, "sess_f")

    completion_loop.call_soon_threadsafe.assert_called_once_with(
        completion_event.set
    )


@pytest.mark.asyncio
async def test_completion_event_withheld_mid_transition():
    """When attach_ws returns DURING a DTMF transition, completion
    MUST NOT fire — the runner would hang up the call mid-DTMF.

    This is the post-Task-5 fix: vjudge saw `attach_ws returning in
    transition` immediately followed by a Twilio hangup API call
    during the live J-001 retest, killing the call while
    `<Play digits>` was still playing the 16 DTMF tones.
    """
    s = _make_mock_session(in_transition=False, has_ws=False)
    completion_event, completion_loop = _register("sess_g", s, end_after=False)
    ws = _make_mock_ws()
    # handle() returns without marking the session ended — exactly
    # what happens when attach_ws sees _in_transition=True in its
    # finally clause and bails out early.

    await srv_mod.media_stream_ws(ws, "sess_g")

    completion_loop.call_soon_threadsafe.assert_not_called()
    # Session must still be registered for the reconnect to find it.
    assert "sess_g" in srv_mod._sessions


@pytest.mark.asyncio
async def test_completion_event_signalled_on_duplicate_ws_rejection():
    """Duplicate-WS branch returns immediately after closing the new
    WS — completion should fire so the test runner / FastAPI handler
    is not stuck."""
    s = _make_mock_session(in_transition=False, has_ws=True)
    completion_event, completion_loop = _register("sess_h", s, end_after=False)
    ws = _make_mock_ws()

    await srv_mod.media_stream_ws(ws, "sess_h")

    completion_loop.call_soon_threadsafe.assert_called_once_with(
        completion_event.set
    )
