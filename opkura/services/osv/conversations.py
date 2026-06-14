"""Normalise the OSV conversations payload into something assertable.

Why this module exists — two blind spots on the text transport, one cause:

1. **`tool_calls` is always empty.** `POST /text_runtime/post_message_web`
   accepts the field name but never populates it, so `CallResult.tool_calls`
   stays `[]` even on turns that demonstrably ran a tool (verified against the
   Veros IB 2.0 runs: the account lookup fired, the response body said
   nothing). Tool-order assertions were therefore impossible.

2. **"the bot said nothing" and "the bot emitted only `<LANG:en-US>`" are the
   same thing over HTTP.** OSV strips terminal/marker tags server-side before
   it fills `output_message`, so a tag-only completion arrives as `""`. That is
   exactly the Veros IB 2.0 failure, and from opkura's side it was
   indistinguishable from a genuinely empty completion — which is why it took a
   Metabase query to diagnose. The stored turn *does* keep the raw
   `<LANG:en-US>`.

Both survive in `GET /client/{c}/interactions/conversations/{i}/`, so fetching
that once after a run recovers them.

**Shape tolerance is deliberate.** The repo already notes that OSV field names
vary by version, and the only existing consumer (`jobs/pipeline`) hands the
payload to an LLM verbatim without mapping it — so no shape has ever been
pinned here. The envelope key, the turn-role key and the message key are each
accepted in several spellings rather than guessed at. `parse_conversations` is
total: it never raises on an unexpected payload, it returns what it recognised.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Matches the marker tags TagsFrameProcessor handles on both transports:
# <LANG:en-US>, <DTMF:1>, <TRANSFER>, <HANGUP>, <OPT_OUT>.
_TAG_RE = re.compile(r"<\s*[A-Za-z_][A-Za-z0-9_]*(?::[^<>]*)?\s*>")

# Envelope keys seen or plausible for the turn list, in preference order.
_LIST_KEYS = ("conversations", "conversation", "results", "data", "turns", "messages")

# Per-turn keys. `type` is what the DB column is called; the API may render it
# as `role` or `speaker`.
_ROLE_KEYS = ("type", "role", "speaker", "sender")
_TEXT_KEYS = ("message", "text", "content", "utterance")

_BOT_ROLES = {"assistant", "bot", "agent", "system_assistant"}
_CALLER_ROLES = {"user", "caller", "human", "customer"}
_TOOL_ROLES = {"tool", "function", "tool_call", "tool_response"}


@dataclass
class OsvTurn:
    """One stored turn, with the text exactly as OSV persisted it."""

    index: int
    speaker: str  # "bot" | "caller" | "tool" | "unknown"
    text: str  # RAW — tags are NOT stripped, that is the point
    tool_calls: list[dict] = field(default_factory=list)
    module: Optional[str] = None
    error: Optional[str] = None

    @property
    def is_tag_only(self) -> bool:
        """Non-empty text that reduces to nothing once tags are removed.

        This is the signature of the Veros IB 2.0 stall: the LLM replied with a
        bare `<LANG:en-US>`, OSV stripped it, and the caller heard silence.
        """
        if not self.text.strip():
            return False
        return not strip_tags(self.text).strip()

    @property
    def tags(self) -> list[str]:
        return _TAG_RE.findall(self.text)


def strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text)


def _coerce_turn_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, dict)]
    if not isinstance(payload, dict):
        return []
    for key in _LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [t for t in value if isinstance(t, dict)]
        # Some OSV endpoints nest one level: {"data": {"conversations": [...]}}
        if isinstance(value, dict):
            nested = _coerce_turn_list(value)
            if nested:
                return nested
    return []


def _first(turn: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in turn and turn[k] is not None:
            return turn[k]
    return None


def _speaker(raw: Any) -> str:
    role = str(raw or "").strip().lower()
    if role in _BOT_ROLES:
        return "bot"
    if role in _CALLER_ROLES:
        return "caller"
    if role in _TOOL_ROLES:
        return "tool"
    return "unknown"


def _tool_calls(turn: dict) -> list[dict]:
    raw = turn.get("tool_calls")
    if isinstance(raw, dict):  # a single call, unwrapped
        return [raw]
    if isinstance(raw, list):
        return [c for c in raw if isinstance(c, dict)]
    return []


def parse_conversations(payload: Any) -> list[OsvTurn]:
    """Total: returns the turns it recognised, never raises."""
    turns: list[OsvTurn] = []
    for i, raw in enumerate(_coerce_turn_list(payload)):
        text = _first(raw, _TEXT_KEYS)
        turns.append(
            OsvTurn(
                index=i,
                speaker=_speaker(_first(raw, _ROLE_KEYS)),
                text="" if text is None else str(text),
                tool_calls=_tool_calls(raw),
                module=(
                    str(raw["current_module"])
                    if raw.get("current_module") is not None
                    else None
                ),
                error=str(raw["error"]) if raw.get("error") else None,
            )
        )
    return turns


def flatten_tool_calls(turns: list[OsvTurn]) -> list[dict]:
    """Every tool call across the conversation, in turn order.

    Each entry is annotated with the turn it came from so a `tool_order`
    assertion can say *when*, not just *whether*.
    """
    out: list[dict] = []
    for turn in turns:
        for call in turn.tool_calls:
            entry = dict(call)
            entry.setdefault("_turn_index", turn.index)
            out.append(entry)
    return out


def tool_names(turns: list[OsvTurn]) -> list[str]:
    """Ordered tool names, for the common `did it call X before Y` assertion."""
    names: list[str] = []
    for call in flatten_tool_calls(turns):
        fn = call.get("function")
        name = fn.get("name") if isinstance(fn, dict) else call.get("name")
        if name:
            names.append(str(name))
    return names


def tag_only_bot_turns(turns: list[OsvTurn]) -> list[OsvTurn]:
    return [t for t in turns if t.speaker == "bot" and t.is_tag_only]
