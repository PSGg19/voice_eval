"""P6 — VoiceSession: the chain-backed session behind the monolith's surface.

Drives the session exactly the way server.py + Twilio do: a fake WebSocket
feeding wire-format JSON, reattach routing on `_in_transition`, the status
webhook via on_call_lifecycle. Pins the DTMF REST-redirect matrix with the
SAME typed errors and result labels as the V2 baseline (the P6 acceptance).
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from vjudge.core.models import CallResult, CallStatus, Scenario
from vjudge.engine.caller import AICallerEngine
from vjudge.engine.conversation import ConversationEngine
from vjudge.llm.base import LLMResult
from vjudge.voice.session import VoiceSession


def make_scenario(max_turns: int = 6) -> Scenario:
    return Scenario(
        name="session-test", target="+15550009999", vars={},
        persona_rendered="You are the caller.",
        evaluation_prompt_rendered="Judge.", max_turns=max_turns,
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
    def __init__(self, script, **cb):
        self.script = script
        self.cb = cb
        self.starts = 0
        self.stops = 0

    async def start(self):
        self.starts += 1

    async def stop(self):
        self.stops += 1

    async def send_audio(self, payload: bytes):
        line = self.script.get(payload)
        if line:
            self.cb["on_speech_started"]()
            self.cb["on_final"](line)
            self.cb["on_utterance_end"]()


class FakeTTS:
    def __init__(self):
        self.spoken = []

    async def synthesize_stream(self, text):
        self.spoken.append(text)
        yield b"\x33" * 160


class FakeWS:
    """Twilio's half of the media stream."""

    def __init__(self):
        self._incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_text(self, msg: str):
        self.sent.append(msg)

    async def iter_text(self):
        while True:
            msg = await self._incoming.get()
            if msg is None:
                return
            yield msg

    # test-side helpers
    async def wire(self, event: dict):
        await self._incoming.put(json.dumps(event))

    async def media(self, payload: bytes):
        await self.wire({"event": "media",
                         "media": {"payload": base64.b64encode(payload).decode()}})

    async def close_from_twilio(self):
        await self._incoming.put(None)


class FakeCallManager:
    def __init__(self, fail: Exception | None = None):
        self.redirects = []
        self.fail = fail

    def play_dtmf_and_reconnect(self, *, call_sid, digits, reconnect_ws_url):
        if self.fail is not None:
            raise self.fail
        self.redirects.append((call_sid, digits, reconnect_ws_url))


def build_session(replies, *, stt_script, call_manager=None,
                  dtmf_timeout=0.3) -> tuple[VoiceSession, CallResult, FakeTTS]:
    scenario = make_scenario()
    caller = AICallerEngine(scenario, ScriptedLLM(replies))
    conversation = ConversationEngine(scenario, caller)
    result = CallResult(id="r", session_id="s" * 32, flow_name=scenario.name,
                        target=scenario.target)
    tts = FakeTTS()
    session = VoiceSession(
        conversation=conversation, caller=caller, result=result,
        stt_factory=lambda **cb: FakeSTT(stt_script, **cb),
        tts_factory=lambda: tts,
        turn_timeout_s=5.0,
        call_manager=call_manager,
        session_id="s" * 32,
        reconnect_ws_url="wss://worker.example/ws/sess",
        dtmf_reconnect_timeout_s=dtmf_timeout,
    )
    # grace down for test speed (Deepgram-mode detector is links[1])
    for link in session.chain.processors:
        if hasattr(link, "grace_s"):
            link.grace_s = 0.1
    return session, result, tts


START = {"event": "start",
         "start": {"streamSid": "MZtest", "callSid": "CAtest01"}}


