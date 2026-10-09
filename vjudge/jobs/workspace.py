"""The on-disk job folder.

Everything a job writes lands under `jobs/{job_id}/`, and `jobs/` is gitignored
in the same change that created it — not afterwards. That ordering matters: these
files contain **real customer PII**, lifted from production interactions on
purpose (the metadata *is* the test credential, see the plan doc). The Veros
campaign that this automates already carries a hand-written warning to that
effect; here it accumulates without anyone deciding to write it down.

    jobs/
      .gitignore              "*" — belt and braces, even if the root rule moves
      {job_id}/
        job.json              the create request as received
        seed/{iuuid}.json      metadata + transcript as fetched from OSV
        scenarios/S01-*.yaml   generated, lint-clean
        campaign.yaml          generated, references ./scenarios/*.yaml
        report.json            the final response
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JOBS_ROOT = Path("jobs")

# Anything that isn't obviously safe in a filename. Job ids are minted by us, but
# a caller may supply one, and it becomes a directory name.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_slug(value: str, *, fallback: str = "x", maxlen: int = 60) -> str:
    """Filesystem-safe fragment. Prevents a supplied job_id escaping the root."""
    cleaned = _UNSAFE.sub("-", (value or "").strip())[:maxlen].strip("-.")
    return cleaned or fallback


def ensure_jobs_root() -> Path:
    """Create `jobs/` with its own .gitignore. Idempotent."""
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    gitignore = JOBS_ROOT / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# Generated job artifacts. Contains real customer PII lifted from\n"
            "# production interactions — never commit anything in here.\n"
            "*\n"
        )
    return JOBS_ROOT


class JobWorkspace:
    """Paths for one job, created on demand."""

    def __init__(self, job_id: str):
        self.job_id = safe_slug(job_id, fallback="job")
        ensure_jobs_root()
        self.root = JOBS_ROOT / self.job_id
        self.seed_dir = self.root / "seed"
        self.scenario_dir = self.root / "scenarios"

    def create(self) -> "JobWorkspace":
        self.seed_dir.mkdir(parents=True, exist_ok=True)
        self.scenario_dir.mkdir(parents=True, exist_ok=True)
        return self

    # ------------------------------------------------------------------ writes

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str, sort_keys=True))
        return path

    def write_seed(self, interaction_uuid: str, payload: Any) -> Path:
        path = self.seed_dir / f"{safe_slug(interaction_uuid)}.json"
        path.write_text(json.dumps(payload, indent=2, default=str, sort_keys=True))
        return path

    def write_scenario(self, scenario_id: str, yaml_text: str) -> Path:
        path = self.scenario_dir / f"{safe_slug(scenario_id)}.yaml"
        path.write_text(yaml_text)
        return path

    def write_campaign(self, yaml_text: str) -> Path:
        path = self.root / "campaign.yaml"
        path.write_text(yaml_text)
        return path

    @property
    def campaign_path(self) -> Path:
        return self.root / "campaign.yaml"
