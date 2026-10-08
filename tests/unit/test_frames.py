"""P2.1 — the frame vocabulary is inert: frozen, behaviourless, import-clean."""
from __future__ import annotations

import dataclasses
import sys

import pytest

import vjudge.frames as frames


ALL_FRAME_TYPES = [
    frames.BotPartialSpeech, frames.BotFinalSpeech, frames.ModuleEntered,
    frames.ToolCalled, frames.BotTurnEnded,
    frames.CallerSpeech, frames.CallerDTMF, frames.CallerHangup,
    frames.ControlStarted, frames.ControlEnded, frames.ControlCancelled,
]


def test_every_frame_is_a_frozen_dataclass():
    for t in ALL_FRAME_TYPES:
        assert dataclasses.is_dataclass(t), t
        assert t.__dataclass_params__.frozen, f"{t.__name__} must be frozen"


def test_frames_are_immutable():
    f = frames.CallerSpeech(text="hello")
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.text = "mutated"  # type: ignore[misc]


def test_frames_carry_no_behaviour():
    """No methods beyond what dataclass generates — the vocabulary is data."""
    # frozen dataclasses also generate the raising __setattr__/__delattr__
    generated = {"__init__", "__repr__", "__eq__", "__hash__",
                 "__setattr__", "__delattr__"}
    for t in ALL_FRAME_TYPES:
        extra = {
            name for name, val in vars(t).items()
            if callable(val) and not name.startswith("__dataclass")
        } - generated
        assert not extra, f"{t.__name__} defines behaviour: {extra}"


def test_module_imports_no_transport_and_no_third_party():
    """The vocabulary must be loadable anywhere without dragging a transport
    (or any third-party package) into the importer."""
    # A pristine interpreter is the honest check: import ONLY the module and
    # inspect what it pulled in.
    import subprocess
    code = (
        "import sys; import vjudge.frames; "
        "bad = [m for m in sys.modules if m.startswith('vjudge.') "
        "and m not in ('vjudge', 'vjudge.frames')]; "
        "third = [m for m in sys.modules if m.split('.')[0] in "
        "('twilio', 'httpx', 'fastapi', 'websockets', 'deepgram', 'pydantic')]; "
        "assert not bad, f'frames imports vjudge modules: {bad}'; "
        "assert not third, f'frames imports third-party: {third}'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_equality_is_by_value():
    assert frames.CallerDTMF(digits="123", ts_ms=5.0) == frames.CallerDTMF(
        digits="123", ts_ms=5.0
    )
    assert frames.BotTurnEnded(reasoning_ms=10.0) != frames.BotTurnEnded(
        reasoning_ms=11.0
    )
