"""YAML → V3 store importer (P3.6).

`judge_it import clients/` maps the on-disk corpus into scenario and
variable-set rows, reporting per file what it did. Properties that matter:

* **Idempotent.** A file whose body already matches the latest version of its
  key is `unchanged`; an edited file becomes a NEW version (scenarios are
  immutable once run, so in-place update is not an option and versioning is
  the honest history).
* **Zero data loss.** The stored `body` is the YAML mapping itself, coerced
  to JSON types by `to_jsonable` (dates → ISO strings — JSONB has no date
  type). `export_scenario_yaml(body)` round-trips: re-loading the export
  equals the stored body, and the stored body equals the coerced original.
  The corpus test pins this.
* **Variable-set extraction with dedupe.** Identical `vars` mappings within
  one client collapse into ONE variable-set row (fingerprint-named), which is
  precisely the "one customer identity spread across a hundred files" problem
  core/variables.py exists to end. Sets are imported UNVERIFIED
  (`is_synthetic=False`) — trust is granted by a human, never by an importer.
* **Lint gate.** A file failing `lint_scenario_data` is reported `failed`
  with its codes and imports nothing — the store never holds a scenario the
  API would refuse to create.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from judge_it.core.lint import detect_file_kind, lint_scenario_data
from judge_it.db.models import Scenario, VariableSet


@dataclass
class FileReport:
    path: str
    action: str  # imported | new_version | unchanged | skipped_* | failed
    key: str = ""
    version: Optional[int] = None
    detail: str = ""


def to_jsonable(value):
    """YAML-typed → JSON-typed, recursively. Dates become ISO strings (JSONB
    has no date type); tuples become lists. Everything else passes through."""
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def export_scenario_yaml(body: dict) -> str:
    """The stored body back as YAML. `yaml.safe_load(export(...)) == body`."""
    return yaml.safe_dump(body, sort_keys=False, allow_unicode=True)


def scenario_key(path: Path, root: Path) -> str:
    """Stable key: the path relative to the import root, extension dropped.
    `clients/sbi/yono-general/tool-multihop-routing.yaml` (root=clients/)
    → `sbi/yono-general/tool-multihop-routing`."""
    rel = path.resolve().relative_to(root.resolve())
    return rel.with_suffix("").as_posix()


def _vars_fingerprint(vars_data: dict) -> str:
    return hashlib.sha256(
        json.dumps(vars_data, sort_keys=True, default=str).encode()
    ).hexdigest()


def _ensure_variable_set(
    session: Session, *, client: str, vars_data: dict, first_seen: str
):
    """One row per distinct vars mapping per client, fingerprint-named."""
    name = f"import-{_vars_fingerprint(vars_data)[:12]}"
    existing = session.execute(
        select(VariableSet).where(
            VariableSet.client == client, VariableSet.name == name
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = VariableSet(
        name=name,
        client=client,
        vars=vars_data,
        provenance={"source": "yaml-import", "first_seen": first_seen},
        is_synthetic=False,  # a human marks sets synthetic, not an importer
    )
    session.add(row)
    session.flush()
    return row


def import_file(session: Session, path: Path, root: Path) -> FileReport:
    rel = str(path)
    kind = detect_file_kind(path)
    if kind != "scenario":
        return FileReport(path=rel, action=f"skipped_{kind}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return FileReport(path=rel, action="failed", detail=f"invalid yaml: {e}")
    if not isinstance(data, dict):
        return FileReport(path=rel, action="failed", detail="not a mapping")

    result = lint_scenario_data(data, path)
    if result.errors:
        codes = ", ".join(sorted({e.code for e in result.errors}))
        return FileReport(path=rel, action="failed", detail=f"lint: {codes}")

    body = to_jsonable(data)
    key = scenario_key(path, root)

    latest = session.execute(
        select(Scenario)
        .where(Scenario.key == key)
        .order_by(Scenario.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is not None and latest.body == body:
        return FileReport(
            path=rel, action="unchanged", key=key, version=latest.version
        )

    vs_id = None
    vars_data = body.get("vars") or {}
    if vars_data:
        client = key.split("/", 1)[0]
        vs_id = _ensure_variable_set(
            session, client=client, vars_data=vars_data, first_seen=key
        ).id

    version = 1 if latest is None else latest.version + 1
    session.add(
        Scenario(
            key=key,
            version=version,
            body=body,
            variable_set_id=vs_id,
            source_path=str(path),
        )
    )
    session.flush()
    return FileReport(
        path=rel,
        action="imported" if latest is None else "new_version",
        key=key,
        version=version,
    )


def import_paths(
    session: Session, paths: Iterable[str | Path], *, root: Optional[Path] = None
) -> list[FileReport]:
    """Import every *.yaml under `paths` (files or directories). `root`
    anchors key derivation; defaults to each directory argument itself (or a
    file's parent), so `judge_it import clients/` keys as `sbi/...`, stable
    across reruns from any cwd."""
    reports: list[FileReport] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            base = root or p
            files = sorted(p.rglob("*.yaml")) + sorted(p.rglob("*.yml"))
        else:
            base = root or p.parent
            files = [p]
        for f in files:
            reports.append(import_file(session, f, base))
    session.commit()
    return reports


def summarize(reports: list[FileReport]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in reports:
        counts[r.action] = counts.get(r.action, 0) + 1
    return counts
