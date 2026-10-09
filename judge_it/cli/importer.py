"""`judge_it import` — the YAML corpus into the V3 store (P3.6).

Deliberately does NOT construct full Settings: an import needs a database and
nothing else, and requiring Twilio/Deepgram credentials to move YAML into
Postgres would be a pointless gate. The URL comes from --database-url, then
JUDGE_IT_DATABASE_URL, then the local default.
"""
from __future__ import annotations

import os
from typing import Optional

import typer
from rich.table import Table

from judge_it.cli.main import cli_app, console


@cli_app.command("import")
def import_cmd(
    paths: list[str] = typer.Argument(..., help="Scenario files or directories (e.g. clients/)"),
    database_url: Optional[str] = typer.Option(
        None, "--database-url", help="Overrides JUDGE_IT_DATABASE_URL"
    ),
    show_all: bool = typer.Option(
        False, "--all", help="List every file, not just failures and imports"
    ),
):
    """Import scenario YAML into the V3 store (idempotent; edits become new versions)."""
    from sqlalchemy.orm import Session

    from judge_it.db.engine import DEFAULT_DATABASE_URL, sync_engine
    from judge_it.db.importer import import_paths, summarize

    url = database_url or os.environ.get("JUDGE_IT_DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = sync_engine(url)
    with Session(engine) as session:
        reports = import_paths(session, paths)
    engine.dispose()

    table = Table(show_header=True, padding=(0, 1), title="Import report")
    table.add_column("Action", width=14)
    table.add_column("Key / path", overflow="fold")
    table.add_column("Detail", overflow="fold")
    styles = {
        "imported": "green",
        "new_version": "green",
        "unchanged": "dim",
        "failed": "red",
    }
    for r in reports:
        if not show_all and r.action.startswith(("unchanged", "skipped")):
            continue
        style = styles.get(r.action, "yellow")
        shown = f"{r.key} v{r.version}" if r.key else r.path
        table.add_row(f"[{style}]{r.action}[/]", shown, r.detail)
    console.print(table)

    counts = summarize(reports)
    console.print(
        "  " + " · ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    )
    raise typer.Exit(code=1 if counts.get("failed") else 0)
