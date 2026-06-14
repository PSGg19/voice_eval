"""Smoke-test: prove Twilio's SIP INVITE reaches LiveKit with our headers.

Validates the `call_transport="sip"` path in isolation, BEFORE trusting a
full scenario run to it. Three separate things can fail and they look
identical from a scenario log ("the call never connected"):

  1. Twilio rejects the URI            → REST 400, no call placed.
  2. LiveKit rejects the INVITE        → call `failed`, SIP 403/404 in
                                         the call events. Usually an IP
                                         allow-list built for Elastic SIP
                                         Trunking egress IPs, which are
                                         NOT the SIP-interface IPs this
                                         path dials from.
  3. LiveKit accepts but routes wrong  → call connects, no bot joins, or
                                         the WRONG bot answers because
                                         X-VirtualNumber didn't survive
                                         (e.g. `%2B` not decoded, or the
                                         trunk's headersToAttributes key
                                         doesn't match our header name).

This probe distinguishes them. It deliberately does NOT use opkura's
`url=` TwiML callback, so **no ngrok / AUTOCALLS_PUBLIC_URL is needed** —
inline TwiML holds the call open long enough for the bot to answer and
start its greeting, then hangs up.

Cost: one short call (~15s).

Usage:
  # See the exact URI without dialing anything — free.
  python scripts/probe_sip_invite.py --dry-run

  # Place the real probe call (reads .env from the current directory).
  python scripts/probe_sip_invite.py

  # Point at a .env living elsewhere (e.g. the main checkout).
  python scripts/probe_sip_invite.py --env-file /path/to/opkura/.env

  # Take the target from a scenario YAML instead of passing it by hand.
  python scripts/probe_sip_invite.py \
      --scenario clients/veros/scenarios/ib2-qa-replication/QA-01-language-flip-on-closing.yaml

  # Override the caller ID (must be a Twilio number on this account).
  python scripts/probe_sip_invite.py --from +12202206097

GETTING THE REAL SIP RESPONSE CODE. Twilio does not surface it anywhere
for these failures — no `error_code` on the Call, no Debugger alert, no
Voice Insights summary. A failed SIP call looks identical whether the far
end said 404 or never answered. Two things that do discriminate:

  * Time-to-terminal. A ~0.5s failure is an explicit SIP error response.
    A ~9s failure is a timeout (compare against a domain running no SIP
    service, e.g. sip:nobody@example.com, which times out in ~9s).
  * Ask the far end yourself. Open a TCP socket to the SIP host and send
    a hand-built INVITE with SDP, then read PAST the provisional
    responses — `100 Processing` and `180 Ringing` are not the answer.
    LiveKit's real verdict arrives after them, e.g.
    `404 Does not match Trunks or Dispatch Rules` (an inbound trunk
    matched but no dispatch rule covers the called user) versus
    `404 No trunk found` (nothing matched at all).

Note LiveKit Cloud answered OPTIONS/INVITE on TCP:5060 and TLS:5061 but
not on UDP from a NAT'd source, so a UDP probe run from a laptop proves
nothing about UDP reachability from Twilio.

Then feed the printed Call SID to the bot-side tooling to confirm what
LiveKit actually saw:
  /skit-call-debug <interaction-or-call-id> --deep
The thing to look for is the SIP participant's `virtualNumber` attribute
matching --target, which is what proves the header round-tripped.

Or query the interaction directly (US Orchestrator Service = Metabase
db 63), which is how this path was first confirmed:

    SELECT key, value
    FROM interactions, jsonb_each_text(interaction_metadata::jsonb)
    WHERE uuid = '<interaction_uuid>'
      AND (key ILIKE '%virtual%' OR key ILIKE 'x-%' OR key = 'ani');

A passing run shows `x-virtualnumber` / `virtual_number` = the --target
with a literal '+', plus `x-opkura-session-id` and `x-is-test`.
"""

from __future__ import annotations

import argparse
import sys
import time

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from opkura.core.config import Settings
from opkura.transport.call import CallManager

