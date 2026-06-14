"""`opkura campaign` subcommand — run many scenarios in parallel.

Wires the Phase 2 stack together: campaign loader (Task 1) → worker-pool
runner (Task 3) → reporter (this task). Each worker drives a real
TestRunner so concurrent calls all hit Twilio/Deepgram/Gemini exactly
the way single-scenario runs do.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.markup import escape as rich_escape
from rich.table import Table

from opkura.cli.main import (
    _setup_logging,
    _start_server,
    cli_app,
    console,
    resolve_settings,
)
from opkura.core.campaign_loader import load as load_campaign
from opkura.core.scenario_loader import peek_transport
from opkura.core.config import Settings
from opkura.core.models import CallResult, CampaignResult
from opkura.orchestrator.campaign_reporter import (
    campaign_log_dir,
    render_json,
    render_markdown,
)
from opkura.orchestrator.campaign_runner import CampaignRunner
from opkura.orchestrator.runner import TestRunner

logger = logging.getLogger(__name__)


@cli_app.command()
def campaign(
    campaign_path: str = typer.Argument(
        ..., metavar="CAMPAIGN_YAML", help="Path to a campaign YAML file."
    ),
    max_concurrency: Optional[int] = typer.Option(
        None,
        "--max-concurrency",
        "-m",
        help="Override the campaign's max_concurrency (>=1).",
    ),
    fail_fast: Optional[bool] = typer.Option(
        None,
        "--fail-fast/--no-fail-fast",
        help="Override the campaign's fail_fast setting.",
    ),
    transport: Optional[str] = typer.Option(
        None,
        "--transport",
        help="Override the deployment transport for this run: pstn | sip | text. "
             "A scenario that pins `transport:` in its YAML still wins.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run all scenarios in a campaign through a worker pool."""
    _setup_logging(verbose)
    settings = resolve_settings(transport)

    try:
        camp = load_campaign(campaign_path)
    except ValueError as exc:
        console.print(f"[red]Campaign file invalid:[/] {rich_escape(str(exc))}")
        raise typer.Exit(code=2)

    # CLI overrides applied AFTER load (so the loader's bounds-check
    # already validated whatever was in YAML; we only re-validate the
    # override here for the same min=1 rule).
    if max_concurrency is not None:
        if max_concurrency < 1:
            console.print("[red]--max-concurrency must be >= 1[/]")
            raise typer.Exit(code=2)
        camp.max_concurrency = max_concurrency
    if fail_fast is not None:
        camp.fail_fast = fail_fast

    # Set up campaign log dir up-front so even a setup error has a
    # place to drop a partial report. uuid suffix mirrors per-call
    # folder naming so the two trees feel consistent.
    campaign_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc)
    log_dir = campaign_log_dir(
        Path("logs/campaigns"), camp.name, started_at, campaign_id
    )
    log_dir.mkdir(parents=True, exist_ok=True)

    console.print()
    console.print(f"[bold]Campaign:[/] {rich_escape(camp.name)}")
    console.print(f"  Scenarios: {len(camp.resolved_scenarios)}")
    console.print(f"  Workers:   {camp.max_concurrency}")
    console.print(f"  fail_fast: {camp.fail_fast}")
    console.print(f"  Logs:      {log_dir}")
    console.print()

    # Skip the media server only when EVERY scenario resolves to text. A mixed
    # campaign still needs it for its audio scenarios, and getting this wrong in
    # the permissive direction would fail those runs — so `all()`, not `any()`.
    # Same reasoning as cli/run.py; see _start_server's docstring.
    effective = [
        peek_transport(p) or settings.call_transport
        for p in camp.resolved_scenarios
    ]
    campaign_transport = (
        "text" if effective and all(t == "text" for t in effective) else None
    )
    _start_server(settings, transport=campaign_transport)
    runner = TestRunner(settings)
    cr = CampaignRunner(runner)

    def on_complete(call_result: CallResult, completed: int, total: int) -> None:
        marker = "[green]PASS[/]" if call_result.passed else "[red]FAIL[/]"
        name = Path(call_result.flow_name).name
        duration = (
            f"{call_result.duration_ms / 1000:.1f}s"
            if call_result.duration_ms is not None
            else "-"
        )
        # Pad completed/total so columns line up regardless of campaign size.
        width = len(str(total))
        console.print(
            f"  [{completed:>{width}}/{total}] {marker} "
            f"{rich_escape(name):40} {duration}"
        )

    async def _run() -> CampaignResult:
        try:
            return await cr.run(camp, on_scenario_complete=on_complete)
        finally:
            await runner.close()

    result = asyncio.run(_run())

    # Persist reports BEFORE printing the final table so a Ctrl-C
    # during table render doesn't lose the on-disk artifact.
    try:
        (log_dir / "campaign-report.json").write_text(
            render_json(result), encoding="utf-8"
        )
        (log_dir / "campaign-report.md").write_text(
            render_markdown(result), encoding="utf-8"
        )
    except Exception as exc:
        logger.exception("failed to write campaign report")
        console.print(f"[yellow]warning:[/] could not write report: {exc}")

    _print_summary(result, log_dir)

    # Exit code: 0 if every scenario passed, 1 otherwise. Matches the
    # `opkura run` per-scenario convention so CI/cron can chain.
    raise typer.Exit(code=0 if result.passed == result.total_scenarios else 1)


def _print_summary(result: CampaignResult, log_dir: Path) -> None:
    console.print()
    all_passed = result.passed == result.total_scenarios and result.total_scenarios > 0
    color = "green" if all_passed else "red"
    console.print(
        f"[bold {color}]{result.passed}/{result.total_scenarios} passed[/] "
        f"({result.pass_rate * 100:.1f}%) — {result.duration_s:.1f}s total"
    )

    table = Table(show_header=True, padding=(0, 2), title="Campaign Summary")
    table.add_column("Metric", style="bold")
    table.add_column("Value")
    table.add_row("Total", str(result.total_scenarios))
    table.add_row("Passed", f"[green]{result.passed}[/]")
    table.add_row(
        "Failed",
        f"[red]{result.failed}[/]" if result.failed else "0",
    )
    table.add_row(
        "Errored",
        f"[red]{result.errored}[/]" if result.errored else "0",
    )
    if result.skipped_paths:
        table.add_row(
            "Skipped (fail_fast)", f"[yellow]{len(result.skipped_paths)}[/]"
        )
    table.add_row("Pass rate", f"[{color}]{result.pass_rate * 100:.1f}%[/]")
    console.print(table)

    failed = [cr for cr in result.call_results if not cr.passed]
    if failed:
        console.print()
        console.print(f"[red]Failed scenarios ({len(failed)}):[/]")
        for cr in failed:
            name = Path(cr.flow_name).name
            console.print(f"  [red]✗[/] {rich_escape(name)}")
            if cr.log_dir:
                console.print(f"    [dim]{cr.log_dir}[/]")

    console.print()
    console.print(f"[bold]Report:[/] {log_dir / 'campaign-report.md'}")
    console.print()
