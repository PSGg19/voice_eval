from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # `extra="ignore"` is deliberate. pydantic-settings defaults to "forbid",
    # which means ANY `AUTOCALLS_*` variable this class doesn't declare crashes
    # `Settings()` — and `Settings()` runs at the top of every CLI command and
    # every server boot. Subsystems now legitimately own their own env vars and
    # read them directly (`services/osv/constants.py`, `jobs/store.py`,
    # `jobs/generator.py`), so under "forbid" adding one config value to a
    # subsystem breaks every unrelated command until this class is edited too.
    # That has already bitten twice: the SIP vars broke `opkura campaign`, and
    # `AUTOCALLS_OSV_*` then broke `opkura serve`.
    # The cost is losing typo detection on this class's own fields — a misspelt
    # `AUTOCALLS_TEMPERATUR` is now silently ignored rather than fatal. That is
    # the better trade: a typo means one setting doesn't apply, whereas "forbid"
    # means nothing runs at all.
    model_config = {"env_prefix": "AUTOCALLS_", "env_file": ".env", "extra": "ignore"}

    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str  # caller ID for outbound calls

    # ---- Outbound call transport ------------------------------------
    # "pstn" (default) dials the scenario `target` as an E.164 number:
    #   opkura → Twilio → PSTN → the bot's DID → that DID's SIP trunk →
    #   origination URI → LiveKit. Hops we don't control, and no way to
    #   carry a correlation key — see transport/server.py's
    #   status_callback note about joining on (caller#, called#, time).
    # "sip" dials `sip:<sip_trunk_number>@<sip_destination_host>`
    #   instead, so Twilio's INVITE lands directly on the LiveKit SIP
    #   host with our `X-` headers intact. The scenario `target` is NOT
    #   the user part — it travels in the X-VirtualNumber header (see
    #   sip_virtual_number_header). The YAML is identical either way, so
    #   scenarios stay portable and the E.164 lint rule still applies.
    # "text" places no call at all. It drives the same bot over OSV's text
    #   runtime (`POST /text_runtime/post_message_web`) with `channel=VOICE`,
    #   so the voice flow, modules, tools and LLM config are identical — only
    #   the audio pipeline is absent. No Twilio, no STT/TTS, no channel cap, and
    #   the interaction_uuid comes back on the first turn so correlation needs
    #   no SIP header. Cannot test barge, DTMF timing, STT mishearing, VAD or
    #   turn latency; scenarios needing those must use pstn/sip. See
    #   docs/dual-transport.md.
    # Env: AUTOCALLS_CALL_TRANSPORT=pstn|sip|text
    call_transport: Literal["pstn", "sip", "text"] = "pstn"
    # SIP host to send the INVITE to — bare hostname[:port], no scheme.
    # For LiveKit Cloud this is the project's SIP host plus the signaling
    # port, e.g. `581mqcm8zvu.sip.livekit.cloud:5060`.
    # Required when call_transport="sip".
    # Env: AUTOCALLS_SIP_DESTINATION_HOST=581mqcm8zvu.sip.livekit.cloud:5060
    sip_destination_host: str = ""
    # The user part of the SIP URI — LiveKit's inbound-trunk identifier
    # (e.g. `1205`), NOT the bot's DID. LiveKit routes on this to select
    # the trunk; which bot answers is decided by the X-VirtualNumber
    # header the trunk maps to the `virtualNumber` attribute.
    # Required when call_transport="sip".
    # Env: AUTOCALLS_SIP_TRUNK_NUMBER=1205
    sip_trunk_number: str = ""
    # Header carrying the scenario `target` (the bot number) to LiveKit.
    # Must match the inbound trunk's headersToAttributes key, e.g.
    #   "headersToAttributes": {"X-VirtualNumber": "virtualNumber"}
    # Env: AUTOCALLS_SIP_VIRTUAL_NUMBER_HEADER=X-VirtualNumber
    sip_virtual_number_header: str = "X-VirtualNumber"
    # Appended as `;transport=` on the URI. Blank (the default) omits the
    # parameter entirely, so the URI is exactly
    # `sip:<trunk>@<host:port>?X-…` and Twilio uses its default (UDP).
    # Set "tcp" if the header section grows enough to push the INVITE
    # near the 1500-byte UDP MTU — fragmented INVITEs get dropped by
    # intermediaries and present as a call that never rings.
    # Env: AUTOCALLS_SIP_TRANSPORT=udp|tcp|tls
    sip_transport: Literal["", "udp", "tcp", "tls"] = ""
    # Digest auth, when the far-end trunk requires username/password
    # instead of IP allow-listing (a LiveKit inbound trunk can be set up
    # either way). Left blank → the params aren't sent at all.
    # Env: AUTOCALLS_SIP_AUTH_USERNAME / AUTOCALLS_SIP_AUTH_PASSWORD
    sip_auth_username: str = ""
    sip_auth_password: str = ""
    # Extra static correlation headers as `X-A=1,X-B=2`. Merged into the
    # per-call headers opkura already injects (X-Opkura-Session-Id,
    # X-Is-Test), which win on conflict. Only `X-`-prefixed names are
    # accepted — Twilio silently drops the rest on outbound SIP.
    # Env: AUTOCALLS_SIP_EXTRA_HEADERS=X-Opkura-Suite=veros-ib2
    sip_extra_headers: str = ""

    # ---- Text transport (call_transport="text") ----------------------
    # Base URL of OSV's text runtime (`main.py --mode text`). Local default
    # matches the documented dev port.
    # Env: AUTOCALLS_TEXT_RUNTIME_BASE_URL=http://localhost:8001
    text_runtime_base_url: str = "http://localhost:8001"
    # Optional bearer token. The local text runtime needs none; a hosted one
    # may. Sent as `Authorization: Bearer …` when non-empty.
    # Env: AUTOCALLS_TEXT_RUNTIME_TOKEN=...
    text_runtime_token: str = ""
    # Address the assistant explicitly by uuid. When blank, the scenario's
    # `target` is sent as `virtual_id` and OSV resolves the assistant from it,
    # which keeps one YAML working for both text and voice. Set this when the
    # target DID is not registered as a virtual number in the environment you
    # are testing against.
    # Env: AUTOCALLS_TEXT_ASSISTANT_UUID=08432036-470c-4941-98b3-2b12c27fcef5
    text_assistant_uuid: str = ""
    # Pin the assistant's flow-config version. 0 = whatever OSV considers
    # current. Pinning matters for a regression suite: an unpinned run silently
    # changes meaning when someone publishes a new version.
    # Env: AUTOCALLS_TEXT_FC_VERSION=9
    text_fc_version: int = 0
    # Deployment default for the `is_test` flag on text interactions. A scenario
    # can override it with `is_test:` in its YAML; this is the fallback.
    #
    # FALSE by default, which is a deliberate trade. Measured on the live US
    # runtime (Loki, 2026-08-17):
    #   is_test=true  → the interaction is INVISIBLE in llm-studio, because the
    #                   conversations view filters on is_test=false. The only
    #                   thing the flag actually suppresses is the compliance
    #                   event (post_call_event.py:326).
    #   is_test=false → the run shows up in llm-studio like any other call, and
    #                   the compliance event fires.
    # It does NOT gate in-call tools, and it does NOT gate post-call CRM writes —
    # those fire after EVERY turn regardless. So `true` buys very little
    # protection while costing all visibility, and being able to open the run in
    # llm-studio is worth more. Set `is_test: true` per scenario when you
    # specifically need the compliance event suppressed.
    # Env: AUTOCALLS_TEXT_IS_TEST=true
    text_is_test: bool = False
    # Per-turn HTTP budget. A bot turn is an LLM call plus any tool calls, so
    # the read timeout has to be generous — 90s covers a slow tool chain.
    text_runtime_read_timeout: float = 90.0
    text_runtime_connect_timeout: float = 5.0
    text_runtime_max_retries: int = 3

    @model_validator(mode="after")
    def _validate_sip_transport_config(self) -> "Settings":
        """Fail at settings load, not on a live dial.

        Everything here is cheap to check and expensive to get wrong: a
        missing host or a malformed header spec would otherwise surface
        as a Twilio 400 after the scenario has already been set up.
        """
        if self.call_transport == "sip":
            from opkura.transport.sip import (
                parse_header_spec,
                validate_header_name,
                validate_sip_host,
                validate_trunk_number,
            )

            validate_sip_host(self.sip_destination_host)
            validate_trunk_number(self.sip_trunk_number)
            validate_header_name(self.sip_virtual_number_header)
            parse_header_spec(self.sip_extra_headers)
            if self.sip_auth_password and not self.sip_auth_username:
                raise ValueError(
                    "AUTOCALLS_SIP_AUTH_PASSWORD set without "
                    "AUTOCALLS_SIP_AUTH_USERNAME"
                )
        return self

    @property
    def parsed_sip_extra_headers(self) -> dict[str, str]:
        """`sip_extra_headers` as a dict (validated by the model validator)."""
        from opkura.transport.sip import parse_header_spec

        return parse_header_spec(self.sip_extra_headers)

    # Deepgram (STT + TTS)
    deepgram_api_key: str
    # Opt-in Deepgram VAD events. When true, STT URL adds
    # `model=nova-2&vad_events=true`. Nova-2 is required because the
    # account-default model may not support vad_events (we saw a silent
    # zero-transcript regression with `vad_events=true` alone). Default
    # false because the smart-grace + utterance_end_ms approach already
    # handles 90% of cases — enable only when you want extra mid-turn
    # pause detection. Env: AUTOCALLS_DEEPGRAM_VAD_EVENTS=true
    deepgram_vad_events: bool = False
    # Local audio-domain VAD (Silero) for turn-end detection. When true,
    # replaces Deepgram's UtteranceEnd-based silence trigger in
    # _wait_for_bot_turn. Deepgram STT keeps running for transcript text
    # — only the turn-end signal is replaced. Expected ~4s latency drop
    # per turn. Cost: ~300MB PyTorch install (see
    # plan/phases/phase-1.5/sub-task/task-1-silero-vad/SETUP.md).
    # Env: AUTOCALLS_USE_SILERO_VAD=true
    use_silero_vad: bool = False
    # TTS transport. "ws" uses Deepgram's WebSocket /v1/speak (lowest
    # TTFB ~250ms, default). "http" uses chunked HTTP /v1/speak
    # (TTFB ~1.1s, kept as fallback). "blocking" waits for the full
    # audio body before sending (legacy).
    # Env: AUTOCALLS_TTS_TRANSPORT=ws|http|blocking
    tts_transport: Literal["ws", "http", "blocking"] = "ws"
    # Legacy alias kept so existing .env files with
    # AUTOCALLS_STREAMING_TTS=false still produce blocking behavior
    # without manual migration. Precedence rule:
    #   - If AUTOCALLS_TTS_TRANSPORT is explicitly set, it wins.
    #   - Else, streaming_tts=False downgrades the default "ws" → "blocking".
    # The resolution happens in `resolved_tts_transport` below.
    # Env: AUTOCALLS_STREAMING_TTS=false
    streaming_tts: bool = True

    @property
    def resolved_tts_transport(self) -> str:
        """Apply the legacy `streaming_tts=False` → "blocking" override.

        When tts_transport is at its default "ws" AND streaming_tts is
        False, return "blocking". Otherwise return tts_transport as-is.
        This lets old .env files keep working without forcing a rename.
        """
        if self.tts_transport == "ws" and not self.streaming_tts:
            return "blocking"
        return self.tts_transport

    # DTMF transport (Phase 3). "rest" uses Twilio REST
    # `<Play digits=>` via call.update — produces RFC 2833 events on
    # the outbound SIP leg which LiveKit decodes natively. "audio"
    # is the legacy mulaw-DTMF-over-Media-Streams path (kept as a
    # debug fallback; LiveKit's SIP integration does NOT do in-band
    # audio-DSP DTMF detection so this path won't actually reach the
    # bot's DTMF handler on a LiveKit-based bot — see Phase 3 plan).
    # Env: AUTOCALLS_DTMF_TRANSPORT=rest|audio
    dtmf_transport: Literal["rest", "audio"] = "rest"
    # How long _send_dtmf waits for the new Media Stream WS to come
    # back after Twilio replays the `<Play digits>` + reconnect TwiML.
    # Observed reconnect typically lands in 3-5s; 30s default gives
    # generous slack against carrier hiccups.
    # Env: AUTOCALLS_DTMF_RECONNECT_TIMEOUT_S=30.0
    dtmf_reconnect_timeout_s: float = 30.0

    # ---- Job mode (v3) ---------------------------------------------------
    # These live HERE, not on `os.getenv` in the subsystems that use them,
    # because `.env` is read by pydantic-settings and is never exported into
    # the process environment. A subsystem calling `os.getenv` therefore sees
    # nothing at all when the value is set in `.env` — which presented as
    # "no OSV token" on a server whose .env had a perfectly good token.
    # Env: AUTOCALLS_OSV_BASE_URL / _ACCESS_TOKEN / _API_PREFIX
    osv_base_url: str = "https://llm-studio.skit.ai/us/api/interface"
    osv_access_token: str = ""
    # Blank means "probe it" — the gateway base URL above needs no prefix, a
    # direct-to-OSV host needs "/api". See services/osv/client.resolve_prefix.
    osv_api_prefix: str = ""
    # Job state store. Env: AUTOCALLS_REDIS_URL
    redis_url: str = "redis://localhost:6379/0"
    # Scenario generator. OpenRouter is OpenAI-schema compatible, so switching
    # provider is just this base URL plus the matching key.
    # Env: AUTOCALLS_LLM_BASE_URL / AUTOCALLS_JOB_GENERATOR_MODEL
    llm_base_url: str = "https://api.openai.com/v1"
    job_generator_model: str = "gpt-4.1"

    # LLM providers (Gemini is V2 default; others are plug-in)
    gemini_api_key: str = ""        # Phase 1 default
    anthropic_api_key: str = ""     # optional plugin
    openai_api_key: str = ""        # optional plugin

    # Default models per role (overridable per-scenario via `llm:` block)
    caller_model: str = "gemini-2.5-flash"
    evaluator_model: str = "gemini-2.5-pro"
    temperature: float = 0.3

    # Load-test mode: skip the LLM evaluator entirely (no Gemini eval calls).
    # The call still runs normally; `passed` is then derived purely from call
    # status (COMPLETED → pass). Set AUTOCALLS_SKIP_EVALUATION=true for load runs.
    skip_evaluation: bool = False

    # Server
    server_host: str = "0.0.0.0"
    server_port: int = 8765
    public_url: str = ""  # https://your-domain.ngrok.io

    # Storage
    db_path: str = "opkura.db"

    @property
    def ws_public_url(self) -> str:
        """Convert https:// public URL to wss:// for WebSocket."""
        return self.public_url.replace("https://", "wss://").replace("http://", "ws://")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls()