async def wait_for(predicate, *, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


# --------------------------------------------------------------- happy path

@pytest.mark.asyncio
async def test_full_call_over_the_wire():
    session, result, tts = build_session(
        [{"speech": "Hi, this is Casey.", "hangup": False},
         {"speech": "Goodbye.", "hangup": True}],
        stt_script={b"T1": "Who am I speaking with?", b"T2": "Anything else?"},
    )
    completion = asyncio.Event()
    session.attach_completion_event(completion, asyncio.get_running_loop())

    ws = FakeWS()
    handle = asyncio.create_task(session.handle(ws))
    await ws.wire(START)
    await ws.media(b"T1")
    assert await wait_for(lambda: tts.spoken == ["Hi, this is Casey."])
    await ws.media(b"T2")
    assert await wait_for(lambda: completion.is_set())   # hangup signalled
    await ws.close_from_twilio()                          # Twilio hangs up
    await asyncio.wait_for(handle, 10)

    assert result.call_sid == "CAtest01"
    assert result.status == CallStatus.COMPLETED
    assert [(e.speaker, e.text) for e in result.transcript] == [
        ("bot", "Who am I speaking with?"),
        ("caller", "Hi, this is Casey."),
        ("bot", "Anything else?"),
        ("caller", "Goodbye."),
    ]
    # TTS audio reached the wire and was recorded as caller audio
    media = [m for m in ws.sent if json.loads(m).get("event") == "media"]
    assert media and session._our_audio_buffer
    assert bytes(session._bot_audio_buffer) == b"T1T2"
    assert session.chain.orphans() == []


# ------------------------------------------------------- DTMF REST redirect

def dtmf_replies():
    return [
        {"speech": "", "dtmf": "1234", "hangup": False},
        {"speech": "Goodbye.", "hangup": True},
    ]


@pytest.mark.asyncio
async def test_dtmf_redirect_reattach_round_trip():
    mgr = FakeCallManager()
    session, result, tts = build_session(
        dtmf_replies(),
        stt_script={b"T1": "Enter your code now.", b"T2": "Got it, thanks."},
        call_manager=mgr, dtmf_timeout=5.0,
    )
    completion = asyncio.Event()
    session.attach_completion_event(completion, asyncio.get_running_loop())

    ws1 = FakeWS()
    handle1 = asyncio.create_task(session.handle(ws1))
    await ws1.wire(START)
    await ws1.media(b"T1")
    assert await wait_for(lambda: mgr.redirects)          # redirect fired
    assert session._in_transition

    # Twilio tears the old WS down as part of the redirect…
    await ws1.close_from_twilio()
    await asyncio.wait_for(handle1, 5)
    assert not session._session_ended                     # paused, not ended

    # …and opens a new one, which server.py routes to reattach().
    ws2 = FakeWS()
    reattach = asyncio.create_task(session.reattach(ws2))
    await asyncio.sleep(0.1)
    assert not session._in_transition                     # reconnected
    await ws2.wire(START)
    await ws2.media(b"T2")                                # conversation resumes
    assert await wait_for(lambda: completion.is_set())
    await ws2.close_from_twilio()
    await asyncio.wait_for(reattach, 10)

    assert session._attach_count == 2
    assert mgr.redirects == [("CAtest01", "1234",
                              "wss://worker.example/ws/sess")]
    assert result.status == CallStatus.COMPLETED
    assert result.error is None or result.error == ""
    # the same STT client was restarted for the new WS (per-attach rebuild)
    assert session._stt._stt.starts == 2


@pytest.mark.asyncio
async def test_dtmf_timeout_yields_the_same_label_as_v2():
    session, result, _ = build_session(
        dtmf_replies(), stt_script={b"T1": "Enter your code."},
        call_manager=FakeCallManager(), dtmf_timeout=0.15,
    )
    completion = asyncio.Event()
    session.attach_completion_event(completion, asyncio.get_running_loop())

    ws = FakeWS()
    handle = asyncio.create_task(session.handle(ws))
    await ws.wire(START)
    await ws.media(b"T1")
    assert await wait_for(lambda: session._in_transition)
    await ws.close_from_twilio()                          # WS gone; nobody reattaches
    await asyncio.wait_for(handle, 5)

    assert await wait_for(lambda: completion.is_set())
    assert result.status == CallStatus.ERROR
    assert result.error.startswith("VJUDGE DTMF TIMEOUT")
    await session.end_session()
    assert session.chain.orphans() == []


@pytest.mark.asyncio
async def test_dtmf_abort_via_status_webhook():
    session, result, _ = build_session(
        dtmf_replies(), stt_script={b"T1": "Enter your code."},
        call_manager=FakeCallManager(), dtmf_timeout=5.0,
    )
    completion = asyncio.Event()
    session.attach_completion_event(completion, asyncio.get_running_loop())

    ws = FakeWS()
    handle = asyncio.create_task(session.handle(ws))
    await ws.wire(START)
    await ws.media(b"T1")
    assert await wait_for(lambda: session._in_transition)

    session.on_call_lifecycle("completed")                # the webhook fires
    assert await wait_for(lambda: result.status == CallStatus.ERROR)
    assert result.error.startswith("DTMF REDIRECT ABORTED")

    await ws.close_from_twilio()
    await asyncio.wait_for(handle, 5)
    await session.end_session()


@pytest.mark.asyncio
async def test_dtmf_rest_failure_rolls_back_the_pause():
    session, result, _ = build_session(
        dtmf_replies(), stt_script={b"T1": "Enter your code."},
        call_manager=FakeCallManager(fail=RuntimeError("twilio 500")),
    )
    completion = asyncio.Event()
    session.attach_completion_event(completion, asyncio.get_running_loop())

    ws = FakeWS()
    handle = asyncio.create_task(session.handle(ws))
    await ws.wire(START)
    await ws.media(b"T1")

    assert await wait_for(lambda: result.status == CallStatus.ERROR)
    assert result.error.startswith("DTMF REDIRECT FAILED")
    assert not session._in_transition                     # rolled back
    await ws.close_from_twilio()
    await asyncio.wait_for(handle, 5)


# ----------------------------------------------------------- surface parity

@pytest.mark.asyncio
async def test_duplicate_attach_is_refused_like_the_monolith():
    session, _, _ = build_session(
        [{"speech": "hi", "hangup": True}], stt_script={},
    )
    ws1, ws2 = FakeWS(), FakeWS()
    handle = asyncio.create_task(session.handle(ws1))
    await asyncio.sleep(0.05)
    with pytest.raises(RuntimeError, match="not in transition"):
        await session.attach_ws(ws2)
    await ws1.close_from_twilio()
    await asyncio.wait_for(handle, 5)


@pytest.mark.asyncio
async def test_reattach_outside_transition_is_refused():
    session, _, _ = build_session(
        [{"speech": "hi", "hangup": True}], stt_script={},
    )
    with pytest.raises(RuntimeError, match="not in transition"):
        await session.reattach(FakeWS())


@pytest.mark.asyncio
async def test_sustained_media_during_playback_does_not_kill_turn_detection():
    """Regression (2026-09-01 live smoke): raw media flooding the chain while
    audio-out paced a long utterance filled the turn detector's inbox, its
    ticker died on QueueFull, and no turn ever ended again. Audio now stops
    at the STT link and tickers survive a full inbox — a call with heavy
    inbound media during OUR playback must still converse to completion."""

    class LongTTS(FakeTTS):
        async def synthesize_stream(self, text):
            self.spoken.append(text)
            yield b"\x44" * (160 * 25)         # ~500ms of paced playback

    scenario = make_scenario()
    caller = AICallerEngine(scenario, ScriptedLLM([
        {"speech": "A reasonably long reply that takes a while.", "hangup": False},
        {"speech": "Goodbye.", "hangup": True},
    ]))
    conversation = ConversationEngine(scenario, caller)
    result = CallResult(id="r", session_id="s" * 32, flow_name=scenario.name,
                        target=scenario.target)
    tts = LongTTS()
    session = VoiceSession(
        conversation=conversation, caller=caller, result=result,
        stt_factory=lambda **cb: FakeSTT(
            {b"T1": "First question?", b"T2": "Second question?"}, **cb),
        tts_factory=lambda: tts, turn_timeout_s=5.0,
        session_id="s" * 32,
    )
    for link in session.chain.processors:
        if hasattr(link, "grace_s"):
            link.grace_s = 0.1

    completion = asyncio.Event()
    session.attach_completion_event(completion, asyncio.get_running_loop())
    ws = FakeWS()
    handle = asyncio.create_task(session.handle(ws))
    await ws.wire(START)
    await ws.media(b"T1")
    # While our long reply is being paced out, the wire keeps delivering
    # media at full rate — far more frames than any inbox holds.
    for _ in range(300):
        await ws.media(b"\xff" * 160)
        await asyncio.sleep(0.002)
    assert await wait_for(lambda: len(tts.spoken) == 1)

    await ws.media(b"T2")                      # the bot's SECOND turn
    assert await wait_for(lambda: completion.is_set(), timeout=10), (
        "turn detection died under media load — the live-smoke failure mode"
    )
    await ws.close_from_twilio()
    await asyncio.wait_for(handle, 10)

    assert tts.spoken == [
        "A reasonably long reply that takes a while.", "Goodbye.",
    ]
    assert result.status == CallStatus.COMPLETED
