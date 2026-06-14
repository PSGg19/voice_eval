from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opkura.llm.gemini import GeminiClient


def _fake_response(text):
    r = MagicMock()
    r.text = text
    return r


@pytest.fixture
def mock_genai_cls():
    with patch("opkura.llm.gemini.genai.Client") as mock_cls:
        instance = MagicMock()
        instance.aio.models.generate_content = AsyncMock()
        mock_cls.return_value = instance
        yield mock_cls


@pytest.fixture
def mock_genai(mock_genai_cls):
    return mock_genai_cls.return_value


@pytest.mark.asyncio
async def test_returns_parsed_dict(mock_genai):
    mock_genai.aio.models.generate_content.return_value = _fake_response(
        '{"speech": "hi", "dtmf": "", "hangup": false}'
    )
    client = GeminiClient("gemini-2.5-flash", "fake-key")
    result = await client.complete_json(
        system="sys", messages=[{"role": "user", "content": "hi"}]
    )
    assert result == {"speech": "hi", "dtmf": "", "hangup": False}


@pytest.mark.asyncio
async def test_role_mapping_assistant_to_model(mock_genai):
    mock_genai.aio.models.generate_content.return_value = _fake_response("{}")
    client = GeminiClient("gemini-2.5-flash", "fake-key")
    await client.complete_json(
        system="sys",
        messages=[
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ],
    )
    contents = mock_genai.aio.models.generate_content.call_args.kwargs["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert [c.parts[0].text for c in contents] == ["u1", "a1", "u2"]


def test_client_constructed_with_api_key(mock_genai_cls):
    GeminiClient("gemini-2.5-flash", "fake-key")
    mock_genai_cls.assert_called_once_with(api_key="fake-key")


@pytest.mark.asyncio
async def test_passes_system_instruction_and_json_mime(mock_genai):
    mock_genai.aio.models.generate_content.return_value = _fake_response("{}")
    client = GeminiClient("gemini-2.5-flash", "fake-key")
    await client.complete_json(
        system="my-system-prompt",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=321,
        temperature=0.42,
    )
    kwargs = mock_genai.aio.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-2.5-flash"
    cfg = kwargs["config"]
    assert cfg.system_instruction == "my-system-prompt"
    assert cfg.response_mime_type == "application/json"
    assert cfg.temperature == 0.42
    assert cfg.max_output_tokens == 321


@pytest.mark.asyncio
async def test_non_json_raises_value_error(mock_genai):
    mock_genai.aio.models.generate_content.return_value = _fake_response("not-json")
    client = GeminiClient("gemini-2.5-flash", "fake-key")
    with pytest.raises(ValueError, match="non-JSON"):
        await client.complete_json(
            system="sys", messages=[{"role": "user", "content": "hi"}]
        )


@pytest.mark.asyncio
async def test_none_text_raises_value_error(mock_genai):
    mock_genai.aio.models.generate_content.return_value = _fake_response(None)
    client = GeminiClient("gemini-2.5-flash", "fake-key")
    with pytest.raises(ValueError, match="non-JSON"):
        await client.complete_json(
            system="sys", messages=[{"role": "user", "content": "hi"}]
        )


@pytest.mark.parametrize("payload", ['"just a string"', "42", "null", "[1, 2, 3]"])
@pytest.mark.asyncio
async def test_non_dict_json_raises_value_error(mock_genai, payload):
    """Valid JSON that isn't an object must raise ValueError at the provider
    boundary — otherwise downstream _coerce_turn's .get() raises an opaque
    AttributeError that wastes a retry attempt.
    """
    mock_genai.aio.models.generate_content.return_value = _fake_response(payload)
    client = GeminiClient("gemini-2.5-flash", "fake-key")
    with pytest.raises(ValueError, match="non-dict JSON"):
        await client.complete_json(
            system="sys", messages=[{"role": "user", "content": "hi"}]
        )


@pytest.mark.asyncio
async def test_last_usage_populated_from_usage_metadata(mock_genai):
    usage = MagicMock()
    usage.prompt_token_count = 42
    usage.candidates_token_count = 17
    response = _fake_response('{"ok": true}')
    response.usage_metadata = usage
    mock_genai.aio.models.generate_content.return_value = response

    client = GeminiClient("gemini-2.5-flash", "fake-key")
    await client.complete_json(system="sys", messages=[{"role": "user", "content": "hi"}])

    assert client.last_usage == {"input_tokens": 42, "output_tokens": 17}


@pytest.mark.asyncio
async def test_last_usage_cleared_on_raise(mock_genai):
    """Regression: stale last_usage from previous successful call must not leak
    into a subsequent failed call's audit entry.
    """
    # Call 1: success populates last_usage.
    usage = MagicMock()
    usage.prompt_token_count = 100
    usage.candidates_token_count = 50
    response = _fake_response('{"ok": true}')
    response.usage_metadata = usage
    mock_genai.aio.models.generate_content.return_value = response

    client = GeminiClient("gemini-2.5-flash", "fake-key")
    await client.complete_json(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert client.last_usage is not None

    # Call 2: generate_content raises (network blip). last_usage must be None,
    # not the stale {input: 100, output: 50} from call 1.
    mock_genai.aio.models.generate_content.side_effect = ConnectionError("upstream 500")
    with pytest.raises(ConnectionError):
        await client.complete_json(system="sys", messages=[{"role": "user", "content": "hi"}])

    assert client.last_usage is None


@pytest.mark.asyncio
async def test_last_usage_cleared_when_message_construction_raises(mock_genai):
    """Tighter version of the stale-state bug: invalidation must happen BEFORE
    contents/config construction so even pre-SDK errors leave last_usage clean.
    """
    # Seed last_usage from a prior successful call.
    usage = MagicMock()
    usage.prompt_token_count = 99
    usage.candidates_token_count = 33
    response = _fake_response('{"ok": true}')
    response.usage_metadata = usage
    mock_genai.aio.models.generate_content.return_value = response

    client = GeminiClient("gemini-2.5-flash", "fake-key")
    await client.complete_json(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert client.last_usage is not None

    # Next call: malformed message missing 'role' key raises KeyError BEFORE
    # the SDK is called. last_usage must still be None afterward.
    with pytest.raises(KeyError):
        await client.complete_json(system="sys", messages=[{"content": "no role"}])

    assert client.last_usage is None