# QA-01's target — the Veros IB 2.0 number the replication scenarios dial.
DEFAULT_TARGET = "+18447331982"

# The scenario the SIP path is being validated against.
DEFAULT_SCENARIO = (
    "clients/veros/scenarios/ib2-qa-replication/"
    "QA-01-language-flip-on-closing.yaml"
)

# Caller ID reserved for these probes.
DEFAULT_FROM = "+12202206097"

# Long enough for LiveKit to answer, dispatch an agent, and begin the
# greeting (so bot-side logs show a real session), short enough to stay
# cheap. We say nothing — this probe tests signaling, not conversation.
PROBE_TWIML = '<Response><Pause length="15"/></Response>'

# Statuses that mean Twilio is done with the call.
TERMINAL = {"completed", "failed", "busy", "no-answer", "canceled"}


def _target_from_scenario(path: str) -> str:
    """Read `target:` out of a scenario YAML.

    Uses a plain YAML load rather than the scenario loader because the
    loader renders Jinja and validates the whole file; a probe should not
    fail because an unrelated field drifted.
    """
    import yaml

    with open(path) as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or not data.get("target"):
        raise ValueError(f"no `target:` found in {path}")
    return str(data["target"])


def _load_settings(env_file: str | None) -> Settings:
    if env_file:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    return Settings.from_env()


