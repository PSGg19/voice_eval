from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vjudge.llm.gemini import GeminiClient


def _fake_response(text):
    r = MagicMock()
    r.text = text
    return r


@pytest.fixture
def mock_genai_cls():
    with patch("vjudge.llm.gemini.genai.Client") as mock_cls:
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
async def test_usage_travels_in_the_result(mock_genai):
    """P1.2: token usage is ON the LLMResult, not on the client. The old
    `last_usage` attribute (whose staleness needed two regression tests) is
    gone — there is no client state for concurrent runs to race on."""
    usage = MagicMock()
    usage.prompt_token_count = 42
    usage.candidates_token_count = 17
    response = _fake_response('{"ok": true}')
    response.usage_metadata = usage
    mock_genai.aio.models.generate_content.return_value = response

    client = GeminiClient("gemini-2.5-flash", "fake-key")
    result = await client.complete_json(
        system="sys", messages=[{"role": "user", "content": "hi"}]
    )

    assert result == {"ok": True}                      # still the JSON mapping
    assert result.usage == {"input_tokens": 42, "output_tokens": 17}
    assert not hasattr(client, "last_usage")           # the attribute is GONE


@pytest.mark.asyncio
async def test_missing_usage_metadata_yields_empty_usage(mock_genai):
    response = _fake_response('{"ok": true}')
    response.usage_metadata = None
    mock_genai.aio.models.generate_content.return_value = response

    client = GeminiClient("gemini-2.5-flash", "fake-key")
    result = await client.complete_json(
        system="sys", messages=[{"role": "user", "content": "hi"}]
    )
    assert result.usage == {}


@pytest.mark.asyncio
async def test_usage_isolation_when_one_client_is_shared(mock_genai):
    """The P1 acceptance property at the provider boundary: N concurrent calls
    through ONE client each see exactly their own usage, because usage never
    touches shared state. Interleaving is forced with a sleep inside the SDK
    double."""
    import asyncio

    calls = {"n": 0}

    async def fake_generate(**kwargs):
        calls["n"] += 1
        mine = calls["n"]
        await asyncio.sleep(0.001 * (mine % 5))  # force interleaving
        usage = MagicMock()
        usage.prompt_token_count = mine
        usage.candidates_token_count = mine * 10
        response = _fake_response('{"i": %d}' % mine)
        response.usage_metadata = usage
        return response

    mock_genai.aio.models.generate_content.side_effect = fake_generate
    client = GeminiClient("gemini-2.5-flash", "fake-key")

    async def one_call():
        result = await client.complete_json(
            system="s", messages=[{"role": "user", "content": "x"}]
        )
        # the usage must belong to THIS reply, not whichever call finished last
        assert result.usage["input_tokens"] == result["i"]
        assert result.usage["output_tokens"] == result["i"] * 10

    await asyncio.gather(*(one_call() for _ in range(50)))
