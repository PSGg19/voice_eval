"""Unit tests for the text transport (`call_transport="text"`).

Hermetic: no HTTP, no OSV, no LLM. `httpx.AsyncClient.post` is patched so the
tests assert on the request opkura *builds* and how it interprets the reply.

The behaviours pinned here are the ones observed live against the text runtime
on 2026-08-05 — see docs/dual-transport.md:

  * `channel` must be VOICE, or a voice bot's flow is not the one exercised.
  * A wrong channel comes back as HTTP **200** with `error` set, so the body has
    to be inspected rather than the status code.
  * Reusing a `recipient_id` silently continues the previous conversation, so
    the id must vary per run.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from opkura.core.config import Settings
from opkura.core.models import LLMConfig, STTConfig, Scenario, TTSConfig
from opkura.transport.text import (
    TextTransport,
    TextTransportError,
    resolve_recipient_id,
)


def _make_settings(**overrides) -> Settings:
    base = dict(
        # Hermetic — otherwise a developer .env carrying
        # AUTOCALLS_CALL_TRANSPORT=sip or a real token leaks into assertions.
        _env_file=None,
        twilio_account_sid="AC_test",
        twilio_auth_token="token_test",
        twilio_phone_number="+10000000000",
        deepgram_api_key="dg_test",
        public_url="https://opkura.example.com",
        call_transport="text",
    )
    base.update(overrides)
    return Settings(**base)


def _make_scenario(**overrides) -> Scenario:
    base = dict(
        recipient_id="2015550137",
        name="Resurgent · Auth · A-001 happy path",
        target="+18664530054",
        vars={"name": "Casey Italia", "zip": "10073"},
        persona_rendered="you are a caller",
        evaluation_prompt_rendered="judge it",
        llm=LLMConfig(),
        stt=STTConfig(),
        tts=TTSConfig(),
        max_turns=30,
    )
    base.update(overrides)
    return Scenario(**base)


def _reply(**overrides) -> dict:
    """A minimal successful post_message_web body."""
    body = {
        "output_message": "Hi, this is Michelle from Resurgent Capital Services.",
        "error": "",
        "current_module": "GREETINGS",
        "tool_calls": [],
        "message_type": "RECORD",
        "total_response_time": 2.974,
        "interaction_uuid": "7ca583dd-f874-4bc4-8e7f-6996c7bf9cec",
        "client_uuid": "bce86fb8-4d25-4f90-8211-186c1e82f2e6",
        "interaction_metadata": {
            "llm_studio_url": "https://llm-studio.skit.ai/clients/x/conversations?interaction_uuid=y",
            "grafana_url": "https://grafana.us.skit.ai/explore?x=1",
        },
        "is_test": True,
    }
    body.update(overrides)
    return body


def _fake_post(bodies):
    """Patch AsyncClient.post to return `bodies` in order; records the payloads.

    Patching the class attribute means the replacement is bound, so it takes
    `self` first — without that, httpx's `json=` kwarg collides with the `url`
    positional.
    """
    sent: list[dict] = []
    queue = list(bodies)

    async def _post(_self, url, json=None, **kwargs):  # noqa: A002 - httpx kwarg name
        sent.append(json)
        body = queue.pop(0)
        return SimpleNamespace(
            status_code=200,
            json=lambda: body,
            text=_dumps(body),
            request=None,
        )

    return _post, sent


def _dumps(obj) -> str:
    import json as _json

    return _json.dumps(obj)


# ----------------------------------------------------------------------
# recipient_id — explicit in the YAML, never generated


def test_recipient_id_comes_from_the_scenario_verbatim():
    """No normalisation. It is compared byte-for-byte against
    Account.phone_number, so opkura must not "helpfully" reformat it."""
    assert resolve_recipient_id(_make_scenario(recipient_id="7145551212")) == "7145551212"
    assert resolve_recipient_id(_make_scenario(recipient_id="a@b.com")) == "a@b.com"


def test_missing_recipient_id_is_a_hard_error():
    """It becomes the bot's phone_number and reaches the client's CRM, so a
    generated value is a value opkura invented into someone's records. An
    earlier default slug did exactly that, reaching a production workflow API."""
    with pytest.raises(TextTransportError, match="recipient_id"):
        resolve_recipient_id(_make_scenario(recipient_id=None))


# ----------------------------------------------------------------------
# request payload


@pytest.mark.asyncio
async def test_first_turn_sends_channel_voice_is_test_and_no_interaction_uuid():
    transport = TextTransport(_make_settings(), _make_scenario(), "sess1234abcd")
    post, sent = _fake_post([_reply()])

    with patch("httpx.AsyncClient.post", new=post):
        turn = await transport.start()

    payload = sent[0]
    # channel=VOICE is the whole point: get_channel_flow has no fallback, so
    # WEB_CHAT would exercise a different flow (or none).
    assert payload["channel"] == "VOICE"
    # False by default — llm-studio's conversations view filters on
    # is_test=false, so true would make the run unopenable in the UI.
    assert payload["is_test"] is False
    assert payload["user_message"] == ""
    assert "interaction_uuid" not in payload, "turn 1 must let OSV mint the interaction"
    # Scenario vars ride along as interaction_metadata, plus the inbound marker.
    assert payload["interaction_metadata"]["name"] == "Casey Italia"
    assert payload["interaction_metadata"]["call_type"] == "inbound"

    assert turn.text.startswith("Hi, this is Michelle")
    assert turn.current_module == "GREETINGS"
    # total_response_time is seconds on the wire, milliseconds internally.
    assert turn.reasoning_ms == pytest.approx(2974.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting, scenario_value, expected",
    [
        (False, None, False),   # deployment default, nothing pinned
        (True, None, True),     # deployment opts in
        (False, True, True),    # scenario opts in despite the default
        (True, False, False),   # scenario opts OUT despite the default
    ],
)
async def test_is_test_scenario_beats_the_deployment_default(
    setting, scenario_value, expected
):
    """`is_test: false` in YAML must survive.

    The bug this guards against is resolving with `or`: `scenario.is_test or
    settings.text_is_test` turns an explicit `false` into the setting's value,
    which is the exact opposite of what the scenario asked for.
    """
    transport = TextTransport(
        _make_settings(text_is_test=setting),
        _make_scenario(is_test=scenario_value),
        "sess1234abcd",
    )
    post, sent = _fake_post([_reply()])

    with patch("httpx.AsyncClient.post", new=post):
        await transport.start()

    assert sent[0]["is_test"] is expected


@pytest.mark.asyncio
async def test_second_turn_echoes_the_interaction_uuid_back():
    transport = TextTransport(_make_settings(), _make_scenario(), "sess1234abcd")
    post, sent = _fake_post([_reply(), _reply(output_message="And your ZIP?")])

    with patch("httpx.AsyncClient.post", new=post):
        await transport.start()
        await transport.send(speech="Casey Italia")

    assert "interaction_uuid" not in sent[0]
    assert sent[1]["interaction_uuid"] == "7ca583dd-f874-4bc4-8e7f-6996c7bf9cec"
    assert sent[1]["user_message"] == "Casey Italia"


@pytest.mark.asyncio
async def test_reset_interaction_is_sent_on_the_first_turn_only():
    """Guards the gotcha from both sides.

    Turn 1 must reset, so a recipient_id that somehow repeats cannot resume a
    stale conversation. Turns 2..N must NOT — sending it every turn would wipe
    the conversation on each request and the bot would loop on its greeting.
    """
    transport = TextTransport(_make_settings(), _make_scenario(), "sess1234abcd")
    post, sent = _fake_post([_reply(), _reply(output_message="And your ZIP?")])

    with patch("httpx.AsyncClient.post", new=post):
        await transport.start()
        await transport.send(speech="Casey Italia")

    assert sent[0]["reset_interaction"] is True
    assert "reset_interaction" not in sent[1]


@pytest.mark.asyncio
async def test_virtual_id_used_when_no_assistant_uuid_configured():
    transport = TextTransport(_make_settings(), _make_scenario(), "sess1234abcd")
    post, sent = _fake_post([_reply()])

    with patch("httpx.AsyncClient.post", new=post):
        await transport.start()

    # Keeps one YAML portable across transports: the same `target` the voice
    # path dials is what OSV resolves the assistant from.
    assert sent[0]["virtual_id"] == "+18664530054"
    assert "assistant_uuid" not in sent[0]


@pytest.mark.asyncio
async def test_scenario_names_its_own_bot_and_version():
    """The scale requirement: WHICH bot is a property of the scenario.

    With these as env globals a single process could only ever address one
    assistant, so a campaign spanning two bots was impossible and a version
    could not be pinned per scenario.
    """
    settings = _make_settings(
        text_assistant_uuid="00000000-dead-beef-0000-000000000000",
        text_fc_version=3,
    )
    scenario = _make_scenario(
        assistant_uuid="00000000-0000-4000-8000-00000000dead", fc_version=10
    )
    transport = TextTransport(settings, scenario, "sess1234abcd")
    post, sent = _fake_post([_reply()])

    with patch("httpx.AsyncClient.post", new=post):
        await transport.start()

    assert sent[0]["assistant_uuid"] == "00000000-0000-4000-8000-00000000dead"
    assert sent[0]["fc_version"] == 10


@pytest.mark.asyncio
async def test_scenario_falls_back_to_the_deployment_bot():
    settings = _make_settings(
        text_assistant_uuid="00000000-dead-beef-0000-000000000000",
        text_fc_version=3,
    )
    transport = TextTransport(settings, _make_scenario(), "sess1234abcd")
    post, sent = _fake_post([_reply()])

    with patch("httpx.AsyncClient.post", new=post):
        await transport.start()

    assert sent[0]["assistant_uuid"] == "00000000-dead-beef-0000-000000000000"
    assert sent[0]["fc_version"] == 3


@pytest.mark.asyncio
async def test_fc_version_zero_in_yaml_means_current_not_the_env_default():
    """0 is MEANINGFUL — "whatever OSV considers current".

    Resolving with `or` would let the deployment's pin leak back in and the
    scenario would silently run a version it explicitly opted out of.
    """
    settings = _make_settings(text_fc_version=9)
    transport = TextTransport(
        settings, _make_scenario(fc_version=0), "sess1234abcd"
    )
    post, sent = _fake_post([_reply()])

    with patch("httpx.AsyncClient.post", new=post):
        await transport.start()

    assert transport.fc_version == 0
    assert "fc_version" not in sent[0], "0 == current, which is the absent case"


@pytest.mark.asyncio
async def test_assistant_uuid_is_added_but_virtual_id_still_required():
    settings = _make_settings(
        text_assistant_uuid="08432036-470c-4941-98b3-2b12c27fcef5",
        text_fc_version=9,
    )
    transport = TextTransport(settings, _make_scenario(), "sess1234abcd")
    post, sent = _fake_post([_reply()])

    with patch("httpx.AsyncClient.post", new=post):
        await transport.start()

    assert sent[0]["assistant_uuid"] == "08432036-470c-4941-98b3-2b12c27fcef5"
    assert sent[0]["fc_version"] == 9
    # virtual_id rides along even when the uuid is explicit: OSV writes it to a
    # NOT NULL column, and omitting it returns a NotNullViolation as HTTP 200.
    assert sent[0]["virtual_id"] == "+18664530054"


@pytest.mark.asyncio
async def test_dtmf_is_sent_as_the_user_message_and_wins_over_speech():
    transport = TextTransport(_make_settings(), _make_scenario(), "sess1234abcd")
    post, sent = _fake_post([_reply(), _reply()])

    with patch("httpx.AsyncClient.post", new=post):
        await transport.start()
        await transport.send(speech="ignored", dtmf="4000000000000002")

    # Over voice the collected digits reach the model as a TranscriptionFrame —
    # i.e. as text — so posting them verbatim is equivalent.
    assert sent[1]["user_message"] == "4000000000000002"


@pytest.mark.asyncio
async def test_empty_caller_turn_is_refused():
    transport = TextTransport(_make_settings(), _make_scenario(), "sess1234abcd")
    post, _ = _fake_post([_reply()])

    with patch("httpx.AsyncClient.post", new=post):
        await transport.start()
        # Sending "" would re-trigger the greeting path and corrupt the turn
        # sequence, so it must fail loudly.
        with pytest.raises(TextTransportError, match="empty caller turn"):
            await transport.send(speech="", dtmf="")


# ----------------------------------------------------------------------
# soft failures arrive as HTTP 200


@pytest.mark.asyncio
async def test_wrong_channel_soft_failure_raises_despite_http_200():
    """The exact shape observed for a voice bot driven with channel=WEB_CHAT."""
    transport = TextTransport(_make_settings(), _make_scenario(), "sess1234abcd")
    post, _ = _fake_post([
        _reply(
            output_message="I apologize, but I encountered an error processing your message.",
            error="TypeError(\"'NoneType' object is not subscriptable\")",
            current_module="error",
            interaction_uuid=None,
            is_test=False,
        )
    ])

    with patch("httpx.AsyncClient.post", new=post):
        with pytest.raises(TextTransportError, match="soft failure"):
            await transport.start()


@pytest.mark.asyncio
async def test_error_module_alone_is_enough_to_fail():
    transport = TextTransport(_make_settings(), _make_scenario(), "sess1234abcd")
    post, _ = _fake_post([_reply(error="", current_module="error")])

    with patch("httpx.AsyncClient.post", new=post):
        with pytest.raises(TextTransportError):
            await transport.start()


# ----------------------------------------------------------------------
# correlation + debug links


@pytest.mark.asyncio
async def test_interaction_uuid_and_debug_urls_captured_from_first_reply():
    transport = TextTransport(_make_settings(), _make_scenario(), "sess1234abcd")
    post, _ = _fake_post([_reply()])

    with patch("httpx.AsyncClient.post", new=post):
        await transport.start()

    # Correlation with zero cross-service work: OSV mints it and hands it back.
    assert transport.interaction_uuid == "7ca583dd-f874-4bc4-8e7f-6996c7bf9cec"
    assert transport.client_uuid == "bce86fb8-4d25-4f90-8211-186c1e82f2e6"
    # OSV already builds these; no reason to reconstruct them.
    assert "llm_studio_url" in transport.debugging_urls
    assert "grafana_url" in transport.debugging_urls


@pytest.mark.asyncio
async def test_http_4xx_raises_with_the_body_for_triage():
    transport = TextTransport(_make_settings(), _make_scenario(), "sess1234abcd")

    async def _post(_self, url, json=None, **kwargs):  # noqa: A002
        return SimpleNamespace(
            status_code=404,
            text='{"detail":"No assistant found"}',
            json=lambda: {},
            request=None,
        )

    with patch("httpx.AsyncClient.post", new=_post):
        with pytest.raises(TextTransportError, match="404"):
            await transport.start()


def test_settings_accepts_text_transport_and_defaults():
    s = _make_settings()
    assert s.call_transport == "text"
    assert s.text_runtime_base_url == "http://localhost:8001"
    # Unpinned by default; a regression suite should set it explicitly.
    assert s.text_fc_version == 0
    assert json.dumps(s.text_runtime_token) == '""'


# --------------------------------------------------- region misroute diagnosis

_NO_ASSISTANT = (
    '{"detail":"No assistant found for '
    "request.assistant_uuid='d024a1eb-4f77-4465-9c19-9349b86240b9' and +18665821622\"}"
)


def _in_region_transport(region=None):
    return TextTransport(
        _make_settings(text_runtime_base_url="https://llm-studio.skit.ai/in/api/text"),
        _make_scenario(region=region),
        "sess1234abcd",
    )


def test_a_404_on_the_wrong_region_says_so_instead_of_blaming_the_uuid():
    """OSV's own wording sends you hunting for a bad uuid. It is the region.

    Cost two debugging sessions: the region is a path segment, assistants do
    not exist across regions, and an exported base URL outranks `.env` with
    nothing in the output saying which gateway was used.
    """
    hint = _in_region_transport()._region_hint(404, _NO_ASSISTANT)

    assert "REGION MISROUTE" in hint
    assert "`in` gateway" in hint  # the region actually used
    assert "AUTOCALLS_TEXT_RUNTIME_BASE_URL" in hint
    assert "EXPORTED variable silently outranks .env" in hint
    assert 'region: "<us|in|ca>"' in hint  # the fix, copy-pasteable
    assert "an IN gateway" in hint  # not "a IN gateway"


def test_when_the_scenario_pins_the_region_the_advice_is_to_change_that_key():
    """Telling someone to 'pin it in the scenario' when they already did is noise."""
    hint = _in_region_transport(region="in")._region_hint(404, _NO_ASSISTANT)

    assert 'the scenario\'s `region: "in"`' in hint
    assert "change that key" in hint
    assert "AUTOCALLS_TEXT_RUNTIME_BASE_URL" not in hint  # not the culprit here


@pytest.mark.parametrize(
    "status,body",
    [
        (500, "internal error"),
        (404, '{"detail":"flow config not found"}'),  # a real 404, not a misroute
        (401, _NO_ASSISTANT),
        (200, _NO_ASSISTANT),
    ],
)
def test_no_region_hint_when_it_is_not_a_region_problem(status, body):
    assert _in_region_transport()._region_hint(status, body) == ""


def test_no_region_hint_when_the_url_has_no_region_segment():
    """A local runtime has no region to be wrong about."""
    transport = TextTransport(
        _make_settings(text_runtime_base_url="http://localhost:8001"),
        _make_scenario(),
        "sess1234abcd",
    )
    assert transport._region_hint(404, _NO_ASSISTANT) == ""


def test_scenario_region_overrides_an_exported_base_url():
    """The whole point of the key: it must beat the environment, not defer to it."""
    transport = TextTransport(
        _make_settings(text_runtime_base_url="https://llm-studio.skit.ai/in/api/text"),
        _make_scenario(region="us"),
        "sess1234abcd",
    )
    assert transport.base_url == "https://llm-studio.skit.ai/us/api/text"
