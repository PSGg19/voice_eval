"""Per-call structured log folder writer."""
from __future__ import annotations

import json
import logging
import re
import shutil
import unicodedata
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import yaml

from vjudge.core.log_context import call_id_var
from vjudge.core.models import CallResult, EvaluationResult, Scenario, TranscriptEntry

logger = logging.getLogger(__name__)


class _CallIdFilter(logging.Filter):
    """Reject log records whose active `call_id_var` doesn't match this writer's `call_id`.

    Used to isolate per-call `debug.log` files when multiple scenarios
    run concurrently (Phase 2 campaign mode). The asyncio task that
    emits the log record must have been entered with `call_id_var`
    set to the owning call's id — handled at the runner and FastAPI
    server boundaries (`_run_v2_scenario` and `media_stream_ws`).

    Records emitted outside any call context (e.g., uvicorn startup
    logs) have `call_id_var.get() == None` and are rejected by every
    per-call filter — they reach the console handler only.
    """

    def __init__(self, call_id: str):
        super().__init__()
        self.call_id = call_id

    def filter(self, record: logging.LogRecord) -> bool:
        return call_id_var.get() == self.call_id

# Attach the per-call FileHandler to the ROOT logger (empty name). This
# captures everything that reaches the console — vjudge.* lines AND
# third-party loggers like twilio.http_client and google_genai.models —
# so debug.log is a complete superset of what scrolled past in the
# terminal. Without this, third-party records propagate up to root for
# the console StreamHandler but bypass the vjudge-scoped FileHandler.
_VJUDGE_LOGGER = ""


def slugify(s: str) -> str:
    normalized = unicodedata.normalize("NFKD", s)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")


REQUIRED_FILES = (
    "scenario.yaml",
    "rendered_persona.txt",
    "rendered_eval.txt",
    "metadata.json",
    "transcript.json",
    "transcript.txt",
    "debug.log",
    "evaluation.json",
    "llm_calls.json",
)

# Per-file stub when close() finds a required file missing. transcript.json
# and llm_calls.json are JSON arrays in the spec — stubbing them with "{}"
# would crash downstream code that does entries[0].
_STUB_CONTENT = {
    "metadata.json": "{}",
    "evaluation.json": "{}",
    "transcript.json": "[]",
    "llm_calls.json": "[]",
}


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    sorted_v = sorted(values)
    # statistics.quantiles is exclusive of endpoints; for small lists pick by index.
    k = max(0, min(len(sorted_v) - 1, int(round((pct / 100.0) * (len(sorted_v) - 1)))))
    return sorted_v[k]


