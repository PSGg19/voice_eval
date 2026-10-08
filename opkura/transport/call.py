"""Twilio outbound call management."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from twilio.rest import Client

from opkura.core.config import Settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opkura.core.region_profile import RegionProfile
from opkura.transport.dtmf import validate_dtmf_string
from opkura.transport.sip import build_sip_uri

logger = logging.getLogger(__name__)

# Value substituted for the SIP auth password in log lines. The request
# params are dumped at DEBUG into the per-call debug.log, which is kept
# as a run artifact — the password must never land there.
_REDACTED = "***redacted***"


def _dump(obj) -> str:
    """Best-effort JSON dump for logging arbitrary payloads.

    Falls back to str() for non-serializable values (datetimes, Decimals,
    nested Twilio objects) so a logging call can never raise and break a
    real call. Returns a placeholder string on total failure.
    """
    try:
        return json.dumps(obj, default=str, sort_keys=True)
    except Exception as exc:  # pragma: no cover - defensive
        return f"<unloggable: {exc}>"


def build_dtmf_redirect_twiml(digits: str, reconnect_ws_url: str) -> str:
    """Build the TwiML that plays DTMF as RFC 2833 then re-establishes
    a Media Stream connection back to opkura.

    Twilio's native `<Play digits>` generator emits proper RFC 2833
    telephone-event/8000 RTP events on the outbound SIP leg — that's
    what a LiveKit-based bot's SIP DTMF handler decodes. The trailing
    `<Connect><Stream>` reopens our Media Streams WebSocket so the
    conversation can resume after DTMF playback finishes.

    `reconnect_ws_url` must be the same `wss://…/ws/{session_id}` URL
    opkura's server already serves on first attach — server.py's WS
    endpoint will see the new connection, look up the still-alive
    session by session_id, see `_in_transition is True`, and route
    to `MediaStreamSession.reattach()` (Task 3).
    """
    validate_dtmf_string(digits)
    if not reconnect_ws_url.startswith(("ws://", "wss://")):
        raise ValueError(
            f"reconnect_ws_url must be ws:// or wss://, got {reconnect_ws_url!r}"
        )
    return (
        "<Response>"
        f'<Play digits="{digits}"/>'
        "<Connect>"
        f'<Stream url="{reconnect_ws_url}"/>'
        "</Connect>"
        "</Response>"
    )


class CallManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    def _resolve_destination(
        self, to: str, session_id: str, profile: "RegionProfile | None" = None
    ) -> tuple[str, dict]:
        """Turn the scenario `target` into the `To` value Twilio dials.

        Returns `(dial_to, extra_create_kwargs)`.

        Under `call_transport="pstn"` this is the identity function and no
        extra kwargs — the historical behavior, unchanged.

        Under `call_transport="sip"` Twilio INVITEs the LiveKit SIP host
        directly instead of routing over the PSTN to the bot's DID. Note
        which value goes where:

          * user part  → `sip_trunk_number` (LiveKit's inbound-trunk
            identifier, e.g. 1205). This selects the TRUNK.
          * `to`, i.e. the scenario `target` → the X-VirtualNumber
            header. This selects the BOT, via the trunk's
            headersToAttributes → `virtualNumber` mapping.

        Putting the DID in the user part instead would reach the trunk
        but leave `virtualNumber` unset on the participant.

        `X-Opkura-Session-Id` is the correlation key. It is opkura's
        session_id rather than the Twilio Call SID because the SID does
        not exist until the `calls.create` below returns — see the module
        docstring in transport/sip.py. `X-Is-Test` marks the call as
        synthetic so the bot side can flag the interaction (the `is_test`
        field docs/platform-evolution.md §5a wants).
        """
        if self.settings.call_transport != "sip":
            return to, {}

        # Region-wise trunk selection (P3.9): a resolved RegionProfile names
        # WHICH LiveKit ingress this run dials — India's project for an India
        # bot, not whatever the global .env happens to hold. No profile means
        # the deployment default (the historical behaviour, and the V1 flow
        # path, which has no region concept). Either way the target `to` stays
        # a HEADER: the trunk number is the user part, never the DID.
        host = profile.sip_destination_host if profile else self.settings.sip_destination_host
        trunk = profile.sip_trunk_number if profile else self.settings.sip_trunk_number

        headers = {
            **self.settings.parsed_sip_extra_headers,
            self.settings.sip_virtual_number_header: to,
            "X-Opkura-Session-Id": session_id,
            # "1" hides the interaction from llm-studio's conversations view
            # and suppresses only the compliance event — the same trade
            # text_is_test already rejected. Default false: visible calls.
            "X-Is-Test": "1" if self.settings.voice_is_test else "0",
        }
        dial_to = build_sip_uri(
            user=trunk,
            host=host,
            transport=self.settings.sip_transport,
            headers=headers,
        )
        extra: dict = {}
        if self.settings.sip_auth_username:
            extra["sip_auth_username"] = self.settings.sip_auth_username
            extra["sip_auth_password"] = self.settings.sip_auth_password
        return dial_to, extra

    def make_call(
        self, to: str, session_id: str, profile: "RegionProfile | None" = None
    ) -> str:
        """Initiate an outbound call. Returns the call SID.

        `profile` (optional) is the run's resolved RegionProfile: in SIP mode
        it selects which region's LiveKit ingress receives the INVITE. Omitted,
        the deployment-default trunk is dialled — the historical behaviour.
        """
        twiml_url = f"{self.settings.public_url}/twiml/{session_id}"
        status_url = f"{self.settings.public_url}/status/{session_id}"

        # `to` may be rewritten into a SIP URI here (call_transport="sip").
        # Everything downstream — TwiML, media streams, the DTMF redirect —
        # is transport-agnostic: Twilio fetches the TwiML at `url` once the
        # SIP call connects, exactly as it does for a PSTN answer.
        dial_to, extra_kwargs = self._resolve_destination(to, session_id, profile)

        # The exact params we hand to Twilio's REST API — logged BEFORE
        # dialing so a call that fails at create() still leaves a record of
        # what we asked for. DEBUG lands in the per-call debug.log.
        request_params = {
            "to": dial_to,
            "from_": self.settings.twilio_phone_number,
            "url": twiml_url,
            "status_callback": status_url,
            "status_callback_event": [
                "initiated",
                "ringing",
                "answered",
                "completed",
            ],
            "status_callback_method": "POST",
            "session_id": session_id,
            "call_transport": self.settings.call_transport,
            **{
                k: (_REDACTED if k == "sip_auth_password" else v)
                for k, v in extra_kwargs.items()
            },
        }
        # `target` is logged separately from `to`: in SIP mode the bot
        # number lives in the X-VirtualNumber header, so `to` alone (the
        # trunk URI) is identical across every scenario and tells you
        # nothing about which bot was dialed.
        logger.info(
            "Initiating call: transport=%s target=%s to=%s session_id=%s",
            self.settings.call_transport,
            to,
            dial_to,
            session_id,
        )
        logger.debug("Call request params: %s", _dump(request_params))

        call = self.client.calls.create(
            to=dial_to,
            from_=self.settings.twilio_phone_number,
            url=twiml_url,
            status_callback=status_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
            **extra_kwargs,
        )

        # Dump every attribute Twilio returned on the Call instance so a
        # failed/odd call can be debugged from the log alone (status,
        # direction, price, api_version, subresource_uris, error_code, …).
        # `_properties` is the raw response dict the SDK hydrates from; fall
        # back to a hand-built dict if the SDK internals ever change.
        try:
            call_props = dict(getattr(call, "_properties", None) or {})
        except Exception:  # pragma: no cover - defensive
            call_props = {}
        if not call_props:
            call_props = {
                "sid": call.sid,
                "status": getattr(call, "status", None),
                "direction": getattr(call, "direction", None),
                "to": getattr(call, "to", None),
                "from": getattr(call, "from_", None),
                "date_created": getattr(call, "date_created", None),
            }

        logger.info(
            "Call created: sid=%s to=%s status=%s",
            call.sid,
            dial_to,
            call_props.get("status"),
        )
        logger.debug("Call response (full): %s", _dump(call_props))
        return call.sid

    def hang_up(self, call_sid: str):
        """Terminate an active call."""
        try:
            self.client.calls(call_sid).update(status="completed")
            logger.info("Call hung up: %s", call_sid)
        except Exception as exc:
            logger.warning("Failed to hang up %s: %s", call_sid, exc)

    # ---- async facades (P1.4) -------------------------------------------
    # The Twilio SDK is synchronous HTTP; called bare from a coroutine it
    # blocks the WHOLE event loop for a full round trip per call — measured
    # saturation around ~450 concurrent. These push the block onto a worker
    # thread; the sync methods stay for non-async callers.

    async def make_call_async(
        self, to: str, session_id: str, profile: "RegionProfile | None" = None
    ) -> str:
        import asyncio

        return await asyncio.to_thread(self.make_call, to, session_id, profile)

    async def hang_up_async(self, call_sid: str) -> None:
        import asyncio

        await asyncio.to_thread(self.hang_up, call_sid)

    async def play_dtmf_and_reconnect_async(
        self, call_sid: str, digits: str, reconnect_ws_url: str
    ) -> None:
        import asyncio

        await asyncio.to_thread(
            self.play_dtmf_and_reconnect,
            call_sid=call_sid, digits=digits, reconnect_ws_url=reconnect_ws_url,
        )

    def play_dtmf_and_reconnect(
        self, call_sid: str, digits: str, reconnect_ws_url: str
    ):
        """Redirect an active call to play `digits` as RFC 2833 DTMF
        and then re-establish a Media Stream back to opkura at
        `reconnect_ws_url`.

        Side effects:
          * Terminates the current `<Connect><Stream>` execution on
            the Twilio call (the current Media Stream WS will close
            shortly after).
          * Plays the DTMF tones via Twilio's native generator —
            arrives at LiveKit as RFC 2833 events.
          * Opens a NEW Media Stream WebSocket back to opkura at
            the given URL once DTMF playback finishes. The session
            on opkura side must be in `_in_transition` to route this
            new WS to `reattach()` instead of treating it as a fresh
            session (server.py Task 3).

        Returns the updated Call object on success. Raises
        `TwilioRestException` on REST failure (caller decides retry /
        abort policy).

        Logs digit COUNT only — never the raw digit string — so
        card numbers etc. don't leak into log aggregation.
        """
        twiml = build_dtmf_redirect_twiml(digits, reconnect_ws_url)
        logger.info(
            "Initiating DTMF redirect: call_sid=%s digit_count=%d reconnect_ws=%s",
            call_sid,
            len(digits),
            reconnect_ws_url,
        )
        return self.client.calls(call_sid).update(twiml=twiml)
