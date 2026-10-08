"""P6.1 — STTProcessor and TurnDetector, isolated frame-in/frame-out.

The turn detector's tests pin the monolith's exact semantics: the grace
window after UtteranceEnd, resumption aborting it (the Mini-Miranda pause),
the dead-air turn timeout, and D3's transport-latency anchor.
"""
from __future__ import annotations

import asyncio

import pytest

from vjudge.frames import BotFinalSpeech, BotPartialSpeech, BotTurnEnded
from vjudge.voice.frames import (
    CallEnded,
    InboundAudio,
    SpeakEnded,
    SpeechStarted,
    UtteranceBoundary,
)
from vjudge.voice.stt_processor import STTProcessor
from vjudge.voice.turn_detect import TurnDetector

from tests.unit.voice.test_processor import run_processor


# ---------------------------------------------------------------------- STT

class FakeSTT:
    def __init__(self, *, on_final, on_interim, on_speech_started,
                 on_utterance_end):
        self.on_final = on_final
        self.on_interim = on_interim
        self.on_speech_started = on_speech_started
        self.on_utterance_end = on_utterance_end
        self.audio: list[bytes] = []
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send_audio(self, payload: bytes):
        self.audio.append(payload)
        # Deepgram-ish: recognise the magic chunk and fire callbacks.
        if payload == b"SPEAK":
            self.on_speech_started()
            self.on_interim("hel")
        if payload == b"DONE":
            self.on_final("hello there")
            self.on_utterance_end()


@pytest.mark.asyncio
async def test_stt_processor_translates_callbacks_into_frames():
    holder = {}

    def factory(**callbacks):
        holder["stt"] = FakeSTT(**callbacks)
        return holder["stt"]

    out = await run_processor(
        STTProcessor(factory),
        [InboundAudio(b"SPEAK"), InboundAudio(b"quiet"), InboundAudio(b"DONE")],
    )

    stt = holder["stt"]
    assert stt.started and stt.stopped              # lifecycle tied to chain
    assert stt.audio == [b"SPEAK", b"quiet", b"DONE"]
    kinds = [type(f).__name__ for f in out]
    # audio is CONSUMED at this link (downstream must never see raw media)
    assert kinds.count("InboundAudio") == 0
    assert any(isinstance(f, SpeechStarted) for f in out)
    assert any(isinstance(f, BotPartialSpeech) and f.text == "hel" for f in out)
    assert any(isinstance(f, BotFinalSpeech) and f.text == "hello there" for f in out)
    assert any(isinstance(f, UtteranceBoundary) for f in out)


@pytest.mark.asyncio
async def test_stt_send_failure_drops_nothing_downstream():
    class Broken(FakeSTT):
        async def send_audio(self, payload):
            raise ConnectionError("deepgram blinked")

    def factory(**callbacks):
        return Broken(**callbacks)

    out = await run_processor(STTProcessor(factory), [InboundAudio(b"x")])
    # audio consumed; the link survived and the sentinel still walked through
    assert not any(isinstance(f, InboundAudio) for f in out)


# ------------------------------------------------------------ turn detection

async def run_detector(frames_with_delays, *, turn_timeout_s=1.0, grace_s=0.15):
    """Feed (frame, post-delay) pairs into a live detector; collect output."""
    det = TurnDetector(turn_timeout_s=turn_timeout_s, grace_s=grace_s)
    out: list = []
    det.to_sink(out)
    await det.start()
    det._run_task = asyncio.create_task(det.run())
    for frame, delay in frames_with_delays:
        if frame is not None:
            await det.inbox.put(frame)
        await asyncio.sleep(delay)
    await det.inbox.put(CallEnded())
    await asyncio.wait_for(det._run_task, 5)
    await det.reap()
    return out


def turn_ends(out) -> list[BotTurnEnded]:
    return [f for f in out if isinstance(f, BotTurnEnded)]


@pytest.mark.asyncio
async def test_utterance_end_plus_quiet_grace_ends_the_turn():
    out = await run_detector([
        (SpeechStarted(), 0.02),
        (BotFinalSpeech(text="Hello there."), 0.02),
        (UtteranceBoundary(), 0.3),            # grace 0.15s passes quietly
    ])
    assert len(turn_ends(out)) == 1


@pytest.mark.asyncio
async def test_mid_turn_pause_does_not_end_the_turn():
    """The Mini-Miranda pause: UtteranceEnd, then the bot RESUMES within the
    grace window — one turn, not two."""
    out = await run_detector([
        (SpeechStarted(), 0.02),
        (UtteranceBoundary(), 0.05),           # inside grace…
        (BotPartialSpeech(text="and further"), 0.02),   # …the bot resumes
        (UtteranceBoundary(), 0.3),            # now it's really done
    ])
    assert len(turn_ends(out)) == 1


@pytest.mark.asyncio
async def test_dead_air_hits_the_turn_timeout():
    out = await run_detector([(None, 0.5)], turn_timeout_s=0.2)
    assert len(turn_ends(out)) >= 1            # timeout turn(s), engine decides


@pytest.mark.asyncio
async def test_transport_latency_is_anchored_to_speak_ended():
    out = await run_detector([
        (SpeakEnded(), 0.1),                   # our line finished going out
        (SpeechStarted(), 0.02),               # bot reacts 100ms later
        (UtteranceBoundary(), 0.3),
    ])
    [end] = turn_ends(out)
    assert end.transport_ms is not None
    assert 50 < end.transport_ms < 500         # ~100ms, generous bounds


@pytest.mark.asyncio
async def test_two_turns_two_ends():
    out = await run_detector([
        (SpeechStarted(), 0.02),
        (UtteranceBoundary(), 0.25),           # turn 1 ends
        (SpeechStarted(), 0.02),
        (UtteranceBoundary(), 0.25),           # turn 2 ends
    ])
    assert len(turn_ends(out)) == 2
