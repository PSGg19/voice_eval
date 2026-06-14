"""Unit tests for DeepgramWebSocketTTS — per-utterance WS with prewarm."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opkura.transport.tts import DEEPGRAM_TTS_WS_URL, DeepgramWebSocketTTS


class _FakeWS:
    """Minimal stand-in for websockets.WebSocketClientProtocol.

    Tests inject inbound frames via `inject_binary` / `inject_json`,
    then iterating the WS pulls them out. Outbound `send` calls are
    captured in `sent` for assertions.

    To simulate the connection dying mid-stream, push a
    `_RAISE_CLOSED` sentinel via `inject_close()` — `__anext__` then
    raises `ConnectionClosed`. We can't monkey-patch __anext__ on the
    instance because Python's async iteration resolves dunder methods
    via the type, not the instance.
    """

    _STOP = object()
    _RAISE_CLOSED = object()

    def __init__(self) -> None:
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._inbox.get()
        if item is _FakeWS._STOP:
            raise StopAsyncIteration
        if item is _FakeWS._RAISE_CLOSED:
            import websockets as _ws_mod
            raise _ws_mod.exceptions.ConnectionClosed(None, None)
        return item

    async def send(self, raw):
        self.sent.append(raw)

    async def close(self):
        self.closed = True
        # Drain any leftover items so a parked __anext__ unblocks cleanly.
        await self._inbox.put(_FakeWS._STOP)

    async def inject_binary(self, data: bytes) -> None:
        await self._inbox.put(data)

    async def inject_json(self, msg: dict) -> None:
        await self._inbox.put(json.dumps(msg))

    async def inject_close(self) -> None:
        """Simulate Deepgram closing the connection mid-stream."""
        await self._inbox.put(_FakeWS._RAISE_CLOSED)


@pytest.mark.asyncio
async def test_synthesize_stream_opens_fresh_when_no_prewarm():
    """Without a prewarm, synthesize_stream opens the WS itself."""
    tts = DeepgramWebSocketTTS(api_key="k-test", model="aura-2-thalia-en")
    fake_ws = _FakeWS()
    captured: dict = {}

    async def fake_connect(url, additional_headers=None, **_kwargs):
        captured["url"] = url
        captured["headers"] = additional_headers
        return fake_ws

    expected_frames = [b"\xaa" * 100, b"\xbb" * 200]

    with patch("opkura.transport.tts.websockets.connect", side_effect=fake_connect):

        async def producer():
            await asyncio.sleep(0.01)
            for frame in expected_frames:
                await fake_ws.inject_binary(frame)
            await fake_ws.inject_json({"type": "Flushed"})

        producer_task = asyncio.create_task(producer())

        received = []
        async for chunk in tts.synthesize_stream("hello"):
            received.append(chunk)
        await producer_task

    assert captured["url"].startswith(DEEPGRAM_TTS_WS_URL)
    assert "model=aura-2-thalia-en" in captured["url"]
    assert "encoding=mulaw" in captured["url"]
    assert captured["headers"]["Authorization"] == "Token k-test"
    assert received == expected_frames
    # WS must be closed after the generator exits.
    assert fake_ws.closed


@pytest.mark.asyncio
async def test_prewarm_opens_ws_and_synthesize_consumes_it():
    """prewarm() opens a WS up-front; the next synthesize_stream uses
    that prewarmed connection instead of opening another."""
    tts = DeepgramWebSocketTTS(api_key="k-test")
    prewarmed_ws = _FakeWS()
    connect_call_count = 0

    async def fake_connect(url, additional_headers=None, **_kwargs):
        nonlocal connect_call_count
        connect_call_count += 1
        return prewarmed_ws

    with patch("opkura.transport.tts.websockets.connect", side_effect=fake_connect):
        await tts.prewarm()
        assert connect_call_count == 1
        assert tts._prewarmed_ws is prewarmed_ws

        async def producer():
            await asyncio.sleep(0.01)
            await prewarmed_ws.inject_binary(b"\xff" * 80)
            await prewarmed_ws.inject_json({"type": "Flushed"})

        producer_task = asyncio.create_task(producer())

        received = []
        async for chunk in tts.synthesize_stream("hi"):
            received.append(chunk)
        await producer_task

    # No second connect — synthesize consumed the prewarmed WS.
    assert connect_call_count == 1
    assert received == [b"\xff" * 80]
    # Prewarm slot must be cleared after consumption.
    assert tts._prewarmed_ws is None
    assert prewarmed_ws.closed


@pytest.mark.asyncio
async def test_prewarm_is_idempotent():
    """Calling prewarm twice with an existing prewarm is a no-op."""
    tts = DeepgramWebSocketTTS(api_key="k-test")
    fake_ws = _FakeWS()
    connect_call_count = 0

    async def fake_connect(*_args, **_kwargs):
        nonlocal connect_call_count
        connect_call_count += 1
        return fake_ws

    with patch("opkura.transport.tts.websockets.connect", side_effect=fake_connect):
        await tts.prewarm()
        await tts.prewarm()  # second call — no-op
        await tts.prewarm()  # third call — still no-op

    assert connect_call_count == 1


@pytest.mark.asyncio
async def test_prewarm_failure_is_swallowed():
    """A failed prewarm logs but doesn't raise; synthesize_stream then
    falls back to opening fresh."""
    tts = DeepgramWebSocketTTS(api_key="k-test")
    fake_ws = _FakeWS()

    connect_calls = 0

    async def fake_connect(*_args, **_kwargs):
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 1:
            raise ConnectionError("simulated prewarm failure")
        return fake_ws

    with patch("opkura.transport.tts.websockets.connect", side_effect=fake_connect):
        # Prewarm fails silently.
        await tts.prewarm()
        assert tts._prewarmed_ws is None

        # synthesize_stream still works — opens fresh.
        async def producer():
            await asyncio.sleep(0.01)
            await fake_ws.inject_binary(b"\xaa" * 50)
            await fake_ws.inject_json({"type": "Flushed"})

        producer_task = asyncio.create_task(producer())

        received = []
        async for chunk in tts.synthesize_stream("hi"):
            received.append(chunk)
        await producer_task

    assert received == [b"\xaa" * 50]
    assert connect_calls == 2  # one failed prewarm + one fresh open


@pytest.mark.asyncio
async def test_synthesize_stream_sends_speak_then_flush():
    """Per-utterance protocol: Speak goes out, then Flush, in that order."""
    tts = DeepgramWebSocketTTS(api_key="k-test")
    fake_ws = _FakeWS()

    async def fake_connect(*_args, **_kwargs):
        return fake_ws

    with patch("opkura.transport.tts.websockets.connect", side_effect=fake_connect):

        async def producer():
            await asyncio.sleep(0.01)
            await fake_ws.inject_json({"type": "Flushed"})

        producer_task = asyncio.create_task(producer())

        async for _ in tts.synthesize_stream("the text"):
            pass
        await producer_task

    sent_types = [json.loads(s).get("type") for s in fake_ws.sent if isinstance(s, str)]
    assert sent_types[0] == "Speak"
    assert sent_types[1] == "Flush"
    speak_payload = json.loads(fake_ws.sent[0])
    assert speak_payload["text"] == "the text"


@pytest.mark.asyncio
async def test_synthesize_stream_empties_skipped_keepalive_frames():
    """Empty binary frames (Deepgram keepalive padding) must be filtered."""
    tts = DeepgramWebSocketTTS(api_key="k-test")
    fake_ws = _FakeWS()

    async def fake_connect(*_args, **_kwargs):
        return fake_ws

    with patch("opkura.transport.tts.websockets.connect", side_effect=fake_connect):

        async def producer():
            await asyncio.sleep(0.01)
            await fake_ws.inject_binary(b"\xaa" * 50)
            await fake_ws.inject_binary(b"")  # empty
            await fake_ws.inject_binary(b"\xbb" * 30)
            await fake_ws.inject_json({"type": "Flushed"})

        producer_task = asyncio.create_task(producer())

        received = []
        async for chunk in tts.synthesize_stream("hi"):
            received.append(chunk)
        await producer_task

    assert b"" not in received
    assert received == [b"\xaa" * 50, b"\xbb" * 30]


@pytest.mark.asyncio
async def test_synthesize_stream_terminates_on_connection_closed_mid_stream():
    """If the WS dies before Flushed arrives, the generator returns
    cleanly (caller's audio is truncated, not crashed)."""
    tts = DeepgramWebSocketTTS(api_key="k-test")
    fake_ws = _FakeWS()

    async def fake_connect(*_args, **_kwargs):
        return fake_ws

    with patch("opkura.transport.tts.websockets.connect", side_effect=fake_connect):

        async def producer():
            await asyncio.sleep(0.01)
            await fake_ws.inject_binary(b"\xaa" * 50)
            await fake_ws.inject_close()  # Deepgram drops the connection

        producer_task = asyncio.create_task(producer())

        received = []
        async for chunk in tts.synthesize_stream("hi"):
            received.append(chunk)
        await producer_task

    assert received == [b"\xaa" * 50]
    assert fake_ws.closed


@pytest.mark.asyncio
async def test_close_cleans_up_prewarmed_ws():
    """close() releases an orphaned prewarmed WS (caller fell back to HTTP)."""
    tts = DeepgramWebSocketTTS(api_key="k-test")
    fake_ws = _FakeWS()

    async def fake_connect(*_args, **_kwargs):
        return fake_ws

    with patch("opkura.transport.tts.websockets.connect", side_effect=fake_connect):
        await tts.prewarm()
        assert tts._prewarmed_ws is fake_ws

        await tts.close()

    assert tts._prewarmed_ws is None
    assert fake_ws.closed


@pytest.mark.asyncio
async def test_close_is_noop_when_no_prewarm():
    """close() with no outstanding prewarm is a safe no-op."""
    tts = DeepgramWebSocketTTS(api_key="k-test")
    # Should not raise.
    await tts.close()
    assert tts._prewarmed_ws is None


@pytest.mark.asyncio
async def test_synthesize_stream_terminates_on_any_unknown_json_message():
    """Any JSON message other than Metadata/Warning is treated as
    end-of-utterance — defensive against unknown Deepgram end markers
    (lowercase, renamed, etc). This prevents the 10s idle-timeout hang
    we saw in production when the end marker didn't match 'Flushed'."""
    tts = DeepgramWebSocketTTS(api_key="k-test")
    fake_ws = _FakeWS()

    async def fake_connect(*_args, **_kwargs):
        return fake_ws

    with patch("opkura.transport.tts.websockets.connect", side_effect=fake_connect):

        async def producer():
            await asyncio.sleep(0.01)
            await fake_ws.inject_binary(b"\xaa" * 50)
            # Some unknown end-marker — must still terminate the generator.
            await fake_ws.inject_json({"type": "SomethingElse", "k": "v"})
            # Anything after this should NEVER be yielded.
            await fake_ws.inject_binary(b"\xbb" * 1000)

        producer_task = asyncio.create_task(producer())

        received = []
        async for chunk in tts.synthesize_stream("hi"):
            received.append(chunk)
        await producer_task

    # Only the frame BEFORE the unknown end marker is yielded.
    assert received == [b"\xaa" * 50]
    assert fake_ws.closed


@pytest.mark.asyncio
async def test_metadata_and_warning_do_not_terminate_stream():
    """Metadata + Warning are non-terminal — audio after them must
    still be yielded."""
    tts = DeepgramWebSocketTTS(api_key="k-test")
    fake_ws = _FakeWS()

    async def fake_connect(*_args, **_kwargs):
        return fake_ws

    with patch("opkura.transport.tts.websockets.connect", side_effect=fake_connect):

        async def producer():
            await asyncio.sleep(0.01)
            await fake_ws.inject_json({"type": "Metadata", "request_id": "abc"})
            await fake_ws.inject_binary(b"\xaa" * 30)
            await fake_ws.inject_json({"type": "Warning", "description": "slow"})
            await fake_ws.inject_binary(b"\xbb" * 40)
            await fake_ws.inject_json({"type": "Flushed"})

        producer_task = asyncio.create_task(producer())

        received = []
        async for chunk in tts.synthesize_stream("hi"):
            received.append(chunk)
        await producer_task

    assert received == [b"\xaa" * 30, b"\xbb" * 40]


@pytest.mark.asyncio
async def test_recv_handles_malformed_json_without_crashing():
    """One garbage text frame must not kill the generator — it's
    logged and skipped, audio frames around it still deliver."""
    tts = DeepgramWebSocketTTS(api_key="k-test")
    fake_ws = _FakeWS()

    async def fake_connect(*_args, **_kwargs):
        return fake_ws

    with patch("opkura.transport.tts.websockets.connect", side_effect=fake_connect):

        async def producer():
            await asyncio.sleep(0.01)
            await fake_ws.inject_binary(b"\xaa" * 30)
            await fake_ws._inbox.put("{not valid json")  # malformed text frame
            await fake_ws.inject_binary(b"\xbb" * 40)
            await fake_ws.inject_json({"type": "Flushed"})

        producer_task = asyncio.create_task(producer())

        received = []
        async for chunk in tts.synthesize_stream("hi"):
            received.append(chunk)
        await producer_task

    assert received == [b"\xaa" * 30, b"\xbb" * 40]
