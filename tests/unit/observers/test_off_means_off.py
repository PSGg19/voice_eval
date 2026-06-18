"""P5 acceptance: with TRACING_PROVIDER=none, no OTel module is imported and
no observer is built. Subprocess, because this test suite itself imports
opentelemetry elsewhere — sys.modules must be judged in a clean interpreter."""
from __future__ import annotations

import subprocess
import sys

SCRIPT = """
import os, sys
os.environ["TRACING_PROVIDER"] = "none"
os.environ.pop("PHOENIX_ENABLED", None)
os.environ.pop("LANGFUSE_ENABLED", None)
from opkura.observers import build_observers
observers = build_observers()
assert observers == [], f"expected no observers, got {observers}"
polluted = [m for m in sys.modules if m.startswith("opentelemetry")]
assert not polluted, f"otel imported despite TRACING_PROVIDER=none: {polluted}"
print("OFF_IS_OFF")
"""


def test_none_imports_no_otel_and_builds_no_observers():
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "OFF_IS_OFF" in proc.stdout
