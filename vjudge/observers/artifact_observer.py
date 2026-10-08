"""Artifact observer (P5.3): accumulate during the run, write ONCE at settle.

Two documents per run, keyed deterministically so anything holding the
run_id can derive them without asking:

    runs/<run_id>/transcript.json   every turn, in order, with timestamps
    runs/<run_id>/run.json          outcome + token totals + counts

Writes happen only on RunFinished — a run that dies mid-flight leaves no
half-written artifact, and the store sees one put per document instead of a
put per turn. Enabled via ARTIFACT_OBSERVER=true (the per-call log folder
CallLogWriter maintains remains the default local artifact until the P5 exit
criterion retires its root-logger handler from concurrent paths).
"""
from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from vjudge.observers.events import (
    BotTurn,
    CallerTurn,
    Generation,
    RunFinished,
    RunStarted,
)

logger = logging.getLogger(__name__)


def transcript_key(run_id: str) -> str:
    return f"runs/{run_id}/transcript.json"


def run_summary_key(run_id: str) -> str:
    return f"runs/{run_id}/run.json"


class ArtifactObserver:
    slow_ok_events = (RunFinished,)  # the store writes happen after the outcome

    def __init__(self, store: Any):
        self._store = store
        self._run: Optional[RunStarted] = None
        self._turns: List[dict] = []
        self._generations: List[Generation] = []

    def on_event(self, event: Any) -> None:
        if isinstance(event, RunStarted):
            self._run, self._turns, self._generations = event, [], []
        elif isinstance(event, BotTurn):
            self._turns.append({
                "speaker": "bot", "turn": event.turn_index, "text": event.text,
                "module": event.module or None, "latency_ms": event.latency_ms,
                "at_ns": event.at_ns,
            })
        elif isinstance(event, CallerTurn):
            self._turns.append({
                "speaker": "caller", "turn": event.turn_index, "text": event.text,
                "dtmf": event.dtmf or None, "at_ns": event.at_ns,
            })
        elif isinstance(event, Generation):
            self._generations.append(event)
        elif isinstance(event, RunFinished) and self._run is not None:
            self._settle(event)

    def _settle(self, finished: RunFinished) -> None:
        run = self._run
        assert run is not None
        self._run = None
        summary = {
            "run_id": run.run_id,
            "scenario": run.scenario_name,
            "target": run.target,
            "region": run.region,
            "transport": run.transport,
            "status": finished.status,
            "passed": finished.passed,
            "error": finished.error or None,
            "interaction_uuid": finished.interaction_uuid or None,
            "started_ns": run.at_ns,
            "ended_ns": finished.at_ns,
            "turns": len(self._turns),
            "llm_calls": [
                {
                    "role": g.role, "model": g.model,
                    "prompt_tokens": g.prompt_tokens,
                    "completion_tokens": g.completion_tokens,
                    "duration_ms": (g.ended_ns - g.started_ns) / 1e6,
                    "error": g.error,
                }
                for g in self._generations
            ],
        }
        t_key = self._store.put_bytes(
            transcript_key(run.run_id),
            json.dumps(self._turns, ensure_ascii=False, indent=1).encode(),
        )
        s_key = self._store.put_bytes(
            run_summary_key(run.run_id),
            json.dumps(summary, ensure_ascii=False, indent=1).encode(),
        )
        logger.info("artifacts written: %s, %s", t_key, s_key)
