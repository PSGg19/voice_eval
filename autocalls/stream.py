"""Twilio Media Stream WebSocket handler.

Receives bot audio from Twilio, pipes through STT, runs flow logic,
generates TTS responses, and sends audio back to Twilio.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
import wave
from pathlib import Path
from typing import Optional

from starlette.websockets import WebSocket

from .dtmf import generate_sequence as generate_dtmf
from .flow import FlowEngine
from .models import CallResult, CallStatus, TranscriptEntry
from .stt import DeepgramSTT
from .tts import DeepgramTTS

logger = logging.getLogger(__name__)

MULAW_SILENCE = b"\xff"  # single byte of mulaw silence
CHUNK_DURATION_S = 0.02  # 20ms per Twilio media chunk
CHUNK_SIZE = 160  # 160 bytes = 20ms at 8kHz mulaw
SILENCE_THRESHOLD_S = 1.5  # seconds of silence → end of bot turn


class MediaStreamSession:
    """Manages a single call's bidirectional audio stream."""

    def __init__(
        self,
        flow_engine: FlowEngine,
        deepgram_api_key: str,
        tts: DeepgramTTS,
        result: CallResult,
    ):
        self.flow = flow_engine
        self.tts = tts
        self.result = result

        self._stream_sid: Optional[str] = None
        self._ws: Optional[WebSocket] = None
        self._start_time: float = 0

        # Audio timing
        self._last_audio_time: float = 0
        self._speech_started: bool = False
        self._we_are_speaking: bool = False

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

    # -- Main handler --

    async def handle(self, ws: WebSocket):
        self._ws = ws
        await ws.accept()
        self._start_time = time.time()
        self.result.status = CallStatus.IN_PROGRESS

        await self._stt.start()

        recv_task = asyncio.create_task(self._receive_loop())
        conv_task = asyncio.create_task(self._conversation_loop())

        try:
            done, pending = await asyncio.wait(
                [recv_task, conv_task], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            # Propagate exceptions from completed tasks
            for t in done:
                if t.exception():
                    raise t.exception()
        except Exception as exc:
            logger.error("Stream session error: %s", exc)
            self.result.finish(CallStatus.ERROR, str(exc))
        finally:
            await self._stt.stop()
            if self.result.status == CallStatus.IN_PROGRESS:
                self.result.finish(CallStatus.COMPLETED)

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

                # Wait for bot to speak and finish
                await self._wait_for_bot_turn(timeout_s)

                # Process bot's turn, get our response step
                response_step = self.flow.end_bot_turn()

                # Record bot transcript
                last_result = self.flow.step_results[-1] if self.flow.step_results else None
                if last_result:
                    self._add_transcript("bot", last_result.bot_transcript)

                if response_step:
                    # Speak if the step has text
                    if response_step.say:
                        self._add_transcript("caller", response_step.say)
                        await self._speak(response_step.say)

                    # Send DTMF tones if the step has digits
                    if response_step.dtmf:
                        self._add_transcript("caller", f"[DTMF: {response_step.dtmf}]")
                        await self._send_dtmf(response_step.dtmf)

                # Reset for next turn
                self._speech_started = False
                self._utterance_boundary.clear()

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Conversation loop error: %s", exc)
            raise

    async def _wait_for_bot_turn(self, timeout_s: float):
        """Wait for the bot to speak and then go silent."""
        deadline = time.time() + timeout_s

        # Phase 1: wait for speech to start
        while not self._speech_started:
            if time.time() > deadline:
                logger.warning("Timeout waiting for bot to speak")
                return
            await asyncio.sleep(0.05)

        # Phase 2: wait for utterance end (Deepgram signal) or silence
        try:
            remaining = max(0.1, deadline - time.time())
            await asyncio.wait_for(self._utterance_boundary.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass

        # Extra grace period for any trailing audio
        await asyncio.sleep(0.3)

    # -- TTS + audio sending --

    async def _speak(self, text: str):
        """Generate TTS audio and send it through the Twilio stream."""
        self._we_are_speaking = True
        try:
            audio = await self.tts.synthesize(text)
            await self._send_audio(audio)
        finally:
            self._we_are_speaking = False

    async def _send_dtmf(self, digits: str):
        """Generate DTMF tones and send them through the Twilio stream."""
        self._we_are_speaking = True
        try:
            audio = generate_dtmf(digits)
            logger.info("Sending DTMF: %s (%d bytes)", digits, len(audio))
            await self._send_audio(audio)
        finally:
            self._we_are_speaking = False

    async def _send_audio(self, audio: bytes):
        """Send mulaw audio to Twilio in 20ms chunks."""
        if not self._stream_sid or not self._ws:
            return

        for i in range(0, len(audio), CHUNK_SIZE):
            chunk = audio[i : i + CHUNK_SIZE]
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
            # Pace at real-time to avoid buffer overflow
            await asyncio.sleep(CHUNK_DURATION_S)

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
