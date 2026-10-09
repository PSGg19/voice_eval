"""`vjudge run` subcommand — execute a V1 flow or V2 scenario end-to-end."""
from __future__ import annotations

import asyncio
from typing import Optional

import typer
from rich.markup import escape as rich_escape

from vjudge.cli.main import (
    resolve_settings,
    _print_result,
    _print_v2_summary,
    _setup_logging,
    _start_server,
    cli_app,
    console,
)
from vjudge.core.config import Settings
from vjudge.core.scenario_loader import peek_transport
from vjudge.orchestrator.runner import TestRunner


@cli_app.command()
def run(
    scenario_path: str = typer.Argument(..., metavar="PATH", help="Path to scenario YAML (V1 flow or V2 scenario)."),
    no_lint: bool = typer.Option(False, "--no-lint", help="Skip pre-call lint (V2 only; default lints first)."),
    transport: Optional[str] = typer.Option(
        None,
        "--transport",
        help="Override the deployment transport for this run: pstn | sip | text. "
             "A scenario that pins `transport:` in its YAML still wins.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run a scenario end-to-end (auto-detects V1 flow vs V2 AI-driven scenario)."""
    _setup_logging(verbose)
    from vjudge.orchestrator.runner import detect_schema

    settings = resolve_settings(transport)

    # V2 pre-flight lint — catches typos and schema errors before we burn
    # a Twilio call. --no-lint opt-out for the "I'm iterating, just run" case.
    try:
        schema = detect_schema(scenario_path)
    except Exception as exc:
        console.print(f"[red]Failed to detect schema:[/] {exc}")
        raise typer.Exit(code=1)

    if schema == "v2" and not no_lint:
        from vjudge.core.lint import lint_scenario

        lr = lint_scenario(scenario_path)
        if not lr.passed:
            console.print(f"[red]Lint failed for {scenario_path}:[/]")
            for e in lr.errors:
                fld = f" ({rich_escape(e.field)})" if e.field else ""
                console.print(f"  [red]error[/] {e.code}{fld}: {e.message}")
            console.print(
                "[dim]Re-run with --no-lint to bypass (not recommended).[/]"
            )
            raise typer.Exit(code=1)
        for w in lr.warnings:
            console.print(f"  [yellow]warning[/] {w.code}: {w.message}")

    # Resolve the EFFECTIVE transport before touching the media server: a V2
    # scenario may pin `transport: text`, in which case there is nothing to
    # serve and binding 8765 would only produce a misleading "address already
    # in use". A light peek, because the runner re-loads (and validates) the
    # scenario properly anyway.
    effective_transport = peek_transport(scenario_path) or settings.call_transport
    _start_server(settings, transport=effective_transport)
    runner = TestRunner(settings)

    async def _run():
        try:
            return await runner.run_scenario_file(scenario_path)
        finally:
            await runner.close()

    result = asyncio.run(_run())
    if schema == "v2":
        _print_v2_summary(result)
    else:
        _print_result(result)
    raise typer.Exit(code=0 if result.passed else 1)
