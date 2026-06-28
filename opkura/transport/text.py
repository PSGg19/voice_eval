"""Text transport — drive a voice bot over OSV's text runtime, no audio.

Why this exists
---------------
`transport/stream.py` tests the bot *and* the whole audio path: Twilio, STT,
TTS, VAD, turn detection. That is the right test for barge, DTMF timing and
carrier latency, but it costs a real phone call per run and is capped by Twilio
channel concurrency.

Most scenarios do not test audio. They test the bot's *reasoning*: module
transitions, tool sequencing, hallucination after a tool error, compliance
wording. OSV's text runtime exposes exactly that path over HTTP:

    POST /text_runtime/post_message_web   →   one user turn, bot reply back

Same `LLMConfigManager`, same flow config, same modules, same tools/MCP. The
only thing missing is the audio pipeline around the model.

`channel=VOICE` is not optional
-------------------------------
An assistant has four separate flow configs (voice / sms / email / web_chat)
and OSV's `get_channel_flow` filters on channel with **no fallback** — it
returns None. So driving a voice bot with `channel=WEB_CHAT` exercises a
different flow, or dies. Verified live 2026-08-05: `channel=VOICE` returns the
voice flow's start module, `WEB_CHAT` on a voice-only assistant returns
`current_module="error"` with an `error` string and HTTP **200**.

That 200-on-failure is why `_check_response` below inspects the body rather
than trusting the status code.

DTMF
----
No special handling is needed. When the bot wants keypad input it emits
`<DTMF:n>`; over voice, the collected digits are handed to the model as a plain
`TranscriptionFrame` — i.e. as text. So posting the digits as `user_message` is
byte-identical to a real keypress, and the caller LLM decides when to send them
from the bot's wording, exactly as it does over voice.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from opkura.core.config import Settings
from opkura.core.models import Scenario
from opkura.core.region import ALLOWED_REGIONS
from opkura.core.region_profile import resolve_profile

logger = logging.getLogger(__name__)

POST_MESSAGE_PATH = "/text_runtime/post_message_web"

# NOTE: there is NO endpoint for opkura to close a text interaction, so every
# run leaves its interaction at `status=ongoing` on the OSV side — forever, and
# visibly "live" in llm-studio. Over voice, hanging up ends the call and OSV
# finalises it; the text runtime has no equivalent, and the caller LLM's
# `hangup` is therefore a client-side signal only.
#
# `/text_runtime/update_interaction_conversation_status/` looks like the answer
# and is not: its request requires `vendor_call_id` and carries
# status_long_code / total_message_segments / created_reply_uuid, i.e. it is the
# SMS/email DELIVERY-status hook. opkura has no vendor_call_id. Confirmed
# against llm-studio-frontend's generated schema
# (`UpdateInteractionConversationStatusRequest`).
#
# Consequence to keep in mind: dangling `ongoing` rows accumulate, one per run.
# Closing them needs an OSV change — do not add one without approval.

# POSTing a turn MUTATES the conversation: OSV appends the user message and runs
# the bot. So a request that may already have been delivered must NOT be retried
# — a retry would duplicate the turn and silently corrupt the transcript.
# Only connect-phase failures are safe, because nothing reached the server.
# A read timeout is explicitly NOT retryable: the bot may well be mid-turn.

# `current_module` OSV uses for its generic failure reply.
ERROR_MODULE = "error"


@dataclass(frozen=True)
class BotTurn:
    """One bot reply. Fields after `text` are text-transport-only extras."""

    text: str
    current_module: Optional[str] = None
    tool_calls: list[dict] = field(default_factory=list)
    reasoning_ms: Optional[float] = None
    message_type: Optional[str] = None
    interaction_uuid: Optional[str] = None


class TextTransportError(RuntimeError):
    """Unrecoverable problem talking to the text runtime."""


def resolve_recipient_id(scenario: Scenario) -> str:
    """Return the scenario's `recipient_id`. It is REQUIRED for text, by design.

    `recipient_id` is not a label — OSV assigns it to the bot's `phone_number`
    variable (`context_manager.py:335` for INBOUND) and it is also the account
    lookup key (`Account.phone_number == recipient_id`, exact string compare).
    From `phone_number` it flows into every integration payload the bot sends.

    So it is deliberately NOT derived, guessed, or generated. On 2026-08-24 a
    generated slug default reached a client's PRODUCTION workflow API as
    `"dialedNumber": "OPKURA_Veros-CI-OB-2-0-LANG-CIO_a4ce6b52"`. Anything
    opkura invents here is a value that ends up in someone's CRM, so the
    scenario states it explicitly or the run does not start.

    Conventions worth following in the YAML:
      * BARE DIGITS, no `+1` — the accounts table stores bare digits and the
        compare is exact, so `+1…` silently matches nothing.
      * a seeded test account's number when the flow needs to authenticate
        (the Veros IB 2.0 test row is `7145551212`);
      * otherwise NPA-555-01XX, the NANP range reserved for fiction, which can
        never collide with a real subscriber or record.
    """
    if not scenario.recipient_id:
        raise TextTransportError(
            f"scenario {scenario.name!r} has no `recipient_id`, which the text "
            f"transport requires. It becomes the bot's `phone_number` and is its "
            f"account lookup key, so it is never generated — an invented value "
            f"ends up in the client's CRM. Add it to the YAML, bare digits and no "
            f"+1: the seeded test account's number if the flow must authenticate, "
            f"otherwise something in the NANP fictional range, e.g.\n"
            f'    recipient_id: "2015550137"'
        )
    return scenario.recipient_id


class TextTransport:
    """Turn-level transport over OSV's text runtime.

    Lifecycle: `start()` once for the bot's opening turn, then `send()` per
    caller turn, then `close()`. Every call is a plain HTTP round-trip, so a
    "call" here holds no sockets and no audio buffers.
    """

    def __init__(
        self,
        settings: Settings,
        scenario: Scenario,
        session_id: str,
        http: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.settings = settings
        self.scenario = scenario
        self.session_id = session_id
        # P1.3: an injected client is the worker's SHARED pool — used, never
        # closed here. Absent, this transport owns (and closes) its own.
        self._injected_http = http
        self._owns_http = False
        self.recipient_id = resolve_recipient_id(scenario)
        # Scenario YAML beats the deployment default. `is None` rather than `or`:
        # an explicit `is_test: false` must not fall through to the setting.
        self.is_test = (
            settings.text_is_test if scenario.is_test is None else scenario.is_test
        )
        # WHICH bot, and which version of its flow. Scenario first, deployment
        # default second — so one process can drive many assistants and a
        # campaign can span several, which env-only globals made impossible.
        self.assistant_uuid = (
            scenario.assistant_uuid or settings.text_assistant_uuid or ""
        )
        # `is None`, not `or`: fc_version 0 is MEANINGFUL ("current"), so a
        # scenario that explicitly says 0 must not fall through to the setting.
        self.fc_version = (
            settings.text_fc_version if scenario.fc_version is None
            else scenario.fc_version
        )

        # Populated from the FIRST response. This is the correlation key: OSV
        # mints the interaction and hands it back, so unlike the PSTN path
        # there is nothing to inject and nothing to reconcile afterwards.
        self.interaction_uuid: Optional[str] = None
        self.client_uuid: Optional[str] = None
        # Debug links OSV already builds into interaction_metadata — worth
        # surfacing rather than rebuilding.
        self.debugging_urls: dict[str, str] = {}

        self._http: Optional[httpx.AsyncClient] = None
        self._turns = 0

    # ---------------------------------------------------------------- helpers

    @property
    def base_url(self) -> str:
        """The text-runtime URL, with the scenario's region substituted in.

        The region is a PATH SEGMENT — `https://llm-studio.skit.ai/us/api/text`
        — and assistants do not exist across regions: asking the US gateway for
        an India assistant returns `404 No assistant found for
        request.assistant_uuid=… and +91…`. So a scenario that knows its region
        should say so, rather than depending on an exported variable that
        silently misroutes the whole run.
        """
        # One resolver for both transports (P3.9): the same call that picks a
        # SIP trunk for a voice run picks this URL for a text run, so the two
        # modes can never disagree about what a region means.
        return resolve_profile(
            self.scenario.region, self.settings
        ).text_runtime_base_url

    def _region_hint(self, status_code: int, body: str) -> str:
        """Explain a region misroute, which OSV's own 404 wording hides.

        `404 No assistant found for request.assistant_uuid='d024a…' and +1866…`
        reads like a bad uuid or a bad number. It is usually neither: the region
        is a PATH SEGMENT and assistants do not exist across regions, so the
        India gateway says exactly this about a perfectly good US assistant.
        This has cost two debugging sessions, both times because an exported
        `AUTOCALLS_TEXT_RUNTIME_BASE_URL` outranked `.env` and nothing in the
        output said which region was actually used.

        opkura knows the region it used and where that came from, so it says so.
        """
        if status_code != 404 or "no assistant found" not in body.lower():
            return ""
        url = self.base_url
        used = next((seg for seg in url.split("/") if seg in ALLOWED_REGIONS), None)
        if not used:
            return ""
        others = " or ".join(f"`{r}`" for r in ALLOWED_REGIONS if r != used)
        article = "an" if used.upper()[0] in "AEIOU" else "a"
        if self.scenario.region:
            source = f'the scenario\'s `region: "{self.scenario.region}"`'
            advice = (
                f"If this assistant actually lives in {others}, change that key."
            )
        else:
            source = (
                "AUTOCALLS_TEXT_RUNTIME_BASE_URL — and note an EXPORTED variable "
                "silently outranks .env, which is how this usually happens"
            )
            advice = (
                f"If this assistant lives in {others}, pin it in the scenario "
                f'rather than fixing the environment — the scenario key wins:\n'
                f'    region: "<us|in|ca>"'
            )
        return (
            f"\n\nThis is very likely a REGION MISROUTE, not a bad assistant_uuid. "
            f"You asked the `{used}` gateway, chosen by {source}. Assistants do "
            f"not exist across regions, so {article} {used.upper()} gateway "
            f"reports a perfectly valid assistant from another region in exactly "
            f"these words. {advice}"
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.text_runtime_token:
            headers["Authorization"] = f"Bearer {self.settings.text_runtime_token}"
        return headers

    def _request_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            self.settings.text_runtime_read_timeout,
            connect=self.settings.text_runtime_connect_timeout,
        )

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._http is None:
            if self._injected_http is not None:
                self._http = self._injected_http
            else:
                # Own one only when nothing was injected (P1.3: the runner
                # shares one pooled client per worker). Headers and timeout
                # travel PER REQUEST either way — a shared client carries no
                # run's state.
                self._http = httpx.AsyncClient()
                self._owns_http = True
        return self._http

    def _payload(self, user_message: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "recipient_id": self.recipient_id,
            "user_message": user_message,
            # Non-negotiable — see module docstring.
            "channel": "VOICE",
            # Scenario YAML wins; otherwise the deployment default (False).
            #
            # Verified against the live US runtime (Loki, 2026-08-17): this flag
            # suppresses ONLY the compliance event, and it hides the interaction
            # from llm-studio's conversations view, which filters on
            # is_test=false. It does NOT stop in-call tool calls, and it does NOT
            # stop post-call processing — which, despite the name, runs after
            # EVERY turn and POSTs the caller's name, account number, SSN last-4
            # and due amount to the client's real CRM through the integration
            # proxy. So a text run writes to production data from the first
            # authenticated turn either way. Scenarios must never settle a
            # payment, and must only use accounts it is safe to touch.
            "is_test": self.is_test,
            "interaction_metadata": {"call_type": "inbound", **(self.scenario.vars or {})},
        }

        # `virtual_id` is ALWAYS sent, even when the assistant is addressed by
        # uuid: OSV writes it straight onto the interaction row, where the column
        # is NOT NULL. Sending only `assistant_uuid` gets a psycopg2
        # NotNullViolation back — as an HTTP 200 with an `error` string, which is
        # why _check_response exists. OSV's own CLI driver passes both
        # (`app/runtime/cli/main.py` uses virtual_id="100").
        payload["virtual_id"] = self.scenario.target
        # An explicit uuid wins for resolution; without it OSV resolves the
        # assistant from the virtual number, which keeps one YAML working for
        # both transports.
        if self.assistant_uuid:
            payload["assistant_uuid"] = self.assistant_uuid

        # 0 means "current", which is what OSV does when the field is absent —
        # so only send a real pin.
        if self.fc_version:
            payload["fc_version"] = self.fc_version

        # Omitted on the first turn so OSV creates the interaction; echoed back
        # afterwards to continue it.
        if self.interaction_uuid:
            payload["interaction_uuid"] = self.interaction_uuid
        else:
            # First turn only. Belt-and-braces against the gotcha that a reused
            # `recipient_id` silently CONTINUES the previous conversation instead
            # of starting one — observed live, the bot just repeated its
            # greeting. A unique recipient_id already prevents it; this makes the
            # intent explicit using the same flag llm-studio's own chat drawer
            # uses (`chat-view.tsx` sends `reset_interaction` on reset).
            #
            # Must NOT be sent on later turns: it would reset the conversation
            # every single turn.
            payload["reset_interaction"] = True

        return payload

    def _check_response(self, data: dict[str, Any]) -> None:
        """Reject a soft failure that arrived as HTTP 200.

        OSV replies 200 with `error` set and `current_module="error"` when the
        channel flow lookup fails (wrong channel, or the assistant has no flow
        for it). Treat that as fatal rather than feeding an apology message to
        the caller LLM as if it were the bot's real turn.
        """
        err = (data.get("error") or "").strip()
        module = data.get("current_module")
        if err or module == ERROR_MODULE:
            raise TextTransportError(
                f"text runtime returned a soft failure (current_module={module!r}): "
                f"{err or 'no error string'} — check that the assistant has a "
                f"voice_flow_config and that assistant_uuid/virtual_id resolve"
            )

    async def _post(self, user_message: str) -> BotTurn:
        client = await self._ensure_client()
        url = f"{self.base_url}{POST_MESSAGE_PATH}"
        payload = self._payload(user_message)

        for attempt in range(1, self.settings.text_runtime_max_retries + 1):
            try:
                resp = await client.post(
                    url, json=payload,
                    headers=self._headers(), timeout=self._request_timeout(),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Nothing reached the server, so re-sending cannot duplicate a
                # turn. Worth retrying: the text runtime restarting under
                # `--reload` produces exactly this.
                if attempt >= self.settings.text_runtime_max_retries:
                    raise TextTransportError(
                        f"text runtime unreachable at {url} after {attempt} "
                        f"attempts: {exc!r}"
                    ) from exc
                backoff = 0.5 * (2 ** (attempt - 1))
                logger.warning(
                    "text runtime connect failed %d/%d (%r) — retrying in %.1fs",
                    attempt, self.settings.text_runtime_max_retries, exc, backoff,
                )
                await asyncio.sleep(backoff)
                continue
            except httpx.ReadTimeout as exc:
                # The request WAS delivered, and the user turn is already
                # committed server-side. Retrying would post it twice, so fail.
                #
                # Observed twice on the live US runtime: the per-request Pipecat
                # task produced no frames, and only its own ~301s idle timeout
                # ended the request — answering HTTP 200 with an EMPTY bot
                # message. So waiting longer than this does not recover the turn;
                # it just costs five minutes. Treat it as a bot failure.
                raise TextTransportError(
                    f"text runtime did not answer within "
                    f"{self.settings.text_runtime_read_timeout:.0f}s (turn "
                    f"{self._turns + 1}, interaction={self.interaction_uuid}). The "
                    f"bot produced no reply — usually a hung tool call or LLM "
                    f"request; the runtime will idle-timeout at ~300s and return "
                    f"an empty message anyway. Not retried: the turn is already "
                    f"applied. Read the turns with "
                    f"OSVClient.get_conversations(client_uuid, interaction_uuid) "
                    f"and the server side with Loki "
                    f"{{app=\"orchestrator-service-text\"}} |= \"{self.interaction_uuid}\"."
                ) from exc

            if resp.status_code >= 400:
                body = resp.text[:400].strip() or "(empty body)"
                # A 5xx BEFORE the interaction exists is safe to retry: no turn
                # has been applied, so re-sending cannot duplicate anything. Seen
                # live against the India runtime — a transient 500 with an empty
                # body on turn 1, where the identical payload succeeded on retry.
                # Mid-conversation 5xx is NOT retried: the turn may already have
                # landed, same reasoning as the read-timeout path below.
                retryable = (
                    resp.status_code >= 500
                    and self.interaction_uuid is None
                    and attempt < self.settings.text_runtime_max_retries
                )
                if retryable:
                    backoff = 0.5 * (2 ** (attempt - 1))
                    logger.warning(
                        "text runtime %d on the opening turn (%s) — retrying in "
                        "%.1fs (%d/%d)",
                        resp.status_code, body, backoff, attempt,
                        self.settings.text_runtime_max_retries,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise TextTransportError(
                    f"text runtime {resp.status_code} at {url}: {body}"
                    + self._region_hint(resp.status_code, body)
                )
            data = resp.json()
            break
        else:  # pragma: no cover - loop always breaks or raises
            raise TextTransportError("text runtime retry loop exhausted")

        self._check_response(data)

        if not self.interaction_uuid and data.get("interaction_uuid"):
            self.interaction_uuid = str(data["interaction_uuid"])
            self.client_uuid = data.get("client_uuid")
            self._capture_debugging_urls(data)
            # client_uuid is logged because it is HALF THE LINK. llm-studio's
            # conversation URL is /clients/<client_uuid>/conversations?
            # interaction_uuid=<uuid>, and the client differs per region and per
            # tenant — so without it a finished run cannot be opened in the UI
            # without going and looking the client up.
            logger.info(
                "text interaction opened: interaction_uuid=%s client_uuid=%s "
                "recipient_id=%s",
                self.interaction_uuid, self.client_uuid or "(not returned)",
                self.recipient_id,
            )
            if self.client_uuid:
                logger.info(
                    "llm-studio: https://llm-studio.skit.ai/clients/%s/"
                    "conversations?interaction_uuid=%s",
                    self.client_uuid, self.interaction_uuid,
                )

        self._turns += 1
        return BotTurn(
            text=data.get("output_message") or "",
            current_module=data.get("current_module"),
            tool_calls=data.get("tool_calls") or [],
            reasoning_ms=(
                float(data["total_response_time"]) * 1000.0
                if data.get("total_response_time") is not None
                else None
            ),
            message_type=data.get("message_type"),
            interaction_uuid=self.interaction_uuid,
        )

    def _capture_debugging_urls(self, data: dict[str, Any]) -> None:
        meta = data.get("interaction_metadata") or {}
        for key in ("grafana_url", "sentry_url", "llm_studio_url", "tempo_url", "phoenix_url"):
            value = meta.get(key)
            if value:
                self.debugging_urls[key] = value

    # ------------------------------------------------------------- public API

    async def start(self) -> BotTurn:
        """Open the interaction and return the bot's first turn.

        An empty `user_message` is how OSV's own CLI driver elicits the
        greeting (`app/runtime/cli/main.py`).
        """
        # Logged BEFORE the first request, and at INFO, because the region is
        # the thing most likely to be wrong and the least likely to be noticed:
        # an exported AUTOCALLS_TEXT_RUNTIME_BASE_URL silently outranks .env,
        # and OSV's 404 blames the assistant_uuid instead. One line up front
        # beats reconstructing it from an error body.
        logger.info(
            "text runtime: %s (region %s, from %s) assistant=%s fc_version=%s "
            "is_test=%s recipient_id=%s",
            self.base_url,
            next((seg for seg in self.base_url.split("/") if seg in ALLOWED_REGIONS), "?"),
            "scenario" if self.scenario.region else "settings",
            self.assistant_uuid, self.fc_version, self.is_test, self.recipient_id,
        )
        return await self._post("")

    async def send(self, speech: str = "", dtmf: str = "") -> BotTurn:
        """Send one caller turn and return the bot's reply.

        `dtmf` wins when both are present: the caller LLM only sets it when the
        bot asked for keypad input, and digits reach the model as text either
        way (see module docstring).
        """
        message = dtmf or speech
        if not message:
            # The engine occasionally produces an empty turn (e.g. a barge that
            # was suppressed). Sending "" would re-trigger the greeting path,
            # so make the no-op explicit instead of silently misbehaving.
            raise TextTransportError("refusing to send an empty caller turn")
        return await self._post(message)

    async def close(self) -> None:
        # Close only what this transport OWNS — the worker's shared pool
        # outlives every run that borrows it (P1.3).
        if self._http is not None and self._owns_http:
            await self._http.aclose()
        self._http = None
