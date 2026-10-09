"""The one selection pattern every optional backend uses.

Its properties matter more than any individual backend: a test harness that
refuses to start because its tracing sidecar is down is a worse tool than one
that runs untraced and says so.
"""
from __future__ import annotations

import logging

import pytest

from vjudge.connectors import select


def _make_factory(val: str):
    return lambda: val


BASIC = {"none": _make_factory("NOOP"), "redis": _make_factory("REDIS")}


def test_default_when_unset(monkeypatch):
    monkeypatch.delenv("X_BACKEND", raising=False)
    assert select(kind="k", env_var="X_BACKEND", default="none", factories=BASIC) == "NOOP"


def test_env_selects(monkeypatch):
    monkeypatch.setenv("X_BACKEND", "redis")
    assert select(kind="k", env_var="X_BACKEND", default="none", factories=BASIC) == "REDIS"


@pytest.mark.parametrize("raw", ["REDIS", " redis ", "Redis"])
def test_env_value_is_normalised(monkeypatch, raw):
    monkeypatch.setenv("X_BACKEND", raw)
    assert select(kind="k", env_var="X_BACKEND", default="none", factories=BASIC) == "REDIS"


def test_override_beats_env(monkeypatch):
    """The option gate — a CLI flag or per-run config wins over the environment."""
    monkeypatch.setenv("X_BACKEND", "redis")
    assert select(kind="k", env_var="X_BACKEND", default="none",
                  factories=BASIC, override="none") == "NOOP"


def test_unknown_name_falls_back_and_says_so(monkeypatch, caplog):
    monkeypatch.setenv("X_BACKEND", "postgres-ish")
    with caplog.at_level(logging.WARNING, logger="vjudge.connectors"):
        assert select(kind="k", env_var="X_BACKEND", default="none", factories=BASIC) == "NOOP"
    assert "not a recognised" in caplog.text
    assert "none, redis" in caplog.text  # tells you what IS valid


def test_a_failing_backend_degrades_rather_than_crashing(caplog):
    """A missing package or an unreachable host must not fail the run."""
    def boom():
        raise ImportError("No module named 'opentelemetry'")

    with caplog.at_level(logging.WARNING, logger="vjudge.connectors"):
        got = select(kind="tracing", env_var="X_BACKEND", default="none",
                     factories={"none": _make_factory("NOOP"), "phoenix": boom}, override="phoenix")
    assert got == "NOOP"
    assert "degraded, not fatal" in caplog.text


def test_a_failing_DEFAULT_backend_does_raise():
    """Degrading to the default is fine; a broken default is a real bug."""
    def boom():
        raise RuntimeError("cannot open the database")

    with pytest.raises(RuntimeError):
        select(kind="store", env_var="X_BACKEND", default="sqlite",
               factories={"sqlite": boom}, override="sqlite")


def test_factories_are_lazy():
    """Off must cost nothing — an unchosen factory is never called."""
    called = []
    select(kind="k", env_var="X_BACKEND", default="none",
           factories={"none": _make_factory("NOOP"), "heavy": lambda: called.append(1)},
           override="none")
    assert called == []
