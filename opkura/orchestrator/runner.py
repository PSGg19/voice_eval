"""Test runner — orchestrates calls and collects results."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from opkura.engine.caller import DEFAULT_BOT_TURN_TIMEOUT_MS, AICallerEngine
from opkura.engine.conversation import ConversationEngine, Termination
from opkura.transport.call import CallManager
from opkura.core.config import Settings
from opkura.core.log_context import call_id_var
import httpx

from opkura.core.region import apply_region, reset_announcements as reset_region_announcements
from opkura.core.region_profile import resolve_profile
from opkura.core.run_context import RunContext
from opkura.observers import (
    BotTurn,
    CallerTurn,
    ObservedLLM,
    RunFinished,
    RunStarted,
    build_observers,
    run_dispatcher,
)
from opkura.observers import emit as emit_event
from opkura.observers.dispatcher import current_dispatcher
from opkura.engine.assertions import evaluate_assertions
from opkura.engine.evaluator import Evaluator
from opkura.legacy.flow import FlowEngine
from opkura.llm import detect_provider, get_client
from opkura.persistence.logwriter import CallLogWriter
from opkura.core.models import (
    CallResult,
    CallStatus,
    EvaluationResult,
    Flow,
    Scenario,
    TranscriptEntry,
)
from opkura.core.scenario_loader import load as load_scenario
from opkura.transport.server import register_session, remove_session
from opkura.db.results import PgResultStore
from opkura.transport.stream import MediaStreamSession
from opkura.transport.text import TextTransport, TextTransportError
from opkura.transport.text_adapter import TextFrameAdapter
from opkura.voice.session import VoiceSession
from opkura.transport.tts import DeepgramTTS

logger = logging.getLogger(__name__)

# Wall-clock cap on the evaluator's two-attempt LLM round-trip. Generous
# enough for gemini-2.5-pro thinking + retry; protects against hangs that
# would otherwise stall the runner forever.
EVALUATOR_TIMEOUT_S = 90.0
# Evaluator wants low variance: a grading verdict should not jitter between
# pass/fail across runs of the same transcript. Hardcoded rather than
# inheriting scenario.llm.temperature (which is tuned for the chatty caller).
EVALUATOR_TEMPERATURE = 0.0


def load_flow(path: str | Path) -> Flow:
    """Load a flow definition from a YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return Flow(**data)


def _scenario_log_root(scenario: Scenario) -> Path:
    """Compute the per-call log folder root from the scenario's source path so
    on-disk layout mirrors the YAML tree:
      clients/resurgent/scenarios/auth/A-001.yaml
    writes calls under:
      logs/clients/resurgent/scenarios/auth/call-<slug>-<utc>-<id8>/
    Falls back to flat "logs/" when source_path is missing (test fixtures
    constructing Scenario directly) or when the source lives outside cwd.
    """
    if not scenario.source_path:
        return Path("logs")
    parent = Path(scenario.source_path).parent
    if parent.is_absolute():
        try:
            parent = parent.relative_to(Path.cwd())
        except ValueError:
            return Path("logs")
    return Path("logs") / parent


def detect_schema(path: str | Path) -> str:
    """Return 'v1' or 'v2' based on YAML shape."""
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"scenario file {path} must contain a YAML mapping")
    v2_keys = {"persona", "evaluation_prompt"}
    present_v2 = sorted(v2_keys & data.keys())
    has_v1 = "steps" in data
    # Any V2 key alongside `steps` is a mixed/typo'd scenario — reject so the
    # orphan key isn't silently dropped.
    if present_v2 and has_v1:
        raise ValueError(
            f"scenario {path} mixes V1 ('steps') with V2 keys {present_v2}; pick one schema"
        )
    if len(present_v2) == len(v2_keys):
        return "v2"
    if has_v1:
        return "v1"
    raise ValueError(
        f"unknown scenario schema in {path}; expected V1 'steps' or V2 'persona'+'evaluation_prompt'"
    )


