"""`vjudge canary` — the scheduled synthetic run (P5.5). Thin shell around
vjudge.canary.run_canary; the exit code is the alerting contract."""
from __future__ import annotations

import asyncio
from typing import Optional

import typer

from vjudge.cli.main import _setup_logging, _start_server, cli_app, console


@cli_app.command()
def canary(
    scenario: Optional[str] = typer.Argument(
        None,
        envvar="VJUDGE_CANARY_SCENARIO",
        help="Scenario YAML to run (a TEXT scenario at a zero-account assistant)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """One synthetic run through the whole path. Exit 0 = green, 1 = page."""
    if not scenario:
        console.print(
            "[red]no scenario: pass a path or set VJUDGE_CANARY_SCENARIO[/]"
        )
        raise typer.Exit(code=2)

    _setup_logging(verbose)

    from vjudge.canary import run_canary
    from vjudge.core.config import Settings
    from vjudge.core.scenario_loader import peek_transport
    from vjudge.orchestrator.runner import TestRunner

    settings = Settings()
    _start_server(settings, transport=peek_transport(scenario))
    runner = TestRunner(settings)

    async def _run():
        try:
            return await run_canary(scenario, runner)
        finally:
            await runner.close()

    outcome = asyncio.run(_run())
    marker = "[bold green]CANARY PASS[/]" if outcome.passed else "[bold red]CANARY FAIL[/]"
    console.print(
        f"{marker} status={outcome.status} duration={outcome.duration_s:.1f}s"
        + (f" error={outcome.error}" if outcome.error else "")
    )
    raise typer.Exit(code=0 if outcome.passed else 1)
