"""The customer-data guard.

This exists because of a specific incident: on 2026-08-24 a text scenario was
aimed at Veros CI OB (assistant 1160, 13,399 real accounts), authenticated, and
the post-call integration POSTed a real application number to the client's
production workflow API. Nothing in vjudge objected.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from vjudge.core.live_assistants import load, lookup


def _list(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "live-assistants.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


_BODY = '''
    generated_at: "2026-08-25"
    region: us
    assistants:
      - uuid: "90cf2cf7-a2be-4f29-9327-2a5c11d0f7d9"
        assistant_id: 1160
        accounts: 13399
        client: "Veros Credit - Customer Interview"
        name: "Customer Interview OB - 2.0"
    extra:
      - uuid: "11111111-1111-4111-8111-111111111111"
        assistant_id: 999
        accounts: 5
        client: "Hand added"
        name: "Known customer facing"
'''


def test_lookup_finds_a_listed_assistant(tmp_path):
    live = lookup("90cf2cf7-a2be-4f29-9327-2a5c11d0f7d9", _list(tmp_path, _BODY))
    assert live is not None
    assert live.assistant_id == 1160
    assert live.accounts == 13399
    assert "13,399 customer account(s)" in live.describe()


def test_lookup_is_case_and_whitespace_insensitive(tmp_path):
    path = _list(tmp_path, _BODY)
    assert lookup("  90CF2CF7-A2BE-4F29-9327-2A5C11D0F7D9  ", path) is not None


def test_the_extra_block_is_merged(tmp_path):
    """Hand-added entries must guard too — the query cannot know everything."""
    assert lookup("11111111-1111-4111-8111-111111111111", _list(tmp_path, _BODY))


def test_an_unlisted_assistant_is_allowed(tmp_path):
    """Zero accounts means absent, and absent is what makes one testable."""
    assert lookup("00000000-0000-4000-8000-00000000dead", _list(tmp_path, _BODY)) is None


@pytest.mark.parametrize("bad", ["", None, 123, []])
def test_a_non_uuid_never_raises(bad, tmp_path):
    assert lookup(bad, _list(tmp_path, _BODY)) is None


def test_a_missing_file_yields_an_empty_map_not_an_error(tmp_path):
    """A broken safety net must not crash a lint run over 1100 scenarios."""
    assert load(tmp_path / "nope.yaml", refresh=True) == {}


def test_malformed_yaml_yields_an_empty_map(tmp_path, caplog):
    p = tmp_path / "live-assistants.yaml"
    p.write_text("assistants: [unclosed", encoding="utf-8")
    import logging
    with caplog.at_level(logging.WARNING, logger="vjudge.core.live_assistants"):
        assert load(p, refresh=True) == {}
    # silently guarding nothing is the worst outcome, so it must be logged
    assert "INACTIVE" in caplog.text


def test_entries_without_a_uuid_are_skipped(tmp_path):
    p = _list(tmp_path, '''
        assistants:
          - assistant_id: 1
            accounts: 5
          - uuid: ""
            accounts: 5
          - uuid: "22222222-2222-4222-8222-222222222222"
            accounts: 3
    ''')
    entries = load(p, refresh=True)
    assert list(entries) == ["22222222-2222-4222-8222-222222222222"]


def test_a_non_numeric_account_count_becomes_zero_not_a_crash(tmp_path):
    p = _list(tmp_path, '''
        assistants:
          - uuid: "33333333-3333-4333-8333-333333333333"
            accounts: "lots"
    ''')
    live = lookup("33333333-3333-4333-8333-333333333333", p)
    assert live is not None and live.accounts == 0


def test_the_shipped_list_still_covers_the_incident_assistant():
    """Regression anchor: 1160 is the bot that leaked. It must stay listed."""
    live = lookup("90cf2cf7-a2be-4f29-9327-2a5c11d0f7d9")
    assert live is not None, "the assistant behind the 2026-08-24 leak is unlisted"
    assert live.assistant_id == 1160
    assert live.accounts > 0


def test_the_shipped_list_covers_an_imported_clone_with_real_accounts():
    """The trap: `Imported - ` does not mean sandbox. 1749 carries 422,515."""
    live = lookup("fd4a18f0-a5d5-4f67-96be-9c04ab11237e")
    assert live is not None
    assert live.name.startswith("Imported")
    assert live.accounts > 400_000
