"""The conversations payload is the only place tool calls and tag-only bot
replies survive — `post_message_web` drops both."""
from __future__ import annotations

import pytest

from judge_it.services.osv.conversations import (
    OsvTurn,
    flatten_tool_calls,
    parse_conversations,
    strip_tags,
    tag_only_bot_turns,
    tool_names,
)


def _turn(**kw):
    base = {"type": "assistant", "message": "hi"}
    base.update(kw)
    return base


# --------------------------------------------------------------- shape tolerance

def test_top_level_list():
    turns = parse_conversations([_turn(message="one"), _turn(type="user", message="two")])
    assert [(t.speaker, t.text) for t in turns] == [("bot", "one"), ("caller", "two")]


@pytest.mark.parametrize("key", ["conversations", "conversation", "results", "data", "turns", "messages"])
def test_envelope_keys(key):
    turns = parse_conversations({key: [_turn(message="wrapped")]})
    assert [t.text for t in turns] == ["wrapped"]


def test_one_level_of_nesting():
    turns = parse_conversations({"data": {"conversations": [_turn(message="deep")]}})
    assert [t.text for t in turns] == ["deep"]


@pytest.mark.parametrize("role_key", ["type", "role", "speaker", "sender"])
def test_role_key_spellings(role_key):
    turns = parse_conversations([{role_key: "user", "message": "x"}])
    assert turns[0].speaker == "caller"


@pytest.mark.parametrize("text_key", ["message", "text", "content", "utterance"])
def test_text_key_spellings(text_key):
    turns = parse_conversations([{"type": "assistant", text_key: "spoken"}])
    assert turns[0].text == "spoken"


@pytest.mark.parametrize(
    "payload",
    [None, "a string", 42, {}, [], {"conversations": None}, {"conversations": "nope"}, [1, 2, 3]],
)
def test_never_raises_on_junk(payload):
    """A diagnostic must not be able to fail the run that produced it."""
    assert parse_conversations(payload) == []


def test_unknown_role_is_not_guessed():
    assert parse_conversations([{"type": "wat", "message": "x"}])[0].speaker == "unknown"


def test_null_message_becomes_empty_string_not_none():
    assert parse_conversations([{"type": "assistant", "message": None}])[0].text == ""


# ------------------------------------------------------------------- tool calls

def test_tool_calls_flatten_in_turn_order_with_provenance():
    turns = parse_conversations(
        [
            _turn(message="looking that up"),
            {
                "type": "tool",
                "message": "",
                "tool_calls": [
                    {"function": {"name": "get_account", "arguments": "{}"}},
                    {"function": {"name": "get_balance", "arguments": "{}"}},
                ],
            },
            _turn(message="your balance is $40"),
        ]
    )
    assert tool_names(turns) == ["get_account", "get_balance"]
    assert [c["_turn_index"] for c in flatten_tool_calls(turns)] == [1, 1]


def test_single_unwrapped_tool_call_dict():
    turns = parse_conversations([{"type": "tool", "tool_calls": {"name": "pay_now"}}])
    assert tool_names(turns) == ["pay_now"]


def test_tool_calls_absent_or_null():
    turns = parse_conversations([_turn(), _turn(tool_calls=None), _turn(tool_calls=[])])
    assert flatten_tool_calls(turns) == []


def test_unnamed_tool_call_is_skipped_not_crashed():
    turns = parse_conversations([{"type": "tool", "tool_calls": [{"arguments": "{}"}]}])
    assert tool_names(turns) == []
    assert len(flatten_tool_calls(turns)) == 1


# ------------------------------------------------------------- tag-only replies

@pytest.mark.parametrize(
    "text,expected",
    [
        ("<LANG:en-US>", True),
        ("  <LANG:en-US>  ", True),
        ("<HANGUP>", True),
        ("<LANG:en-US><HANGUP>", True),
        ("<DTMF:1>", True),
        ("<OPT_OUT>", True),
        ("<LANG:en-US> Hello there", False),  # a real utterance, tagged
        ("Hello there", False),
        ("", False),  # genuinely empty is NOT tag-only
        ("   ", False),
        ("a < b and c > d", False),  # not a tag
    ],
)
def test_is_tag_only(text, expected):
    assert OsvTurn(index=0, speaker="bot", text=text).is_tag_only is expected


def test_the_veros_ib2_failure_shape():
    """The exact stall: the SSN lands, the bot replies with a bare <LANG:en-US>,
    OSV strips it, and judge_it's caller hears silence for ~300s.
    """
    turns = parse_conversations(
        {
            "conversations": [
                {"type": "assistant", "message": "What are the last four of your SSN?"},
                {"type": "user", "message": "1234"},
                {"type": "assistant", "message": "<LANG:en-US>"},
            ]
        }
    )
    silent = tag_only_bot_turns(turns)
    assert [t.index for t in silent] == [2]
    assert silent[0].tags == ["<LANG:en-US>"]


def test_caller_turn_that_is_tag_only_is_not_reported():
    """Only the bot going silent is a defect worth shouting about."""
    turns = parse_conversations([{"type": "user", "message": "<LANG:en-US>"}])
    assert tag_only_bot_turns(turns) == []


def test_strip_tags_leaves_the_utterance():
    assert strip_tags("<LANG:en-US>Your balance is $40.<HANGUP>") == "Your balance is $40."
