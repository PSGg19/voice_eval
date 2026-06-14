"""P3.2 — sandboxed rendering.

The PRD's acceptance tests live here verbatim:
* `{{ ''.__class__.__mro__ }}` is rejected (sandbox escape)
* a 1MB template is rejected at submit
* a typo'd `{{ vars.foo }}` is an error naming the missing key

Plus the corpus-compatibility guarantee: everything the 1,148 scenario files
actually use (`if`/`for` over vars, `macro`, `loop.*`) still renders.
"""
from __future__ import annotations

import pytest

from opkura.core.rendering import (
    MAX_TEMPLATE_BYTES,
    RenderError,
    render_sandboxed,
)


# ---------------------------------------------------------------- happy path

def test_plain_vars_render():
    out = render_sandboxed("Hi {{ vars.name }}!", {"name": "Iris"}, "persona")
    assert out == "Hi Iris!"


def test_corpus_constructs_still_render():
    """The exact constructs found in the scenario corpus."""
    template = (
        "{% macro amt(v) %}${{ v }}{% endmacro %}"
        "{% if vars.vip %}VIP {% endif %}"
        "{% for a in vars.accounts %}{{ amt(a) }}"
        "{% if not loop.last %}, {% endif %}{% endfor %}"
    )
    out = render_sandboxed(
        template, {"vip": True, "accounts": [10, 20]}, "persona"
    )
    assert out == "VIP $10, $20"


def test_trailing_newline_kept():
    # keep_trailing_newline=True is loader behaviour scenarios rely on.
    assert render_sandboxed("x\n", {}, "persona") == "x\n"


def test_filters_still_work():
    out = render_sandboxed(
        "{{ vars.names | join(', ') | upper }}", {"names": ["a", "b"]}, "persona"
    )
    assert out == "A, B"


# ------------------------------------------------------------ PRD acceptance

def test_mro_escape_rejected():
    with pytest.raises(RenderError) as exc:
        render_sandboxed("{{ ''.__class__.__mro__ }}", {}, "persona")
    assert exc.value.error_type == "sandbox_violation"


def test_one_megabyte_template_rejected():
    with pytest.raises(RenderError) as exc:
        render_sandboxed("x" * (1024 * 1024), {}, "persona")
    assert exc.value.error_type == "template_too_large"
    assert str(MAX_TEMPLATE_BYTES) in str(exc.value)


def test_typo_names_the_missing_key():
    with pytest.raises(RenderError) as exc:
        render_sandboxed("{{ vars.foo }}", {"bar": 1}, "persona")
    assert exc.value.error_type == "unresolved_variable"
    assert "foo" in str(exc.value)
    # Format-compatible with the historical loader message.
    assert "unresolved {{ vars.* }} reference in persona" in str(exc.value)


# ------------------------------------------------------------------- attacks

def test_vars_class_attribute_walk_rejected():
    with pytest.raises(RenderError) as exc:
        render_sandboxed("{{ vars.__class__ }}", {}, "persona")
    # StrictUndefined + sandbox: either way it must not resolve.
    assert exc.value.error_type in ("sandbox_violation", "unresolved_variable")


def test_range_is_gone():
    """Globals are cleared — `range` is the CPU bomb enabler and must not exist."""
    with pytest.raises(RenderError) as exc:
        render_sandboxed("{% for i in range(10) %}x{% endfor %}", {}, "persona")
    assert exc.value.error_type == "unresolved_variable"


@pytest.mark.parametrize("name", ["lipsum", "cycler", "namespace", "dict"])
def test_other_default_globals_are_gone(name):
    with pytest.raises(RenderError):
        render_sandboxed("{{ %s() }}" % name, {}, "persona")


def test_power_operator_rejected():
    with pytest.raises(RenderError) as exc:
        render_sandboxed("{{ 2 ** 30 }}", {}, "persona")
    assert exc.value.error_type == "sandbox_violation"


def test_string_repetition_bomb_rejected():
    with pytest.raises(RenderError) as exc:
        render_sandboxed("{{ 'x' * 100000 }}", {}, "persona")
    assert exc.value.error_type == "sandbox_violation"


def test_small_repetition_allowed():
    assert render_sandboxed("{{ '-' * 10 }}", {}, "persona") == "-" * 10


def test_output_cap_aborts_early():
    # 100 chunks of 10KB = ~1MB output from a small template + small vars:
    # must abort at the cap, not materialise the megabyte first.
    template = "{% for row in vars.rows %}{{ row }}{% endfor %}"
    rows = ["y" * 10_000] * 100
    with pytest.raises(RenderError) as exc:
        render_sandboxed(
            template, {"rows": rows}, "persona", max_output_bytes=50_000
        )
    assert exc.value.error_type == "output_too_large"


def test_macro_recursion_becomes_render_error():
    template = "{% macro f() %}{{ f() }}{% endmacro %}{{ f() }}"
    with pytest.raises(RenderError) as exc:
        render_sandboxed(template, {}, "persona")
    assert exc.value.error_type in ("template_recursion", "sandbox_violation")


def test_syntax_error_reported_with_line():
    with pytest.raises(RenderError) as exc:
        render_sandboxed("{% if x %}no end", {}, "persona")
    assert exc.value.error_type == "template_syntax"


def test_render_error_is_a_value_error():
    """Loader callers catch ValueError; RenderError must remain one."""
    assert issubclass(RenderError, ValueError)
