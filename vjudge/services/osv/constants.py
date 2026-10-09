"""OSV client configuration (env-driven).

Mirrors `integration-proxy/iproxy/services/osv/constants.py` so the two stay
recognisable to the same reader, with vjudge's `VJUDGE_` prefix.

`OSV_ACCESS_TOKEN` is a service JWT minted with OSV's shared signing secret
(role=VJudgeAdmin, so it bypasses per-endpoint policy checks). Keep it in the
environment — never hardcode, never log it.

`OSV_API_PREFIX` exists because the routers mount under a prefix that differs
per deployment: `account_router` declares `prefix="/accounts"` and
`interaction_router` declares `prefix="/client/{client_uuid}/interactions"`,
and iproxy's working client reaches them as `/api/...`. Rather than guess in
five call sites, the prefix is one setting and `OSVClient` probes it once
(see `client.resolve_prefix`).
"""

from __future__ import annotations

import os

# The llm-studio gateway, which is what a browser session talks to and what a
# VJudgeAdmin access token is minted for. VERIFIED 2026-08-11 against this base:
#   GET /clients/                                              -> 200
#   GET /accounts/interaction/{uuid}                           -> 200
#   GET /client/{client_uuid}/interactions/conversations/{u}/   -> 200
#   GET /client/{client_uuid}/interactions/                     -> 200
#   GET /assistants/{assistant_id}/                             -> 200
# Region is in the path (`/us/`, `/in/`, `/ca/`), so switching region is a base
# URL change and nothing else.
OSV_BASE_URL = os.getenv(
    "VJUDGE_OSV_BASE_URL", "https://llm-studio.vjudge.dev/us/api/interface"
).rstrip("/")
OSV_ACCESS_TOKEN = os.getenv("VJUDGE_OSV_ACCESS_TOKEN", "")

# Prefix candidates for resolve_prefix(). "" first now: the gateway base URL
# above already contains `/api/interface`, so adding another `/api` 404s. The
# `/api` candidate is kept for talking to an OSV instance directly (which is
# what iproxy does, at `/api/assistants/...`).
OSV_API_PREFIX = os.getenv("VJUDGE_OSV_API_PREFIX", "")
OSV_PREFIX_CANDIDATES = ("", "/api")

OSV_READ_TIMEOUT = float(os.getenv("VJUDGE_OSV_READ_TIMEOUT", "30"))
OSV_CONNECT_TIMEOUT = float(os.getenv("VJUDGE_OSV_CONNECT_TIMEOUT", "5"))
OSV_MAX_RETRIES = int(os.getenv("VJUDGE_OSV_MAX_RETRIES", "3"))

# Statuses worth a retry — transient gateway errors only. A 4xx is a real
# answer and must surface immediately.
OSV_RETRY_STATUSES = (502, 503, 504)
