"""`TestRunner._enrich_text_result_from_osv` — recovering what HTTP drops.

`post_message_web` never returns `tool_calls`, and it strips marker tags before
filling `output_message`, so a `<LANG:en-US>`-only reply arrives as `""`. Both
survive in the conversations API. What's pinned here is mostly the *failure*
paths: this runs after a completed call, so it must be incapable of turning a
good result into a bad one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from judge_it.core.config import Settings
from judge_it.core.models import CallResult, LLMConfig, STTConfig, Scenario, TTSConfig


def _settings(token: str = "osv_token", **kw) -> Settings:
    return Settings(
        _env_file=None,
        twilio_account_sid="AC_test",
        twilio_auth_token="token_test",
        twilio_phone_number="+10000000000",
        deepgram_api_key="dg_test",
        public_url="https://judge_it.example.com",
        call_transport="text",
        osv_access_token=token,
        **kw,
    )


def _scenario(region: Optional[str] = None) -> Scenario:
    return Scenario(
        name="enrich-test",
        target="+18664530054",
        vars={},
        persona_rendered="caller",
        evaluation_prompt_rendered="judge",
        llm=LLMConfig(),
        stt=STTConfig(),
        tts=TTSConfig(),
        region=region,
    )


@dataclass
class FakeTransport:
    interaction_uuid: str = "int-uuid-1"
    client_uuid: Optional[str] = "client-uuid-1"


@dataclass
class FakeOSV:
    """Records how it was constructed, then returns/raises a scripted payload."""

    payload: Any = None
    raises: Optional[BaseException] = None
    constructed: dict = field(default_factory=dict)
    called_with: list = field(default_factory=list)

    async def get_conversations(self, client_uuid, interaction_uuid):
        self.called_with.append((client_uuid, interaction_uuid))
        if self.raises is not None:
            raise self.raises
        return self.payload


def _install(monkeypatch, fake: FakeOSV) -> None:
    import judge_it.services.osv.client as osv_client

    def _ctor(**kwargs):
        fake.constructed = kwargs
        return fake

    monkeypatch.setattr(osv_client, "OSVClient", _ctor)


async def _enrich(monkeypatch, fake, *, settings=None, scenario=None, result=None,
                  transport=None) -> CallResult:
    from judge_it.orchestrator.runner import TestRunner

    _install(monkeypatch, fake)
    runner = TestRunner.__new__(TestRunner)  # bypass __init__: no Twilio/TTS/DB
    runner.settings = settings or _settings()
    result = result or CallResult(id="r1", session_id="r1", flow_name="f", target="+1")
    await runner._enrich_text_result_from_osv(
        scenario=scenario or _scenario(),
        transport=transport or FakeTransport(),
        result=result,
    )
    return result


# ------------------------------------------------------------------ happy paths

@pytest.mark.asyncio
async def test_recovers_tool_calls_the_http_response_omitted(monkeypatch):
    fake = FakeOSV(
        payload={
            "conversations": [
                {"type": "assistant", "message": "one moment"},
                {
                    "type": "tool",
                    "message": "",
                    "tool_calls": [{"function": {"name": "get_account"}}],
                },
                {"type": "assistant", "message": "found it"},
            ]
        }
    )
    result = await _enrich(monkeypatch, fake)

    assert fake.called_with == [("client-uuid-1", "int-uuid-1")]
    assert [c["function"]["name"] for c in result.tool_calls] == ["get_account"]
    assert result.tool_calls[0]["_turn_index"] == 1


@pytest.mark.asyncio
async def test_flags_the_tag_only_bot_turn_that_reached_the_caller_as_silence(monkeypatch):
    fake = FakeOSV(
        payload=[
            {"type": "assistant", "message": "Last four of your SSN?"},
            {"type": "user", "message": "1234"},
            {"type": "assistant", "message": "<LANG:en-US>"},
        ]
    )
    result = await _enrich(monkeypatch, fake)

    assert result.tag_only_bot_turns == [
        {"turn_index": 2, "tags": ["<LANG:en-US>"], "raw": "<LANG:en-US>"}
    ]


@pytest.mark.asyncio
async def test_a_clean_conversation_flags_nothing(monkeypatch):
    fake = FakeOSV(
        payload=[
            {"type": "assistant", "message": "<LANG:en-US> Hello, Michelle here."},
            {"type": "user", "message": "Hi."},
        ]
    )
    result = await _enrich(monkeypatch, fake)

    assert result.tag_only_bot_turns == []
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_richer_http_tool_calls_are_not_clobbered(monkeypatch):
    """If the runtime ever starts returning tool_calls, don't lose them."""
    fake = FakeOSV(payload=[{"type": "assistant", "message": "hi"}])
    result = CallResult(id="r1", session_id="r1", flow_name="f", target="+1")
    result.tool_calls = [{"name": "from_http"}]

    await _enrich(monkeypatch, fake, result=result)
    assert result.tool_calls == [{"name": "from_http"}]