def _resolve_uri(settings: Settings, target: str) -> str:
    """Build the URI exactly as make_call would, via the same code path.

    Reusing CallManager._resolve_destination rather than re-deriving the
    URI here is the point: a probe that builds its own URI can pass while
    production dials something different.
    """
    with_client = object.__new__(CallManager)
    with_client.settings = settings
    dial_to, extra = with_client._resolve_destination(target, "probe-session")
    if extra:
        print(f"  sip auth:  username={extra.get('sip_auth_username')!r} (password set)")
    return dial_to


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--target",
        default=None,
        help=f"bot number (E.164). Default: read from --scenario, else {DEFAULT_TARGET}",
    )
    ap.add_argument(
        "--scenario",
        default=DEFAULT_SCENARIO,
        help="scenario YAML to take `target` from (default: the QA-01 Veros scenario)",
    )
    ap.add_argument(
        "--from",
        dest="from_number",
        default=DEFAULT_FROM,
        help=f"caller ID, a Twilio number on this account (default {DEFAULT_FROM})",
    )
    ap.add_argument("--env-file", default=None, help="path to a .env to load")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the URI and exit without placing a call",
    )
    ap.add_argument(
        "--wait", type=int, default=25, help="seconds to poll for a terminal status"
    )
    args = ap.parse_args()

    try:
        settings = _load_settings(args.env_file)
    except Exception as exc:
        print(f"FAIL — could not load settings: {exc}")
        return 2

    if settings.call_transport != "sip":
        print(
            f"FAIL — AUTOCALLS_CALL_TRANSPORT is {settings.call_transport!r}, not 'sip'.\n"
            "       This probe tests the SIP path; set it (and "
            "AUTOCALLS_SIP_DESTINATION_HOST /\n"
            "       AUTOCALLS_SIP_TRUNK_NUMBER) before running."
        )
        return 2

    # Explicit --target wins; otherwise take it from the scenario so the
    # probe dials whatever the real run would dial.
    target = args.target
    target_source = "--target"
    if not target:
        try:
            target = _target_from_scenario(args.scenario)
            target_source = args.scenario
        except Exception as exc:
            print(f"WARN — could not read target from {args.scenario}: {exc}")
            target = DEFAULT_TARGET
            target_source = "built-in default"

    try:
        dial_to = _resolve_uri(settings, target)
    except Exception as exc:
        print(f"FAIL — could not build the SIP URI: {exc}")
        return 2

    print("Resolved dial parameters")
    print(f"  From:      {args.from_number}")
    print(f"  To:        {dial_to}")
    print(f"  target:    {target}  (carried in "
          f"{settings.sip_virtual_number_header}, not the user part)")
    print(f"  target from: {target_source}")
    print(f"  TwiML:     {PROBE_TWIML}  (inline — no public URL needed)")
    print()

    if args.dry_run:
        print("--dry-run — no call placed.")
        return 0

    print(f"Placing a REAL call to {settings.sip_destination_host} …")
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    create_kwargs = {"to": dial_to, "from_": args.from_number,
                     "twiml": PROBE_TWIML}
    if settings.sip_auth_username:
        create_kwargs["sip_auth_username"] = settings.sip_auth_username
        create_kwargs["sip_auth_password"] = settings.sip_auth_password

    try:
        call = client.calls.create(**create_kwargs)
    except TwilioRestException as exc:
        print(f"FAIL (1) — Twilio rejected the request; no INVITE was sent.")
        print(f"  status={exc.status} code={exc.code} msg={exc.msg}")
        print()
        print("  Most likely the SIP URI is malformed, or `from` isn't a number")
        print("  on this account. Fix before looking at LiveKit.")
        return 1

    print(f"  Twilio accepted it. Call SID: {call.sid}")
    print(f"  Poll for up to {args.wait}s …")
    print()

    final = call
    for _ in range(args.wait):
        time.sleep(1)
        final = client.calls(call.sid).fetch()
        if final.status in TERMINAL:
            break
        print(f"    status={final.status}")

    print()
    print("Result")
    print(f"  status:      {final.status}")
    print(f"  duration:    {getattr(final, 'duration', None)}s")
    print(f"  error_code:  {getattr(final, 'error_code', None)}")
    print(f"  error_msg:   {getattr(final, 'error_message', None)}")
    print()

    # Call Events carry the SIP request/response pairs — the only place a
    # 403/404 from the far end is visible. Notifications carry Twilio-side
    # warnings/errors. Both are best-effort: Events needs Voice Insights
    # enabled on the account, so absence here is not itself a failure.
    for label, fetch in (
        ("SIP / call events", lambda: client.calls(call.sid).events.list(limit=20)),
        ("notifications", lambda: client.calls(call.sid).notifications.list(limit=10)),
    ):
        print(f"  {label}:")
        try:
            items = fetch()
        except TwilioRestException as exc:
            print(f"    (unavailable: {exc.status} {exc.msg})")
            continue
        except Exception as exc:
            print(f"    (unavailable: {exc})")
            continue
        if not items:
            print("    (none returned)")
        for item in items:
            props = getattr(item, "_properties", None) or {}
            print(f"    {props if props else item}")
        print()

    connected = final.status == "completed" and (int(
        getattr(final, "duration", 0) or 0) > 0)
    print("Verdict")
    if connected:
        print("  PASS (signaling) — LiveKit accepted the INVITE and the call")
        print("  connected. Signaling, auth and IP allow-listing are all fine.")
        print()
        print("  STILL TO CONFIRM — routing. Check the bot side for this call:")
        print(f"    /skit-call-debug {call.sid} --deep")
        print(f"  The SIP participant's `virtualNumber` must equal {target!r}.")
        print("  If it's empty or shows '%2B…' unlisted, the header didn't")
        print("  round-trip: either LiveKit isn't percent-decoding the value,")
        print("  or the trunk's headersToAttributes key doesn't match")
        print(f"  {settings.sip_virtual_number_header!r}.")
        return 0

    print(f"  FAIL (2) — the call did not connect (status={final.status}).")
    print("  Check the events/notifications above for a SIP response code:")
    print("    403 Forbidden  → LiveKit rejected the source. Its inbound trunk")
    print("                     likely allow-lists Elastic SIP Trunking egress")
    print("                     IPs; this path dials from Twilio's SIP-interface")
    print("                     IPs instead. Widen the allow-list or switch the")
    print("                     trunk to digest auth and set")
    print("                     AUTOCALLS_SIP_AUTH_USERNAME/_PASSWORD.")
    print("    404 Not Found  → the trunk number in the user part is wrong.")
    print(f"                     We sent {settings.sip_trunk_number!r}.")
    print("    478 / 480      → host or port unreachable; check")
    print(f"                     {settings.sip_destination_host!r}.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