class CallLogWriter:
    """Writes one folder per call. Stream-writes as events arrive; finalizes on close."""

    def __init__(
        self,
        root: Union[str, Path],
        scenario: Scenario,
        call_id: str,
        *,
        capture_debug_log: bool = True,
    ):
        # Second-resolution timestamp so name-sort == chronological-sort for
        # same-minute reruns. call_id suffix still guards against the rare
        # same-second collision (parallel runs in Phase 2).
        utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        slug = slugify(scenario.name) or "scenario"
        self.dir = Path(root) / f"call-{slug}-{utc}-{call_id[:8]}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.scenario = scenario
        self.call_id = call_id
        self._llm_call_entries: list[dict] = []
        self._bot_response_latencies_ms: list[float] = []
        self._log_handler: Optional[logging.FileHandler] = None
        self._closed = False
        self._snapshot_inputs()
        if capture_debug_log:
            self._configure_debug_log()
        else:
            # P1.5 — one log stream. In concurrent modes each per-run root
            # FileHandler makes EVERY log record in the process traverse one
            # filter per in-flight run (plus root-logger lock contention), so
            # no handler is attached at all. The shared structured stream
            # (LOG_SINK=json|loki) already carries run_id on every line;
            # debug.log stays in the folder shape as a pointer to it.
            (self.dir / "debug.log").write_text(
                "per-run debug.log capture is off in concurrent mode.\n"
                "Logs are on the shared structured stream (LOG_SINK) — "
                f"filter by run_id={call_id}\n",
                encoding="utf-8",
            )

    def _snapshot_inputs(self) -> None:
        src = self.scenario.source_path
        if src and Path(src).exists():
            shutil.copyfile(src, self.dir / "scenario.yaml")
        else:
            # Fall back to dumping the resolved Scenario (post-vars rendering).
            data = asdict(self.scenario) if is_dataclass(self.scenario) else dict(self.scenario.__dict__)
            (self.dir / "scenario.yaml").write_text(
                yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
            )

        (self.dir / "rendered_persona.txt").write_text(
            self.scenario.persona_rendered, encoding="utf-8"
        )
        (self.dir / "rendered_eval.txt").write_text(
            self.scenario.evaluation_prompt_rendered, encoding="utf-8"
        )

    def _configure_debug_log(self) -> None:
        handler = logging.FileHandler(self.dir / "debug.log", encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        # Phase 2 fix: the FileHandler is attached to the ROOT logger, so
        # under campaign mode (N parallel CallLogWriters) every log record
        # would hit every per-call handler — corrupting each debug.log with
        # interleaved noise from other calls. The filter rejects records
        # whose active `call_id_var` doesn't match this writer's call_id.
        # The runner and the FastAPI WS handler set `call_id_var` at their
        # task boundaries; asyncio.Task copies the context at spawn so
        # subtasks (STT recv, TTS WS, conv loop, barge fires) inherit it.
        handler.addFilter(_CallIdFilter(self.call_id))
        logging.getLogger(_VJUDGE_LOGGER).addHandler(handler)
        self._log_handler = handler

    def record_llm_call(
        self,
        *,
        turn: int,
        role: str,
        input_messages: list[dict],
        output: Optional[dict],
        latency_ms: float,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        self._llm_call_entries.append(
            {
                "turn": turn,
                "role": role,
                "input_messages": input_messages,
                "output": output,
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "error": error,
            }
        )
        # Flush incrementally so a runner crash between record_llm_call and
        # close() doesn't lose the call history.
        try:
            self.flush_llm_calls()
        except Exception as exc:
            logger.warning("incremental flush_llm_calls failed: %s", exc)

    def record_bot_response_latency(self, latency_ms: float) -> None:
        self._bot_response_latencies_ms.append(latency_ms)

    def write_transcript(self, transcript: list[TranscriptEntry]) -> None:
        entries: list[dict] = []
        for e in transcript:
            # TranscriptEntry is Pydantic — use model_dump, not asdict.
            entries.append(e.model_dump() if hasattr(e, "model_dump") else dict(e))
        (self.dir / "transcript.json").write_text(
            json.dumps(entries, indent=2), encoding="utf-8"
        )

        lines: list[str] = []
        for e in transcript:
            seconds = (e.timestamp_ms or 0) / 1000.0
            speaker = e.speaker.upper()
            prefix = "[BARGE] " if getattr(e, "barge", False) else ""
            suffix = ""
            if getattr(e, "bot_response_after_ms", None) is not None:
                suffix = f" (bot reacted in {e.bot_response_after_ms:.0f}ms)"
            lines.append(
                f"[+{seconds:.1f}s] {speaker}: {prefix}{e.text}{suffix}"
            )
        (self.dir / "transcript.txt").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )

    def write_evaluation(self, evaluation: Optional[EvaluationResult]) -> None:
        if evaluation is None:
            return
        # evaluation is a stdlib @dataclass — asdict, not model_dump.
        payload = asdict(evaluation) if is_dataclass(evaluation) else dict(evaluation)
        (self.dir / "evaluation.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    def write_metadata(
        self,
        *,
        result: CallResult,
        llm_models: dict,
        duration_ms: float,
        cost_estimate: Optional[float] = None,
    ) -> None:
        caller_latencies = [
            e["latency_ms"] for e in self._llm_call_entries if e["role"] == "caller"
        ]
        in_tokens = sum(
            e["input_tokens"] or 0 for e in self._llm_call_entries if e["input_tokens"] is not None
        )
        out_tokens = sum(
            e["output_tokens"] or 0 for e in self._llm_call_entries if e["output_tokens"] is not None
        )
        total_tokens: Optional[int] = (
            in_tokens + out_tokens
            if any(e["input_tokens"] is not None or e["output_tokens"] is not None for e in self._llm_call_entries)
            else None
        )

        bot_lat = self._bot_response_latencies_ms
        meta = {
            "call_sid": result.call_sid,
            # The join keys back to the bot side. `session_id` is the
            # X-VJudge-Session-Id SIP header (voice); `interaction_uuid` is
            # returned on turn 1 by the text runtime. Without these in the
            # artifacts, a finished run cannot be opened in llm-studio or
            # queried with OSVClient.get_conversations() — the uuid was only
            # recoverable by grepping debug.log, which is not a workflow.
            "session_id": result.session_id,
            "interaction_uuid": result.interaction_uuid,
            # Text-transport observations the audio path cannot make.
            "modules_visited": result.modules_visited,
            "tool_calls": result.tool_calls,
            "tag_only_bot_turns": result.tag_only_bot_turns,
            "osv_enriched": result.osv_enriched,
            "assertion_outcomes": result.assertion_outcomes,
            "status": result.status.value,
            "started_at": result.started_at.isoformat() if result.started_at else None,
            "ended_at": result.ended_at.isoformat() if result.ended_at else None,
            "overall_passed": (
                result.evaluation.overall_passed if result.evaluation is not None else None
            ),
            "duration_ms": duration_ms,
            "llm_models": llm_models,
            "llm_tokens_used": total_tokens,
            "cost_estimate": cost_estimate,
            "p50_caller_llm_ms": _percentile(caller_latencies, 50),
            "p95_caller_llm_ms": _percentile(caller_latencies, 95),
            "max_caller_llm_ms": max(caller_latencies) if caller_latencies else None,
            "bot_response_latencies_ms": bot_lat,
            "bot_response_p50_ms": _percentile(bot_lat, 50),
            "bot_response_p95_ms": _percentile(bot_lat, 95),
            "bot_response_max_ms": max(bot_lat) if bot_lat else None,
        }
        (self.dir / "metadata.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

    def save_audio(self, save_recording_fn) -> None:
        """Invoke MediaStreamSession.save_recording into our dir and rename to bot.wav / caller.wav."""
        try:
            paths = save_recording_fn(self.dir)
        except Exception as exc:
            logger.warning("save_audio: save_recording failed: %s", exc)
            return
        for kind, src_path in (paths or {}).items():
            target_name = f"{kind}.wav"  # "bot" → "bot.wav", "caller" → "caller.wav"
            src = Path(src_path)
            dst = self.dir / target_name
            try:
                if src.exists() and src != dst:
                    src.rename(dst)
            except OSError as exc:
                logger.warning("save_audio: rename %s → %s failed: %s", src, dst, exc)

    def flush_llm_calls(self) -> None:
        (self.dir / "llm_calls.json").write_text(
            json.dumps(self._llm_call_entries, indent=2), encoding="utf-8"
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.flush_llm_calls()
        except Exception as exc:
            logger.warning("flush_llm_calls failed during close: %s", exc)
        if self._log_handler is not None:
            try:
                logging.getLogger(_VJUDGE_LOGGER).removeHandler(self._log_handler)
                self._log_handler.close()
            except Exception as exc:
                logger.warning("debug-log handler detach failed: %s", exc)
            self._log_handler = None
        # Spec acceptance: "All listed files exist after every call (including
        # failed/aborted calls)." Stub any required file the runner didn't
        # write so downstream consumers (Task 7 lint, Phase 2 replay) can rely
        # on a consistent folder shape. bot.wav/caller.wav are excluded since
        # empty bytes aren't a valid WAV file.
        for fname in REQUIRED_FILES:
            p = self.dir / fname
            if not p.exists():
                p.write_text(_STUB_CONTENT.get(fname, ""), encoding="utf-8")
