"""Async HTTP client for OSV (orchestrator-service) interface API.

Ported from `integration-proxy/iproxy/services/osv/client.py` — same shape
(class + module-level singleton, env-driven base URL/token, split
connect/read timeouts, retry on transient gateway errors, Bearer auth) with
two deliberate changes:

  * **httpx instead of `requests_wrapper_v2`.** That wrapper is iproxy-internal
    and opkura is async throughout, so porting it would mean dragging in a sync
    transport plus its logging conventions. httpx is already a dependency.
  * **Three methods, not two.** `get_interaction_metadata` is new and is the
    one that makes the others usable: `get_conversations` needs `client_uuid`
    as a path parameter, and a caller who starts from an llm-studio link only
    has an `interaction_uuid`. This endpoint is what turns one into the other.

Usage:

    from opkura.services.osv import osv_client
    meta = await osv_client.get_interaction_metadata(interaction_uuid)
    convo = await osv_client.get_conversations(meta["client_uuid"], interaction_uuid)
    bot = await osv_client.get_bot_data(meta["assistant_id"])
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from opkura.services.osv import constants

logger = logging.getLogger(__name__)

# A UUID anywhere in a string — used to pull the interaction uuid out of an
# llm-studio URL when it isn't in the `interaction_uuid` query param.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


class OSVError(RuntimeError):
    """OSV returned an error, or was unreachable after retries."""


def parse_interaction_ref(ref: str) -> str:
    """Extract an `interaction_uuid` from a bare uuid or an llm-studio URL.

    Accepts:
      * `c26d1903-...`                                    → itself
      * `https://llm-studio…/conversations/1354?interaction_uuid=c26d1903-…`
      * any URL containing a uuid (falls back to the first uuid found)

    The `region` query param is deliberately ignored — it is a hint that is
    frequently wrong, and the authoritative region is whichever OSV base URL
    actually returns the record.
    """
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("empty interaction reference")

    if _UUID_RE.fullmatch(ref):
        return ref.lower()

    parsed = urlparse(ref)
    qs = parse_qs(parsed.query)
    for key in ("interaction_uuid", "interactionUuid", "interaction_id"):
        if key in qs and qs[key]:
            candidate = qs[key][0]
            if _UUID_RE.fullmatch(candidate):
                return candidate.lower()

    found = _UUID_RE.search(ref)
    if found:
        return found.group(0).lower()

    raise ValueError(f"no interaction uuid found in {ref!r}")


class OSVClient:
    """Reusable async client for OSV interface endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        api_prefix: str | None = None,
        read_timeout: float | None = None,
        connect_timeout: float | None = None,
        max_retries: int | None = None,
    ):
        # Explicit args win; otherwise resolve from Settings LAZILY. Lazily
        # because the module-level singleton is constructed at import time, and
        # `Settings()` raises if the required Twilio/Deepgram fields are absent —
        # importing this module must not depend on that.
        self._base_url_override = base_url
        self._token_override = token
        self._settings: Settings | None = None
        # None means "not yet resolved"; "" is a valid resolved value (no prefix).
        self._api_prefix: str | None = api_prefix
        self.read_timeout = read_timeout if read_timeout is not None else constants.OSV_READ_TIMEOUT
        self.connect_timeout = (
            connect_timeout if connect_timeout is not None else constants.OSV_CONNECT_TIMEOUT
        )
        self.max_retries = max_retries if max_retries is not None else constants.OSV_MAX_RETRIES

    # ---------------------------------------------------------------- internals

    def _cfg(self):
        """Memoised Settings, or None when it can't be constructed.

        None is tolerated so the client still works in a bare context (a probe
        script, a unit test) where unrelated required settings are absent — it
        then falls back to the module constants.
        """
        if self._settings is None:
            try:
                from opkura.core.config import Settings

                self._settings = Settings()
            except Exception as exc:  # pragma: no cover - env-dependent
                logger.debug("Settings unavailable; using env fallbacks: %s", exc)
                return None
        return self._settings

    @property
    def base_url(self) -> str:
        if self._base_url_override:
            return self._base_url_override.rstrip("/")
        cfg = self._cfg()
        base = getattr(cfg, "osv_base_url", "") if cfg else ""
        return (base or constants.OSV_BASE_URL).rstrip("/")

    @property
    def _token(self) -> str:
        if self._token_override is not None:
            return self._token_override
        cfg = self._cfg()
        from_cfg = getattr(cfg, "osv_access_token", "") if cfg else ""
        return from_cfg or constants.OSV_ACCESS_TOKEN

    def _require_token(self) -> None:
        """Fail fast, and legibly, when no token is configured.

        Without this an empty token renders as the header `Bearer ` (trailing
        space), which httpx rejects as `Illegal header value`. That surfaces as a
        transport error, gets retried three times with backoff, and finally
        reports "OSV unreachable" — sending the reader after a network problem
        when the real cause is an unset environment variable.
        """
        if not (self._token or "").strip():
            raise OSVError(
                "no OSV token — set AUTOCALLS_OSV_ACCESS_TOKEN to a SkitAdmin "
                "access JWT (copy the `authorization: Bearer …` header from a "
                "logged-in llm-studio session; it expires in ~8h)"
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token.strip()}",
            "accept": "application/json",
        }

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.read_timeout, connect=self.connect_timeout)

    async def _request(self, path: str, params: dict | None = None) -> Any:
        """GET `{base_url}{prefix}{path}`, retrying transient gateway errors.

        Raises OSVError on a non-2xx that isn't worth retrying, so callers get
        one exception type regardless of whether the failure was transport or
        status.
        """
        self._require_token()
        prefix = await self.resolve_prefix()
        url = f"{self.base_url}{prefix}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout()) as http:
                    resp = await http.get(url, params=params, headers=self._headers())
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "OSV transport error (attempt %d/%d) %s: %s",
                    attempt, self.max_retries, url, exc,
                )
            else:
                if resp.status_code in constants.OSV_RETRY_STATUSES:
                    last_exc = OSVError(f"{resp.status_code} from {url}")
                    logger.warning(
                        "OSV %d (attempt %d/%d) %s",
                        resp.status_code, attempt, self.max_retries, url,
                    )
                elif resp.is_success:
                    return resp.json()
                elif resp.status_code in (401, 403):
                    # Called out separately so the message can name the causes.
                    # The one that actually wasted time was neither expiry nor
                    # revocation: the token was quoted in `.env`, and a helper
                    # that read the file itself sent `Bearer "eyJ..."` including
                    # the quotes. pydantic-settings strips them, so the service
                    # was fine while a side script reported "Invalid token".
                    raise OSVError(
                        f"OSV {resp.status_code} — token rejected. Check, in this "
                        "order: (1) is the token quoted in .env and read by "
                        "something that does not strip quotes? (2) has it expired? "
                        "(3) refresh it from a logged-in llm-studio session. "
                        f"Response: {resp.text[:200]}"
                    )
                else:
                    # Other 4xx and non-retryable 5xx: a real answer, surface now.
                    raise OSVError(
                        f"OSV {resp.status_code} for {url}: {resp.text[:300]}"
                    )
            if attempt < self.max_retries:
                await asyncio.sleep(0.5 * attempt)  # linear backoff, short by design

        raise OSVError(f"OSV unreachable after {self.max_retries} attempts: {url}") from last_exc

    async def resolve_prefix(self) -> str:
        """Determine the API prefix once, then cache it.

        The routers declare `/accounts` and `/client/{...}/interactions`, but the
        live service mounts them under a prefix — iproxy reaches them at
        `/api/...`. Rather than hardcode a guess that silently 404s, probe the
        candidates against a cheap endpoint and remember the winner.

        `AUTOCALLS_OSV_API_PREFIX` short-circuits this entirely.
        """
        if self._api_prefix is not None:
            return self._api_prefix

        # An explicitly configured prefix short-circuits the probe. Read it from
        # Settings (i.e. `.env`) as well as the env, for the same reason the
        # token is read there.
        cfg = self._cfg()
        configured = (getattr(cfg, "osv_api_prefix", "") if cfg else "") or constants.OSV_API_PREFIX
        if configured:
            self._api_prefix = configured
            return self._api_prefix

        self._require_token()
        for candidate in constants.OSV_PREFIX_CANDIDATES:
            url = f"{self.base_url}{candidate}/assistants/"
            try:
                async with httpx.AsyncClient(timeout=self._timeout()) as http:
                    resp = await http.get(url, headers=self._headers())
            except httpx.HTTPError:
                continue
            # Anything other than 404 means the path exists (401/403 included —
            # the route is there, our token just may not suit that endpoint).
            if resp.status_code != 404:
                logger.info("OSV api prefix resolved to %r", candidate)
                self._api_prefix = candidate
                return candidate

        # Nothing answered; fall back to no prefix, which is correct for the
        # default llm-studio gateway base URL (it already ends in
        # /api/interface). The caller then gets a meaningful error from the real
        # request rather than from probing.
        logger.warning("OSV api prefix probe inconclusive; defaulting to ''")
        self._api_prefix = ""
        return self._api_prefix

    # ----------------------------------------------------------------- endpoints

    async def get_interaction_metadata(self, interaction_uuid: str) -> dict[str, Any]:
        """`GET /accounts/interaction/{interaction_uuid}`.

        The entry point for everything else: returns the interaction plus
        `client_uuid`, `assistant_id`, `assistant_uuid`, `assistant_name` and
        `flow_config_id`, and the full `interaction_metadata` blob — which is
        where the real account data (and our own `x-opkura-session-id`) lives.
        """
        return await self._request(f"/accounts/interaction/{interaction_uuid}")

    async def get_conversations(
        self, client_uuid: str, interaction_uuid: str
    ) -> dict[str, Any]:
        """Transcript + per-turn tool calls for one interaction."""
        return await self._request(
            f"/client/{client_uuid}/interactions/conversations/{interaction_uuid}/"
        )

    async def get_bot_data(
        self, assistant_id: int | str, version: int | str | None = None
    ) -> dict[str, Any]:
        """Full assistant config — the prompt, `variables`, `runtime_config`.

        **Omit `version` unless you know the exact flow-config version number.**
        Verified 2026-08-11: `/assistants/1734/` returns 200, while the same path
        with `?version=15232` (the interaction's `flow_config_id`) returns
        `404 Assistant not found` — and `?version=128` did too. A `flow_config_id`
        is NOT a version, and passing one turns a working request into a 404 that
        reads like the assistant doesn't exist. Callers that only have a
        flow_config_id should pass nothing and get the production config, which is
        what a replication test wants anyway.

        `assistant_id` must be the integer id; the uuid gives a 422.
        """
        params = {"version": version} if version is not None else None
        return await self._request(f"/assistants/{assistant_id}/", params=params)

    async def list_client_interactions(
        self,
        client_uuid: str,
        assistant_id: int | None = None,
        recipient_id: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        page_size: int = 100,
        **extra: Any,
    ) -> dict[str, Any]:
        """`GET /client/{client_uuid}/interactions/` — hop 1 of correlation.

        `client_uuid` is a path parameter, which is why this is the right way to
        find our calls: the DB index `ix_interactions_client_assistant_created`
        leads on `client_uuid`, and a path param cannot be accidentally omitted
        the way a query filter can.

        Note the response's `interaction_metadata` is NOT the real blob — the
        view rebuilds it as a single `is_awaiting_reply` key
        (`call_report.py:311`, confirmed live 2026-08-11). So this narrows to
        candidates; `get_interaction_metadata` identifies which one is ours.

        Rows come back under the **`interactions`** key, each with
        `interaction_uuid` (not `uuid`).

        `recipient_id` is the strongest filter available: for an inbound test
        call it holds opkura's caller ID, and only opkura dials from that number.
        Verified — filtering on it returned exactly our two SIP calls.
        """
        params: dict[str, Any] = {"page_size": page_size}
        if assistant_id is not None:
            params["assistant_id"] = assistant_id
        if recipient_id:
            params["recipient_id"] = recipient_id
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        params.update(extra)
        return await self._request(f"/client/{client_uuid}/interactions/", params=params)


