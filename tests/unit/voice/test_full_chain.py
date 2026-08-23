"""P6.1 — the whole chain, no monolith: STT → turn-detect → engine → TTS →
audio-out, driven by fake wire audio, settling a real ConversationEngine.

This is the decomposed replacement for what MediaStreamSession does on the
happy path, proven end-to-end with every external dependency faked at its
seam (STT client, LLM, TTS client, WS send) and zero orphaned tasks after
stop — the P6.5 shape.
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from opkura.core.models import Scenario
from opkura.engine.caller import AICallerEngine
from opkura.engine.conversation import ConversationEngine, Termination
from opkura.frames import CallerHangup
from opkura.llm.base import LLMResult
from opkura.voice.audio_out import AudioOut
from opkura.voice.engine_bridge import EngineBridge
from opkura.voice.frames import InboundAudio
from opkura.voice.processor import Chain
from opkura.voice.serializers import TwilioSerializer
from opkura.voice.stt_processor import STTProcessor
from opkura.voice.turn_detect import TurnDetector
from opkura.voice.tts_processor import TTSProcessor


def make_scenario() -> Scenario:
    return Scenario(
        name="chain-test", target="+15550009999", vars={},
        persona_rendered="You are the caller.",
        evaluation_prompt_rendered="Judge.", max_turns=6,
    )


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def complete_json(self, system, messages, **kw) -> LLMResult:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LLMResult(dict(reply), {"input_tokens": 1, "output_tokens": 1})


class FakeSTT:
    """Recognises magic audio chunks and fires Deepgram-shaped callbacks."""

    def __init__(self, script: dict, **callbacks):
        self.script = script
        self.cb = callbacks

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send_audio(self, payload: bytes):
        line = self.script.get(payload)
        if line:
            self.cb["on_speech_started"]()
            self.cb["on_final"](line)
            self.cb["on_utterance_end"]()


class FakeTTS:
    def __init__(self):
        self.spoken: list[str] = []

    async def synthesize_stream(self, text: str):
        self.spoken.append(text)
        yield b"\x11" * 320          # two 20ms chunks worth of "audio"


@pytest.mark.asyncio
async def test_full_chain_conversation_end_to_end():
    scenario = make_scenario()
    llm = ScriptedLLM([
        {"speech": "Hi, this is Casey.", "hangup": False},
        {"speech": "Goodbye now.", "hangup": True},
    ])
    conversation = ConversationEngine(scenario, AICallerEngine(scenario, llm))

    stt_script = {
        b"TURN1": "Hello, who am I speaking with?",
        b"TURN2": "Thanks Casey, anything else?",
    }
    fake_tts = FakeTTS()
    wire_out: list[str] = []

    async def send_text(msg: str) -> None:
        wire_out.append(msg)

    serializer = TwilioSerializer()
    serializer.stream_sid = "MZchain"
    stt = STTProcessor(lambda **cb: FakeSTT(stt_script, **cb))
    detect = TurnDetector(turn_timeout_s=5.0, grace_s=0.1)
    bridge = EngineBridge(conversation)
    tts = TTSProcessor(lambda: fake_tts)
    out = AudioOut(send_text, serializer)
    chain = Chain([stt, detect, bridge, tts, out])
    tap: list = []
    out.to_sink(tap)

    await chain.start()

    # Turn 1: the bot speaks, the caller answers, audio goes out.
    await chain.feed(InboundAudio(b"TURN1"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if fake_tts.spoken:
            break
    assert fake_tts.spoken == ["Hi, this is Casey."]

    # Turn 2: the bot speaks again; the persona says goodbye and hangs up.
    await chain.feed(InboundAudio(b"TURN2"))
    for _ in range(150):
        await asyncio.sleep(0.02)
        if bridge.hangup_delivered:
            break
    assert bridge.hangup_delivered

    await chain.stop(reason="test over")

    # The engine settled with the right story.
    outcome = bridge.outcome
    assert outcome is not None
    assert outcome.termination == Termination.CALLER_HANGUP
    assert outcome.bot_turns == 2 and outcome.caller_turns == 2
    assert [(e.speaker, e.text) for e in outcome.transcript] == [
        ("bot", "Hello, who am I speaking with?"),
        ("caller", "Hi, this is Casey."),
        ("bot", "Thanks Casey, anything else?"),
        ("caller", "Goodbye now."),
    ]
    # D1 on the chain: the goodbye was SYNTHESISED, not just transcribed.
    assert fake_tts.spoken == ["Hi, this is Casey.", "Goodbye now."]
    # …and its audio reached the wire, chunked to the invariant.
    media = [m for m in wire_out if json.loads(m).get("event") == "media"]
    assert len(media) == 4                    # 2 utterances × 320B → 2 chunks
    for m in media:
        assert len(base64.b64decode(json.loads(m)["media"]["payload"])) == 160
    # the hangup passed the terminal link for the wire owner to act on;
    # CallerSpeech is CONSUMED by the TTS link (SpeakEnded marks each one)
    assert any(isinstance(f, CallerHangup) for f in tap)
    from opkura.voice.frames import SpeakEnded

    assert sum(isinstance(f, SpeakEnded) for f in tap) == 2
    # P6.5's shape: nothing orphaned.
    assert chain.orphans() == []


@pytest.mark.asyncio
async def test_chain_cancel_mid_conversation_reaps_everything():
    scenario = make_scenario()
    llm = ScriptedLLM([{"speech": "Hi.", "hangup": False}])
    conversation = ConversationEngine(scenario, AICallerEngine(scenario, llm))

    async def send_text(msg):
        pass

    stt = STTProcessor(lambda **cb: FakeSTT({b"T": "Hello?"}, **cb))
    bridge = EngineBridge(conversation)
    chain = Chain([
        stt, TurnDetector(turn_timeout_s=5.0, grace_s=0.1), bridge,
        TTSProcessor(lambda: FakeTTS()), AudioOut(send_text, TwilioSerializer()),
    ])
    await chain.start()
    await chain.feed(InboundAudio(b"T"))
    await asyncio.sleep(0.05)                 # mid-conversation

    await asyncio.wait_for(chain.cancel(), 5)

    assert chain.orphans() == []              # engine drive task included
