"""FastAPI server — Twilio webhook + media stream WebSocket."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from opkura.core.config import Settings
from opkura.core.log_context import call_id_var
from opkura.core.models import CallResult, CallStatus
from .stream import MediaStreamSession

logger = logging.getLogger(__name__)

app = FastAPI(title="Autocalls")

# Job mode (v3) — /v1/jobs. Registered here because Twilio already needs this
# app reachable on a public URL, so jobs get an HTTP surface for free rather
# than standing up a second server. Imported lazily-ish and guarded: a missing
# Redis dependency must not stop the Twilio webhooks from serving, since those
# are what an in-flight call depends on.
try:
    from opkura.jobs.api import hooks_router as _hooks_router
    from opkura.jobs.api import router as _jobs_router

    app.include_router(_jobs_router)
    app.include_router(_hooks_router)
except Exception as _exc:  # pragma: no cover - optional subsystem
    logger.warning("Job API not mounted (%s: %s)", type(_exc).__name__, _exc)

# V3 run API — /v1/runs, /v1/scenarios, /v1/variable-sets, /v1/targets,
# /v1/run-batches. Same defensive mounting as jobs and for the same reason:
# a missing DB driver or unreachable Postgres must not take the Twilio
# webhooks down with it. (Engine creation is lazy — no connection happens
# here — so "unreachable" only surfaces on the first V3 request.)
try:
    from opkura.api import install_v3 as _install_v3

    _install_v3(app)
except Exception as _exc:  # pragma: no cover - optional subsystem
    logger.warning("V3 API not mounted (%s: %s)", type(_exc).__name__, _exc)

# Prometheus exposition (P5.4) — same choice-gating as the rest of
# observability: METRICS_ENABLED=true mounts GET /metrics, anything else
# leaves the route absent entirely.
import os as _os

if _os.getenv("METRICS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
    try:
        from prometheus_client import make_asgi_app as _make_metrics_app

        app.mount("/metrics", _make_metrics_app())
    except Exception as _exc:  # pragma: no cover - optional subsystem
        logger.warning("/metrics not mounted (%s: %s)", type(_exc).__name__, _exc)

# In-memory session registry (session_id → session state)
_sessions: dict[str, dict[str, Any]] = {}


def register_session(session_id: str, session_data: dict[str, Any]):
    _sessions[session_id] = session_data


def get_session(session_id: str) -> dict[str, Any] | None:
    return _sessions.get(session_id)


def remove_session(session_id: str):
    _sessions.pop(session_id, None)


@app.post("/twiml/{session_id}")
async def twiml_webhook(session_id: str):
    """Twilio calls this to get TwiML instructions.

    Returns <Connect><Stream> pointing to our WebSocket endpoint.
    """
    session = get_session(session_id)
    if not session:
        logger.error("Unknown session: %s", session_id)
        resp = VoiceResponse()
        resp.say("Error: unknown test session.")
        resp.hangup()
        return Response(content=str(resp), media_type="application/xml")

    settings: Settings = session["settings"]
    resp = VoiceResponse()
    connect = Connect()
    stream = Stream(url=f"{settings.ws_public_url}/ws/{session_id}")
    connect.append(stream)
    resp.append(connect)

    logger.info("TwiML served for session %s", session_id)
    return Response(content=str(resp), media_type="application/xml")


@app.websocket("/ws/{session_id}")
async def media_stream_ws(websocket: WebSocket, session_id: str):
    """Handle bidirectional audio stream from Twilio."""
    # Phase 2 fix: set the per-call ContextVar HERE too, not just in the
    # runner. FastAPI runs its own asyncio loop in a separate thread —
    # ContextVar state is per-task (within a loop) and does NOT cross the
    # runner-loop / FastAPI-loop boundary. Setting it again here ensures
    # records emitted by stream_session.handle() and its subtasks (STT
    # recv loop, conv loop, TTS WS, barge fires) carry the right call_id
    # for CallLogWriter's _CallIdFilter to accept.
    call_id_token = call_id_var.set(session_id)
    try:
        session = get_session(session_id)
        if not session:
            logger.error("WebSocket for unknown session: %s", session_id)
            await websocket.close()
            return

        stream_session: MediaStreamSession = session["stream_session"]
        completion_event = session["completion_event"]
        # FastAPI server runs in its own thread/loop. The runner's completion_event
        # belongs to a different loop, so we must schedule .set() via that loop's
        # call_soon_threadsafe — direct .set() does NOT wake the runner. Pass both
        # the event and its origin loop into stream_session for the early signal.
        completion_loop = session["completion_loop"]
        stream_session.attach_completion_event(completion_event, completion_loop)

        # Phase 3 (Task 3) — three-way routing on this WS:
        #   (a) In-transition session → reattach() (Twilio just played
        #       <Play digits=> + <Connect><Stream> → new WS for the
        #       SAME session_id; we resume instead of starting fresh).
        #   (b) First attach (no _ws yet) → handle() (today's path).
        #   (c) Duplicate non-transition attach → close + warn
        #       (programmer error or stale Twilio reconnect).
        ws_finalised_in_handler = False
        try:
            if stream_session._in_transition:
                logger.info(
                    "WS reconnect for in-transition session %s (attach_count=%d)",
                    session_id,
                    stream_session._attach_count,
                )
                try:
                    await stream_session.reattach(websocket)
                except Exception as exc:
                    logger.error("Reattach handler error: %s", exc)
                    stream_session.result.finish(CallStatus.ERROR, str(exc))
                # Reattach drives attach_ws which calls end_session()
                # on a clean close — by the time we return here, the
                # session has either ended or is paused for ANOTHER
                # transition (in which case end_session ran already
                # via attach_ws's finally).
            elif stream_session._ws is not None:
                logger.warning(
                    "Duplicate WS for active session %s — refusing and closing",
                    session_id,
                )
                await websocket.close()
                ws_finalised_in_handler = True
            else:
                try:
                    await stream_session.handle(websocket)
                except Exception as exc:
                    logger.error("WebSocket handler error: %s", exc)
                    stream_session.result.finish(CallStatus.ERROR, str(exc))
        finally:
            # Phase 3 fix (post-Task 5 retest): the runner uses
            # completion_event as its "call ended → hang up Twilio"
            # signal. If we fire it during a DTMF transition (the WS
            # closed but the session is paused waiting for reattach),
            # the runner hangs up the call WHILE TWILIO IS PLAYING
            # `<Play digits=>` — killing the DTMF before all 16 tones
            # reach the bot. Only signal completion when the session
            # has actually ended (handle()/attach_ws called
            # end_session) OR when we rejected a duplicate WS.
            should_signal = ws_finalised_in_handler or stream_session._session_ended
            if should_signal:
                try:
                    completion_loop.call_soon_threadsafe(completion_event.set)
                except RuntimeError as exc:
                    logger.warning(
                        "could not schedule completion set on runner loop: %s",
                        exc,
                    )
            else:
                logger.info(
                    "WS closed mid-transition for session %s — withholding "
                    "completion signal so the call stays alive for DTMF playback",
                    session_id,
                )
            # Only deregister the session when it's truly done. If we
            # just rejected a duplicate WS, the original session is
            # still alive and must stay registered.
            if not ws_finalised_in_handler and stream_session._session_ended:
                remove_session(session_id)
    finally:
        call_id_var.reset(call_id_token)


@app.post("/status/{session_id}")
async def status_callback(session_id: str, request: Request):
    """Twilio call status updates.

    Phase 3 (Task 4) — forwards lifecycle events to the session so
    `_send_dtmf_rest` can wake up and raise `DTMFRedirectAbortedError`
    if the call ends mid-transition (e.g. bot hung up between the
    `<Play digits>` and the `<Connect><Stream>` reconnect).
    """
    try:
        form = await request.form()
        event = form.get("CallStatus", "")
    except Exception as exc:  # pragma: no cover — non-form bodies
        logger.warning("status_callback form parse failed: %s", exc)
        form = {}
        event = ""

    # Log the ENTIRE Twilio status-callback payload — this is the only
    # channel where the carrier/terminating leg reports back to us, so
    # it's where any far-end identifier (SipCallId, Called/Caller,
    # ForwardedFrom, etc.) would surface. `CallSid` here is our OUTBOUND
    # leg's SID (matches make_call's), included so per-call debug.log
    # lines can be tied back to the dial. Note: for a plain PSTN target
    # the orchestrator's own interaction UUID does NOT appear here — it
    # lives on the Skit side and must be joined by (caller#, called#, time).
    try:
        payload = {k: v for k, v in form.items()}
    except Exception:  # pragma: no cover — defensive
        payload = {}
    logger.info(
        "Status callback: session=%s event=%s call_sid=%s",
        session_id,
        event,
        payload.get("CallSid", ""),
    )
    logger.debug("Status callback (full payload) session=%s: %s", session_id, payload)
    session = get_session(session_id)
    if session and event:
        stream_session: MediaStreamSession = session["stream_session"]
        try:
            stream_session.on_call_lifecycle(event)
        except Exception as exc:
            logger.warning("on_call_lifecycle raised: %s", exc)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok", "sessions": len(_sessions)}
