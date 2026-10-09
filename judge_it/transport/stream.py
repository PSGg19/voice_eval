"""The LEGACY V1 media-stream session (P2.6-frozen, P6-trimmed).

Serves exactly two callers: `judge_it load` and `judge_it watch`, whose scripted
FlowEngine walks steps deterministically. Everything V2 lives in
`judge_it/voice/` as the processor chain (`VoiceSession`); this file was
1,577 lines of monolith and is now the minimal remainder — the barge
machinery, Silero path and WebSocket-TTS fast path moved to (or were
superseded by) the chain, and the typed DTMF errors and μ-law helpers moved
to `transport/dtmf_errors.py` / `transport/audio.py` (re-exported here for
the import surface tests and callers rely on).

Bug fixes only, per the V1 freeze.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

from fastapi import WebSocket

from judge_it.core.models import CallResult, CallStatus, TranscriptEntry
from judge_it.transport.audio import mulaw_to_wav as _mulaw_to_wav  # noqa: F401 — re-export
from judge_it.transport.audio import mulaw_decode as _mulaw_decode  # noqa: F401 — re-export
from judge_it.transport.dtmf import generate_sequence as generate_dtmf
from judge_it.transport.dtmf import validate_dtmf_string
from judge_it.transport.dtmf_errors import (  # noqa: F401 — stable re-exports
    DTMFReconnectTimeoutError,
    DTMFRedirectAbortedError,
    DTMFRedirectFailedError,
)
from judge_it.transport.stt import DeepgramSTT
from judge_it.transport.tts import DeepgramTTS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from judge_it.engine.caller import AICallerEngine
    from judge_it.legacy.flow import FlowEngine
    from judge_it.transport.call import CallManager

logger = logging.getLogger(__name__)

MULAW_SILENCE = b"\xff"  # single byte of mulaw silence
CHUNK_DURATION_S = 0.02  # 20ms per Twilio media chunk
CHUNK_SIZE = 160  # 160 bytes = 20ms at 8kHz mulaw


class MediaStreamSession:
    """One V1 call: Twilio Media Streams WS ↔ STT ↔ FlowEngine ↔ TTS.

    The lifecycle survives WS teardowns for REST-DTMF redirects:
    start_session → attach_ws (first) → [pause → reattach]* → end_session.
    server.py routes on `_in_transition` / `_ws` / `_session_ended`.
    """

    def __init__(
        self,
        engine: Union["FlowEngine", "AICallerEngine"],
        deepgram_api_key: str,
        tts: DeepgramTTS,
        result: CallResult,
        stt_utterance_end_ms: int = 2500,
        stt_vad_events: bool = False,
        streaming_tts: bool = True,
        tts_transport: str = "http",
        call_manager: "Optional[CallManager]" = None,
        session_id: str = "",
        reconnect_ws_url: str = "",
        dtmf_transport: str = "rest",
        dtmf_reconnect_timeout_s: float = 30.0,
        # Accepted-and-ignored legacy knobs, so pre-trim call sites and test
        # fixtures keep constructing. Silero and WS-TTS belong to the chain.
        use_silero_vad: bool = False,
        silero_silence_threshold_s: float = 1.5,
    ):
        self.flow = engine
        self.tts = tts
        self.result = result
        if use_silero_vad:
            logger.warning(
                "use_silero_vad ignored on the legacy V1 session — Silero "
                "turn detection lives in the V2 chain (judge_it/voice)."
            )

        self._stream_sid: Optional[str] = None
        self._ws: Optional[WebSocket] = None
        self._start_time: float = 0.0
        self._speech_started = False
        self._we_are_speaking = False
        self._we_finished_at: Optional[float] = None

        self._bot_audio_buffer = bytearray()
        self._our_audio_buffer = bytearray()

        self._bot_text_finalized = asyncio.Event()
        self._utterance_boundary = asyncio.Event()
        self._stt = DeepgramSTT(
            api_key=deepgram_api_key,
            on_final=self._on_stt_final,
            on_interim=self._on_stt_interim,
            on_speech_started=self._on_stt_speech_started,
            utterance_end_ms=stt_utterance_end_ms,
            vad_events=stt_vad_events,
        )

        # "ws" (the V2 fast path) degrades to HTTP streaming here.
        self._streaming_tts = streaming_tts
        self._tts_transport = "http" if tts_transport == "ws" else tts_transport
        self._send_lock = asyncio.Lock()

        # Lifecycle / DTMF-transition state.
        self._session_started = False
        self._session_ended = False
        self._attach_count = 0
        self._in_transition = False
        self._reconnected = asyncio.Event()
        self._aborted = asyncio.Event()
        self._aborted_event_name = ""
        self._conv_task: Optional[asyncio.Task] = None
        self._call_manager = call_manager
        self.session_id = session_id
        self._reconnect_ws_url = reconnect_ws_url
        # No CallManager wired (test fixtures, degraded config) → the REST
        # redirect is impossible; auto-downgrade to the audio fallback.
        if dtmf_transport == "rest" and call_manager is None:
            dtmf_transport = "audio"
        self._dtmf_transport = dtmf_transport
        self._dtmf_reconnect_timeout_s = dtmf_reconnect_timeout_s

        self._completion_event: Optional[asyncio.Event] = None
        self._completion_loop: Optional[asyncio.AbstractEventLoop] = None

    # -- STT callbacks --

    def _on_stt_final(self, transcript: str):
        if transcript:
            self.flow.accumulate_bot_speech(transcript)
            self._speech_started = True
        else:
            # Empty string = Deepgram UtteranceEnd signal.
            self._utterance_boundary.set()

    def _on_stt_interim(self, transcript: str):
        self._speech_started = True

    def _on_stt_speech_started(self) -> None:
        # Audio-domain VAD: beats STT interim by 50-200ms; feeds the
        # grace-period resumption check in _wait_for_bot_turn.
        self._speech_started = True

    # -- completion plumbing (runner loop ≠ server loop) --

    def attach_completion_event(
        self, event: asyncio.Event, loop: asyncio.AbstractEventLoop
    ) -> None:
        self._completion_event = event
        self._completion_loop = loop

    def _signal_completion(self) -> None:
        if self._completion_event is None or self._completion_loop is None:
            return
        if self._completion_event.is_set():
            return
        try:
            self._completion_loop.call_soon_threadsafe(self._completion_event.set)
        except RuntimeError as exc:
            logger.warning("could not schedule completion set: %s", exc)

    # -- lifecycle --

    def start_session(self) -> None:
        if self._session_started:
            return
        self._session_started = True
        self._start_time = time.time()
        self.result.status = CallStatus.IN_PROGRESS

    async def handle(self, ws: WebSocket):
        """First-attach entry. MUST NOT end the session when attach_ws
        returns mid-DTMF-transition — the next WS reattaches."""
        self.start_session()
        try:
            await self.attach_ws(ws)
        finally:
            if not self._session_ended and not self._in_transition:
                await self.end_session()

    async def attach_ws(self, ws: WebSocket) -> None:
        if not self._session_started:
            self.start_session()
        if self._ws is not None and not self._in_transition:
            raise RuntimeError(
                "attach_ws called while session has an active WS and is "
                "not in transition — refusing to clobber."
            )

        await ws.accept()
        self._ws = ws
        self._attach_count += 1
        is_first_attach = self._attach_count == 1

        if not is_first_attach:
            assert self._conv_task is not None, (
                "reattach without an existing conv_task — programmer error"
            )
            # Flip state consistent BEFORE anything that can fail.
            self._in_transition = False
            self._reconnected.set()
            conv_task = self._conv_task
        else:
            conv_task = None

        await self._stt.start()
        recv_task = asyncio.create_task(self._receive_loop())
        if is_first_attach:
            conv_task = asyncio.create_task(self._conversation_loop())
            self._conv_task = conv_task

        try:
            done, pending = await asyncio.wait(
                [recv_task, conv_task], return_when=asyncio.FIRST_COMPLETED
            )
            # Unblock the runner NOW on a real call end (not a transition
            # close) — FastAPI's WS close handshake can wait on Twilio.
            if (
                self._completion_event is not None
                and not self._completion_event.is_set()
                and not self._in_transition
            ):
                logger.info("Conversation ended — signaling runner")
                self._signal_completion()
            for t in pending:
                # Never cancel the conv loop on a transition close — it is
                # suspended in _send_dtmf awaiting _reconnected.
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
            label = {
                DTMFRedirectFailedError: "DTMF REDIRECT FAILED",
                DTMFReconnectTimeoutError: "JUDGE_IT DTMF TIMEOUT",
                DTMFRedirectAbortedError: "DTMF REDIRECT ABORTED",
            }[type(exc)]
            logger.error("Stream session %s: %s", label, exc)
            self.result.finish(CallStatus.ERROR, f"{label}: {exc}")
        except Exception as exc:
            logger.error("Stream session error: %s", exc)
            self.result.finish(CallStatus.ERROR, str(exc))
        finally:
            try:
                await self._stt.stop()
            except Exception as exc:
                logger.warning("STT stop failed in attach_ws finally: %s", exc)
            self._ws = None
            if self._in_transition:
                logger.info(
                    "attach_ws returning in transition (attach_count=%d)",
                    self._attach_count,
                )
                return
            await self.end_session()

    async def pause(self, reason: str = "") -> None:
        """Flag the session in-transition before a REST-DTMF redirect.
        Twilio tears the WS down itself; attach_ws's finally sees the flag."""
        if self._in_transition:
            raise RuntimeError("Session is already paused")
        self._reconnected = asyncio.Event()
        self._aborted = asyncio.Event()
        self._aborted_event_name = ""
        self._in_transition = True
        logger.info("Session paused: reason=%s", reason)

    def on_call_lifecycle(self, event: str) -> None:
        """Status-webhook hook: abort a mid-transition redirect when the call
        ends underneath it."""
        if not self._in_transition:
            return
        if event in {"completed", "failed", "canceled", "no-answer", "busy"}:
            logger.error(
                "dtmf.redirect.aborted call_sid=%s lifecycle_event=%s",
                self.result.call_sid, event,
            )
            self._aborted_event_name = event
            self._aborted.set()

    async def reattach(self, ws: WebSocket) -> None:
        if not self._in_transition:
            raise RuntimeError(
                "reattach called on a session that is not in transition"
            )
        await self.attach_ws(ws)

    async def end_session(self) -> None:
        if self._session_ended:
            return
        self._session_ended = True
        if self.result.status == CallStatus.IN_PROGRESS:
            self.result.finish(CallStatus.COMPLETED)
        self._signal_completion()

    # -- Twilio receive loop --

    async def _receive_loop(self):
        try:
            async for raw in self._ws.iter_text():
                data = json.loads(raw)
                event = data.get("event")
                if event == "start":
                    meta = data["start"]
                    self._stream_sid = meta["streamSid"]
                    self.result.call_sid = meta.get("callSid", "")
                    logger.info("Stream started: sid=%s call=%s",
                                self._stream_sid, self.result.call_sid)
                elif event == "media":
                    audio = base64.b64decode(data["media"]["payload"])
                    self._bot_audio_buffer.extend(audio)
                    await self._stt.send_audio(audio)
                elif event == "stop":
                    logger.info("Twilio stream stopped")
                    break
        except Exception as exc:
            logger.error("Receive loop error: %s", exc)
            raise

    # -- conversation loop (V1: the scripted step walk) --

    async def _conversation_loop(self):
        try:
            while not self.flow.is_complete:
                step = self.flow.current_flow_step
                timeout_s = (step.timeout_ms / 1000) if step else 10.0
                await self._wait_for_bot_turn(timeout_s)

                response_step = self.flow.end_bot_turn()
                if inspect.iscoroutine(response_step):
                    response_step = await response_step

                # V1 transcript: the step result carries the bot text.
                last_result = (
                    self.flow.step_results[-1] if self.flow.step_results else None
                )
                if last_result:
                    self._add_transcript("bot", last_result.bot_transcript)
                else:
                    bot_text = getattr(self.flow, "last_bot_utterance", "")
                    if bot_text:
                        self._add_transcript("bot", bot_text)

                if response_step:
                    speech = (getattr(response_step, "say", None)
                              or getattr(response_step, "speech", None))
                    dtmf = getattr(response_step, "dtmf", None)
                    if speech:
                        self._add_transcript("caller", speech)
                        await self._speak(speech)
                    if dtmf:
                        self._add_transcript("caller", f"[DTMF: {dtmf}]")
                        await self._send_dtmf(dtmf)

                self._speech_started = False
                self._utterance_boundary.clear()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Conversation loop error: %s", exc)
            raise

    async def _wait_for_bot_turn(self, timeout_s: float):
        """Deepgram UtteranceEnd + smart grace: a mid-turn sentence pause
        must not read as a turn end (the Mini-Miranda pause)."""
        deadline = time.time() + timeout_s
        while not self._speech_started:
            if time.time() > deadline:
                logger.warning("Timeout waiting for bot to speak")
                return
            await asyncio.sleep(0.05)

        if self._we_finished_at is not None and hasattr(
            self.flow, "record_bot_response_latency"
        ):
            latency_ms = (time.monotonic() - self._we_finished_at) * 1000.0
            self.flow.record_bot_response_latency(latency_ms)
            self._we_finished_at = None

        GRACE_PERIOD_S = 1.5
        while True:
            try:
                remaining = max(0.1, deadline - time.time())
                await asyncio.wait_for(
                    self._utterance_boundary.wait(), timeout=remaining
                )
            except asyncio.TimeoutError:
                return
            self._utterance_boundary.clear()
            self._speech_started = False
            grace_deadline = time.time() + GRACE_PERIOD_S
            bot_resumed = False
            while time.time() < grace_deadline:
                if self._speech_started:
                    bot_resumed = True
                    break
                await asyncio.sleep(0.05)
            if not bot_resumed:
                return

    # -- TTS --

    async def _speak(self, text: str):
        self._we_are_speaking = True
        try:
            if self._streaming_tts and self._tts_transport != "blocking":
                await self._speak_streaming(text)
            else:
                await self._speak_blocking(text)
        finally:
            self._we_are_speaking = False
            self._we_finished_at = time.monotonic()

    async def _speak_blocking(self, text: str):
        audio = await self.tts.synthesize(text)
        await self._send_audio(audio)

    async def _speak_streaming(self, text: str):
        """Send Twilio chunks as Deepgram HTTP-stream bytes arrive, with
        deadline pacing (per-chunk sleep drifts audibly over a long line)."""
        async with self._send_lock:
            loop = asyncio.get_running_loop()
            start = loop.time()
            send_idx = 0
            buf = bytearray()
            async for tts_chunk in self.tts.synthesize_stream(text):
                buf.extend(tts_chunk)
                while len(buf) >= CHUNK_SIZE:
                    chunk = bytes(buf[:CHUNK_SIZE])
                    del buf[:CHUNK_SIZE]
                    await self._send_media_chunk(chunk)
                    deadline = start + (send_idx + 1) * CHUNK_DURATION_S
                    remaining = deadline - loop.time()
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                    send_idx += 1
            if buf:
                await self._send_media_chunk(
                    bytes(buf) + MULAW_SILENCE * (CHUNK_SIZE - len(buf))
                )

    async def _send_media_chunk(self, chunk: bytes) -> None:
        self._our_audio_buffer.extend(chunk)
        payload = base64.b64encode(chunk).decode("ascii")
        await self._ws.send_text(json.dumps({
            "event": "media",
            "streamSid": self._stream_sid,
            "media": {"payload": payload},
        }))

    async def _send_audio(self, audio: bytes):
        """Send mulaw audio in paced 20ms chunks, then an end-of-speech mark."""
        if not self._stream_sid or not self._ws:
            return
        async with self._send_lock:
            loop = asyncio.get_running_loop()
            start = loop.time()
            for idx, offset in enumerate(range(0, len(audio), CHUNK_SIZE)):
                chunk = audio[offset:offset + CHUNK_SIZE]
                if len(chunk) < CHUNK_SIZE:
                    chunk += MULAW_SILENCE * (CHUNK_SIZE - len(chunk))
                await self._send_media_chunk(chunk)
                deadline = start + (idx + 1) * CHUNK_DURATION_S
                remaining = deadline - loop.time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
            await self._ws.send_text(json.dumps({
                "event": "mark",
                "streamSid": self._stream_sid,
                "mark": {"name": f"done_{time.time():.0f}"},
            }))

    # -- DTMF --

    async def _send_dtmf(self, digits: str):
        """REST redirect (production: RFC 2833 via <Play digits>) or the
        legacy mulaw-tone fallback (diagnostics only — LiveKit bots decode
        RFC 2833, not audio tones)."""
        validate_dtmf_string(digits)
        self._we_are_speaking = True
        try:
            if self._dtmf_transport == "rest":
                await self._send_dtmf_rest(digits)
            else:
                await self._send_audio(generate_dtmf(digits))
        finally:
            self._we_are_speaking = False
            self._we_finished_at = time.monotonic()

    async def _send_dtmf_rest(self, digits: str) -> None:
        from judge_it.transport.dtmf_redirect import send_dtmf_rest

        await send_dtmf_rest(self, digits)

    # -- helpers --

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
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        paths: dict[str, str] = {}
        if self._bot_audio_buffer:
            bot_path = directory / f"{self.session_id}_bot.wav"
            _mulaw_to_wav(bytes(self._bot_audio_buffer), str(bot_path))
            paths["bot"] = str(bot_path)
        if self._our_audio_buffer:
            caller_path = directory / f"{self.session_id}_caller.wav"
            _mulaw_to_wav(bytes(self._our_audio_buffer), str(caller_path))
            paths["caller"] = str(caller_path)
        return paths