@pytest.mark.asyncio
async def test_region_is_applied_to_the_osv_base_url(monkeypatch):
    """A run against the India runtime must not look itself up in the US."""
    fake = FakeOSV(payload=[])
    await _enrich(
        monkeypatch, fake,
        settings=_settings(osv_base_url="https://llm-studio.judge_it.dev/us/api/interface"),
        scenario=_scenario(region="in"),
    )
    assert fake.constructed["base_url"] == "https://llm-studio.judge_it.dev/in/api/interface"
    assert fake.constructed["token"] == "osv_token"


# ---------------------------------------------------------------- failure paths

@pytest.mark.asyncio
async def test_no_token_skips_silently_without_constructing_a_client(monkeypatch):
    fake = FakeOSV(raises=AssertionError("must not be called"))
    result = await _enrich(monkeypatch, fake, settings=_settings(token=""))

    assert fake.called_with == []
    assert result.tool_calls == []
    assert result.tag_only_bot_turns == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport",
    [
        FakeTransport(interaction_uuid="", client_uuid="c"),
        FakeTransport(interaction_uuid="i", client_uuid=None),
        FakeTransport(interaction_uuid="", client_uuid=None),
    ],
)
async def test_missing_correlation_key_skips(monkeypatch, transport):
    fake = FakeOSV(raises=AssertionError("must not be called"))
    await _enrich(monkeypatch, fake, transport=transport)
    assert fake.called_with == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("OSV 401 Invalid token"),
        TimeoutError("read timeout"),
        ValueError("not json"),
    ],
)
async def test_a_failed_fetch_never_fails_the_run(monkeypatch, boom):
    """The call already completed. A diagnostic must not retroactively break it."""
    fake = FakeOSV(raises=boom)
    result = CallResult(id="r1", session_id="r1", flow_name="f", target="+1")
    result.modules_visited = ["GREETINGS", "AUTH"]

    await _enrich(monkeypatch, fake, result=result)

    assert result.modules_visited == ["GREETINGS", "AUTH"]  # untouched
    assert result.tool_calls == []
    assert result.tag_only_bot_turns == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {}, [], "surprise", {"unexpected": "shape"}])
async def test_an_unrecognised_payload_is_a_warning_not_a_crash(monkeypatch, payload):
    result = await _enrich(monkeypatch, FakeOSV(payload=payload))
    assert result.tool_calls == []
    assert result.tag_only_bot_turns == []


