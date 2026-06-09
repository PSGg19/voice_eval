"""Twilio outbound call management."""

from __future__ import annotations

import logging

from twilio.rest import Client

from .config import Settings

logger = logging.getLogger(__name__)


class CallManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    def make_call(self, to: str, session_id: str) -> str:
        """Initiate an outbound call. Returns the call SID."""
        twiml_url = f"{self.settings.public_url}/twiml/{session_id}"
        status_url = f"{self.settings.public_url}/status/{session_id}"

        call = self.client.calls.create(
            to=to,
            from_=self.settings.twilio_phone_number,
            url=twiml_url,
            status_callback=status_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )

        logger.info("Call created: sid=%s to=%s", call.sid, to)
        return call.sid

    def hang_up(self, call_sid: str):
        """Terminate an active call."""
        try:
            self.client.calls(call_sid).update(status="completed")
            logger.info("Call hung up: %s", call_sid)
        except Exception as exc:
            logger.warning("Failed to hang up %s: %s", call_sid, exc)
