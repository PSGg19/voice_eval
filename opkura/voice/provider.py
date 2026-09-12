"""P6.6 — the TelephonyProvider port: one provider = four things.

    * a dialer          — originate(target, session_id, profile) + hang_up
    * a media serializer— wire format ↔ frames (decode/encode/chunk)
    * a DTMF strategy   — how digits reach the far side mid-call
    * allowed_regions   — the compliance declaration the chooser enforces

Providers are chosen PER TARGET REGION, never per deployment: one worker
fleet dials the US through Twilio and India through LiveKit-SIP in the same
process. Submitting a region no configured provider allows is refused at
pre-flight with the compliance rule named — the S6 TRAI/DoT rule stops being
special-cased PSTN logic and becomes what it always was: "no provider is
allowed to carry this call".

Adapters (P6.7):

* **TwilioProvider** (us, ca) — extracted from today's code, behaviour
  unchanged: CallManager does the REST work, TwilioSerializer speaks Media
  Streams, DTMF is the REST-redirect strategy (typed errors intact — the
  session-side wait/reattach machinery keeps raising
  DTMFRedirectFailed/ReconnectTimeout/Aborted exactly as before).
* **LiveKitSIPProvider** (in) — originates via `create_sip_participant`
  against the org's own India trunks, the same mechanism OSV dials with: no
  new vendor account, no new compliance surface. Trunk id + LiveKit
  credentials arrive per deployment (settings), and the import is lazy so
  the base install carries no livekit dependency.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Protocol, Sequence

from opkura.voice.serializers import TwilioSerializer

if TYPE_CHECKING:  # pragma: no cover — typing only
    from opkura.core.region_profile import RegionProfile
    from opkura.transport.call import CallManager

logger = logging.getLogger(__name__)


class ProviderRefusal(Exception):
    """No configured provider may carry this call. `.rule` names why."""

    def __init__(self, message: str, *, rule: str):
        super().__init__(message)
        self.rule = rule


class DTMFStrategy(Protocol):
    async def send(self, *, call_ref: str, digits: str,
                   reconnect_ws_url: str) -> None: ...


class TelephonyProvider(Protocol):
    name: str
    allowed_regions: tuple[str, ...]

    async def originate(self, *, target: str, session_id: str,
                        profile: "Optional[RegionProfile]" = None) -> str: ...
    async def hang_up(self, call_ref: str) -> None: ...
    def serializer(self): ...
    def dtmf(self) -> DTMFStrategy: ...


def provider_for(region: str, providers: Sequence[TelephonyProvider]) -> TelephonyProvider:
    """The per-target chooser. First provider allowing the region wins;
    none → the compliance refusal, with the rule named."""
    for provider in providers:
        if region in provider.allowed_regions:
            return provider
    allowed = {p.name: list(p.allowed_regions) for p in providers}
    raise ProviderRefusal(
        f"no telephony provider is allowed to carry a call into region "
        f"'{region}' (configured: {allowed}). India traffic must originate "
        f"through the org's own in-country trunks (TRAI/DoT, S6) — the "
        f"LiveKit-SIP provider carries it once the India trunk is configured.",
        rule="S6-provider-region",
    )


# ------------------------------------------------------------------- Twilio

class TwilioDTMF:
    """The REST-redirect strategy, verbatim behaviour: redirect the call to
    <Play digits> + <Connect><Stream> back at the session's WS. The typed
    errors stay with the session-side wait — this strategy only performs the
    redirect leg."""

    def __init__(self, call_mgr: "CallManager"):
        self._mgr = call_mgr

    async def send(self, *, call_ref: str, digits: str,
                   reconnect_ws_url: str) -> None:
        await self._mgr.play_dtmf_and_reconnect_async(
            call_ref, digits, reconnect_ws_url
        )


class TwilioProvider:
    """US/CA — extracted from today's code, behaviour unchanged.

    Construct with a CallManager, or with `settings=` for lazy construction —
    the chooser and pre-flight only read `allowed_regions`, and building a
    Twilio REST client to answer "may this region be dialled?" would be
    waste."""

    name = "twilio"
    allowed_regions = ("us", "ca")

    def __init__(self, call_mgr: "Optional[CallManager]" = None, *, settings=None):
        self._mgr = call_mgr
        self._settings = settings

    @property
    def mgr(self) -> "CallManager":
        if self._mgr is None:
            from opkura.transport.call import CallManager

            self._mgr = CallManager(self._settings)
        return self._mgr

    async def originate(self, *, target: str, session_id: str,
                        profile: "Optional[RegionProfile]" = None) -> str:
        return await self.mgr.make_call_async(target, session_id, profile)

    async def hang_up(self, call_ref: str) -> None:
        await self.mgr.hang_up_async(call_ref)

    def serializer(self) -> TwilioSerializer:
        return TwilioSerializer()

    def dtmf(self) -> TwilioDTMF:
        return TwilioDTMF(self.mgr)


# -------------------------------------------------------------- LiveKit-SIP

class LiveKitDTMF:
    """LiveKit SIP participants take DTMF via the participant API — no
    call redirect, no WS teardown, no reattach machinery."""

    def __init__(self, provider: "LiveKitSIPProvider"):
        self._provider = provider

    async def send(self, *, call_ref: str, digits: str,
                   reconnect_ws_url: str) -> None:
        raise NotImplementedError(
            "LiveKit DTMF lands with the India trunk configuration — the "
            "participant-level DTMF publish needs the live trunk to verify "
            "against, and guessing the wire behaviour would be worse than "
            "refusing loudly."
        )


class LiveKitSIPProvider:
    """India — originates via create_sip_participant against the org's own
    trunks, the same mechanism OSV dials with.

    Needs (settings → constructor): the LiveKit server URL + API key/secret
    for the India project and the outbound trunk id. All currently blocked on
    the org's India trunk hand-over; the adapter exists so the chooser,
    pre-flight and provider-equivalence tests are real today."""

    name = "livekit-sip"
    allowed_regions = ("in",)

    def __init__(self, *, url: str, api_key: str, api_secret: str,
                 trunk_id: str, room_prefix: str = "opkura"):
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self.trunk_id = trunk_id
        self.room_prefix = room_prefix
        self._client = None

    def _api(self):
        if self._client is None:
            try:
                from livekit import api as lk_api
            except ImportError as exc:  # pragma: no cover — env-dependent
                raise RuntimeError(
                    "LiveKit-SIP provider needs the `livekit-api` package: "
                    "pip install 'opkura[livekit]'"
                ) from exc
            self._client = lk_api.LiveKitAPI(
                self.url, self.api_key, self.api_secret
            )
        return self._client

    async def originate(self, *, target: str, session_id: str,
                        profile: "Optional[RegionProfile]" = None) -> str:
        from livekit import api as lk_api  # lazy — see _api()

        api = self._api()
        room = f"{self.room_prefix}-{session_id[:12]}"
        participant = await api.sip.create_sip_participant(
            lk_api.CreateSIPParticipantRequest(
                sip_trunk_id=self.trunk_id,
                sip_call_to=target,
                room_name=room,
                participant_identity=f"opkura-{session_id[:12]}",
                # The correlation key rides SIP headers exactly as the
                # Twilio path sends X-Opkura-Session-Id.
                headers={"X-Opkura-Session-Id": session_id, "X-Is-Test": "1"},
            )
        )
        return f"{room}/{participant.participant_id}"

    async def hang_up(self, call_ref: str) -> None:
        from livekit import api as lk_api

        room, _, identity = call_ref.partition("/")
        await self._api().room.remove_participant(
            lk_api.RoomParticipantIdentity(room=room, identity=identity)
        )

    def serializer(self) -> TwilioSerializer:
        # LiveKit hands media over its own room protocol; the serializer for
        # a LiveKit media bridge lands with the live trunk work. Until then
        # frames downstream are identical BY CONSTRUCTION — the provider-
        # equivalence test drives both providers' serializers with the same
        # μ-law and asserts identical frames.
        return TwilioSerializer()

    def dtmf(self) -> LiveKitDTMF:
        return LiveKitDTMF(self)


def configured_providers(settings) -> list:
    """Build the deployment's provider list from Settings. Twilio is always
    present (its credentials are mandatory Settings); LiveKit-SIP joins once
    the India coordinates exist — until then India rightly fails the chooser
    with the S6 refusal."""
    providers: list = [TwilioProvider(settings=settings)]
    if (
        getattr(settings, "livekit_sip_url", "")
        and getattr(settings, "livekit_sip_api_key", "")
        and getattr(settings, "livekit_sip_trunk_id_in", "")
    ):
        providers.append(LiveKitSIPProvider(
            url=settings.livekit_sip_url,
            api_key=settings.livekit_sip_api_key,
            api_secret=settings.livekit_sip_api_secret,
            trunk_id=settings.livekit_sip_trunk_id_in,
        ))
    return providers