# Reusable singleton — import this everywhere, as iproxy does.
osv_client = OSVClient()


# Region is a PATH SEGMENT in the llm-studio gateway URL, so switching region is
# a base-URL swap and nothing else. `us` is verified; `in` and `ca` follow the
# same pattern and are unverified.
REGION_BASE_URLS: dict[str, str] = {
    "us": "https://llm-studio.skit.ai/us/api/interface",
    "in": "https://llm-studio.skit.ai/in/api/interface",
    "ca": "https://llm-studio.skit.ai/ca/api/interface",
}

# Aliases a caller might reasonably send.
_REGION_ALIASES = {
    "usa": "us", "united-states": "us",
    "india": "in", "ind": "in",
    "canada": "ca", "can": "ca",
}

_region_clients: dict[str, OSVClient] = {}


def normalise_region(region: str | None) -> str:
    r = (region or "us").strip().lower()
    return _REGION_ALIASES.get(r, r)


def client_for_region(region: str | None) -> OSVClient:
    """An OSVClient pointed at `region`'s gateway.

    Returns the module singleton when the region resolves to the configured
    base URL, so the common single-region case shares one client (and one
    resolved prefix) rather than making a new one per job.

    Raises ValueError on an unknown region, because silently falling back to US
    would make a job read the wrong region's calls and produce a confidently
    wrong report.
    """
    key = normalise_region(region)
    if key not in REGION_BASE_URLS:
        raise ValueError(
            f"unknown region {region!r} — expected one of "
            f"{sorted(REGION_BASE_URLS)}"
        )
    base = REGION_BASE_URLS[key]
    if base.rstrip("/") == osv_client.base_url:
        return osv_client
    if key not in _region_clients:
        _region_clients[key] = OSVClient(base_url=base)
    return _region_clients[key]
