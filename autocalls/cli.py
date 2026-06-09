"""CLI interface for autocalls."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from .config import Settings
from .models import CallResult, StepStatus
from .runner import TestRunner, load_flow
from .server import app as fastapi_app
from .store import ResultStore

cli_app = typer.Typer(name="autocalls", help="Automated voice AI testing tool")
console = Console()


def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def _start_server(settings: Settings):
    """Start the FastAPI server in a background thread."""
    config = uvicorn.Config(
        fastapi_app,
        host=settings.server_host,
        port=settings.server_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Give server a moment to start
    import time

    time.sleep(1)
    return server


def _print_result(result: CallResult):
    """Pretty-print a call result."""
    status_color = "green" if result.passed else "red"
    console.print()
    console.print(
        f"[bold {status_color}]{'PASS' if result.passed else 'FAIL'}[/] "
        f"[bold]{result.flow_name}[/] → {result.target}"
    )

    if result.duration_ms:
        console.print(f"  Duration: {result.duration_ms:.0f}ms")
    if result.error:
        console.print(f"  [red]Error: {result.error}[/]")

    # Steps table
    if result.steps:
        table = Table(show_header=True, padding=(0, 1))
        table.add_column("Step", style="dim", width=5)
        table.add_column("Status", width=8)
        table.add_column("Bot said", max_width=50)
        table.add_column("We said", max_width=30)

        for step in result.steps:
            status_style = "green" if step.status == StepStatus.PASSED else "red"
            # Show speech and/or DTMF in the "We said" column
            actions = []
            if step.our_utterance:
                actions.append(step.our_utterance[:25])
            if step.our_dtmf:
                actions.append(f"DTMF:{step.our_dtmf}")
            action_str = " + ".join(actions) if actions else "-"

            table.add_row(
                str(step.step_index),
                f"[{status_style}]{step.status.value}[/]",
                step.bot_transcript[:50] + ("..." if len(step.bot_transcript) > 50 else ""),
                action_str,
            )
        console.print(table)

    # Transcript
    if result.transcript:
        console.print("\n[bold]Transcript:[/]")
        for entry in result.transcript:
            prefix = "[cyan]Bot:[/]" if entry.speaker == "bot" else "[yellow]Us:[/]"
            console.print(f"  {prefix} {entry.text}")

    console.print()


@cli_app.command()
def run(
    flow_path: str = typer.Argument(..., help="Path to flow YAML file"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run a single test flow."""
    _setup_logging(verbose)
    settings = Settings()

    _start_server(settings)
    runner = TestRunner(settings)

    async def _run():
        try:
            result = await runner.run_flow_file(flow_path)
            _print_result(result)
            return result
        finally:
            await runner.close()

    result = asyncio.run(_run())
    raise typer.Exit(code=0 if result.passed else 1)


@cli_app.command()
def watch(
    flow_path: str = typer.Argument(..., help="Path to flow YAML file"),
    interval: int = typer.Option(60, "--interval", "-i", help="Seconds between runs"),
    count: int = typer.Option(0, "--count", "-n", help="Number of runs (0 = forever)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run a flow repeatedly on an interval."""
    _setup_logging(verbose)
    settings = Settings()

    _start_server(settings)
    runner = TestRunner(settings)

    async def _watch():
        flow = load_flow(flow_path)
        try:
            results = await runner.run_repeated(flow, count=count, interval_s=interval)
            console.print(f"\n[bold]Completed {len(results)} runs[/]")
            passed = sum(1 for r in results if r.passed)
            color = "green" if passed == len(results) else "red"
            console.print(f"  [{color}]{passed}/{len(results)} passed[/]")
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped.[/]")
        finally:
            await runner.close()

    asyncio.run(_watch())


@cli_app.command()
def results(
    flow_name: str = typer.Option(None, "--flow", "-f", help="Filter by flow name"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """Show past test results."""
    settings = Settings()
    store = ResultStore(settings.db_path)

    async def _results():
        rows = await store.get_results(flow_name=flow_name, limit=limit)
        stats = await store.get_stats(flow_name=flow_name)

        if stats.get("total", 0) > 0:
            console.print(f"\n[bold]Stats:[/] {stats['pass_rate']} pass rate "
                          f"({stats['passed']}/{stats['total']})")
            if stats.get("avg_duration_ms"):
                console.print(f"  Avg duration: {stats['avg_duration_ms']:.0f}ms")

        if not rows:
            console.print("[dim]No results found.[/]")
            return

        table = Table(show_header=True, padding=(0, 1))
        table.add_column("ID", style="dim", width=12)
        table.add_column("Flow", width=20)
        table.add_column("Target", width=15)
        table.add_column("Status", width=10)
        table.add_column("Passed", width=7)
        table.add_column("Duration", width=10)
        table.add_column("Time", width=20)

        for row in rows:
            passed = bool(row.get("passed"))
            style = "green" if passed else "red"
            duration = f"{row['duration_ms']:.0f}ms" if row.get("duration_ms") else "-"
            table.add_row(
                row["id"],
                row["flow_name"],
                row["target"],
                f"[{style}]{row['status']}[/]",
                f"[{style}]{'yes' if passed else 'no'}[/]",
                duration,
                row.get("created_at", ""),
            )
        console.print(table)

    asyncio.run(_results())


@cli_app.command()
def serve(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run the webhook server standalone (for external integrations)."""
    _setup_logging(verbose)
    settings = Settings()
    console.print(f"Starting server on {settings.server_host}:{settings.server_port}")
    uvicorn.run(
        fastapi_app,
        host=settings.server_host,
        port=settings.server_port,
    )


# Entry point alias
app = cli_app