def test_enrichment_is_called_from_finally_not_the_success_path():
    """Placement regression guard, checked against the real source.

    The Veros IB 2.0 hang ends in a ReadTimeout -> TextTransportError, and that
    is the single most important case for the tag-only diagnosis — so the call
    must sit in the text block's `finally`, not on the success path. Asserting
    the shape of a re-implemented try/finally here would prove nothing, so this
    reads `runner.py` and checks the ordering directly.
    """
    import inspect
    import re

    from judge_it.orchestrator.runner import TestRunner

    src = inspect.getsource(TestRunner._run_v2_scenario_body)
    call = src.index("self._enrich_text_result_from_osv(")

    # The `finally:` that owns the text-transport block is the last one before
    # the call; `close()` follows, so enrichment sees the transport alive.
    finallys = [m.start() for m in re.finditer(r"^\s+finally:", src, re.M)]
    assert any(f < call for f in finallys), "enrichment must be inside a `finally:`"

    except_clauses = [m.start() for m in re.finditer(r"^\s+except .*:", src, re.M)]
    preceding_finally = max(f for f in finallys if f < call)
    assert not any(
        preceding_finally < e < call for e in except_clauses
    ), "an `except` between the `finally:` and the call means it is not in that finally"

    assert call < src.index("await text_transport.close()"), (
        "enrichment must run before close()"
    )


# ------------------------------------------------- token fallback (same host only)

_LLM_STUDIO = "https://llm-studio.judge_it.dev/us/api/interface"


@pytest.mark.asyncio
async def test_falls_back_to_the_text_runtime_token_on_the_same_host(monkeypatch):
    """Both are one llm-studio JWT in practice — without this the feature ships off."""
    fake = FakeOSV(payload=[])
    await _enrich(
        monkeypatch, fake,
        settings=_settings(
            token="",
            osv_base_url=_LLM_STUDIO,
            text_runtime_base_url="https://llm-studio.judge_it.dev/us/api/text",
            text_runtime_token="runtime_jwt",
        ),
    )
    assert fake.constructed["token"] == "runtime_jwt"
    assert fake.called_with == [("client-uuid-1", "int-uuid-1")]


@pytest.mark.asyncio
async def test_does_not_send_a_local_runtime_token_to_llm_studio(monkeypatch):
    """The text runtime defaults to localhost:8001. Its token is not OSV's."""
    fake = FakeOSV(raises=AssertionError("must not be called"))
    await _enrich(
        monkeypatch, fake,
        settings=_settings(
            token="",
            osv_base_url=_LLM_STUDIO,
            text_runtime_base_url="http://localhost:8001",
            text_runtime_token="local_dev_token",
        ),
    )
    assert fake.called_with == []


@pytest.mark.asyncio
async def test_explicit_osv_token_wins_over_the_runtime_token(monkeypatch):
    fake = FakeOSV(payload=[])
    await _enrich(
        monkeypatch, fake,
        settings=_settings(
            token="explicit_osv",
            osv_base_url=_LLM_STUDIO,
            text_runtime_base_url="https://llm-studio.judge_it.dev/us/api/text",
            text_runtime_token="runtime_jwt",
        ),
    )
    assert fake.constructed["token"] == "explicit_osv"


@pytest.mark.asyncio
async def test_no_token_anywhere_still_skips(monkeypatch):
    fake = FakeOSV(raises=AssertionError("must not be called"))
    await _enrich(
        monkeypatch, fake,
        settings=_settings(token="", osv_base_url=_LLM_STUDIO, text_runtime_token=""),
    )
    assert fake.called_with == []


@pytest.mark.asyncio
async def test_the_region_is_applied_to_both_urls_before_the_host_check(monkeypatch):
    """An India-pinned run must compare in-region hosts, and fetch in-region."""
    fake = FakeOSV(payload=[])
    await _enrich(
        monkeypatch, fake,
        settings=_settings(
            token="",
            osv_base_url=_LLM_STUDIO,
            text_runtime_base_url="https://llm-studio.judge_it.dev/us/api/text",
            text_runtime_token="runtime_jwt",
        ),
        scenario=_scenario(region="in"),
    )
    assert fake.constructed["base_url"] == "https://llm-studio.judge_it.dev/in/api/interface"
    assert fake.constructed["token"] == "runtime_jwt"
