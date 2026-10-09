"""The REST-DTMF redirect state machine, shared by both sessions (P6).

One implementation, two callers — the legacy V1 `MediaStreamSession` and the
chain-backed `VoiceSession` — because two verbatim copies of a state machine
with three typed failure modes is how the copies drift. The session supplies
the mutable state through the small protocol below; the sequencing, the
event race, and the typed errors live here.

Sequence: pause the session (fresh reconnect/abort events) → Twilio
`Call.update(<Play digits> + <Connect><Stream>)` off-thread → race the
reconnect WS against the status-webhook abort against the timeout. Raises
DTMFRedirectFailedError / DTMFReconnectTimeoutError / DTMFRedirectAbortedError
with the same fields and labels the V2 baseline reports.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from vjudge.transport.dtmf_errors import (
    DTMFReconnectTimeoutError,
    DTMFRedirectAbortedError,
    DTMFRedirectFailedError,
)

logger = logging.getLogger(__name__)


class RedirectableSession(Protocol):
    """What the state machine needs from a session."""

    result: object                        # .call_sid read
    _call_manager: object                 # .play_dtmf_and_reconnect (sync)
    _reconnect_ws_url: str
    _dtmf_reconnect_timeout_s: float
    _reconnected: asyncio.Event
    _aborted: asyncio.Event
    _aborted_event_name: str
    _in_transition: bool
    _attach_count: int

    async def pause(self, reason: str = "") -> None: ...


async def send_dtmf_rest(session: RedirectableSession, digits: str) -> None:
    if session._call_manager is None:
        raise RuntimeError(
            "REST DTMF requested but no CallManager was wired in — pass "
            "call_manager= to the session or set dtmf_transport='audio'."
        )
    if not session._reconnect_ws_url:
        raise RuntimeError(
            "REST DTMF requested but reconnect_ws_url is empty — the "
            "<Connect><Stream> TwiML needs the wss://…/ws/{session_id} URL."
        )
    call_sid = session.result.call_sid
    if not call_sid:
        raise RuntimeError(
            "REST DTMF requested before Twilio sent 'start' — no call_sid "
            "known yet, cannot redirect."
        )

    logger.info("dtmf.redirect.start call_sid=%s digit_count=%d",
                call_sid, len(digits))
    # Pause FIRST so the session's WS-death path doesn't finalise. pause()
    # rebuilds both events — capture references AFTER.
    await session.pause(reason="dtmf_redirect")
    reconnected_evt = session._reconnected
    aborted_evt = session._aborted
    transition_started = time.monotonic()

    try:
        # to_thread around the SYNC method (P1.4) — the seam tests fake.
        await asyncio.to_thread(
            session._call_manager.play_dtmf_and_reconnect,
            call_sid=call_sid, digits=digits,
            reconnect_ws_url=session._reconnect_ws_url,
        )
        logger.info("dtmf.redirect.twiml_ok call_sid=%s", call_sid)
    except Exception as exc:
        # REST failed BEFORE the stream was torn down: roll back the pause so
        # the existing WS keeps working.
        logger.error("dtmf.redirect.failed call_sid=%s exc=%s", call_sid, exc)
        session._in_transition = False
        raise DTMFRedirectFailedError(
            call_sid=call_sid, cause=exc,
            twilio_status=getattr(exc, "status", None),
            twilio_code=getattr(exc, "code", None),
        ) from exc

    recon_task = asyncio.create_task(reconnected_evt.wait())
    abort_task = asyncio.create_task(aborted_evt.wait())
    try:
        done, _ = await asyncio.wait(
            [recon_task, abort_task],
            timeout=session._dtmf_reconnect_timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (recon_task, abort_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    elapsed_ms = (time.monotonic() - transition_started) * 1000.0
    if not done:
        logger.warning("dtmf.redirect.timeout call_sid=%s elapsed_ms=%.0f",
                       call_sid, elapsed_ms)
        raise DTMFReconnectTimeoutError(
            call_sid=call_sid, digit_count=len(digits),
            elapsed_ms=elapsed_ms,
            last_state=f"in_transition={session._in_transition}, "
                       f"attach_count={session._attach_count}",
        )
    if aborted_evt.is_set():
        # _in_transition stays True: the abandoned WS's cleanup path must not
        # finalise — the raise does the result-status work.
        raise DTMFRedirectAbortedError(
            call_sid=call_sid,
            lifecycle_event=session._aborted_event_name or "unknown",
        )
    logger.info(
        "dtmf.redirect.reconnected call_sid=%s total_ms=%.0f attach_count=%d",
        call_sid, elapsed_ms, session._attach_count,
    )
