"""Sandboxed Jinja rendering for scenario templates (P3.2).

Personas and evaluation prompts are Jinja templates over `vars`. From a YAML
file on disk the author is semi-trusted; over HTTP (the V3 run API) the
template is input. Same renderer either way — one rule set, no "trusted path"
that quietly diverges from the validated one.

What the sandbox buys, concretely:

* `SandboxedEnvironment` blocks the attribute-walk escapes
  (`{{ ''.__class__.__mro__ }}` and friends) with `SecurityError`.
* The globals are CLEARED. The template context is `vars`, full stop — no
  `range`, `lipsum`, `cycler`, `namespace`. This is also the CPU story:
  without `range` a loop can only iterate over data the caller supplied, so
  iteration count is bounded by payload size, which the API already caps.
* `**` is rejected and sequence repetition is bounded (`{{ 'x' * 10**9 }}`
  would otherwise build a gigabyte string in one opcode, before any output
  cap could see it).
* Template size is capped before parse; output size is capped during render
  (streamed, so an overrun aborts early instead of materialising first).
* `StrictUndefined` stays: a typo'd `{{ vars.foo }}` names the missing key.

The render-time cap is enforced between output chunks. That is honest
best-effort, not preemption — but with the globals gone, the only way to burn
CPU without producing chunks is macro recursion, which Python's recursion
limit turns into `RecursionError` (caught below). Verified by the tests.

The scenario corpus (1,148 files) uses `{% if %}`, `{% for %}` over vars,
`{% macro %}` and `loop.*` — all untouched by these rules.
"""
from __future__ import annotations

import time

from jinja2 import StrictUndefined, TemplateSyntaxError
from jinja2.exceptions import UndefinedError
from jinja2.sandbox import SandboxedEnvironment, SecurityError

# A persona is prose — kilobytes. 64KB is ~30 pages of text: anything larger
# is a mistake or an attack, and either way not a scenario.
MAX_TEMPLATE_BYTES = 64 * 1024
# Rendered output may legitimately exceed the template (loops over vars), but
# not by this much.
MAX_OUTPUT_BYTES = 256 * 1024
# Checked between output chunks — see module docstring for what this can and
# cannot interrupt.
MAX_RENDER_SECONDS = 2.0

# Bound for `sequence * n` repetition inside a template.
_MAX_REPEAT = 1_000


class RenderError(ValueError):
    """A template was rejected. `error_type` is the stable machine-readable
    code the V3 API surfaces in its 422 envelope (P3.5); `str(err)` is the
    human sentence."""

    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


class _ScenarioEnvironment(SandboxedEnvironment):
    """Sandbox plus the two operator rules the base class doesn't enforce."""

    intercepted_binops = frozenset(["**", "*"])

    def call_binop(self, context, operator, left, right):
        if operator == "**":
            raise SecurityError("`**` is not allowed in scenario templates")
        if operator == "*":
            for seq, count in ((left, right), (right, left)):
                if isinstance(seq, (str, bytes, list, tuple)) and isinstance(
                    count, int
                ) and count > _MAX_REPEAT:
                    raise SecurityError(
                        f"sequence repetition over {_MAX_REPEAT}x is not allowed "
                        f"in scenario templates (got *{count})"
                    )
        return super().call_binop(context, operator, left, right)


def build_environment() -> SandboxedEnvironment:
    """The one environment scenario templates render in — loader and API alike."""
    env = _ScenarioEnvironment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    # The context is `vars`, nothing ambient. Clearing globals is what removes
    # `range` — see module docstring for why that is the CPU bound.
    env.globals.clear()
    return env


def render_sandboxed(
    template_str: str,
    vars_data: dict,
    field_name: str,
    *,
    max_template_bytes: int = MAX_TEMPLATE_BYTES,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    max_render_seconds: float = MAX_RENDER_SECONDS,
) -> str:
    """Render `template_str` with `vars=vars_data`, or raise RenderError.

    Error types: `template_too_large`, `template_syntax`, `sandbox_violation`,
    `unresolved_variable`, `output_too_large`, `render_timeout`,
    `template_recursion`.
    """
    size = len(template_str.encode("utf-8", errors="replace"))
    if size > max_template_bytes:
        raise RenderError(
            "template_too_large",
            f"{field_name} template is {size} bytes; the limit is "
            f"{max_template_bytes} bytes",
        )

    env = build_environment()
    try:
        template = env.from_string(template_str)
    except TemplateSyntaxError as e:
        raise RenderError(
            "template_syntax",
            f"{field_name} template does not parse: {e.message} (line {e.lineno})",
        ) from e

    chunks: list[str] = []
    rendered_bytes = 0
    deadline = time.monotonic() + max_render_seconds
    try:
        for chunk in template.stream(vars=vars_data):
            rendered_bytes += len(chunk.encode("utf-8", errors="replace"))
            if rendered_bytes > max_output_bytes:
                raise RenderError(
                    "output_too_large",
                    f"{field_name} rendered past {max_output_bytes} bytes; "
                    f"the template loops over more data than a scenario can be",
                )
            if time.monotonic() > deadline:
                raise RenderError(
                    "render_timeout",
                    f"{field_name} took longer than {max_render_seconds}s to render",
                )
            chunks.append(chunk)
    except SecurityError as e:
        raise RenderError(
            "sandbox_violation",
            f"{field_name} template is not allowed to do that: {e}",
        ) from e
    except UndefinedError as e:
        # Kept format-compatible with the historical loader message, which
        # tests and humans both pattern-match on.
        raise RenderError(
            "unresolved_variable",
            f"unresolved {{{{ vars.* }}}} reference in {field_name}: {e.message}",
        ) from e
    except RecursionError:
        raise RenderError(
            "template_recursion",
            f"{field_name} template recursed past the interpreter limit "
            f"(a macro calling itself?)",
        ) from None
    return "".join(chunks)
