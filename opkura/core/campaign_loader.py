"""Load YAML campaign files into Campaign dataclasses.

Mirrors the validation style of `scenario_loader.py` — hand-rolled
checks with clear error messages rather than relying on Pydantic. Each
field is validated; unknown top-level keys are rejected.

Globs in `scenarios` are expanded relative to the campaign file's
directory, so a campaign YAML in `clients/resurgent/campaigns/` can
reference `../scenarios/auth/*.yaml` portably regardless of cwd.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

import yaml

from .models import Campaign

REQUIRED_KEYS = {"name", "scenarios"}
OPTIONAL_KEYS = {"description", "max_concurrency", "fail_fast"}
ALLOWED_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS

# Default max_concurrency when a campaign doesn't specify one. There is no
# hard upper cap: if a campaign explicitly sets max_concurrency, that value is
# honored as-is (the caller owns the trade-off — Twilio limits, tunnel
# capacity, Gemini RPM, and triage difficulty all scale with it).
DEFAULT_MAX_CONCURRENCY = 50


def _expand_scenarios(
    raw_scenarios: list[str], campaign_dir: Path
) -> list[str]:
    """Expand glob patterns and validate every resolved path exists.

    Resolves both explicit paths and globs relative to the campaign
    file's directory. Dedupes while preserving insertion order so the
    YAML's intent (explicit ordering for sequential debugging) is
    respected.
    """
    seen: dict[str, None] = {}  # ordered set via dict insertion order
    for raw in raw_scenarios:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(
                f"`scenarios` entries must be non-empty strings, got {raw!r}"
            )
        entry = raw.strip()
        # Globs vs explicit paths: any unescaped *, ?, or [ → treat as glob.
        is_glob = any(c in entry for c in "*?[")
        if is_glob:
            # Path.glob doesn't accept absolute patterns; split into
            # anchor + relative pattern. For simplicity require globs
            # to be relative to campaign_dir.
            if Path(entry).is_absolute():
                raise ValueError(
                    f"glob `{entry}` must be relative to the campaign file's "
                    "directory; absolute globs are not supported"
                )
            matches = sorted(str(p.resolve()) for p in campaign_dir.glob(entry))
            if not matches:
                raise ValueError(
                    f"glob `{entry}` matched no scenario files "
                    f"(relative to {campaign_dir})"
                )
            for m in matches:
                seen.setdefault(m, None)
        else:
            p = Path(entry)
            if not p.is_absolute():
                p = (campaign_dir / p).resolve()
            if not p.exists():
                raise ValueError(
                    f"scenario file not found: {entry} "
                    f"(resolved to {p})"
                )
            seen.setdefault(str(p), None)
    return list(seen.keys())


def load(path: Union[str, Path]) -> Campaign:
    """Parse a campaign YAML file into a `Campaign` dataclass.

    Raises ValueError on any validation failure with a message that
    points at the offending field. Globs in `scenarios` are expanded
    immediately so the returned Campaign has both the raw entries and
    the resolved absolute paths populated.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"failed to parse YAML in {path}: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(
            f"campaign file {path} must contain a YAML mapping at top level"
        )

    missing = sorted(REQUIRED_KEYS - data.keys())
    if missing:
        raise ValueError(f"missing required key(s): {', '.join(missing)}")

    unknown = sorted(data.keys() - ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"unknown top-level key(s): {', '.join(unknown)}")

    name = data["name"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("`name` must be a non-empty string")

    description = data.get("description", "")
    if not isinstance(description, str):
        raise ValueError(
            f"`description` must be a string, got {type(description).__name__}"
        )

    raw_concurrency = data.get("max_concurrency", DEFAULT_MAX_CONCURRENCY)
    try:
        max_concurrency = int(raw_concurrency)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"`max_concurrency` must be an integer, got {raw_concurrency!r}"
        ) from e
    if max_concurrency < 1:
        raise ValueError(
            f"`max_concurrency` must be >= 1, got {max_concurrency}"
        )

    fail_fast_raw = data.get("fail_fast", False)
    if not isinstance(fail_fast_raw, bool):
        raise ValueError(
            f"`fail_fast` must be a boolean, got {type(fail_fast_raw).__name__}"
        )

    scenarios = data["scenarios"]
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("`scenarios` must be a non-empty list")

    campaign_dir = path.parent.resolve()
    resolved = _expand_scenarios(scenarios, campaign_dir)
    if not resolved:
        raise ValueError(
            "no scenarios resolved after glob expansion — check paths and patterns"
        )

    return Campaign(
        name=name.strip(),
        description=description,
        scenarios=list(scenarios),
        resolved_scenarios=resolved,
        max_concurrency=max_concurrency,
        fail_fast=fail_fast_raw,
        source_path=str(path),
    )
