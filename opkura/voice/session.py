"""P6 — VoiceSession: the chain-backed replacement for MediaStreamSession.

The server-facing surface is IDENTICAL to the monolith's (server.py routes on
`_in_transition` / `_attach_count` / `_ws` / `_session_ended`, calls
handle/reattach, wires attach_completion_event) — the internals are the
processor chain:

    wire → [serializer.decode] → STT → (VAD) → turn-detect → barge →
        engine bridge → TTS → audio-out → wire
                                        ↘ tail sink (DTMF, hangup, feedback)

Ported with semantics intact, not rewritten: the DTMF REST-redirect state
machine (pause → Twilio Call.update → reattach race against abort/timeout,
raising the SAME typed errors with the SAME labels), the per-attach STT
rebuild, the recording buffers, and the completion-event cross-thread
signalling. Ported with a DELIBERATE simplification: the transcript is the
engine's (D2-unified — voice and text now share one transcript authority);
barge utterances appear as `barge=True` caller entries, without the
monolith's `bot_partial_at_barge` / `bot_response_after_ms` diagnostics.

The whole session lives on the SERVER's loop — the runner only dials, waits
on the completion event, and reads the result afterwards, so the two-loop
bridge the wrapped monolith needed does not exist here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from fastapi import WebSocket

from opkura.core.models import CallResult, CallStatus, TranscriptEntry
from opkura.engine.caller import AICallerEngine
from opkura.engine.conversation import ConversationEngine
from opkura.frames import CallerDTMF, CallerHangup, CallerSpeech
from opkura.transport.dtmf import generate_sequence as generate_dtmf
from opkura.transport.dtmf import validate_dtmf_string
from opkura.transport.audio import mulaw_to_wav as _mulaw_to_wav
from opkura.transport.dtmf_errors import (
    DTMFReconnectTimeoutError,
    DTMFRedirectAbortedError,
    DTMFRedirectFailedError,
)
from opkura.voice.audio_out import AudioOut
from opkura.voice.barge_processor import BargeProcessor
from opkura.voice.engine_bridge import EngineBridge
from opkura.voice.frames import CallStarted, InboundAudio, SpeakEnded
from opkura.voice.processor import Chain
from opkura.voice.serializers import TwilioSerializer
from opkura.voice.stt_processor import STTProcessor
from opkura.voice.turn_detect import TurnDetector
from opkura.voice.tts_processor import TTSProcessor
from opkura.voice.vad_processor import VADProcessor

logger = logging.getLogger(__name__)

# Typed DTMF error → result-status label, verbatim from the monolith so the
# scenario report keeps its vocabulary (the P6.3/P6 acceptance).
DTMF_ERROR_LABELS = {
    DTMFRedirectFailedError: "DTMF REDIRECT FAILED",
    DTMFReconnectTimeoutError: "OPKURA DTMF TIMEOUT",
    DTMFRedirectAbortedError: "DTMF REDIRECT ABORTED",
}


class VoiceSession:
    """One voice call over the processor chain.

    Factories keep config out: `stt_factory(**callbacks)` builds the STT
    client (adapting Deepgram's empty-final-means-UtteranceEnd convention to
    the on_utterance_end callback), `tts_factory()` the synthesis client,
    `vad_factory` (optional) the Silero VAD — Silero mode suppresses the
    STT boundary and sets grace to zero, keeping the VAD the sole turn-end
    authority.
    """

    def __init__(
        self,
        *,
        conversation: ConversationEngine,
        caller: AICallerEngine,
        result: CallResult,
        stt_factory: Callable,
        tts_factory: Callable,
        vad_factory: Optional[Callable] = None,
        silero_silence_threshold_s: float = 1.2,
        turn_timeout_s: float = 15.0,
        call_manager=None,
        session_id: str = "",
        reconnect_ws_url: str = "",
        dtmf_transport: str = "rest",
        dtmf_reconnect_timeout_s: float = 30.0,
    ):
        self.conversation = conversation
        self.result = result
        self.session_id = session_id
        self._call_manager = call_manager
        self._reconnect_ws_url = reconnect_ws_url
        self._dtmf_transport = dtmf_transport
        self._dtmf_reconnect_timeout_s = dtmf_reconnect_timeout_s

        # D2 completed for voice: the engine's transcript IS the transcript.
        self.result.transcript = conversation.transcript

        # ---- the chain -----------------------------------------------------
        self.serializer = TwilioSerializer()
        self._stt = STTProcessor(
            stt_factory, emit_utterance_boundary=vad_factory is None
        )
        # Chain order matters: the VAD sits BEFORE the STT link because the
        # STT link CONSUMES audio — nothing past it may depend on raw media
        # (audio flooding the downstream inboxes is how the 2026-09-01 smoke
        # lost turn detection).
        links: list = []
        if vad_factory is not None:
            links.append(VADProcessor(
                vad_factory, silence_threshold_s=silero_silence_threshold_s
            ))
            detector = TurnDetector(turn_timeout_s=turn_timeout_s, grace_s=0.0)
        else:
            detector = TurnDetector(turn_timeout_s=turn_timeout_s)
        links.append(self._stt)
        links.append(detector)
        links.append(BargeProcessor(caller))
        self._bridge = EngineBridge(conversation)
        links.append(self._bridge)
        links.append(TTSProcessor(tts_factory))
        self._audio_out = AudioOut(self._wire_send, self.serializer)
        links.append(self._audio_out)
        self.chain = Chain(links)
        self._tail: list = []
        self._audio_out.to_sink(self._tail)

        # ---- wire / lifecycle state (the monolith's exact flags) ------------
        self._ws: Optional[WebSocket] = None
        self._attach_count = 0
        self._session_started = False
        self._session_ended = False
        self._chain_started = False
        self._in_transition = False
        self._reconnected = asyncio.Event()
        self._aborted = asyncio.Event()
        self._aborted_event_name = ""
        self._start_time: float = 0.0
        self._completion_event: Optional[asyncio.Event] = None
        self._completion_loop: Optional[asyncio.AbstractEventLoop] = None

        # recording
        self._bot_audio_buffer = bytearray()
        self._our_audio_buffer = bytearray()

        self._dtmf_task: Optional[asyncio.Task] = None
        self._tail_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------- wire out

    async def _wire_send(self, msg: str) -> None:
        """AudioOut's send: tolerant of the WS being gone (DTMF transition)."""
        ws = self._ws
        if ws is None:
            return
        import base64 as _b64
        import json as _json

        try:
            parsed = _json.loads(msg)
            if parsed.get("event") == "media":
                self._our_audio_buffer.extend(
                    _b64.b64decode(parsed["media"]["payload"])
                )
            await ws.send_text(msg)
        except Exception as exc:
            logger.debug("wire send dropped (%s)", exc)

    # ---------------------------------------------------- completion plumbing

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
            logger.warning("could not signal completion: %s", exc)

    # -------------------------------------------------------------- lifecycle

    def start_session(self) -> None:
        if self._session_started:
            return
        self._session_started = True
        self._start_time = time.time()
        self.result.status = CallStatus.IN_PROGRESS

    async def handle(self, ws: WebSocket) -> None:
        """server.py's first-attach entry — monolith contract: do NOT end the
        session when attach_ws returns mid-transition."""
        self.start_session()
        try:
            await self.attach_ws(ws)
        finally:
            if not self._session_ended and not self._in_transition:
                await self.end_session()

    async def reattach(self, ws: WebSocket) -> None:
        if not self._in_transition:
            raise RuntimeError(
                "reattach called on a session that is not in transition"
            )
        await self.attach_ws(ws)

    async def pause(self, reason: str = "") -> None:
        if self._in_transition:
            raise RuntimeError("Session is already paused")
        self._reconnected = asyncio.Event()
        self._aborted = asyncio.Event()
        self._aborted_event_name = ""
        self._in_transition = True
        logger.info("Session paused: reason=%s", reason)

    def on_call_lifecycle(self, event: str) -> None:
        if not self._in_transition:
            return
        if event in {"completed", "failed", "canceled", "no-answer", "busy"}:
            logger.error(
                "dtmf.redirect.aborted call_sid=%s lifecycle_event=%s",
                self.result.call_sid, event,
            )
            self._aborted_event_name = event
            self._aborted.set()

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
        is_reattach = self._attach_count > 1

        if is_reattach:
            # Same ordering discipline as the monolith: flip the state
            # machine consistent BEFORE anything that can fail.
            self._in_transition = False
            self._reconnected.set()
            await self._stt.restart()

        if not self._chain_started:
            await self.chain.start()
            self._chain_started = True
            self._tail_task = asyncio.create_task(self._tail_loop())

        try:
            await self._receive_loop(ws)
        except Exception as exc:
            logger.error("Voice session error: %s", exc)
            self.result.finish(CallStatus.ERROR, str(exc))
        finally:
            self._ws = None
            if self._in_transition:
                logger.info(
                    "attach_ws returning in transition (attach_count=%d) — "
                    "awaiting reattach", self._attach_count,
                )
                return
            self._signal_completion()
            await self.end_session()

    async def _receive_loop(self, ws: WebSocket) -> None:
        async for raw in ws.iter_text():
            frame = self.serializer.decode(raw)
            if frame is None:
                continue
            if isinstance(frame, CallStarted):
                self.result.call_sid = frame.call_sid or self.result.call_sid
                logger.info("Stream started: sid=%s call=%s",
                            frame.stream_sid, frame.call_sid)
                continue
            if isinstance(frame, InboundAudio):
                self._bot_audio_buffer.extend(frame.payload)
                await self.chain.feed(frame)
                continue
            # CallEnded (Twilio 'stop'): the WS is over. NOT fed into the
            # chain — a DTMF transition tears this WS down while the
            # conversation lives on; end_session() ends the chain instead.
            logger.info("Twilio stream stopped")
            return

    async def end_session(self) -> None:
        if self._session_ended:
            return
        self._session_ended = True
        if self._chain_started:
            try:
                await self.chain.stop(reason="session end", timeout_s=10)
            except Exception as exc:
                logger.warning("chain stop failed: %s", exc)
        if self._tail_task is not None:
            self._tail_task.cancel()
            try:
                await self._tail_task
            except (asyncio.CancelledError, Exception):
                pass
        outcome = self._bridge.outcome
        if outcome is not None:
            logger.info(
                "voice conversation settled: termination=%s caller_turns=%d",
                outcome.termination, outcome.caller_turns,
            )
            # D3: voice measures TRANSPORT latency; feed it to the logwriter
            # through the caller engine (the metadata.json percentiles).
            caller = self.conversation.caller
            if hasattr(caller, "record_bot_response_latency"):
                for ms in outcome.transport_latencies_ms:
                    caller.record_bot_response_latency(ms)
        if self.result.status == CallStatus.IN_PROGRESS:
            self.result.finish(CallStatus.COMPLETED)
        self._signal_completion()

    # ----------------------------------------------------------- tail frames

    async def _tail_loop(self) -> None:
        """Act on frames that reach the end of the chain."""
        seen = 0
        while True:
            await asyncio.sleep(0.02)
            while seen < len(self._tail):
                frame = self._tail[seen]
                seen += 1
                await self._on_tail_frame(frame)

    async def _on_tail_frame(self, frame) -> None:
        if isinstance(frame, SpeakEnded):
            # Feedback edge: turn detection (upstream) anchors D3's transport
            # latency on this; the detector consumes it, ending the loop.
            await self.chain.feed(frame)
            return
        if isinstance(frame, CallerSpeech) and frame.barge:
            elapsed = (time.time() - self._start_time) * 1000
            self.result.transcript.append(TranscriptEntry(
                speaker="caller", text=frame.text,
                timestamp_ms=elapsed, barge=True,
            ))
            return
        if isinstance(frame, CallerDTMF):
            if self._dtmf_task is None or self._dtmf_task.done():
                self._dtmf_task = asyncio.create_task(
                    self._dtmf_guard(frame.digits)
                )
            return
        if isinstance(frame, CallerHangup):
            # The persona is done; the runner hangs the call up on this
            # signal. Audio for the goodbye already went out — queue order.
            logger.info("caller hangup (%s) — signaling runner", frame.reason)
            self._signal_completion()
            return

    async def _dtmf_guard(self, digits: str) -> None:
        """The DTMF task's error boundary. A redirect failure has nobody to
        raise INTO — after a timeout no WS ever reattaches — so the guard
        fails the run directly with the monolith's exact labels and wakes
        the runner."""
        try:
            await self._send_dtmf(digits)
        except (DTMFRedirectFailedError, DTMFReconnectTimeoutError,
                DTMFRedirectAbortedError) as exc:
            label = DTMF_ERROR_LABELS[type(exc)]
            logger.error("Voice session %s: %s", label, exc)
            self.result.finish(CallStatus.ERROR, f"{label}: {exc}")
            self._signal_completion()
        except Exception as exc:
            logger.exception("DTMF send failed")
            self.result.finish(CallStatus.ERROR, f"DTMF send failed: {exc}")
            self._signal_completion()

    # ----------------------------------------------------------------- DTMF

    async def _send_dtmf(self, digits: str) -> None:
        validate_dtmf_string(digits)
        if self._dtmf_transport == "rest":
            await self._send_dtmf_rest(digits)
        else:
            # Debug fallback: mulaw tones over the media stream. Will not
            # decode on a LiveKit bot (RFC 2833 only) — same caveat as ever.
            from opkura.voice.frames import OutboundAudio

            await self.chain.tail.inbox.put(
                OutboundAudio(payload=generate_dtmf(digits))
            )

    async def _send_dtmf_rest(self, digits: str) -> None:
        from opkura.transport.dtmf_redirect import send_dtmf_rest

        await send_dtmf_rest(self, digits)

    # ------------------------------------------------------------- recording

    def save_recording(self, directory: str | Path) -> dict[str, str]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        paths: dict[str, str] = {}
        if self._bot_audio_buffer:
            p = directory / f"{self.session_id}_bot.wav"
            _mulaw_to_wav(bytes(self._bot_audio_buffer), str(p))
            paths["bot"] = str(p)
            logger.info("Saved bot audio: %s (%d bytes)", p,
                        len(self._bot_audio_buffer))
        if self._our_audio_buffer:
            p = directory / f"{self.session_id}_caller.wav"
            _mulaw_to_wav(bytes(self._our_audio_buffer), str(p))
            paths["caller"] = str(p)
            logger.info("Saved caller audio: %s (%d bytes)", p,
                        len(self._our_audio_buffer))
        return paths
