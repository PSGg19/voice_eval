"""SIP URI construction for pure SIP-to-SIP outbound dialing.

Twilio's REST ``POST /Calls`` accepts a SIP URI in ``To`` as well as an
E.164 number. When ``To`` is a SIP URI, Twilio originates the INVITE
straight at that host from its SIP interface — no PSTN leg, no DID
termination — and, the reason this module exists, custom ``X-`` headers
on that INVITE survive to the far end.

That matters for correlation. With a PSTN target the only join between
an vjudge run and the bot's own interaction record is
(caller#, called#, wall-clock) — see the note in
``transport/server.py``'s ``status_callback``. Over SIP we carry an
explicit key instead.

**We cannot inject the Twilio Call SID ourselves.** It is minted BY the
``calls.create`` request that sends the INVITE, so it does not exist yet
when the URI is built. What we inject is vjudge's ``session_id``, which
vjudge already maps to the returned Call SID (runner.py sets
``result.call_sid`` right after ``make_call``).

Twilio does, however, stamp its own ``X-Twilio-CallSid`` on the outbound
INVITE — undocumented, but VERIFIED on the live trunk, arriving as
``x-twilio-callsid`` and matching the SID ``calls.create`` returned. So
the bot gets BOTH identifiers and correlation works from either end:

    x-twilio-callsid     = CA…   (stamped by Twilio, == the returned SID)
    x-vjudge-session-id  = …     (ours, maps to call_sid vjudge-side)

Do not depend on ``x-twilio-callsid`` alone: it is not contractual and
only exists on this SIP path, whereas ``x-vjudge-session-id`` is ours.

Header rules enforced here come from Twilio's outbound-SIP docs:
  * Only ``X-``-prefixed headers are forwarded. Twilio drops anything
    else silently — the worst failure mode — so we reject it loudly.
  * Values may not contain spaces or the URI delimiters
    (``? & = ; @``). We percent-encode rather than guess.
  * The whole header section must stay under 1024 characters.

LiveKit's side of the contract: an inbound trunk configured with
``headers_to_attributes`` surfaces a named ``X-`` header to the agent as
a participant attribute, e.g.::

    "headersToAttributes": {"X-VirtualNumber": "virtualNumber"}

``include_headers`` instead exposes every header as
``sip.h.<lowercased-name>``. Without one of those trunk settings the
headers arrive on the wire and are simply never exposed to the agent.

**Routing is by header, not by the user part.** The user part is
LiveKit's inbound-trunk identifier (a value like ``1205``) and selects
the trunk; the bot to actually reach is named by ``X-VirtualNumber``,
which the trunk maps to the ``virtualNumber`` attribute. So the URI
vjudge dials looks like::

    sip:1205@581mqcm8zvu.sip.livekit.cloud:5060?X-VirtualNumber=%2B18447331982&…

Note the ``%2B``: a literal ``+`` in a URI query is conventionally read
as a space, so `encode_header_value` escapes it.

VERIFIED against the live trunk (interaction
``ed845f16-9fe7-4837-a038-903c5fe54f9d``, 2026-08-11): LiveKit
percent-decodes the value, and the headers land on the interaction as

    x-virtualnumber      = +18447331982     <- %2B decoded back to '+'
    virtual_number       = +18447331982     <- headersToAttributes mapping
    x-vjudge-session-id  = probe-session
    x-is-test            = 1
    ani / caller_number  = +12202206097     <- the Twilio caller ID

So no ``+``-stripping is needed. Note header NAMES arrive lowercased
(``x-virtualnumber``, not ``X-VirtualNumber``) — match on the lowercase
form when reading them on the bot side.
"""

from __future__ import annotations

import re
from urllib.parse import quote

# Twilio's documented ceiling on the custom-header section of an
# outbound SIP URI. Counted over the rendered `?k=v&k=v` string.
SIP_HEADER_BUDGET = 1024

# Only X--prefixed headers are forwarded by Twilio. The rest of the name
# must be a SIP/HTTP token — letters, digits, and `-` in practice.
_HEADER_NAME_RE = re.compile(r"^X-[A-Za-z0-9][A-Za-z0-9-]*$")

# Hostname for the SIP destination: no scheme, no userinfo, no spaces.
# Accepts an optional `:port`. Deliberately strict — a host that quietly
# contains "sip:" produces `sip:num@sip:host` and a 404 from Twilio with
# no useful diagnostic.
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:\d{1,5})?$")

# The user part of the URI — for us, LiveKit's inbound-trunk identifier
# (`1205`). A leading `+` is accepted because the same builder serves any
# far end that does route on a DID; `+` is legal in a SIP user part per
# RFC 3261's user-unreserved set.
_USER_RE = re.compile(r"^\+?[A-Za-z0-9][A-Za-z0-9._-]*$")

VALID_TRANSPORTS = ("udp", "tcp", "tls")


