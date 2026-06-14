"""Twilio Media Stream WebSocket handler.

Receives bot audio from Twilio, pipes through STT, runs flow logic,
generates TTS responses, and sends audio back to Twilio.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import struct
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

from starlette.websockets import WebSocket

from opkura.engine.barge import ScheduledTriggerEvaluator
from .dtmf import generate_sequence as generate_dtmf, validate_dtmf_string
from opkura.legacy.flow import FlowEngine
from opkura.core.models import CallResult, CallStatus, TranscriptEntry
from .stt import DeepgramSTT
from .tts import DeepgramTTS, DeepgramWebSocketTTS

if TYPE_CHECKING:
    from opkura.engine.caller import AICallerEngine

logger = logging.getLogger(__name__)

MULAW_SILENCE = b"\xff"  # single byte of mulaw silence
CHUNK_DURATION_S = 0.02  # 20ms per Twilio media chunk
CHUNK_SIZE = 160  # 160 bytes = 20ms at 8kHz mulaw
SILENCE_THRESHOLD_S = 1.5  # seconds of silence → end of bot turn


# Phase 3 (Task 2) — typed errors raised from _send_dtmf so the runner
# can map them to scenario-result statuses without confusing them with
# generic exceptions. Task 4 adds two siblings (DTMFRedirectFailedError
# for Twilio REST failures, DTMFRedirectAbortedError for mid-transition
# call-end events).
class DTMFReconnectTimeoutError(Exception):
    """Raised when the new Media Stream WS never arrives within
    `Settings.dtmf_reconnect_timeout_s` after Twilio accepted the
    `<Play digits>` redirect TwiML.

    Common causes: misconfigured `public_url` (the `<Connect><Stream>`
    URL points somewhere opkura can't be reached), Twilio outage,
    Twilio's TwiML execution stalling. Runner translates this to
    `OPKURA DTMF TIMEOUT` in the scenario result.
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


class MediaStreamSession:
    """Manages a single call's bidirectional audio stream."""

    def __init__(
        self,
        engine: Union["FlowEngine", "AICallerEngine"],
        deepgram_api_key: str,
        tts: DeepgramTTS,
        result: CallResult,
        *,
        stt_utterance_end_ms: int = 2500,
        stt_vad_events: bool = False,
        use_silero_vad: bool = False,
        silero_silence_threshold_s: float = 1.5,
        streaming_tts: bool = True,
        tts_transport: str = "ws",
        # Phase 3 (Task 2). Optional so existing test fixtures that
        # build a session without a Twilio client still work — when
        # call_manager is None, _send_dtmf falls back to the legacy
        # audio path regardless of dtmf_transport.
        call_manager=None,
        session_id: str = "",
        reconnect_ws_url: str = "",
        dtmf_transport: str = "rest",
        dtmf_reconnect_timeout_s: float = 30.0,
    ):
        self.flow = engine
        self.tts = tts
        self.result = result

        self._stream_sid: Optional[str] = None
        self._ws: Optional[WebSocket] = None
        self._start_time: float = 0

        # Audio timing
        self._last_audio_time: float = 0
        self._speech_started: bool = False
        self._we_are_speaking: bool = False
        # Monotonic timestamp of when *we* finished sending an utterance.
        # Used to compute bot_response_latency_ms (TTS-finish → next bot-turn-start).
        self._we_finished_at: Optional[float] = None

        # Audio recording
        self._bot_audio_buffer: bytearray = bytearray()
        self._our_audio_buffer: bytearray = bytearray()

        # STT
        self._bot_text_finalized: asyncio.Event = asyncio.Event()
        self._utterance_boundary: asyncio.Event = asyncio.Event()
        self._stt = DeepgramSTT(
            api_key=deepgram_api_key,
            on_final=self._on_stt_final,
            on_interim=self._on_stt_interim,
            on_speech_started=self._on_stt_speech_started,
            utterance_end_ms=stt_utterance_end_ms,
            vad_events=stt_vad_events,
        )

        # Barge state (Task 5). Only populated when the engine reports a
        # non-"off" barge_mode; turn-taking calls leave these untouched.
        self._barge_mode: str = getattr(engine, "barge_mode", "off")
        self._trigger_evaluator: Optional[ScheduledTriggerEvaluator] = None
        if self._barge_mode == "scheduled":
            self._trigger_evaluator = ScheduledTriggerEvaluator(engine.scenario.barge_in)
        # Latest cumulative bot partial transcript from interim STT events. Reset
        # at end of each turn so scheduled triggers / LLM decisions don't see
        # last turn's text.
        self._partial_bot_transcript: str = ""
        # When a barge audio emission starts (monotonic clock). Used to compute
        # bot_response_after_ms once the bot's reactive utterance completes.
        self._barge_emitted_at: Optional[float] = None
        # Transcript entry waiting to have bot_response_after_ms filled in.
        # Cleared once filled or once the call ends.
        self._pending_barge_entry: Optional[TranscriptEntry] = None
        # Background fire tasks (scheduled-mode AND llm-mode emits) tracked so
        # the conversation loop drains them between turns. Without this the
        # next turn's caller _speak could start while a barge emit is still
        # writing audio chunks, interleaving on the WebSocket.
        self._barge_tasks: list[asyncio.Task] = []
        # Single LLM decision task running while bot is speaking (llm-mode only).
        self._decision_task: Optional[asyncio.Task] = None
        # Serializes _send_audio so concurrent emitters can't interleave their
        # mulaw chunks on the wire or in _our_audio_buffer. Belt-and-suspenders
        # alongside the per-turn drain.
        self._send_lock: asyncio.Lock = asyncio.Lock()
        # Signaled the instant the conversation ends (engine.hangup=True,
        # max_turns hit, error in either loop). Lets the runner unblock and
        # tell Twilio to hang up immediately, instead of waiting for FastAPI's
        # WS close-handshake to finish — which deadlocks because Twilio keeps
        # streaming media until WE tell it to stop. The loop reference is
        # required because the FastAPI server runs in a separate thread with
        # its own event loop; .set() must be scheduled cross-thread via
        # call_soon_threadsafe or waiters on the runner's loop never wake.
        self._completion_event: Optional[asyncio.Event] = None
        self._completion_loop: Optional[asyncio.AbstractEventLoop] = None

        # Phase 3 lifecycle state (Task 1). Decouples session lifetime
        # from any single WebSocket so a DTMF redirect can tear down
        # and re-open the Media Stream while preserving conversation
        # state (transcript, flow engine position, audio buffers).
        #
        #   _in_transition  — True between pause() and reattach(). When
        #                     True, the recv-loop close path does NOT
        #                     finalize the session — it waits for the
        #                     replacement WS to arrive.
        #   _reconnected    — Fires when reattach() binds a fresh WS.
        #                     _send_dtmf (Task 2) awaits this. Rebuilt
        #                     per pause() so a second DTMF turn in the
        #                     same call gets a fresh Event with no
        #                     leftover set-state from the first turn.
        #   _attach_count   — Increments on every attach_ws() call.
        #                     attach_count==1 is the initial bind;
        #                     attach_count>1 is a reattach and we
        #                     don't restart the conversation loop
        #                     (it's still alive, suspended in _send_dtmf).
        #   _conv_task      — Held across pause/reattach so the second
        #                     attach can re-await the same conv task
        #                     instead of starting a new one. None until
        #                     first attach_ws.
        #   _session_started / _session_ended — idempotency guards for
        #                     start_session / end_session so the
        #                     handle(ws) backward-compat wrapper and
        #                     direct callers can both invoke safely.
        self._in_transition: bool = False
        self._reconnected: asyncio.Event = asyncio.Event()
        self._aborted: asyncio.Event = asyncio.Event()
        self._aborted_event_name: str = ""
        self._attach_count: int = 0
        self._conv_task: Optional[asyncio.Task] = None
        self._session_started: bool = False
        self._session_ended: bool = False

        # Phase 3 (Task 2) — REST-DTMF transport configuration.
        # See opkura/core/config.py for the env-var binding and
        # `Settings.dtmf_transport` field documentation. When
        # call_manager is None OR transport is "audio", _send_dtmf
        # uses the legacy mulaw audio path (which a LiveKit-based
        # bot will never decode — see Phase 3 plan).
        self._call_manager = call_manager
        self._session_id = session_id
        self._reconnect_ws_url = reconnect_ws_url
        self._dtmf_transport = dtmf_transport if call_manager is not None else "audio"
        self._dtmf_reconnect_timeout_s = dtmf_reconnect_timeout_s

        # Streaming TTS toggle. When True (default), _speak streams
        # Deepgram audio chunks to Twilio as they arrive — saves ~1.5s
        # of synth time per turn. False uses the legacy blocking path.
        self._streaming_tts = streaming_tts

        # TTS transport ("ws" | "http" | "blocking"). Routes _speak to
        # the matching backend. "ws" uses a long-lived Deepgram
        # WebSocket TTS connection (DeepgramWebSocketTTS) — opened in
        # handle() after STT, closed in the finally block. "http" and
        # "blocking" both reuse the original DeepgramTTS HTTP client
        # passed in as `tts`.
        #
        # Legacy back-compat: callers that still pass only
        # `streaming_tts=False` (no `tts_transport`) expect the blocking
        # path. Resolve that here so the default `tts_transport="ws"`
        # doesn't accidentally upgrade them. The runner applies the
        # same rule for env-var paths; this catches direct constructor
        # call-sites (e.g., unit tests).
        if tts_transport == "ws" and not streaming_tts:
            tts_transport = "blocking"
        self._tts_transport = tts_transport
        self._tts_ws: Optional[DeepgramWebSocketTTS] = None
        if tts_transport == "ws":
            self._tts_ws = DeepgramWebSocketTTS(
                api_key=deepgram_api_key,
                model=tts.model,
            )

        # Local audio-domain VAD. Replaces Deepgram's UtteranceEnd-based
        # turn-end detection when enabled. Lazy import — torch is heavy
        # and shouldn't load unless explicitly requested.
        self._silero_vad = None
        self._silero_silence_threshold_s = silero_silence_threshold_s
        if use_silero_vad:
            from .vad import SileroBotVAD  # noqa: WPS433  lazy by design
            self._silero_vad = SileroBotVAD(threshold=0.5)
            logger.info(
                "Silero VAD enabled for turn-end detection "
                "(silence_threshold=%.2fs)",
                silero_silence_threshold_s,
            )

    # -- STT callbacks (called from STT's receive task) --

    def _on_stt_final(self, transcript: str):
        if transcript:
            self.flow.accumulate_bot_speech(transcript)
            self._speech_started = True
        else:
            # Empty string = Deepgram UtteranceEnd signal
            self._utterance_boundary.set()

    def _on_stt_interim(self, transcript: str):
        self._speech_started = True
        self._last_audio_time = time.time()
        if not transcript:
            return
        # Latest cumulative partial — used by both scheduled trigger evaluation
        # and the LLM decision loop.
        self._partial_bot_transcript = transcript

        if self._barge_mode == "scheduled" and self._trigger_evaluator is not None:
            word_count = len(transcript.split())
            # Deepgram interims are roughly monotonic-increasing within a turn,
            # but occasionally regress on re-segmentation. Keep the high-water
            # mark so a regression doesn't unfire a trigger threshold.
            self.flow.bot_word_count_in_current_turn = max(
                self.flow.bot_word_count_in_current_turn,
                word_count,
            )
            fire = self._trigger_evaluator.check(
                # Engine's turn_count increments at END of bot turn, so the
                # bot's "Nth turn" while it's speaking corresponds to
                # turn_count + 1.
                current_turn=self.flow.turn_count + 1,
                bot_word_count=self.flow.bot_word_count_in_current_turn,
                already_fired=self.flow.triggers_fired,
            )
            if fire is not None:
                # Claim the trigger synchronously here so a rapid second
                # interim event can't see it as un-fired before the task runs.
                self.flow.triggers_fired.add(fire.trigger_index)
                self._barge_tasks.append(
                    asyncio.create_task(
                        self._fire_scheduled_barge(fire, bot_partial=transcript)
                    )
                )

    def _on_stt_speech_started(self) -> None:
        """Deepgram server-side VAD detected voice activity. Audio-domain
        signal, beats STT interim by ~50-200ms and fires on ANY voice
        activity (breaths, hesitations) — not just recognized words.
        Used by _wait_for_bot_turn's grace period to catch bot resumption
        more reliably than STT alone.
        """
        self._speech_started = True
        self._last_audio_time = time.time()

    # -- Main handler --

    def attach_completion_event(
        self, event: asyncio.Event, loop: asyncio.AbstractEventLoop
    ) -> None:
        """Wire the runner's completion event AND its owning loop. The loop is
        required for cross-thread set(): the FastAPI server runs in its own
        thread/loop, so calling event.set() directly from handle() would not
        wake the runner's await event.wait()."""
        self._completion_event = event
        self._completion_loop = loop

    def _signal_completion(self) -> None:
        """Set the completion event safely across thread boundaries."""
        if self._completion_event is None or self._completion_loop is None:
            return
        if self._completion_event.is_set():
            return
        try:
            self._completion_loop.call_soon_threadsafe(self._completion_event.set)
        except RuntimeError as exc:
            # Loop closed mid-shutdown — best-effort; runner will hit timeout
            # but at least logs are now informative.
            logger.warning("could not schedule completion set on runner loop: %s", exc)

    async def handle(self, ws: WebSocket):
        """Backward-compat wrapper. Equivalent to
        ``start_session() + attach_ws(ws) + end_session()`` and is what
        server.py's WS endpoint calls for first-attach today.

        CRITICAL: when attach_ws returns mid-transition (DTMF redirect
        in flight, waiting for the new WS to come in via reattach()),
        the session is intentionally NOT ended yet — we must NOT call
        end_session() here. Doing so finalises the result, fires the
        completion event, and the runner immediately hangs up the
        call mid-DTMF-playback. The post-Task-5 hangup-bug retest
        revealed this — symptom was "attach_ws returning in
        transition" immediately followed by Twilio hangup.
        """
        self.start_session()
        try:
            await self.attach_ws(ws)
        finally:
            # Only finalise if we're NOT mid-transition. If
            # _in_transition is True, the session is alive and waiting
            # for the next WS to land via reattach(); end_session()
            # there would kill the call before DTMF playback finishes.
            if not self._session_ended and not self._in_transition:
                await self.end_session()

    # ------------------------------------------------------------------
    # Phase 3 lifecycle (Task 1)
    # ------------------------------------------------------------------
    #
    # The session lifetime is no longer bound 1:1 to a Twilio Media
    # Streams WebSocket. A single MediaStreamSession can survive one or
    # more WebSocket tear-downs (Task 2's _send_dtmf triggers them) and
    # reattach to a fresh WS while preserving all conversation state.
    #
    #   start_session()  - one-shot setup (status=IN_PROGRESS,
    #                      start time). Idempotent.
    #   attach_ws(ws)    - bind a WS, run STT + recv loop + conv loop
    #                      (first attach) or just STT + recv loop
    #                      (reattach: conv loop is already alive,
    #                      suspended in _send_dtmf). Returns when the
    #                      WS dies. On a clean (non-transition) close,
    #                      calls end_session() and finalises the result.
    #                      On a transition close, returns without
    #                      finalising — the caller (server.py) will
    #                      route the next WS for this session_id back
    #                      through reattach().
    #   pause(reason)    - Task 2's _send_dtmf calls this just before
    #                      it asks Twilio to redirect the call. Sets
    #                      _in_transition + rebuilds _reconnected so a
    #                      second redirect in the same call gets a
    #                      fresh Event.
    #   reattach(ws)     - assert _in_transition, then attach_ws(ws).
    #                      The attach_ws branch for attach_count>1
    #                      handles the rest.
    #   end_session()    - final cleanup. Idempotent. Stops STT (if
    #                      still running), closes TTS WS, finalises
    #                      result.status, signals completion. Called
    #                      automatically by attach_ws on a clean close.

    def start_session(self) -> None:
        """One-shot session initialisation. Safe to call multiple times
        (no-op after the first call)."""
        if self._session_started:
            return
        self._session_started = True
        self._start_time = time.time()
        self.result.status = CallStatus.IN_PROGRESS

    async def attach_ws(self, ws: WebSocket) -> None:
        """Bind a WebSocket to this session and run the audio + conv
        loops until that WS dies.

        First attach (attach_count==1) starts both the STT receive loop
        AND the conversation loop. Reattach (attach_count>1) starts only
        the STT receive loop — the conversation loop is already running,
        suspended in _send_dtmf's `await _reconnected.wait()`, and is
        woken up here by setting _reconnected.

        On clean close (the WS dies without _in_transition being set),
        end_session() runs and the result is finalised. On a transition
        close (_in_transition is True when the WS dies), this method
        returns without finalising so the next WS for this session_id
        can be routed back through reattach().
        """
        if not self._session_started:
            # Belt-and-suspenders for direct attach_ws callers.
            self.start_session()
        if self._ws is not None and not self._in_transition:
            raise RuntimeError(
                "attach_ws called while session has an active WS and is "
                "not in transition — likely a stale duplicate Media "
                "Stream WebSocket. Refusing to clobber."
            )

        await ws.accept()
        self._ws = ws
        self._attach_count += 1
        is_first_attach = self._attach_count == 1
        is_reattach = not is_first_attach

        if is_reattach:
            # Reattach: clear in-flight VAD state so the stale tail of
            # audio from the previous WS doesn't bleed into turn-end
            # detection for the new one. Then clear _in_transition AND
            # fire _reconnected *before* anything that can fail (STT
            # start, recv-loop task creation) so a partial failure in
            # this method leaves the state machine internally
            # consistent rather than stuck-in-transition.
            if self._silero_vad is not None:
                self._silero_vad.reset()
            assert self._conv_task is not None, (
                "reattach without an existing conv_task — programmer error"
            )
            self._in_transition = False
            self._reconnected.set()
            conv_task = self._conv_task
        else:
            conv_task = None  # filled in below after STT starts

        await self._stt.start()

        recv_task = asyncio.create_task(self._receive_loop())

        if is_first_attach:
            conv_task = asyncio.create_task(self._conversation_loop())
            self._conv_task = conv_task

        try:
            done, pending = await asyncio.wait(
                [recv_task, conv_task], return_when=asyncio.FIRST_COMPLETED
            )
            # Conversation just ended OR the WS died. Unblock the runner
            # NOW so it can tell Twilio to hang up — otherwise FastAPI's
            # WS-close handshake waits for Twilio's close-ack, which
            # never arrives because the call is still active on Twilio's
            # side. Only signal completion when this is a real call end,
            # not a transition close (that one will reattach soon).
            if (
                self._completion_event is not None
                and not self._completion_event.is_set()
                and not self._in_transition
            ):
                logger.info(
                    "Conversation ended — signaling runner "
                    "(handle cleanup continues in background)"
                )
                self._signal_completion()
            for t in pending:
                # Don't cancel the conv loop on a transition close —
                # it's the one that called pause() and is awaiting
                # _reconnected. The next attach_ws call will keep it
                # alive.
                if t is self._conv_task and self._in_transition:
                    continue
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            for t in done:
                if t.exception():
                    raise t.exception()
        except (
            DTMFRedirectFailedError,
            DTMFReconnectTimeoutError,
            DTMFRedirectAbortedError,
        ) as exc:
            # Task 4: typed DTMF errors get distinct, human-readable
            # status labels so the runner / scenario report doesn't
            # confuse them with generic engine errors.
            label_map = {
                DTMFRedirectFailedError: "DTMF REDIRECT FAILED",
                DTMFReconnectTimeoutError: "OPKURA DTMF TIMEOUT",
                DTMFRedirectAbortedError: "DTMF REDIRECT ABORTED",
            }
            label = label_map[type(exc)]
            logger.error("Stream session %s: %s", label, exc)
            self.result.finish(CallStatus.ERROR, f"{label}: {exc}")
        except Exception as exc:
            logger.error("Stream session error: %s", exc)
            self.result.finish(CallStatus.ERROR, str(exc))
        finally:
            # STT is rebuilt per attach. Stop the current one so the
            # next attach (or end_session) doesn't see leftover state.
            try:
                await self._stt.stop()
            except Exception as exc:
                logger.warning("STT stop failed during attach_ws finally: %s", exc)
            self._ws = None
            if self._in_transition:
                # Pause case — leave session alive, conv_task suspended,
                # waiting for the next WS to come in via reattach().
                logger.info(
                    "attach_ws returning in transition (attach_count=%d) — awaiting reattach",
                    self._attach_count,
                )
                return
            # Clean close — finalise.
            await self.end_session()

    async def pause(self, reason: str = "") -> None:
        """Mark the session as in-transition. Called by _send_dtmf
        (Task 2) just before it tells Twilio to redirect the call.

        Note: pause() does NOT actively tear down the WS or stop STT —
        Twilio will close the WS on its own as part of the
        ``call.update(twiml=…)`` round-trip, and attach_ws's finally
        clause stops STT at that point. pause() only flips the state
        flag so attach_ws knows not to finalise.
        """
        if self._in_transition:
            raise RuntimeError("Session is already paused")
        # Rebuild both Events so a second DTMF turn in the same call
        # doesn't see leftover set-state from the first turn.
        self._reconnected = asyncio.Event()
        self._aborted = asyncio.Event()
        self._aborted_event_name = ""
        self._in_transition = True
        logger.info("Session paused: reason=%s", reason)

    def on_call_lifecycle(self, event: str) -> None:
        """Hook called by the Twilio status webhook
        (``/status/{session_id}``) whenever Twilio reports a lifecycle
        event for this call.

        If we're mid-transition AND the event indicates the call ended
        ("completed", "failed", "canceled", "no-answer"), set
        ``_aborted`` so a waiting ``_send_dtmf_rest`` wakes up and
        raises ``DTMFRedirectAbortedError`` instead of hanging until
        the reconnect timeout.

        When not mid-transition this is a no-op — normal end-of-call
        flow handles status events via the WS "stop" event.
        """
        if not self._in_transition:
            return
        if event in {"completed", "failed", "canceled", "no-answer", "busy"}:
            logger.error(
                "dtmf.redirect.aborted call_sid=%s lifecycle_event=%s",
                self.result.call_sid,
                event,
            )
            self._aborted_event_name = event
            self._aborted.set()

    async def reattach(self, ws: WebSocket) -> None:
        """Bind a replacement WebSocket after a pause(). Thin convenience
        wrapper around attach_ws — the real work is the attach_count>1
        branch inside attach_ws."""
        if not self._in_transition:
            raise RuntimeError(
                "reattach called on a session that is not in transition"
            )
        await self.attach_ws(ws)

    async def end_session(self) -> None:
        """Final cleanup. Idempotent — safe to call from both the
        normal close path (inside attach_ws) and the outer handle()
        wrapper's defensive finally."""
        if self._session_ended:
            return
        self._session_ended = True

        if self._tts_ws is not None:
            try:
                await self._tts_ws.close()
            except Exception as exc:
                logger.warning("WS TTS close failed: %s", exc)
        if self.result.status == CallStatus.IN_PROGRESS:
            self.result.finish(CallStatus.COMPLETED)
        # Signal completion in case attach_ws's branch didn't fire
        # (e.g. handle() was bypassed and end_session is called
        # directly).
        if (
            self._completion_event is not None
            and not self._completion_event.is_set()
        ):
            self._signal_completion()

    # -- Twilio receive loop --

    async def _receive_loop(self):
        """Read messages from Twilio's media stream WebSocket."""
        try:
            async for raw in self._ws.iter_text():
                data = json.loads(raw)
                event = data.get("event")

                if event == "connected":
                    logger.info("Twilio stream connected")

                elif event == "start":
                    meta = data["start"]
                    self._stream_sid = meta["streamSid"]
                    self.result.call_sid = meta.get("callSid", "")
                    logger.info(
                        "Stream started: sid=%s call=%s",
                        self._stream_sid,
                        self.result.call_sid,
                    )

                elif event == "media":
                    audio = base64.b64decode(data["media"]["payload"])
                    self._bot_audio_buffer.extend(audio)
                    await self._stt.send_audio(audio)
                    self._last_audio_time = time.time()
                    # Fan out to local VAD (sync, ~5ms per Silero frame).
                    # No-op when use_silero_vad=False (self._silero_vad is None).
                    if self._silero_vad is not None:
                        self._silero_vad.feed_mulaw(audio)

                elif event == "stop":
                    logger.info("Twilio stream stopped")
                    break

        except Exception as exc:
            logger.error("Receive loop error: %s", exc)
            raise

    # -- Conversation loop --

    async def _conversation_loop(self):
        """Drive the scripted conversation flow."""
        try:
            while not self.flow.is_complete:
                step = self.flow.current_flow_step
                timeout_s = (step.timeout_ms / 1000) if step else 10.0

                # Barge-mode setup for this turn. The decision loop runs in
                # parallel with _wait_for_bot_turn so the LLM can be polled
                # while the bot is still speaking.
                if self._barge_mode == "llm":
                    self._decision_task = asyncio.create_task(self._llm_decision_loop())

                try:
                    await self._wait_for_bot_turn(timeout_s)
                finally:
                    if self._decision_task is not None:
                        self._decision_task.cancel()
                        try:
                            await self._decision_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        self._decision_task = None

                # v1 bugfix: in-flight barge emissions must finish before the
                # next caller turn's _speak starts. Otherwise two coroutines
                # write to the same WebSocket concurrently and the chunks
                # interleave on the wire and in _our_audio_buffer, garbling
                # caller.wav. The _send_lock is belt-and-suspenders; the drain
                # is the primary guard.
                await self._drain_barge_tasks()

                # If a barge fired this turn, the bot's reactive utterance
                # just ended — fill in bot_response_after_ms on the pending entry.
                self._fill_bot_response_after_barge_if_pending()

                # Prewarm WS TTS in parallel with the caller LLM call.
                # The ~150-350ms WS handshake hides under Gemini's
                # ~1.3s latency, so by the time end_bot_turn() returns
                # text, the WS is already open and ready for Speak +
                # Flush. Net effect: WS TTFB drops from ~600ms (per-turn
                # cold) to ~250ms (prewarmed) on the critical path.
                #
                # Prewarm only when transport=="ws"; HTTP/blocking
                # paths skip this. prewarm() is idempotent and swallows
                # failures internally, so synthesize_stream just opens
                # fresh if the prewarm didn't land.
                prewarm_task: Optional[asyncio.Task] = None
                if self._tts_transport == "ws" and self._tts_ws is not None:
                    prewarm_task = asyncio.create_task(self._tts_ws.prewarm())

                # Process bot's turn, get our response (engine may be sync or async)
                # Timing: how long does the caller LLM (Gemini) take from
                # "turn-end detected" to "response in hand"? Includes any
                # engine bookkeeping (history append, JSON parse, retry).
                t_llm_start = time.monotonic()
                response_step = self.flow.end_bot_turn()
                if inspect.iscoroutine(response_step):
                    response_step = await response_step
                t_llm_done = time.monotonic()
                logger.info(
                    "Caller LLM (end_bot_turn) took %.0fms",
                    (t_llm_done - t_llm_start) * 1000,
                )

                # Wait for the prewarm to finish (almost always already
                # done by now — LLM ~1.3s, handshake ~250ms — but a
                # slow LLM or transient network blip could flip the
                # order). prewarm() never raises, so this can't crash.
                if prewarm_task is not None:
                    try:
                        await prewarm_task
                    except Exception as exc:
                        logger.warning("prewarm await failed: %s", exc)

                # Record bot transcript. V1 routes through step_results[-1];
                # V2's AICallerEngine has no step_results, so fall back to its
                # last_bot_utterance attribute. Without this fallback, V2 calls
                # would land caller-only transcripts in result.transcript —
                # breaking both transcript.json and the evaluator (Task 3).
                last_result = self.flow.step_results[-1] if self.flow.step_results else None
                if last_result:
                    self._add_transcript("bot", last_result.bot_transcript)
                else:
                    bot_text = getattr(self.flow, "last_bot_utterance", "")
                    if bot_text:
                        self._add_transcript("bot", bot_text)

                if response_step:
                    # Engine-agnostic field access: FlowStep has .say, CallerTurn has .speech
                    speech = getattr(response_step, "say", None) or getattr(response_step, "speech", None)
                    dtmf = getattr(response_step, "dtmf", None)

                    if speech:
                        self._add_transcript("caller", speech)
                        await self._speak(speech)

                    if dtmf:
                        self._add_transcript("caller", f"[DTMF: {dtmf}]")
                        await self._send_dtmf(dtmf)

                # Reset for next turn
                self._speech_started = False
                self._utterance_boundary.clear()
                self._partial_bot_transcript = ""
                if hasattr(self.flow, "reset_bot_turn_counters"):
                    self.flow.reset_bot_turn_counters()

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Conversation loop error: %s", exc)
            raise
        finally:
            # Cancel any in-flight barge fire tasks so the loop teardown isn't
            # waiting on TTS to drain.
            for t in self._barge_tasks:
                if not t.done():
                    t.cancel()

    async def _wait_for_bot_turn_silero(self, start_timeout_s: float):
        """Silero-driven turn detection.

        Flow:
          1. Reset Silero so previous-turn state doesn't leak in.
          2. Poll Silero.has_seen_voice() — wait for bot to actually
             start making sound. Capped by start_timeout_s (default 15s
             from caller config; if bot doesn't speak at all in 15s,
             something is broken upstream).
          3. Poll Silero.time_since_voice_s() — wait for silence
             ≥ silence_threshold_s. **No upper cap.** Silero is the
             sole authority on "bot is done". If the bot keeps
             speaking, we keep listening. Runner's outer max_duration
             (typically 405s for a 25-turn scenario) is the safety net
             for genuinely pathological cases.
        """
        self._silero_vad.reset()

        # Step 1: wait for bot to start (audio-domain, not Deepgram).
        start_deadline = time.time() + start_timeout_s
        while not self._silero_vad.has_seen_voice():
            if time.time() > start_deadline:
                logger.warning(
                    "Silero: bot did not start speaking within %.0fs",
                    start_timeout_s,
                )
                return
            await asyncio.sleep(0.05)

        # Latency tracking — anchor to our last utterance end.
        if self._we_finished_at is not None and hasattr(
            self.flow, "record_bot_response_latency"
        ):
            latency_ms = (time.monotonic() - self._we_finished_at) * 1000.0
            self.flow.record_bot_response_latency(latency_ms)
            self._we_finished_at = None

        # Step 2: wait for confirmed silence. NO upper bound — trust VAD.
        silence_threshold = self._silero_silence_threshold_s
        step2_start = time.time()
        while True:
            if self._silero_vad.time_since_voice_s() >= silence_threshold:
                stats = self._silero_vad.frame_stats()
                step2_dur = time.time() - step2_start
                logger.info(
                    "Silero turn-end: bot_spoke_for=%.1fs silence_dur=%.2fs "
                    "frames(high=%d low=%d silent=%d total=%d)",
                    step2_dur,
                    self._silero_vad.time_since_voice_s(),
                    stats["high"], stats["low"], stats["silent"], stats["total"],
                )
                return
            await asyncio.sleep(0.05)

    async def _wait_for_bot_turn(self, timeout_s: float):
        """Wait for the bot to speak and then go silent.

        Two code paths:

        Silero path (use_silero_vad=True) — purely audio-driven, no
        internal time caps. Silero is the sole authority for turn-end
        detection. The runner's outer max_duration is the safety net.

        Legacy Deepgram path (use_silero_vad=False) — text-based
        UtteranceEnd + smart grace. Uses timeout_s as overall deadline.
        """
        if self._silero_vad is not None:
            await self._wait_for_bot_turn_silero(timeout_s)
            return

        # Legacy Deepgram path keeps its shared-deadline behavior.
        deadline = time.time() + timeout_s

        # Phase 1: wait for speech to start
        while not self._speech_started:
            if time.time() > deadline:
                logger.warning("Timeout waiting for bot to speak")
                return
            await asyncio.sleep(0.05)

        # Bot just started — compute time from our last utterance end (if any).
        if self._we_finished_at is not None and hasattr(
            self.flow, "record_bot_response_latency"
        ):
            latency_ms = (time.monotonic() - self._we_finished_at) * 1000.0
            self.flow.record_bot_response_latency(latency_ms)
            self._we_finished_at = None

        # Legacy Deepgram path — UtteranceEnd + grace.
        # Deepgram fires UtteranceEnd after `utterance_end_ms` of silence, but
        # bots with multi-sentence turns naturally pause that long between
        # sentences (e.g. after "...used for this purpose." in the Mini-Miranda).
        # Acting on the first UtteranceEnd would have us speak while the bot
        # is mid-turn — looks like a barge on the bot's side.
        #
        # Strategy: after each UtteranceEnd, clear _speech_started and wait
        # GRACE_PERIOD_S. If new STT interim fires during the grace window the
        # bot resumed mid-turn — keep waiting for the next UtteranceEnd.
        # Otherwise it's a real turn end.
        GRACE_PERIOD_S = 1.5
        while True:
            try:
                remaining = max(0.1, deadline - time.time())
                await asyncio.wait_for(
                    self._utterance_boundary.wait(), timeout=remaining
                )
            except asyncio.TimeoutError:
                # Hit overall phase-2 deadline — caller's turn anyway.
                return

            self._utterance_boundary.clear()
            self._speech_started = False
            grace_deadline = time.time() + GRACE_PERIOD_S
            bot_resumed = False
            while time.time() < grace_deadline:
                if self._speech_started:
                    bot_resumed = True
                    logger.debug(
                        "Bot resumed mid-turn after UtteranceEnd — keep listening"
                    )
                    break
                await asyncio.sleep(0.05)

            if not bot_resumed:
                # Real silence — bot is done.
                return
            # else: loop, wait for the next UtteranceEnd

    # -- Barge (Task 5) --

    async def _fire_scheduled_barge(self, fire, bot_partial: str):
        """Scheduled-mode: trigger has been claimed in triggers_fired by the
        caller; this task emits the scripted utterance and records the
        transcript marker.
        """
        try:
            await self._emit_barge(
                speech=fire.trigger.say,
                dtmf="",
                bot_partial=bot_partial,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("scheduled barge fire failed: %s", exc)

    async def _llm_decision_loop(self):
        """LLM-mode: poll the caller LLM every decide_every_ms with the latest
        bot partial. If the LLM returns barge=true with speech/dtmf, emit it
        and exit (at most one barge per bot turn).
        """
        cfg = self.flow.scenario.barge_in
        interval_s = max(cfg.decide_every_ms, 1) / 1000.0
        last_partial = ""
        try:
            while not self._utterance_boundary.is_set() and not self.flow.hangup:
                await asyncio.sleep(interval_s)
                current_partial = self._partial_bot_transcript
                if not current_partial.strip():
                    continue
                if current_partial == last_partial:
                    continue
                last_partial = current_partial
                try:
                    turn = await self.flow.maybe_barge(current_partial)
                except Exception as e:
                    logger.warning("barge decision LLM failed: %s", e)
                    continue
                if turn is None:
                    continue
                # v1 bugfix: spawn the emission as a tracked task instead of
                # awaiting it inline. The decision task is cancelled in the
                # conversation loop's finally block when the bot's utterance
                # ends — if the emit were awaited here, that cancel would
                # truncate the audio chunk loop mid-frame ("Wait, slo—"
                # instead of "Wait, slow down"). _conversation_loop drains
                # _barge_tasks before the next caller turn.
                self._barge_tasks.append(
                    asyncio.create_task(
                        self._emit_barge(
                            speech=turn.speech,
                            dtmf=turn.dtmf,
                            bot_partial=current_partial,
                        )
                    )
                )
                return
        except asyncio.CancelledError:
            raise

    async def _emit_barge(self, speech: str, dtmf: str, bot_partial: str):
        """Common emission path for scheduled and LLM-mode barges. Records the
        transcript marker, updates engine history, then emits TTS/DTMF audio.
        The transcript entry's bot_response_after_ms is filled in later when
        the bot's reactive utterance ends.
        """
        if not speech and not dtmf:
            return
        self._barge_emitted_at = time.monotonic()
        text = speech if speech else f"[DTMF: {dtmf}]"
        elapsed_ms = (time.time() - self._start_time) * 1000
        entry = TranscriptEntry(
            speaker="caller",
            text=text,
            timestamp_ms=elapsed_ms,
            dtmf=dtmf or None,
            barge=True,
            bot_partial_at_barge=bot_partial,
            bot_response_after_ms=None,
        )
        self.result.transcript.append(entry)
        logger.info("[+%.1fs] CALLER (barge): %s", elapsed_ms / 1000.0, text)
        self._pending_barge_entry = entry
        # Commit to engine history so the next end_bot_turn invocation sees
        # what the caller said mid-utterance.
        if hasattr(self.flow, "record_barge"):
            self.flow.record_barge(speech=speech, dtmf=dtmf, bot_partial=bot_partial)
        if speech:
            await self._speak(speech)
        if dtmf:
            await self._send_dtmf(dtmf)
        # v1 bugfix: _speak / _send_dtmf set _we_finished_at in their finally
        # blocks. For a normal caller turn that's the right anchor for the
        # next bot_response_latency measurement (caller-end → next bot start).
        # A barge isn't a "caller turn" though — using its end as the anchor
        # would inflate the next turn's latency reading. Clear so the next
        # _wait_for_bot_turn skips the record_bot_response_latency call.
        self._we_finished_at = None

    async def _drain_barge_tasks(self) -> None:
        """Await all in-flight barge audio emissions and prune the task list.
        Called between bot turns so concurrent _send_audio writers can't
        interleave on the WebSocket or in the caller audio buffer.
        """
        pending = [t for t in self._barge_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Prune so the list doesn't grow unboundedly over a long call.
        self._barge_tasks = [t for t in self._barge_tasks if not t.done()]

    def _fill_bot_response_after_barge_if_pending(self):
        if self._pending_barge_entry is None or self._barge_emitted_at is None:
            return
        latency_ms = (time.monotonic() - self._barge_emitted_at) * 1000.0
        self._pending_barge_entry.bot_response_after_ms = latency_ms
        self._pending_barge_entry = None
        self._barge_emitted_at = None

    # -- TTS + audio sending --

    async def _speak(self, text: str):
        """Generate TTS audio and send it through the Twilio stream.

        Three-way router on self._tts_transport:
          "ws"       → DeepgramWebSocketTTS, frames arrive ~250ms TTFB
          "http"     → DeepgramTTS.synthesize_stream, chunked HTTP ~1.1s TTFB
          "blocking" → DeepgramTTS.synthesize, full body before send
        """
        self._we_are_speaking = True
        try:
            if self._tts_transport == "ws":
                await self._speak_ws(text)
            elif self._tts_transport == "http":
                await self._speak_streaming(text)
            else:
                await self._speak_blocking(text)
        finally:
            self._we_are_speaking = False
            self._we_finished_at = time.monotonic()

    async def _speak_blocking(self, text: str):
        """Legacy path — wait for full TTS synth then send. Used when
        streaming_tts=False (manual fallback)."""
        t0 = time.monotonic()
        audio = await self.tts.synthesize(text)
        t1 = time.monotonic()
        audio_duration_s = len(audio) / 8000.0
        await self._send_audio(audio)
        t2 = time.monotonic()
        logger.info(
            "_speak (blocking): tts_synth=%.0fms audio_send=%.0fms total=%.0fms "
            "(audio_duration=%.2fs / %d bytes for %d-char text)",
            (t1 - t0) * 1000,
            (t2 - t1) * 1000,
            (t2 - t0) * 1000,
            audio_duration_s,
            len(audio),
            len(text),
        )

    async def _speak_streaming(self, text: str):
        """Send Twilio chunks as Deepgram TTS bytes arrive.

        We open a streaming HTTP response to Deepgram, accumulate the
        variable-size chunks into a buffer, and drain the buffer in
        160-byte (20ms) Twilio media events with real-time pacing.

        Time-to-first-byte (`ttfb`) is logged — if it's close to the
        blocking synth time (~1.8s), Deepgram is buffering on their
        side and we'd need their WebSocket TTS endpoint instead.
        """
        t0 = time.monotonic()
        t_ttfb: Optional[float] = None
        bytes_sent = 0

        async with self._send_lock:
            loop = asyncio.get_running_loop()
            start = loop.time()
            send_idx = 0
            buf = bytearray()

            async for tts_chunk in self.tts.synthesize_stream(text):
                if t_ttfb is None:
                    t_ttfb = time.monotonic()
                buf.extend(tts_chunk)

                # Drain complete 20ms Twilio chunks as they accumulate
                while len(buf) >= CHUNK_SIZE:
                    chunk = bytes(buf[:CHUNK_SIZE])
                    del buf[:CHUNK_SIZE]
                    self._our_audio_buffer.extend(chunk)
                    payload = base64.b64encode(chunk).decode("ascii")
                    msg = json.dumps(
                        {
                            "event": "media",
                            "streamSid": self._stream_sid,
                            "media": {"payload": payload},
                        }
                    )
                    await self._ws.send_text(msg)
                    bytes_sent += CHUNK_SIZE
                    # Real-time pacing — same deadline math as _send_audio
                    deadline = start + (send_idx + 1) * CHUNK_DURATION_S
                    remaining = deadline - loop.time()
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                    send_idx += 1

            # Flush remainder, padded with silence so Twilio chunk size
            # invariant holds (always 160 bytes).
            if buf:
                chunk = bytes(buf) + MULAW_SILENCE * (CHUNK_SIZE - len(buf))
                self._our_audio_buffer.extend(chunk)
                payload = base64.b64encode(chunk).decode("ascii")
                msg = json.dumps(
                    {
                        "event": "media",
                        "streamSid": self._stream_sid,
                        "media": {"payload": payload},
                    }
                )
                await self._ws.send_text(msg)
                bytes_sent += CHUNK_SIZE

        t_done = time.monotonic()
        ttfb_ms = (t_ttfb - t0) * 1000 if t_ttfb else -1
        logger.info(
            "_speak (streaming): ttfb=%.0fms total=%.0fms "
            "(audio_duration=%.2fs / %d bytes for %d-char text)",
            ttfb_ms,
            (t_done - t0) * 1000,
            bytes_sent / 8000.0,
            bytes_sent,
            len(text),
        )

    async def _speak_ws(self, text: str):
        """Send Twilio chunks as Deepgram WebSocket TTS frames arrive.

        Same Twilio 160-byte repackaging + deadline pacing as
        `_speak_streaming` — only the source generator changes.
        TTFB target ~250ms vs ~1.1s for HTTP streaming.

        Resilience: if the WS path fails (connection closed by
        Deepgram, keepalive miss, network blip), fall back to HTTP
        streaming for this turn so a single WS failure doesn't kill
        the conversation. Subsequent turns will still try WS — if it
        died entirely, every turn will fall back, which is graceful
        degradation rather than a hard failure.
        """
        if self._tts_ws is None:
            raise RuntimeError(
                "_speak_ws called but DeepgramWebSocketTTS was not initialized"
            )
        try:
            await self._speak_ws_inner(text)
        except Exception as exc:
            logger.warning(
                "_speak_ws failed (%s) — falling back to HTTP streaming for this turn",
                exc,
            )
            await self._speak_streaming(text)

    async def _speak_ws_inner(self, text: str):
        t0 = time.monotonic()
        t_ttfb: Optional[float] = None
        bytes_sent = 0

        async with self._send_lock:
            loop = asyncio.get_running_loop()
            start = loop.time()
            send_idx = 0
            buf = bytearray()

            async for tts_chunk in self._tts_ws.synthesize_stream(text):
                if t_ttfb is None:
                    t_ttfb = time.monotonic()
                buf.extend(tts_chunk)

                while len(buf) >= CHUNK_SIZE:
                    chunk = bytes(buf[:CHUNK_SIZE])
                    del buf[:CHUNK_SIZE]
                    self._our_audio_buffer.extend(chunk)
                    payload = base64.b64encode(chunk).decode("ascii")
                    msg = json.dumps(
                        {
                            "event": "media",
                            "streamSid": self._stream_sid,
                            "media": {"payload": payload},
                        }
                    )
                    await self._ws.send_text(msg)
                    bytes_sent += CHUNK_SIZE
                    deadline = start + (send_idx + 1) * CHUNK_DURATION_S
                    remaining = deadline - loop.time()
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                    send_idx += 1

            if buf:
                chunk = bytes(buf) + MULAW_SILENCE * (CHUNK_SIZE - len(buf))
                self._our_audio_buffer.extend(chunk)
                payload = base64.b64encode(chunk).decode("ascii")
                msg = json.dumps(
                    {
                        "event": "media",
                        "streamSid": self._stream_sid,
                        "media": {"payload": payload},
                    }
                )
                await self._ws.send_text(msg)
                bytes_sent += CHUNK_SIZE

        t_done = time.monotonic()
        ttfb_ms = (t_ttfb - t0) * 1000 if t_ttfb else -1
        logger.info(
            "_speak (ws): ttfb=%.0fms total=%.0fms "
            "(audio_duration=%.2fs / %d bytes for %d-char text)",
            ttfb_ms,
            (t_done - t0) * 1000,
            bytes_sent / 8000.0,
            bytes_sent,
            len(text),
        )

    async def _send_dtmf(self, digits: str):
        """Send DTMF to the bot.

        Production transport (Phase 3 Task 2, default `dtmf_transport=
        "rest"`): redirects the live Twilio call to play `<Play digits=>`
        which emits RFC 2833 telephone-event RTP packets on the
        outbound SIP leg — the only DTMF format a LiveKit-based bot
        decodes. Side effect: terminates the current Media Stream WS
        and reconnects via TwiML's trailing `<Connect><Stream>` →
        server.py routes that new WS into `reattach()` (Task 3) →
        `_reconnected` fires here and we return.

        Legacy / debug transport (`dtmf_transport="audio"` or when no
        `CallManager` was wired in): generates mulaw DTMF tones and
        plays them over the Media Stream WS as audio. Will NOT reach
        a LiveKit bot's DTMF handler (LiveKit's SIP only decodes RFC
        2833), so this mode exists only for diagnostic A/B testing.
        """
        # Validate the digit alphabet up front so a bad string fails
        # fast — same guard used by build_dtmf_redirect_twiml so audio
        # and rest paths reject the same set of inputs.
        validate_dtmf_string(digits)

        self._we_are_speaking = True
        try:
            if self._dtmf_transport == "rest":
                await self._send_dtmf_rest(digits)
            else:
                await self._send_dtmf_audio(digits)
        finally:
            self._we_are_speaking = False
            self._we_finished_at = time.monotonic()

    async def _send_dtmf_audio(self, digits: str) -> None:
        """Legacy mulaw-audio DTMF path. Retained as a debug fallback —
        will NOT decode on a LiveKit-based bot."""
        audio = generate_dtmf(digits)
        logger.info(
            "Sending DTMF (audio transport): digits=%d bytes=%d",
            len(digits),
            len(audio),
        )
        await self._send_audio(audio)

    async def _send_dtmf_rest(self, digits: str) -> None:
        """REST-DTMF path: redirect the Twilio call to play digits as
        RFC 2833, pause the session, await the reconnect, resume.

        Sequence:
          1. Pause the session (sets `_in_transition`, gives us a
             fresh `_reconnected` Event).
          2. Call `CallManager.play_dtmf_and_reconnect` which posts
             `<Response><Play digits=…/><Connect><Stream url=…/></Connect>`
             via Twilio's REST `Call.update` API. Twilio tears down
             the current `<Connect><Stream>`, plays DTMF as RFC 2833
             on the SIP leg, then opens a new Media Stream WS back
             to opkura.
          3. server.py's WS endpoint (Task 3) sees the new WS for an
             in-transition session and calls `reattach(new_ws)`,
             which fires `_reconnected` here.
          4. We clear and return. Conv loop continues with full
             session state intact.

        Raises:
          DTMFReconnectTimeoutError — if the new WS never arrives
                                       within `dtmf_reconnect_timeout_s`.
          Any other Twilio REST error propagates as-is (Task 4 wraps
          these in `DTMFRedirectFailedError`).
        """
        if self._call_manager is None:
            raise RuntimeError(
                "_send_dtmf_rest called but no CallManager was wired in. "
                "Either pass call_manager= to MediaStreamSession() or set "
                "Settings.dtmf_transport='audio' for the debug fallback."
            )
        if not self._reconnect_ws_url:
            raise RuntimeError(
                "_send_dtmf_rest called but reconnect_ws_url is empty. "
                "MediaStreamSession needs the wss://…/ws/{session_id} URL "
                "to put in the <Connect><Stream> TwiML."
            )
        call_sid = self.result.call_sid
        if not call_sid:
            raise RuntimeError(
                "_send_dtmf_rest called before Twilio sent the 'start' "
                "event — no call_sid known yet, cannot redirect."
            )

        logger.info(
            "dtmf.redirect.start call_sid=%s digit_count=%d",
            call_sid,
            len(digits),
        )

        # Pause FIRST so attach_ws's finally clause doesn't call
        # end_session() when the WS goes away. pause() rebuilds both
        # _reconnected AND _aborted, so capture references after.
        await self.pause(reason="dtmf_redirect")
        reconnected_evt = self._reconnected
        aborted_evt = self._aborted
        transition_started = time.monotonic()

        try:
            self._call_manager.play_dtmf_and_reconnect(
                call_sid=call_sid,
                digits=digits,
                reconnect_ws_url=self._reconnect_ws_url,
            )
            logger.info("dtmf.redirect.twiml_ok call_sid=%s", call_sid)
        except Exception as exc:
            # Twilio REST failed before the stream was torn down.
            # Roll back the pause so the existing WS keeps working
            # — the caller can decide whether to fail the scenario.
            logger.error(
                "dtmf.redirect.failed call_sid=%s exc=%s",
                call_sid,
                exc,
            )
            self._in_transition = False
            raise DTMFRedirectFailedError(
                call_sid=call_sid,
                cause=exc,
                twilio_status=getattr(exc, "status", None),
                twilio_code=getattr(exc, "code", None),
            ) from exc

        # Race the two outcomes: success (reconnect WS arrives) vs
        # abort (Twilio status webhook fires completed/failed mid-
        # redirect). Whichever fires first wins. If neither fires,
        # the wait_for timeout is the third terminator.
        recon_task = asyncio.create_task(reconnected_evt.wait())
        abort_task = asyncio.create_task(aborted_evt.wait())
        try:
            done, pending = await asyncio.wait(
                [recon_task, abort_task],
                timeout=self._dtmf_reconnect_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (recon_task, abort_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

        elapsed_ms = (time.monotonic() - transition_started) * 1000.0

        if not done:
            logger.warning(
                "dtmf.redirect.timeout call_sid=%s elapsed_ms=%.0f",
                call_sid,
                elapsed_ms,
            )
            raise DTMFReconnectTimeoutError(
                call_sid=call_sid,
                digit_count=len(digits),
                elapsed_ms=elapsed_ms,
                last_state=f"in_transition={self._in_transition}, "
                f"attach_count={self._attach_count}",
            )

        if aborted_evt.is_set():
            # Call ended mid-transition. _in_transition stays True so
            # attach_ws's clean-up path (when the abandoned WS finally
            # closes) doesn't try to finalise — we raise here and let
            # the runner do the result-status work.
            raise DTMFRedirectAbortedError(
                call_sid=call_sid,
                lifecycle_event=self._aborted_event_name or "unknown",
            )

        logger.info(
            "dtmf.redirect.reconnected call_sid=%s total_ms=%.0f attach_count=%d",
            call_sid,
            elapsed_ms,
            self._attach_count,
        )

    async def _send_audio(self, audio: bytes):
        """Send mulaw audio to Twilio in 20ms chunks."""
        if not self._stream_sid or not self._ws:
            return

        async with self._send_lock:
            # Deadline-based pacing instead of `sleep(20ms)` between chunks.
            # asyncio.sleep granularity is ~10-15ms on most platforms, so a
            # per-chunk sleep drifts 5-10ms each iteration; over a 5-second
            # utterance (~250 chunks) that's >1s of cumulative gap, audible
            # as crackle / pop / dropped phonemes. Anchoring every chunk to
            # `start + idx*chunk_duration` keeps the stream aligned to
            # wall-clock and lets a fast chunk's "spare time" absorb a slow
            # chunk's delay instead of compounding.
            loop = asyncio.get_running_loop()
            start = loop.time()
            for idx, offset in enumerate(range(0, len(audio), CHUNK_SIZE)):
                chunk = audio[offset : offset + CHUNK_SIZE]
                # Pad last chunk with silence if needed
                if len(chunk) < CHUNK_SIZE:
                    chunk += MULAW_SILENCE * (CHUNK_SIZE - len(chunk))

                self._our_audio_buffer.extend(chunk)
                payload = base64.b64encode(chunk).decode("ascii")
                msg = json.dumps(
                    {
                        "event": "media",
                        "streamSid": self._stream_sid,
                        "media": {"payload": payload},
                    }
                )
                await self._ws.send_text(msg)
                deadline = start + (idx + 1) * CHUNK_DURATION_S
                remaining = deadline - loop.time()
                if remaining > 0:
                    await asyncio.sleep(remaining)

            # Mark end of our speech
            mark = json.dumps(
                {
                    "event": "mark",
                    "streamSid": self._stream_sid,
                    "mark": {"name": f"done_{time.time():.0f}"},
                }
            )
            await self._ws.send_text(mark)
            logger.debug("Finished sending TTS audio")

    # -- Helpers --

    def _add_transcript(self, speaker: str, text: str):
        if text:
            elapsed = (time.time() - self._start_time) * 1000
            self.result.transcript.append(
                TranscriptEntry(speaker=speaker, text=text, timestamp_ms=elapsed)
            )
            logger.info("[+%.1fs] %s: %s", elapsed / 1000.0, speaker.upper(), text)

    @property
    def elapsed_ms(self) -> float:
        return (time.time() - self._start_time) * 1000

    def save_recording(self, directory: str | Path) -> dict[str, str]:
        """Save bot and caller audio as WAV files. Returns paths."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)

        paths = {}
        session_id = self.result.id

        if self._bot_audio_buffer:
            bot_path = directory / f"{session_id}_bot.wav"
            _mulaw_to_wav(bytes(self._bot_audio_buffer), str(bot_path))
            paths["bot"] = str(bot_path)
            logger.info("Saved bot audio: %s (%d bytes)", bot_path, len(self._bot_audio_buffer))

        if self._our_audio_buffer:
            caller_path = directory / f"{session_id}_caller.wav"
            _mulaw_to_wav(bytes(self._our_audio_buffer), str(caller_path))
            paths["caller"] = str(caller_path)

        return paths


def _mulaw_decode(byte: int) -> int:
    """Decode a single mu-law byte to signed 16-bit PCM."""
    byte = ~byte & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = ((mantissa << 3) + 0x84) << exponent
    sample -= 0x84
    return -sample if sign else sample


def _mulaw_to_wav(mulaw_data: bytes, path: str):
    """Convert raw mulaw 8kHz audio to a PCM 16-bit WAV file."""
    pcm_data = bytearray()
    for b in mulaw_data:
        sample = _mulaw_decode(b)
        pcm_data.extend(struct.pack("<h", sample))

    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(8000)
        wf.writeframes(bytes(pcm_data))
