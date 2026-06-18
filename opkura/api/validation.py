"""Payload validation for the V3 API (P3.5 + S4).

One rule set, two doors: `lint_scenario_data` is the SAME function the file
linter runs, so a rule added for YAML exists for HTTP by construction. On top
of lint, two API-only concerns:

* S4 — credential-shaped values are refused outright. A payload is not a
  place for a key, ever; a 422 here is cheaper than a rotated secret later.
* Sandbox render check — the PRD's "rejected at submit": the persona and
  evaluation prompt must render (with the body's own vars) inside the
  sandbox before a scenario row exists.
"""
from __future__ import annotations

import re
from typing import Iterator

from opkura.api.errors import api_error
from opkura.core.lint import lint_scenario_data

# Shapes that are secrets wherever they appear. Deliberately few and precise —
# a false 422 on innocent data teaches people to ignore the guard.
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("an OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("a JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")),
    ("a Twilio account SID", re.compile(r"\bAC[0-9a-f]{32}\b")),
    ("a bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}")),
    ("an AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
)


def _walk_strings(value, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path or "<root>", value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{i}]")


def reject_credentials(payload, *, where: str) -> None:
    """S4: refuse any payload carrying a credential-shaped string (422)."""
    for path, value in _walk_strings(payload):
        for label, pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(value):
                raise api_error(
                    422,
                    "credential_in_payload",
                    f"{where} field {path!r} contains what looks like {label}; "
                    f"credentials never travel in payloads (S4). Remove it — "
                    f"the value was not stored.",
                    field=path,
                )


def validate_scenario_body(body: dict) -> list[dict]:
    """Lint a scenario body; 422 (`lint_failed`, every code listed) on errors,
    warnings returned for the caller to surface.

    No separate render pass: lint's jinja check IS a sandboxed render with the
    scenario's own vars (P3.2's renderer), so `sandbox_violation`,
    `template_too_large` and friends arrive as lint codes. The run path
    re-renders with the RESOLVED vars — different data, its own 422.
    """
    result = lint_scenario_data(body)
    if result.errors:
        raise api_error(
            422,
            "lint_failed",
            "scenario body failed lint; every finding listed in `errors`",
            errors=[
                {"code": e.code, "message": e.message, "field": e.field}
                for e in result.errors
            ],
        )
    return [
        {"code": w.code, "message": w.message, "field": w.field}
        for w in result.warnings
    ]