def validate_sip_host(host: str) -> None:
    """Raise ValueError unless `host` is a bare hostname[:port].

    Rejects a value that carries a scheme or userinfo, because
    `build_sip_uri` adds those itself and the doubled-up result fails at
    Twilio with an unhelpful error.
    """
    if not isinstance(host, str):
        raise TypeError(f"SIP host must be str, got {type(host).__name__}")
    if not host:
        raise ValueError(
            "SIP host is empty — set VJUDGE_SIP_DESTINATION_HOST "
            "(e.g. 581mqcm8zvu.sip.livekit.cloud)"
        )
    if not _HOST_RE.match(host):
        raise ValueError(
            f"SIP host must be a bare hostname[:port], got {host!r}. "
            "Drop any 'sip:' scheme, 'user@' prefix, or ';transport=' "
            "suffix — build_sip_uri adds those."
        )


def validate_trunk_number(number: str) -> None:
    """Raise ValueError unless `number` is a usable SIP URI user part.

    This is LiveKit's inbound-trunk identifier (e.g. `1205`), not the
    bot's DID — the DID goes in the X-VirtualNumber header. Called from
    Settings so a blank value fails at load rather than producing
    `sip:@host` on a live dial.
    """
    if not isinstance(number, str):
        raise TypeError(f"trunk number must be str, got {type(number).__name__}")
    if not number:
        raise ValueError(
            "SIP trunk number is empty — set VJUDGE_SIP_TRUNK_NUMBER "
            "to LiveKit's inbound-trunk identifier (e.g. 1205)"
        )
    if not _USER_RE.match(number):
        raise ValueError(
            f"SIP trunk number must be a bare identifier, got {number!r}"
        )


def validate_header_name(name: str) -> None:
    """Raise ValueError unless `name` is an X--prefixed SIP header token.

    Twilio forwards ONLY X--prefixed headers on outbound SIP and drops
    the rest without complaint, so a non-X- name here is a silent
    correlation failure at runtime. Fail at the boundary instead.
    """
    if not isinstance(name, str):
        raise TypeError(f"header name must be str, got {type(name).__name__}")
    if not _HEADER_NAME_RE.match(name):
        raise ValueError(
            f"SIP header name must match X-<token>, got {name!r}. "
            "Twilio only forwards X--prefixed headers on outbound SIP; "
            "anything else is dropped silently."
        )


def encode_header_value(value: str) -> str:
    """Percent-encode a header value for use in a SIP URI query.

    Everything outside the unreserved set is escaped, so spaces and the
    URI delimiters `? & = ; @` can't break out of the value and corrupt
    the URI. Empty values are rejected — a header that arrives with no
    value is indistinguishable from a dropped header on the far end.
    """
    if not isinstance(value, str):
        raise TypeError(f"header value must be str, got {type(value).__name__}")
    if not value:
        raise ValueError("SIP header value is empty")
    return quote(value, safe="")


def build_sip_uri(
    user: str,
    host: str,
    transport: str | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """Build `sip:<user>@<host>[;transport=…][?X-A=1&X-B=2]`.

    `user` is LiveKit's inbound-trunk identifier, not the bot's DID —
    the DID rides in an `X-VirtualNumber` header instead. `host` may
    carry an explicit port (`…:5060`).

    `transport` of None or "" omits the parameter, leaving the URI
    exactly `sip:user@host?headers` so Twilio applies its own default.

    Ordering is not cosmetic: SIP URI parameters (`;transport=`) come
    BEFORE the header query (`?`), per RFC 3261. Reversing them yields a
    URI Twilio accepts and then misroutes.

    Headers are emitted in sorted order so the same inputs always render
    the same URI — makes the DEBUG log line diffable across runs.
    """
    if transport and transport not in VALID_TRANSPORTS:
        raise ValueError(
            f"SIP transport must be one of {VALID_TRANSPORTS}, got {transport!r}"
        )
    if not isinstance(user, str) or not _USER_RE.match(user or ""):
        raise ValueError(
            f"SIP user part must be an E.164 number or extension, got {user!r}"
        )
    validate_sip_host(host)

    uri = f"sip:{user}@{host}"
    if transport:
        uri += f";transport={transport}"

    if headers:
        parts = []
        for name in sorted(headers):
            validate_header_name(name)
            parts.append(f"{name}={encode_header_value(headers[name])}")
        query = "&".join(parts)
        if len(query) > SIP_HEADER_BUDGET:
            raise ValueError(
                f"SIP custom headers total {len(query)} chars, over Twilio's "
                f"{SIP_HEADER_BUDGET}-char limit; drop or shorten headers"
            )
        uri += f"?{query}"

    return uri


def parse_header_spec(spec: str) -> dict[str, str]:
    """Parse a `X-A=1,X-B=2` config string into a header dict.

    This is the shape `VJUDGE_SIP_EXTRA_HEADERS` takes, so operators
    can add correlation keys (a release tag, a suite id) without a code
    change. Blank spec → empty dict. Each entry is validated here so a
    typo in `.env` fails at settings load, not mid-run on a live call.
    """
    if not spec or not spec.strip():
        return {}
    out: dict[str, str] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(
                f"SIP header spec entry must be NAME=VALUE, got {chunk!r}"
            )
        name, _, value = chunk.partition("=")
        name, value = name.strip(), value.strip()
        validate_header_name(name)
        if not value:
            raise ValueError(f"SIP header {name!r} has an empty value")
        out[name] = value
    return out