class TestRunner:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        per_run_debug_log: bool = True,
        persist_result: bool = True,
    ):
        """`settings=None` (P1.1) is legal for context-driven execution: a
        fully-ported RunContext supplies the collaborators, and the heavy
        Settings-backed ones below are built lazily so a text run with fakes
        never constructs a Twilio client, a Deepgram client, or a DB engine."""
        self.settings = settings
        # P1.5 — one log stream. True only for single-run use (the CLI at
        # concurrency 1), where a per-run debug.log is genuinely convenient.
        # Concurrent modes (campaign with workers > 1, the API executor) set
        # this False: each per-run root FileHandler is O(in-flight runs)
        # filter work on every record, and the structured LOG_SINK stream
        # already carries run_id per line. CampaignRunner flips it from
        # max_concurrency at campaign start.
        self.per_run_debug_log = per_run_debug_log
        # False when a caller owns the run record already — the queue's
        # StageWorker persists the canonical `runs` row itself, and the
        # runner writing its own would double-account every call.
        self.persist_result = persist_result
        self._call_mgr: Optional[CallManager] = None
        self._tts: Optional[DeepgramTTS] = None
        self._store: Optional[PgResultStore] = None
        # Shared per-worker clients (P1.3). Safe to share because LLM clients
        # carry no per-call state since P1.2 (usage rides in the LLMResult),
        # and the HTTP client's headers/timeouts travel per request.
        self._llm_clients: dict[tuple[str, Optional[str]], object] = {}
        self._shared_http: Optional[httpx.AsyncClient] = None

    @property
    def shared_http(self) -> httpx.AsyncClient:
        """One pooled AsyncClient per worker, with EXPLICIT limits — an
        unbounded pool hides a concurrency bug until the far end runs out of
        sockets mid-campaign."""
        if self._shared_http is None:
            self._shared_http = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
            )
        return self._shared_http

    def _shared_llm(self, model: str, provider: Optional[str]) -> object:
        """One LLM client per (model, provider) per worker."""
        key = (model, provider)
        if key not in self._llm_clients:
            api_key = self._api_key_for(model, provider)
            self._llm_clients[key] = get_client(model, api_key, provider=provider)
        return self._llm_clients[key]

    def _require_settings(self, what: str) -> Settings:
        if self.settings is None:
            raise RuntimeError(
                f"{what} needs Settings — this TestRunner was built without them "
                f"(context-driven mode); provide the matching RunContext port instead"
            )
        return self.settings

    @property
    def call_mgr(self) -> CallManager:
        if self._call_mgr is None:
            self._call_mgr = CallManager(self._require_settings("Twilio dialing"))
        return self._call_mgr

    @property
    def tts(self) -> DeepgramTTS:
        if self._tts is None:
            self._tts = DeepgramTTS(
                api_key=self._require_settings("TTS").deepgram_api_key
            )
        return self._tts

    @property
    def store(self) -> PgResultStore:
        if self._store is None:
            self._store = PgResultStore(
                self._require_settings("result persistence").database_url
            )
        return self._store

    async def run_flow(self, flow: Flow) -> CallResult:
        """Execute a single test flow: make a call, converse, evaluate."""
        # v1 bugfix: full uuid hex (was [:12]) — 48 bits is too narrow for the
        # process-wide session registry; collision swaps engines between calls.
        session_id = uuid.uuid4().hex
        # Phase 2 fix: set per-call ContextVar for log routing. V1 path
        # doesn't use CallLogWriter today, but keeping the contextvar
        # lifecycle uniform across V1/V2 means future V1 logwriter
        # adoption doesn't need a separate plumbing pass.
        call_id_token = call_id_var.set(session_id)
        try:
            return await self._run_flow_body(flow, session_id)
        finally:
            call_id_var.reset(call_id_token)

    async def _run_flow_body(self, flow: Flow, session_id: str) -> CallResult:
        result = CallResult(
            id=session_id,
            session_id=session_id,  # see the V2 path — the SIP-header join key
            flow_name=flow.name,
            target=flow.target,
            started_at=datetime.now(timezone.utc),
        )

        engine = FlowEngine(flow)
        stream_session = MediaStreamSession(
            engine=engine,
            deepgram_api_key=self.settings.deepgram_api_key,
            tts=self.tts,
            result=result,
            stt_vad_events=self.settings.deepgram_vad_events,
            use_silero_vad=self.settings.use_silero_vad,
            streaming_tts=self.settings.streaming_tts,
            tts_transport=self.settings.resolved_tts_transport,
            # Phase 3 (Task 2) — REST-DTMF redirect plumbing.
            call_manager=self.call_mgr,
            session_id=session_id,
            reconnect_ws_url=f"{self.settings.ws_public_url}/ws/{session_id}",
            dtmf_transport=self.settings.dtmf_transport,
            dtmf_reconnect_timeout_s=self.settings.dtmf_reconnect_timeout_s,
        )

        completion = asyncio.Event()
        # See V2 path comment — FastAPI server runs in a separate thread, so
        # we must stash this loop for cross-thread .set() via call_soon_threadsafe.
        runner_loop = asyncio.get_running_loop()
        register_session(
            session_id,
            {
                "settings": self.settings,
                "stream_session": stream_session,
                "completion_event": completion,
                "completion_loop": runner_loop,
                "result": result,
            },
        )

        call_sid: str | None = None
        try:
            try:
                call_sid = await self.call_mgr.make_call_async(flow.target, session_id)
                result.call_sid = call_sid
                result.status = CallStatus.RINGING
            except Exception as exc:
                result.finish(CallStatus.ERROR, f"Failed to initiate call: {exc}")
                return result

            max_duration = sum(s.timeout_ms for s in flow.steps) / 1000 + 30
            try:
                await asyncio.wait_for(completion.wait(), timeout=max_duration)
            except asyncio.TimeoutError:
                logger.warning("Call timed out after %.0fs", max_duration)
                result.finish(CallStatus.ERROR, "Call timed out")
                try:
                    await self.call_mgr.hang_up_async(call_sid)
                except Exception as exc:
                    logger.warning("hang_up failed during timeout cleanup: %s", exc)

            result.steps = engine.step_results

            # v1 bugfix: previously only finalized when status==IN_PROGRESS;
            # RINGING calls that never reached the media stream stayed stuck
            # with ended_at=None, breaking duration_ms / passed downstream.
            if result.status not in (CallStatus.COMPLETED, CallStatus.ERROR, CallStatus.FAILED):
                result.finish(CallStatus.COMPLETED)

            try:
                paths = stream_session.save_recording("recordings")
                if paths:
                    result.recording_paths = paths
            except Exception as exc:
                logger.warning("Failed to save recording: %s", exc)

            try:
                if self.persist_result:
                    await self.store.save_result(
                        result, transport=self.settings.call_transport
                    )
            except Exception as exc:
                logger.error(
                    "Failed to persist result (is Postgres reachable? "
                    "AUTOCALLS_DATABASE_URL): %s", exc,
                )
            return result
        finally:
            # v1 bugfix: prevents session-registry leak when make_call raises
            # or the WS handler never runs to clean up. remove_session is
            # idempotent so double-removal (WS + here) is safe.
            remove_session(session_id)
            if call_sid and result.status in (CallStatus.RINGING, CallStatus.IN_PROGRESS):
                try:
                    await self.call_mgr.hang_up_async(call_sid)
                except Exception as exc:
                    logger.warning("hang_up failed during finally cleanup: %s", exc)

    async def run_flow_file(self, path: str | Path) -> CallResult:
        """Load a YAML flow and run it."""
        flow = load_flow(path)
        return await self.run_flow(flow)

    async def run_scenario_file(self, path: str | Path) -> CallResult:
        """Detect schema and dispatch to V1 or V2 runner."""
        schema = detect_schema(path)
        if schema == "v1":
            # P2.6 decision: the sync FlowEngine is retained (frozen) for
            # `opkura load` / `opkura watch`; running a V1 steps file as a
            # conversational test is deprecated in favour of V2 scenarios.
            logger.warning(
                "DEPRECATED: %s is a V1 `steps:` flow. V1 conversational runs "
                "are frozen — write a V2 scenario (persona + evaluation_prompt) "
                "instead. V1 remains supported only for `opkura load`/`watch`.",
                path,
            )
            return await self.run_flow_file(path)
        scenario = load_scenario(path)
        return await self._run_v2_scenario(scenario)

    async def run_scenario(
        self, scenario: Scenario, ctx: Optional[RunContext] = None,
        *, run_id: Optional[str] = None,
    ) -> CallResult:
        """Run an already-parsed V2 scenario. Public entry for callers that
        hold a Scenario object rather than a file — the V3 run API (P3.3)
        builds one from a `scenarios.body` row via parse_scenario_data.

        `ctx` (P1.1) carries the run's resolved config and ports; omitted, it
        is built here from Settings — this method IS the boundary.

        `run_id` lets a caller that already owns a run identity (the queue's
        StageWorker owns the `runs` row) stamp it on the session, the log
        context, and the trace — one id across DB, logs, and traces."""
        return await self._run_v2_scenario(scenario, ctx, run_id=run_id)

    async def _run_v2_scenario(
        self, scenario: Scenario, ctx: Optional[RunContext] = None,
        *, run_id: Optional[str] = None,
    ) -> CallResult:
        session_id = run_id or uuid.uuid4().hex
        # Phase 2 concurrent-writer fix: set the per-call ContextVar so
        # every log record emitted within this scenario's task subtree
        # carries the right call_id. CallLogWriter's _CallIdFilter reads
        # this var and routes records to the correct debug.log. Without
        # this, parallel campaign runs would interleave each others' log
        # lines into every debug.log. Reset in `finally` so a subsequent
        # scenario (or the campaign reporter) doesn't inherit this id.
        call_id_token = call_id_var.set(session_id)
        # Region resolution announces itself once per (region, url) decision so a
        # per-turn property doesn't repeat it. Clear per scenario, or a campaign
        # (and `opkura serve`, which is long-lived) would announce only the first
        # run's routing and then go silent for every scenario after it.
        reset_region_announcements()
        # The boundary (P1.1): all ambient config is resolved into the
        # RunContext here; the body below asks the context, not Settings.
        if ctx is None:
            ctx = RunContext.from_settings(
                scenario, self._require_settings("running without a RunContext"),
                run_id=session_id,
            )
        # Observability lives BESIDE the run (P5): observers are built from the
        # environment at the boundary; with everything off this installs
        # nothing and every emit() below is a single ContextVar read.
        observers = build_observers()
        try:
            with run_dispatcher(observers):
                emit_event(RunStarted(
                    run_id=session_id,
                    scenario_name=scenario.name,
                    target=ctx.target,
                    region=ctx.region,
                    transport=ctx.transport,
                ))
                result: CallResult | None = None
                try:
                    result = await self._run_v2_scenario_body(scenario, session_id, ctx)
                    return result
                finally:
                    if result is not None:
                        emit_event(RunFinished(
                            status=result.status.value,
                            passed=result.passed,
                            error=result.error or "",
                            interaction_uuid=result.interaction_uuid or "",
                        ))
                    else:  # body raised/cancelled before producing a result
                        emit_event(RunFinished(status="error", error="run aborted"))
        finally:
            call_id_var.reset(call_id_token)

    def _build_voice_session(
        self, *, scenario: Scenario, engine: AICallerEngine,
        result: CallResult, session_id: str,
    ) -> VoiceSession:
        """The V2 voice path's session, chain-backed (P6). Heavy clients are
        FACTORIES: the session builds them lazily on the server's loop, so
        nothing here constructs a Deepgram connection on the runner's loop."""
        settings = self._require_settings("voice transport")
        conversation = ConversationEngine(scenario, engine)

        def stt_factory(*, on_final, on_interim, on_speech_started,
                        on_utterance_end):
            from opkura.transport.stt import DeepgramSTT

            # DeepgramSTT signals UtteranceEnd as on_final("") — adapt that
            # convention to the chain's explicit on_utterance_end callback.
            def final_adapter(text: str) -> None:
                if text:
                    on_final(text)
                else:
                    on_utterance_end()

            return DeepgramSTT(
                api_key=settings.deepgram_api_key,
                on_final=final_adapter,
                on_interim=on_interim,
                on_speech_started=on_speech_started,
                utterance_end_ms=scenario.stt.utterance_end_ms,
                vad_events=settings.deepgram_vad_events,
            )

        def tts_factory():
            if scenario.tts.model:
                logger.info("Caller TTS voice override: %s", scenario.tts.model)
                return DeepgramTTS(api_key=settings.deepgram_api_key,
                                   model=scenario.tts.model)
            return DeepgramTTS(api_key=settings.deepgram_api_key)

        vad_factory = None
        if settings.use_silero_vad:
            def vad_factory():  # noqa: F811 — deliberate rebind
                from opkura.transport.vad import SileroBotVAD

                return SileroBotVAD()

        return VoiceSession(
            conversation=conversation,
            caller=engine,
            result=result,
            stt_factory=stt_factory,
            tts_factory=tts_factory,
            vad_factory=vad_factory,
            silero_silence_threshold_s=scenario.stt.silence_threshold_s,
            turn_timeout_s=DEFAULT_BOT_TURN_TIMEOUT_MS / 1000,
            call_manager=self.call_mgr,
            session_id=session_id,
            reconnect_ws_url=f"{settings.ws_public_url}/ws/{session_id}",
            dtmf_transport=settings.dtmf_transport,
            dtmf_reconnect_timeout_s=settings.dtmf_reconnect_timeout_s,
            # Hand the runner's observer dispatcher across the task-tree gap:
            # the chain runs under the server's websocket handler, which never
            # inherited this context, and voice traces were bare roots.
            dispatcher=current_dispatcher(),
        )

    async def _run_v2_scenario_body(
        self, scenario: Scenario, session_id: str, ctx: RunContext
    ) -> CallResult:
        result = CallResult(
            id=session_id,
            # Set explicitly as well as via `id`: job-mode correlation joins on
            # this exact value (it is the X-Opkura-Session-Id SIP header), and
            # depending on `id` happening to equal the session id is the kind of
            # coincidence that breaks silently the day `id` changes.
            session_id=session_id,
            flow_name=scenario.name,
            target=scenario.target,
            started_at=datetime.now(timezone.utc),
        )

        caller_model = ctx.caller_model
        eval_model = ctx.evaluator_model

        # Logwriter constructed FIRST — before _api_key_for / get_client can
        # raise — so even config errors produce a log folder for triage. The
        # outer try/finally below guarantees logwriter.close() always runs,
        # detaching the debug.log FileHandler from the 'opkura' logger.
        # Without this, a leaked handler causes every subsequent V2 call's
        # debug.log to be polluted with prior calls' log lines.
        #
        # Log root mirrors the scenario's source path so triage is trivial:
        #   clients/resurgent/scenarios/auth/A-001.yaml
        # writes to
        #   logs/clients/resurgent/scenarios/auth/call-<slug>-<utc>-<id8>/
        # Falls back to flat "logs/" when source_path is missing (test
        # fixtures that construct Scenario directly).
        log_root = _scenario_log_root(scenario)
        logwriter = CallLogWriter(
            root=log_root, scenario=scenario, call_id=session_id,
            capture_debug_log=self.per_run_debug_log,
        )
        # Surface the log folder on the result BEFORE any failure can occur —
        # setup errors (api_key, provider mismatch) shouldn't hide where the
        # debug.log lives.
        result.log_dir = str(logwriter.dir)
        call_start_monotonic = time.monotonic()

        # Transport resolution: the scenario wins over the deployment default.
        # A barge scenario has to run on audio and a reasoning scenario is
        # cheaper on text, so the scenario is the right place to say which —
        # while `AUTOCALLS_CALL_TRANSPORT` stays the default for everything that
        # does not care. Validated at lint time (core/lint.py::_check_transport).
        transport = ctx.transport
        if scenario.transport and self.settings and scenario.transport != self.settings.call_transport:
            logger.info(
                "scenario pins transport=%s (deployment default is %s)",
                scenario.transport, self.settings.call_transport,
            )
        # "text" swaps the entire audio path for HTTP turns against OSV's text
        # runtime. Everything else in this method — logwriter, engine, evaluator,
        # persistence, the finally block — is transport-agnostic and shared.
        is_text = transport == "text"
        stream_session: Optional[VoiceSession] = None
        text_transport: Optional[TextTransport] = None
        # Per-scenario caller voice. None until the VOICE branch needs one —
        # touching self.tts here would build a Deepgram client for every text
        # run (and demand Settings a context-driven run doesn't have). The
        # default is the shared self.tts (English); a scenario may pick a
        # Spanish voice via `tts.model`, which gets its own DeepgramTTS,
        # closed in the outer finally.
        call_tts: Optional[DeepgramTTS] = None
        try:
            try:
                # Ports first (P1.1): an injected collaborator is used verbatim;
                # None falls back to the Settings-backed production default.
                if ctx.ports.caller_llm is not None:
                    base_llm = ctx.ports.caller_llm
                else:
                    base_llm = self._shared_llm(caller_model, scenario.llm.provider)
                # ObservedLLM measures each complete_json around the actual
                # await and emits a Generation event; the engine sees an
                # identical client surface.
                llm = ObservedLLM(base_llm, role="caller")
                engine = AICallerEngine(scenario, llm)
                engine.attach_logwriter(logwriter)
                if is_text:
                    # No audio path at all: no TTS voice, no media-stream
                    # session, no Twilio. `stream_session` stays None and the
                    # text branch below drives the conversation over HTTP.
                    if ctx.ports.text_transport is not None:
                        text_transport = ctx.ports.text_transport(scenario, session_id)
                    else:
                        text_transport = TextTransport(
                            settings=self._require_settings("the OSV text transport"),
                            scenario=scenario,
                            session_id=session_id,
                            http=self.shared_http,  # the worker's pool (P1.3)
                        )
                else:
                    # P6 — voice runs on the processor chain. The whole
                    # session (engine included) lives on the SERVER's loop;
                    # this runner only dials, waits on the completion event,
                    # and reads the result afterwards. All heavy clients are
                    # factories so the session builds them on its own loop.
                    stream_session = self._build_voice_session(
                        scenario=scenario, engine=engine, result=result,
                        session_id=session_id,
                    )
            except Exception as setup_exc:
                logger.exception("V2 setup failed before dialing")
                result.finish(CallStatus.ERROR, f"setup failed: {setup_exc}")

            if is_text and text_transport is not None:
                # P1.6 — the wall-clock cap voice already has, now OWNED by the
                # conversation engine (P2.2): it covers receive, the persona
                # LLM and the HTTP send alike. Resolved at the boundary
                # (RunContext.from_settings); a directly-built context may
                # leave it None, so fall back to the identical voice formula
                # rather than running uncapped.
                deadline_s = ctx.limits.run_deadline_s
                if deadline_s is None:
                    per_turn_s = DEFAULT_BOT_TURN_TIMEOUT_MS / 1000
                    deadline_s = scenario.max_turns * per_turn_s + 30
                conversation = ConversationEngine(
                    scenario, engine, deadline_s=deadline_s
                )
                # Live-shared: a crash mid-run still leaves the partial
                # transcript, module path and tool calls on the result for
                # triage and enrichment — proven necessary by the 2026-08-31
                # Veros probe, where a turn-18 runtime hang (TextTransportError)
                # skipped the post-run copies and shipped an ERROR result with
                # modules_visited=[] while the engine knew GREETINGS.
                result.transcript = conversation.transcript
                result.modules_visited = conversation.modules_visited
                result.tool_calls = conversation.tool_calls
                result.status = CallStatus.IN_PROGRESS
                try:
                    outcome = await conversation.run(TextFrameAdapter(text_transport))
                    # D3: only the bot's own reasoning latency feeds the
                    # per-run latency stats; the HTTP round trip is a separate
                    # stream and not comparable.
                    for ms in outcome.reasoning_latencies_ms:
                        engine.record_bot_response_latency(ms)
                    if outcome.termination == Termination.DEADLINE:
                        logger.warning(
                            "text run exceeded its %.0fs wall-clock deadline",
                            deadline_s,
                        )
                        result.finish(
                            CallStatus.ERROR,
                            f"Run deadline exceeded ({deadline_s:.0f}s wall clock)",
                        )
                    elif outcome.termination == Termination.CANCELLED:
                        result.finish(CallStatus.ERROR, "run cancelled")
                    elif result.status not in (
                        CallStatus.COMPLETED,
                        CallStatus.ERROR,
                        CallStatus.FAILED,
                    ):
                        result.finish(CallStatus.COMPLETED)
                except TextTransportError as exc:
                    # A soft failure from the text runtime (wrong channel, no
                    # flow config, unreachable host). Fail the run rather than
                    # letting the caller LLM converse with an apology message.
                    logger.error("text transport failed: %s", exc)
                    result.finish(CallStatus.ERROR, f"text transport failed: {exc}")
                except Exception as exc:
                    logger.exception("text conversation crashed")
                    result.finish(CallStatus.ERROR, f"text conversation crashed: {exc}")
                finally:
                    # Correlation must survive an errored run — the join key
                    # is most valuable exactly when the run needs triage.
                    if not result.interaction_uuid:
                        result.interaction_uuid = (
                            conversation.interaction_uuid
                            or text_transport.interaction_uuid
                            or ""
                        )
                    # Deliberately in `finally`, and BEFORE close(): a stalled
                    # or crashed run is exactly when the tag-only diagnosis
                    # matters most. The Veros IB 2.0 hang ends in a ReadTimeout
                    # -> TextTransportError, so enriching only on the success
                    # path would miss every case worth explaining. It swallows
                    # its own exceptions, so it cannot mask the real failure.
                    await self._enrich_text_result_from_osv(
                        scenario=scenario, transport=text_transport, result=result
                    )
                    await text_transport.close()

            elif stream_session is not None:
                completion = asyncio.Event()
                # FastAPI server runs in a separate thread (cli/main.py:53).
                # The server thread cannot call completion.set() directly —
                # asyncio.Event is not thread-safe. Stash this loop so server
                # code can schedule .set() via call_soon_threadsafe.
                runner_loop = asyncio.get_running_loop()
                register_session(
                    session_id,
                    {
                        "settings": self.settings,
                        "stream_session": stream_session,
                        "completion_event": completion,
                        "completion_loop": runner_loop,
                        "result": result,
                    },
                )

                call_sid: str | None = None
                try:
                    try:
                        # Region-wise trunk selection (P3.8/P3.9). Resolving
                        # BEFORE dialing means an unconfigured region (India
                        # trunk coordinates unknown) or a non-compliant
                        # transport/region pair (PSTN into India — S6) is an
                        # ERROR result with a message naming the fix, and no
                        # call is ever placed.
                        profile = resolve_profile(
                            scenario.region, self.settings, for_transport=transport
                        )
                        call_sid = await self.call_mgr.make_call_async(
                            scenario.target, session_id, profile
                        )
                        result.call_sid = call_sid
                        result.status = CallStatus.RINGING
                    except Exception as exc:
                        result.finish(CallStatus.ERROR, f"Failed to initiate call: {exc}")

                    if result.status == CallStatus.RINGING:
                        per_turn_s = DEFAULT_BOT_TURN_TIMEOUT_MS / 1000
                        setup_buffer_s = 30
                        max_duration = scenario.max_turns * per_turn_s + setup_buffer_s
                        try:
                            await asyncio.wait_for(completion.wait(), timeout=max_duration)
                        except asyncio.TimeoutError:
                            logger.warning("Scenario timed out after %.0fs", max_duration)
                            result.finish(CallStatus.ERROR, "Scenario timed out")
                            try:
                                await self.call_mgr.hang_up_async(call_sid)
                            except Exception as exc:
                                logger.warning("hang_up failed during timeout cleanup: %s", exc)

                        if result.status not in (CallStatus.COMPLETED, CallStatus.ERROR, CallStatus.FAILED):
                            result.finish(CallStatus.COMPLETED)
                finally:
                    remove_session(session_id)
                    # Always tell Twilio to end the call when V2 control flow
                    # exits — the AI caller engine ends the conversation locally
                    # (max_turns hit, LLM double-failure, hangup=true) without
                    # sending TwiML's <Hangup>, so without this REST call the
                    # bot stays on the line until Twilio times out. hang_up()
                    # is already exception-safe (logs warning + swallows).
                    if call_sid:
                        try:
                            await self.call_mgr.hang_up_async(call_sid)
                        except Exception as exc:
                            logger.warning("hang_up failed during finally cleanup: %s", exc)

            # Load-test mode: skip the LLM evaluator entirely. No Gemini eval
            # call is made; pass/fail is derived from call status alone
            # (COMPLETED → pass). Keeps an EvaluationResult so `passed`, the
            # campaign report, and the DB row all stay consistent.
            if ctx.skip_evaluation:
                connected = result.status == CallStatus.COMPLETED
                result.evaluation = EvaluationResult(
                    overall_passed=connected,
                    overall_reasoning=(
                        "evaluation skipped (load-test mode); "
                        f"pass derived from call status={result.status.value}"
                    ),
                )
                logger.info(
                    "Evaluator skipped (skip_evaluation=true) — passed=%s (status=%s)",
                    connected, result.status.value,
                )
            else:
                # Evaluator — always runs, even on call failure / empty transcript.
                # logwriter is attached so evaluator LLM calls appear in
                # llm_calls.json with role="evaluator".
                logger.info(
                    "Evaluator starting (model=%s, timeout=%.0fs)",
                    eval_model, EVALUATOR_TIMEOUT_S,
                )
                self._run_assertions(scenario, result)
                result.evaluation = await self._run_evaluator(
                    scenario, result, logwriter=logwriter, eval_model=eval_model,
                    ctx=ctx,
                )
                if result.evaluation is not None:
                    logger.info(
                        "Evaluator finished — overall_passed=%s reasoning=%s",
                        result.evaluation.overall_passed,
                        (result.evaluation.overall_reasoning or "")[:120],
                    )

            # Persist after recording_paths is set (in finally below) so the
            # DB row reflects the canonical audio location. On cancellation
            # this await raises CancelledError immediately, which is fine —
            # the per-call log folder still captures the run state via the
            # finally block.
            try:
                if self.persist_result:
                    store = ctx.ports.result_store if ctx.ports.result_store is not None else self.store
                    await store.save_result(
                        result, scenario=scenario, transport=transport
                    )
            except Exception as exc:
                logger.error(
                    "Failed to persist result (is Postgres reachable? "
                    "AUTOCALLS_DATABASE_URL): %s", exc,
                )

            return result
        finally:
            # Folder finalization runs on EVERY exit path: success, exception,
            # KeyboardInterrupt/CancelledError. All writes are sync, so they
            # complete even when the task is in the cancelling state. Audio is
            # saved into the call folder directly (no separate "recordings/"
            # dir for V2) and recording_paths points at the canonical location.
            duration_ms = (time.monotonic() - call_start_monotonic) * 1000.0
            try:
                logwriter.write_transcript(result.transcript)
            except Exception as exc:
                logger.warning("logwriter.write_transcript failed: %s", exc)
            if result.evaluation is not None:
                try:
                    logwriter.write_evaluation(result.evaluation)
                except Exception as exc:
                    logger.warning("logwriter.write_evaluation failed: %s", exc)
            if stream_session is not None:
                try:
                    logwriter.save_audio(stream_session.save_recording)
                    paths = {}
                    # "mixed" is the dual-channel conversation file (bot left,
                    # opkura right) — the one a human actually listens to.
                    for kind in ("mixed", "bot", "caller"):
                        wav = logwriter.dir / f"{kind}.wav"
                        if wav.exists():
                            paths[kind] = str(wav)
                    if paths:
                        result.recording_paths = paths
                except Exception as exc:
                    logger.warning("logwriter.save_audio failed: %s", exc)
            try:
                logwriter.write_metadata(
                    result=result,
                    llm_models={"caller": caller_model, "evaluator": eval_model},
                    duration_ms=duration_ms,
                )
            except Exception as exc:
                logger.warning("logwriter.write_metadata failed: %s", exc)
            # Always close — handler detach + missing-file stubs run here no
            # matter what (setup error, runner cancellation, evaluator crash).
            try:
                logwriter.close()
            except Exception as exc:
                logger.warning("logwriter.close failed in outer finally: %s", exc)
            # Close the per-scenario caller TTS (Spanish voice etc.). The shared
            # self.tts is closed once at runner shutdown, so skip it here.
            # Compare against the raw slot, not the property — the property
            # BUILDS a client, which a text run must never trigger.
            if call_tts is not None and call_tts is not self._tts:
                try:
                    await call_tts.close()
                except Exception as exc:
                    logger.warning("per-scenario TTS close failed: %s", exc)

    def _run_assertions(self, scenario: Scenario, result: CallResult) -> None:
        """Check the scenario's `assert:` block against what was observed.

        Runs before the evaluator so a structural failure is already recorded
        when the verdict is assembled, and so the log reads in causal order. A
        failure vetoes `CallResult.passed` regardless of what the judge says.
        """
        spec = getattr(scenario, "assertions", None)
        if spec is None or spec.is_empty:
            return
        report = evaluate_assertions(
            spec,
            modules_visited=result.modules_visited,
            tool_calls=result.tool_calls,
            tag_only_bot_turns=result.tag_only_bot_turns,
            tools_observed=result.osv_enriched,
        )
        result.assertion_outcomes = [
            {"name": o.name, "status": o.status, "detail": o.detail}
            for o in report.outcomes
        ]
        logger.info("assertions: %s", report.summary())

    def _osv_enrichment_token(self, osv_url: str, scenario: Scenario) -> str:
        """The token for the post-run conversations fetch.

        `AUTOCALLS_OSV_ACCESS_TOKEN` if set. Otherwise fall back to
        `AUTOCALLS_TEXT_RUNTIME_TOKEN` — **only when both URLs are the same
        host**. In practice they are one llm-studio access JWT copied from the
        same browser session, so without the fallback this whole feature ships
        switched off for everyone already running text mode. But the text
        runtime can legitimately be pointed somewhere else (the default is
        `http://localhost:8001`), and sending a local token to llm-studio would
        be leaking a credential to a host it was not issued for. Hence the
        host check rather than a bare `or`.
        """
        if self.settings.osv_access_token:
            return self.settings.osv_access_token
        runtime_token = self.settings.text_runtime_token
        if not runtime_token:
            return ""
        from urllib.parse import urlsplit

        runtime_url = apply_region(
            self.settings.text_runtime_base_url,
            scenario.region,
            what="text_runtime_base_url",
        )
        if urlsplit(osv_url).netloc != urlsplit(runtime_url).netloc:
            logger.debug(
                "not reusing the text-runtime token for OSV enrichment: %r and "
                "%r are different hosts",
                urlsplit(runtime_url).netloc, urlsplit(osv_url).netloc,
            )
            return ""
        return runtime_token

    async def _enrich_text_result_from_osv(
        self,
        *,
        scenario: Scenario,
        transport: TextTransport,
        result: CallResult,
    ) -> None:
        """Recover what the text-runtime HTTP response drops.

        Context-driven runs without Settings (P1.1) skip enrichment: there is
        no OSV base URL or token to fetch with, and enrichment is additive by
        contract (its absence marks assertions INCONCLUSIVE, never failed).

        Two things never make it back through `post_message_web`:

        * **`tool_calls`** — the response accepts the field but never fills it,
          so `result.tool_calls` was always `[]` even on turns that ran a tool.
          Ordered tool assertions were impossible.
        * **tag-only bot turns** — OSV strips `<LANG:…>`/`<HANGUP>`/… before it
          fills `output_message`, so a completion consisting solely of a marker
          tag arrives as `""`, identical to genuine silence. That *is* the Veros
          IB 2.0 stall, and from here it looked like the bot said nothing.

        Both are persisted, so one conversations fetch after the run recovers
        them. Strictly best-effort: no token, no client_uuid, a 401, or an
        unfamiliar payload shape all just leave the result as it was. A
        diagnostic must never be able to fail the run that produced it.
        """
        if self.settings is None:  # context-driven run: nothing to fetch with
            return
        osv_url = apply_region(
            self.settings.osv_base_url, scenario.region, what="osv_base_url"
        )
        token = self._osv_enrichment_token(osv_url, scenario)
        if not token:
            logger.debug(
                "skipping OSV enrichment: no AUTOCALLS_OSV_ACCESS_TOKEN. "
                "tool_calls will stay empty and a tag-only bot reply will be "
                "reported as silence."
            )
            return
        if not (transport.interaction_uuid and transport.client_uuid):
            logger.debug(
                "skipping OSV enrichment: interaction_uuid=%r client_uuid=%r",
                transport.interaction_uuid, transport.client_uuid,
            )
            return

        try:
            from opkura.services.osv.client import OSVClient
            from opkura.services.osv.conversations import (
                flatten_tool_calls,
                parse_conversations,
                tag_only_bot_turns,
                tool_names,
            )

            osv = OSVClient(base_url=osv_url, token=token)
            payload = await osv.get_conversations(
                transport.client_uuid, transport.interaction_uuid
            )
            turns = parse_conversations(payload)
            if not turns:
                logger.warning(
                    "OSV conversations returned no recognisable turns for %s "
                    "(payload keys: %s) — leaving the result unenriched",
                    transport.interaction_uuid,
                    sorted(payload)[:12] if isinstance(payload, dict) else type(payload).__name__,
                )
                return

            # Records that we actually looked. Without this an empty
            # `tool_calls` is ambiguous between "the bot called nothing" and "we
            # had no token", and every tool assertion would fail on config.
            result.osv_enriched = True

            recovered = flatten_tool_calls(turns)
            if len(recovered) > len(result.tool_calls):
                result.tool_calls = recovered
                logger.info(
                    "recovered %d tool call(s) from OSV: %s",
                    len(recovered), " → ".join(tool_names(turns)) or "(unnamed)",
                )

            silent = tag_only_bot_turns(turns)
            if silent:
                # The single most useful line in the log when a text run stalls.
                logger.warning(
                    "%d bot turn(s) were TAG-ONLY and reached the caller as "
                    "silence: %s. This is a bot-flow defect, not an opkura "
                    "one — the same reply is dead air on a voice call.",
                    len(silent),
                    "; ".join(
                        f"turn {t.index}: {' '.join(t.tags)}" for t in silent[:5]
                    ),
                )
                result.tag_only_bot_turns = [
                    {"turn_index": t.index, "tags": t.tags, "raw": t.text}
                    for t in silent
                ]
        except Exception as exc:
            logger.warning(
                "OSV enrichment failed for %s (%s) — result kept as-is",
                transport.interaction_uuid, exc,
            )


    async def _run_evaluator(
        self,
        scenario: Scenario,
        result: CallResult,
        logwriter: Optional[CallLogWriter] = None,
        eval_model: Optional[str] = None,
        ctx: Optional[RunContext] = None,
    ) -> EvaluationResult:
        try:
            resolved_model = (
                eval_model or scenario.llm.evaluator_model or self.settings.evaluator_model
            )
            if ctx is not None and ctx.ports.evaluator_llm is not None:
                base_eval_llm = ctx.ports.evaluator_llm
            else:
                base_eval_llm = self._shared_llm(resolved_model, None)
            eval_llm = ObservedLLM(base_eval_llm, role="evaluator")
            evaluator = Evaluator(
                evaluation_prompt=scenario.evaluation_prompt_rendered,
                llm=eval_llm,
                temperature=EVALUATOR_TEMPERATURE,
                metric_spec=scenario.metrics,
            )
            if logwriter is not None:
                evaluator.attach_logwriter(logwriter)
            # Text-transport observations the audio path cannot produce. Passed
            # only when the transport actually recorded them, so a voice run's
            # evaluator prompt is byte-identical to before. Without this the
            # model narrates around the gap: the 1740 probe passed a
            # `flow_depth` criterion on the evidence "modules_visited was not
            # provided, but the flow progressed as expected".
            observed = {}
            if result.modules_visited or result.tool_calls or result.interaction_uuid:
                observed = {
                    "modules_visited": result.modules_visited,
                    "tool_calls": result.tool_calls,
                    "tag_only_bot_turns": result.tag_only_bot_turns,
                }
            return await asyncio.wait_for(
                evaluator.evaluate(result.transcript, **observed),
                timeout=EVALUATOR_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.error("Evaluator timed out after %.0fs", EVALUATOR_TIMEOUT_S)
            return EvaluationResult(
                overall_passed=False,
                overall_reasoning=f"evaluator timed out after {EVALUATOR_TIMEOUT_S}s",
                malformed=True,
            )
        except Exception as exc:
            logger.exception("Evaluator crashed")
            return EvaluationResult(
                overall_passed=False,
                overall_reasoning=f"evaluator crashed: {exc}",
                malformed=True,
            )

    def _api_key_for(self, model: str, provider: str | None) -> str:
        # If both are given, they must agree — otherwise we'd hand a Gemini key
        # to an Anthropic client (or vice versa) and only learn at first 401.
        if provider is not None:
            try:
                detected = detect_provider(model)
            except ValueError:
                detected = None  # unknown prefix; explicit provider is the only signal
            if detected is not None and detected != provider:
                raise ValueError(
                    f"provider/model mismatch: provider={provider!r} but model "
                    f"{model!r} belongs to provider={detected!r}"
                )
            resolved = provider
        else:
            resolved = detect_provider(model)

        if resolved == "gemini":
            key = self.settings.gemini_api_key
        elif resolved == "anthropic":
            key = self.settings.anthropic_api_key
        elif resolved == "openai":
            key = self.settings.openai_api_key
        else:
            raise ValueError(f"no API key slot for provider {resolved!r}")
        if not key:
            raise ValueError(
                f"API key for {resolved!r} is not configured "
                f"(set AUTOCALLS_{resolved.upper()}_API_KEY)"
            )
        return key

    async def run_batch(self, paths: list[str | Path]) -> list[CallResult]:
        """Run multiple flows sequentially."""
        results = []
        for p in paths:
            result = await self.run_flow_file(p)
            results.append(result)
        return results

    async def run_repeated(
        self, flow: Flow, count: int = 0, interval_s: float = 60
    ) -> list[CallResult]:
        """Run a flow repeatedly. count=0 means forever."""
        results = []
        iteration = 0
        while count == 0 or iteration < count:
            iteration += 1
            logger.info("--- Run %d ---", iteration)
            result = await self.run_flow(flow)
            results.append(result)
            if count == 0 or iteration < count:
                logger.info("Waiting %.0fs before next run...", interval_s)
                await asyncio.sleep(interval_s)
        return results

    async def close(self):
        # Raw slots, not the properties: close() must not BUILD a client just
        # to close it (and a context-driven runner has none to build).
        if self._tts is not None:
            await self._tts.close()
        if self._shared_http is not None:
            await self._shared_http.aclose()
            self._shared_http = None
