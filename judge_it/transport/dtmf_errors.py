"""Typed DTMF-redirect errors (extracted from the stream monolith, P6).

Shared by the legacy V1 session (transport/stream.py) and the chain-backed
V2 session (voice/session.py). The runner and scenario reports key on the
distinct types — DTMF REDIRECT FAILED / JUDGE_IT DTMF TIMEOUT / DTMF
REDIRECT ABORTED — so these classes and their fields are a stable surface.
"""
from __future__ import annotations


class DTMFReconnectTimeoutError(Exception):
    """Raised when the new Media Stream WS never arrives within
    `Settings.dtmf_reconnect_timeout_s` after Twilio accepted the
    `<Play digits>` redirect TwiML.

    Common causes: misconfigured `public_url` (the `<Connect><Stream>`
    URL points somewhere judge_it can't be reached), Twilio outage,
    Twilio's TwiML execution stalling. Runner translates this to
    `JUDGE_IT DTMF TIMEOUT` in the scenario result.
    """

    def __init__(
        self,
        call_sid: str,
        digit_count: int,
        elapsed_ms: float,
        last_state: str = "",
    ):
        self.call_sid = call_sid
        self.digit_count = digit_count
        self.elapsed_ms = elapsed_ms
        self.last_state = last_state
        super().__init__(
            f"DTMF reconnect timeout after {elapsed_ms:.0f}ms "
            f"(call={call_sid}, digits={digit_count}, last_state={last_state!r})"
        )


class DTMFRedirectFailedError(Exception):
    """Raised when Twilio's REST `call.update(twiml=...)` rejected the
    redirect — e.g. invalid auth, call not in-progress, Twilio API
    outage. Runner translates this to `BOT FAILURE` (or similar
    transport-level fault label) in the scenario result.

    No pause has happened yet at the point this is raised, so the
    session and its existing WS are still usable.
    """

    def __init__(
        self,
        call_sid: str,
        cause: Exception,
        twilio_status: int | None = None,
        twilio_code: int | None = None,
    ):
        self.call_sid = call_sid
        self.cause = cause
        self.twilio_status = twilio_status
        self.twilio_code = twilio_code
        super().__init__(
            f"DTMF redirect failed for call={call_sid}: "
            f"status={twilio_status} code={twilio_code} cause={cause}"
        )


class DTMFRedirectAbortedError(Exception):
    """Raised when Twilio's status callback fires `completed` / `failed`
    / `canceled` / `no-answer` while we're mid-transition, before the
    new Media Stream WS could connect back. The conv loop was still
    blocked on `_reconnected.wait()`.

    Typical cause: bot or carrier hung up the call during the
    `<Play digits>` execution.
    """

    def __init__(
        self,
        call_sid: str,
        lifecycle_event: str,
    ):
        self.call_sid = call_sid
        self.lifecycle_event = lifecycle_event
        super().__init__(
            f"DTMF redirect aborted: call={call_sid} ended with "
            f"lifecycle event={lifecycle_event!r} mid-transition"
        )
