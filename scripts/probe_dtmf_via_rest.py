"""Smoke-test: prove Twilio REST `<Play digits=>` reaches the LiveKit bot.

The DTMF problem we hit (see logs around 19:55:20 on the J-001 run):
  * JudgeIt generates clean DTMF audio (verified by decoding caller.wav
    back to "4012000033330125" three times — generation is flawless).
  * Audio reaches Twilio cleanly via Media Streams (event:"media").
  * LiveKit's SIP trunk only listens for RFC 2833 telephone-event/8000
    DTMF — NOT in-band audio-DSP DTMF. The tones arrive at the bot as
    audio bytes and its DTMF callback never fires.

Hypothesis to validate before committing 3-4 days to the "Option 3"
TwiML-redirect + reconnect-stream architecture in stream.py:
  Twilio's native `<Play digits=>` generator emits proper RFC 2833
  events on the outbound SIP leg, which LiveKit *will* deliver to
  the bot agent as DTMF events.

If this script causes the bot to accept the card → architecture works,
proceed with the real implementation. If the bot still says "Sorry, I
wasn't able to get that" → LiveKit's SIP trunk DTMF mode is even
narrower than expected and we need to talk to the LiveKit team
before spending engineering time.

Cost: ~$0.01 — one in-flight call modified once. The call ends
after `<Play digits=>` finishes because we don't include a
`<Connect><Stream>` continuation (that's the next test, after this
one validates the core DTMF-event delivery).

Usage:
  1. Start a J-001 (or any DTMF-path) scenario in judge_it.
  2. Watch judge_it logs for the `Call created: sid=CA...` line and
     also for the bot's `<DTMF:16>` prompt. The window between
     those is when you want to fire this script.
  3. Run:
       python scripts/probe_dtmf_via_rest.py CAXXXXXXXXXXXXXXXX
     Optionally override the digit string:
       python scripts/probe_dtmf_via_rest.py CAXXX 1234567890123456
  4. Watch judge_it logs (or the bot side if you have access) for the
     bot's next response. PASS = bot acknowledges the card / proceeds
     to expiry. FAIL = bot says "Sorry, I wasn't able to get that"
     a fourth time.
"""

from __future__ import annotations

import sys

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from judge_it.core.config import Settings


DEFAULT_DIGITS = "4012000033330125"  # variables.json cardNo (test Visa PAN, declines at processor)


def main() -> int:
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("Usage: python scripts/probe_dtmf_via_rest.py <call_sid> [digits]")
        print("       digits defaults to 4012000033330125 (the test card).")
        print("       Use 'w' for a 0.5s pause, e.g. 'w1234' for pause+1234.")
        return 2

    call_sid = sys.argv[1]
    digits = sys.argv[2] if len(sys.argv) == 3 else DEFAULT_DIGITS

    if not call_sid.startswith("CA"):
        print(f"WARN: call_sid '{call_sid}' doesn't start with 'CA' — that's the")
        print("      Twilio Call SID prefix. Did you pass a Stream SID by mistake?")

    settings = Settings.from_env()
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    # Minimal TwiML: just play the digits via Twilio's native generator,
    # which produces proper RFC 2833 telephone-event/8000 RTP events on
    # the outbound SIP leg. The call will end after the Play finishes
    # because we omit any continuation — this is intentional for the
    # smoke test. The full Option-3 implementation would chain into a
    # `<Connect><Stream url="..."/></Connect>` to re-establish the
    # Media Stream after the digits play.
    twiml = f'<Response><Play digits="{digits}"/></Response>'

    print(f"Call SID:    {call_sid}")
    print(f"Digits:      {digits}")
    print(f"TwiML:       {twiml}")
    print(f"")
    print(f"Firing call.update — this WILL terminate the active Media Stream.")
    print(f"Watch the bot side for DTMF event arrival.")
    print(f"")

    try:
        call = client.calls(call_sid).update(twiml=twiml)
    except TwilioRestException as exc:
        print(f"FAIL — Twilio rejected the update:")
        print(f"  status={exc.status} code={exc.code} msg={exc.msg}")
        if exc.code == 21220:
            print(f"  (21220 = call not in-progress; did the scenario finish?)")
        return 1

    print(f"OK — Twilio accepted the update.")
    print(f"  call.status = {call.status}")
    print(f"  call.sid    = {call.sid}")
    print(f"")
    print(f"What to look for next (in order of confidence):")
    print(f"  1. Bot says something like 'Got it, the card ending in 0125'")
    print(f"     → PASS. RFC 2833 path works. Proceed with Option-3 build.")
    print(f"  2. Bot says 'Sorry, I wasn't able to get that' again")
    print(f"     → FAIL. LiveKit SIP trunk DTMF mode mismatch — DON'T")
    print(f"       build Option 3 yet, talk to LiveKit team first.")
    print(f"  3. Call drops immediately / error in Twilio Debugger")
    print(f"     → TwiML transition issue — investigate before any build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())