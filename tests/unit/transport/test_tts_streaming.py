"""Unit tests for streaming TTS — DeepgramTTS.synthesize_stream()."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vjudge.transport.tts import DEEPGRAM_TTS_URL, DeepgramTTS


@pytest.mark.asyncio
async def test_synthesize_stream_yields_chunks_in_order():
    """Verify the async generator yields bytes from Deepgram's response
    in the order they arrive."""
    tts = DeepgramTTS(api_key="k-test")

    # Build a mock response whose aiter_bytes yields known chunks.
    expected_chunks = [b"\xaa" * 1024, b"\xbb" * 2048, b"\xcc" * 512]

    async def fake_aiter_bytes(chunk_size=4096):
        for chunk in expected_chunks:
            yield chunk

    fake_response = MagicMock()
    fake_response.aiter_bytes = fake_aiter_bytes
    fake_response.raise_for_status = MagicMock()

    fake_stream_cm = MagicMock()
    fake_stream_cm.__aenter__ = AsyncMock(return_value=fake_response)
    fake_stream_cm.__aexit__ = AsyncMock(return_value=None)

    with patch.object(tts._client, "stream", return_value=fake_stream_cm):
        received = []
        async for chunk in tts.synthesize_stream("hello"):
            received.append(chunk)

    assert received == expected_chunks


@pytest.mark.asyncio
async def test_synthesize_stream_skips_empty_chunks():
    """Empty bytes (keepalive frames) must be filtered out."""
    tts = DeepgramTTS(api_key="k-test")

    async def fake_aiter_bytes(chunk_size=4096):
        yield b"\xaa" * 100
        yield b""  # keepalive
        yield b"\xbb" * 200
        yield b""
        yield b"\xcc" * 50

    fake_response = MagicMock()
    fake_response.aiter_bytes = fake_aiter_bytes
    fake_response.raise_for_status = MagicMock()

    fake_stream_cm = MagicMock()
    fake_stream_cm.__aenter__ = AsyncMock(return_value=fake_response)
    fake_stream_cm.__aexit__ = AsyncMock(return_value=None)

    with patch.object(tts._client, "stream", return_value=fake_stream_cm):
        received = []
        async for chunk in tts.synthesize_stream("hello"):
            received.append(chunk)

    # Empty chunks should be dropped — only real audio yielded.
    assert all(len(c) > 0 for c in received)
    assert len(received) == 3


@pytest.mark.asyncio
async def test_synthesize_stream_passes_correct_request_params():
    """Verify HTTP request is shaped correctly for Deepgram TTS."""
    tts = DeepgramTTS(api_key="k-test-secret", model="aura-2-thalia-en")

    captured_kwargs = {}

    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()

    async def fake_aiter_bytes(chunk_size=4096):
        yield b"\xaa" * 100

    fake_response.aiter_bytes = fake_aiter_bytes

    fake_stream_cm = MagicMock()
    fake_stream_cm.__aenter__ = AsyncMock(return_value=fake_response)
    fake_stream_cm.__aexit__ = AsyncMock(return_value=None)

    def capture_stream(method, url, **kwargs):
        captured_kwargs["method"] = method
        captured_kwargs["url"] = url
        captured_kwargs.update(kwargs)
        return fake_stream_cm

    with patch.object(tts._client, "stream", side_effect=capture_stream):
        async for _ in tts.synthesize_stream("Hello, world."):
            pass

    assert captured_kwargs["method"] == "POST"
    assert captured_kwargs["url"] == DEEPGRAM_TTS_URL
    assert captured_kwargs["params"] == {
        "model": "aura-2-thalia-en",
        "encoding": "mulaw",
        "sample_rate": "8000",
        "container": "none",
    }
    assert captured_kwargs["headers"]["Authorization"] == "Token k-test-secret"
    assert captured_kwargs["headers"]["Content-Type"] == "application/json"
    assert captured_kwargs["json"] == {"text": "Hello, world."}


@pytest.mark.asyncio
async def test_synthesize_stream_propagates_http_errors():
    """Non-200 response should raise via raise_for_status."""
    tts = DeepgramTTS(api_key="k-test")

    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock(
        side_effect=Exception("401 Unauthorized")
    )

    fake_stream_cm = MagicMock()
    fake_stream_cm.__aenter__ = AsyncMock(return_value=fake_response)
    fake_stream_cm.__aexit__ = AsyncMock(return_value=None)

    with patch.object(tts._client, "stream", return_value=fake_stream_cm):
        with pytest.raises(Exception, match="401 Unauthorized"):
            async for _ in tts.synthesize_stream("hi"):
                pass


@pytest.mark.asyncio
async def test_blocking_synthesize_still_works():
    """Regression: the original synthesize() must still function for
    fallback when VJUDGE_STREAMING_TTS=false."""
    tts = DeepgramTTS(api_key="k-test")

    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.content = b"\xaa" * 1234

    with patch.object(tts._client, "post", AsyncMock(return_value=fake_response)):
        result = await tts.synthesize("hi")

    assert result == b"\xaa" * 1234
