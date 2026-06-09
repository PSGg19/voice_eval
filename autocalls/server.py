"""FastAPI server — Twilio webhook + media stream WebSocket."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.responses import Response
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from .config import Settings
from .models import CallResult, CallStatus
from .stream import MediaStreamSession

logger = logging.getLogger(__name__)

app = FastAPI(title="Autocalls")

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
    session = get_session(session_id)
    if not session:
        logger.error("WebSocket for unknown session: %s", session_id)
        await websocket.close()
        return

    stream_session: MediaStreamSession = session["stream_session"]
    completion_event = session["completion_event"]

    try:
        await stream_session.handle(websocket)
    except Exception as exc:
        logger.error("WebSocket handler error: %s", exc)
        stream_session.result.finish(CallStatus.ERROR, str(exc))
    finally:
        completion_event.set()
        remove_session(session_id)


@app.post("/status/{session_id}")
async def status_callback(session_id: str):
    """Twilio call status updates."""
    logger.debug("Status callback for session %s", session_id)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok", "sessions": len(_sessions)}
