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

from judge_it.core.models import Scenario
from judge_it.engine.caller import AICallerEngine
from judge_it.engine.conversation import ConversationEngine, Termination
from judge_it.frames import CallerHangup
from judge_it.llm.base import LLMResult
from judge_it.voice.audio_out import AudioOut
from judge_it.voice.engine_bridge import EngineBridge
from judge_it.voice.frames import InboundAudio
from judge_it.voice.processor import Chain
from judge_it.voice.serializers import TwilioSerializer
from judge_it.voice.stt_processor import STTProcessor
from judge_it.voice.turn_detect import TurnDetector
from judge_it.voice.tts_processor import TTSProcessor


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
    from judge_it.voice.frames import SpeakEnded

    assert sum(isinstance(f, SpeakEnded) for f in tap) == 2
    # P6.5's shape: nothing orphaned.
    assert chain.orphans() == []


# ------------------------------------- PRD acceptance: transport identity

class FakeTextBackend:
    """Text side of the identity test: same bot lines, HTTP-shaped."""

    def __init__(self, lines: list[str]):
        self._lines = list(lines)
        self.interaction_uuid = "int-identity"

    def _next(self):
        from judge_it.transport.text import BotTurn

        text = self._lines.pop(0) if self._lines else "(exhausted)"
        return BotTurn(text=text, message_type="RECORD")

    async def start(self):
        return self._next()

    async def send(self, speech: str = "", dtmf: str = ""):
        return self._next()

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_text_adapter_and_voice_chain_produce_identical_conversations():
    """PRD acceptance: the same scenario through the text adapter and through
    the full voice chain yields identical transcripts and termination."""
    from judge_it.transport.text_adapter import TextFrameAdapter

    scenario = make_scenario()
    bot_lines = [
        "Hello, this is Riverline Bank.",
        "Can I get your date of birth?",
        "Thank you. Anything else today?",
    ]
    replies = [
        {"speech": "Hi, you called me earlier.", "hangup": False},
        {"speech": "Fourth of August, two thousand one.", "hangup": False},
        {"speech": "No, that's all. Goodbye.", "hangup": True},
    ]

    # -- text path
    text_engine = ConversationEngine(
        scenario, AICallerEngine(scenario, ScriptedLLM(replies))
    )
    text_outcome = await text_engine.run(TextFrameAdapter(FakeTextBackend(bot_lines)))

    # -- voice path: the same bot lines arrive as recognised audio
    conversation = ConversationEngine(
        scenario, AICallerEngine(scenario, ScriptedLLM(replies))
    )
    stt_script = {f"T{i}".encode(): line for i, line in enumerate(bot_lines)}

    async def send_text(msg: str) -> None:
        pass

    bridge = EngineBridge(conversation)
    chain = Chain([
        STTProcessor(lambda **cb: FakeSTT(stt_script, **cb)),
        TurnDetector(turn_timeout_s=5.0, grace_s=0.1),
        bridge,
        TTSProcessor(lambda: FakeTTS()),
        AudioOut(send_text, TwilioSerializer()),
    ])
    await chain.start()
    for i in range(len(bot_lines)):
        await chain.feed(InboundAudio(f"T{i}".encode()))
        for _ in range(150):
            await asyncio.sleep(0.02)
            if bridge.hangup_delivered or len(
                conversation.transcript
            ) >= (i + 1) * 2:
                break
    await chain.stop(reason="identity test over")
    voice_outcome = bridge.outcome
    assert voice_outcome is not None

    def shape(outcome):
        return [(e.speaker, e.text, e.dtmf) for e in outcome.transcript]

    assert shape(text_outcome) == shape(voice_outcome)
    assert (
        text_outcome.termination
        == voice_outcome.termination
        == Termination.CALLER_HANGUP
    )
    assert text_outcome.caller_turns == voice_outcome.caller_turns == 3
    assert text_outcome.bot_turns == voice_outcome.bot_turns == 3


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
