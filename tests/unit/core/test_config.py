from __future__ import annotations

import pytest
from pydantic import ValidationError

from judge_it.core.config import Settings


REQUIRED = {
    "JUDGE_IT_TWILIO_ACCOUNT_SID": "x",
    "JUDGE_IT_TWILIO_AUTH_TOKEN": "x",
    "JUDGE_IT_TWILIO_PHONE_NUMBER": "x",
    "JUDGE_IT_DEEPGRAM_API_KEY": "x",
}


def _set_required(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)


def test_from_env_reads_judge_it_prefixed_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("JUDGE_IT_GEMINI_API_KEY", "secret-gemini")
    monkeypatch.setenv("JUDGE_IT_CALLER_MODEL", "gemini-flash-test")
    monkeypatch.setenv("JUDGE_IT_EVALUATOR_MODEL", "gemini-pro-test")
    monkeypatch.setenv("JUDGE_IT_TEMPERATURE", "0.7")

    s = Settings(_env_file=None)

    assert s.gemini_api_key == "secret-gemini"
    assert s.caller_model == "gemini-flash-test"
    assert s.evaluator_model == "gemini-pro-test"
    assert s.temperature == pytest.approx(0.7)


def test_defaults_apply_when_env_unset(monkeypatch):
    _set_required(monkeypatch)
    for k in ("JUDGE_IT_CALLER_MODEL", "JUDGE_IT_EVALUATOR_MODEL", "JUDGE_IT_TEMPERATURE"):
        monkeypatch.delenv(k, raising=False)

    s = Settings(_env_file=None)

    assert s.caller_model == "gemini-2.5-flash"
    assert s.evaluator_model == "gemini-2.5-pro"
    assert s.temperature == pytest.approx(0.3)


def test_temperature_coerces_from_string(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("JUDGE_IT_TEMPERATURE", "0.9")
    assert Settings(_env_file=None).temperature == pytest.approx(0.9)


def test_invalid_temperature_raises(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("JUDGE_IT_TEMPERATURE", "abc")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_missing_required_raises(monkeypatch):
    for k in REQUIRED:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
