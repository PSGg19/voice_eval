"""CLI dispatcher for `opkura` — registers run / lint / bcg subcommands."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import typer
import uvicorn
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.table import Table

from opkura.core.config import Settings
from opkura.core.scenario_loader import ALLOWED_TRANSPORTS
from opkura.legacy.load import LoadTestRunner
from opkura.core.models import CallResult, LoadTestConfig, LoadTestResult, StepStatus
from opkura.orchestrator.runner import TestRunner, load_flow
from opkura.transport.server import app as fastapi_app
from opkura.db.results import PgResultStore

cli_app = typer.Typer(name="opkura", help="Opkura — voice-bot test runner")
console = Console()


def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    # The sink is a choice (P5): LOG_SINK = text | json | loki, resolved by the
    # connectors layer with graceful fallback to text. Loki is ADDITIVE — the
    # terminal keeps its readable stream, and the same records also ship to
    # Loki (queued, off the event loop) for Grafana.
    import os as _os

    from opkura.connectors.logs import build as _build_log_handler

    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(_build_log_handler())
        if (_os.getenv("LOG_SINK", "").strip().lower() == "loki"
                and type(root.handlers[0]).__name__ == "LokiQueueHandler"):
            from opkura.connectors.logs import _text_handler

            root.addHandler(_text_handler())
    # Quiet noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    # google-genai SDK prints "AFC is enabled with max remote calls: 10" at
    # INFO for every API call. AFC (Automatic Function Calling) is irrelevant
    # here — we don't register tools — so the line is pure noise.
    logging.getLogger("google_genai.models").setLevel(logging.WARNING)


def _start_server(settings: Settings, transport: str | None = None):
    """Start the FastAPI server in a background thread.

    No-op on the text transport: that server exists only to serve Twilio's
    TwiML webhook and the media-stream WebSocket, and text mode places no call.
    Starting it anyway produces a spurious "address already in use" whenever
    another opkura is running — noise that reads like a real failure.

    `transport` is the EFFECTIVE transport for this invocation, which is not
    necessarily `settings.call_transport`: a scenario can pin `transport: text`
    in its YAML while the deployment default is sip. Callers that know the
    scenario should pass it; without it only the deployment default is
    considered, and a YAML-pinned text run would still bind the port.
    """
    if (transport or settings.call_transport) == "text":
        logging.getLogger(__name__).info(
            "text transport — skipping media-stream server"
        )
        return None

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


def _print_v2_summary(result: CallResult):
    """Pretty-print a V2 (AI-driven scenario) call result with per-criterion table.

    All LLM-generated text fields (reasoning, criteria name/evidence) and
    the runner's error string are run through rich.markup.escape because
    Rich treats `[foo]` as a style tag — without escaping, transcript
    references like `[turn 3]` or `[INFO]` would be silently dropped from
    output.
    """
    passed = result.passed
    marker = "[bold green]PASS[/]" if passed else "[bold red]FAIL[/]"
    console.print()
    console.print(f"{marker} {result.flow_name} → {result.target}")
    if result.duration_ms:
        console.print(f"  Duration: {result.duration_ms:.0f}ms")
    if result.error:
        console.print(f"  [red]Error:[/] {rich_escape(result.error)}")

    # The conversation itself, in the terminal. It is the first thing anyone
    # wants after a FAIL, and having to go find transcript.txt to read it made
    # the CLI output strictly less useful than the file it points at.
    if result.transcript:
        table = Table(show_header=True, padding=(0, 1), title="Transcript")
        table.add_column("At", style="dim", width=8, justify="right")
        table.add_column("Who", width=6)
        table.add_column("Said", overflow="fold")
        for e in result.transcript:
            who = "[cyan]BOT[/]" if e.speaker == "bot" else "[magenta]CALLER[/]"
            # Collapse the flow config's own newlines/indentation so one turn is
            # one row; transcript.txt keeps the text verbatim.
            # Escape FIRST, then add our own markup — a transcript containing
            # "[INFO]" or "[turn 3]" is otherwise eaten by Rich as a style tag.
            said = rich_escape(" ".join((e.text or "").split()))
            if e.dtmf:
                keys = f"[yellow]dtmf {rich_escape(e.dtmf)}[/]"
                said = f"{said} ({keys})" if said else keys
            table.add_row(f"+{e.timestamp_ms / 1000:.1f}s", who, said or "[dim](silence)[/]")
        console.print(table)

    ev = result.evaluation
    if ev is not None:
        console.print(f"  [bold]Reasoning:[/] {rich_escape(ev.overall_reasoning)}")
        if ev.malformed:
            console.print(
                "  [yellow]Evaluator output was malformed; verdict synthesized.[/]"
            )
        if ev.criteria:
            table = Table(show_header=True, padding=(0, 1), title="Criteria")
            table.add_column("#", style="dim", width=4)
            table.add_column("Result", width=8)
            table.add_column("Name", width=28)
            table.add_column("Evidence", overflow="fold")
            for c in ev.criteria:
                style = "green" if c.passed else "red"
                table.add_row(
                    str(c.id),
                    f"[{style}]{'PASS' if c.passed else 'FAIL'}[/]",
                    rich_escape(c.name),
                    rich_escape(c.evidence),
                )
            console.print(table)
        if ev.metrics:
            console.print(f"  [dim]Metrics: {rich_escape(str(ev.metrics))}[/]")

    # Surface the log folder so engineers can jump straight to artifacts.
    # Prefer result.log_dir (populated by the runner before any failure can
    # occur) so setup-error calls — exactly when triage needs the folder —
    # still print the path. Fall back to recording_paths only if log_dir is
    # missing (V1 path or pre-Task-8 result).
    log_dir = result.log_dir
    if not log_dir:
        paths = result.recording_paths or {}
        for k in ("bot", "caller"):
            if k in paths:
                log_dir = str(Path(paths[k]).parent)
                break
    if log_dir:
        console.print(f"  [dim]Logs: {log_dir}[/]")
    # The bot-side join key. On text it is known from turn 1; on voice/job mode
    # it is filled in by correlation. Printing it matters because it is the ONLY
    # way to open this run on the OSV side afterwards — and a text run marked
    # is_test=true is invisible in llm-studio's conversations list, so the API
    # read is sometimes the only route.
    if result.interaction_uuid:
        console.print(f"  [dim]Interaction: {result.interaction_uuid}[/]")
    if result.modules_visited:
        console.print(f"  [dim]Modules: {' → '.join(result.modules_visited)}[/]")
    if result.tool_calls:
        names = [
            (t.get("function") or {}).get("name") or t.get("name") or "?"
            for t in result.tool_calls
        ]
        console.print(f"  [dim]Tools: {', '.join(names)}[/]")
    # Assertions print BEFORE the transcript-derived extras and in colour,
    # because a failure here overrides the evaluator's verdict — burying it under
    # a dim line would hide the reason the run failed.
    if result.assertion_outcomes:
        table = Table(show_header=True, padding=(0, 1), title="Assertions")
        table.add_column("Assertion", style="bold")
        table.add_column("Result", width=14)
        table.add_column("Detail")
        for outcome in result.assertion_outcomes:
            status = outcome.get("status", "")
            style, label = {
                "passed": ("green", "PASS"),
                "failed": ("red", "FAIL"),
            }.get(status, ("yellow", "INCONCLUSIVE"))
            table.add_row(
                rich_escape(str(outcome.get("name", ""))),
                f"[{style}]{label}[/]",
                rich_escape(str(outcome.get("detail", ""))),
            )
        console.print(table)

    # Loud, not dim: this is the difference between "your test is broken" and
    # "the bot is broken", and it is the single question every stalled text run
    # raised this week.
    if result.tag_only_bot_turns:
        turns = result.tag_only_bot_turns
        detail = "; ".join(
            f"turn {t.get('turn_index')}: {' '.join(t.get('tags') or [])}"
            for t in turns[:5]
        )
        console.print(
            f"  [yellow]Silent bot turns: {len(turns)} tag-only "
            f"({rich_escape(detail)}) — reached the caller as silence[/]"
        )
    console.print()


def _print_load_result(result: LoadTestResult):
    """Pretty-print a load test result."""
    all_passed = result.passed_calls == result.total_calls
    color = "green" if all_passed else "red"

    console.print()
    console.print(f"[bold]Load Test: {result.flow_name}[/] → {result.target}")
    console.print(
        f"  Config: {result.config.calls} calls at {result.config.cps} CPS"
    )
    if result.total_duration_ms:
        console.print(f"  Total time: {result.total_duration_ms / 1000:.1f}s")
    console.print()

    # Summary
    summary = Table(show_header=True, padding=(0, 2), title="Summary")
    summary.add_column("Metric", style="bold")
    summary.add_column("Value")
    summary.add_row("Total calls", str(result.total_calls))
    summary.add_row("Passed", f"[green]{result.passed_calls}[/]")
    summary.add_row("Failed", f"[red]{result.failed_calls}[/]" if result.failed_calls else "0")
    summary.add_row("Errors", f"[red]{result.error_calls}[/]" if result.error_calls else "0")
    summary.add_row("Pass rate", f"[{color}]{result.pass_rate:.1f}%[/]")
    console.print(summary)
    console.print()

    # Duration stats
    if result.avg_duration_ms > 0:
        dur = Table(show_header=True, padding=(0, 2), title="Call Duration")
        dur.add_column("Metric", style="bold")
        dur.add_column("Value")
        dur.add_row("Avg", f"{result.avg_duration_ms:.0f}ms")
        dur.add_row("Min", f"{result.min_duration_ms:.0f}ms")
        dur.add_row("Max", f"{result.max_duration_ms:.0f}ms")
        dur.add_row("P50", f"{result.p50_duration_ms:.0f}ms")
        dur.add_row("P95", f"{result.p95_duration_ms:.0f}ms")
        console.print(dur)
        console.print()

    # Per-call breakdown
    if result.call_results:
        calls_table = Table(show_header=True, padding=(0, 1), title="Per-Call Results")
        calls_table.add_column("#", style="dim", width=4)
        calls_table.add_column("ID", style="dim", width=12)
        calls_table.add_column("Status", width=10)
        calls_table.add_column("Passed", width=7)
        calls_table.add_column("Duration", width=10)
        calls_table.add_column("Error", max_width=40)

        for i, cr in enumerate(result.call_results):
            passed = cr.passed
            style = "green" if passed else "red"
            duration = f"{cr.duration_ms:.0f}ms" if cr.duration_ms else "-"
            table_error = (cr.error or "")[:40]
            calls_table.add_row(
                str(i + 1),
                cr.id,
                f"[{style}]{cr.status.value}[/]",
                f"[{style}]{'yes' if passed else 'no'}[/]",
                duration,
                f"[red]{table_error}[/]" if table_error else "",
            )
        console.print(calls_table)

    console.print()


@cli_app.command()
def load(
    flow_path: str = typer.Argument(..., help="Path to flow YAML file"),
    calls: int = typer.Option(10, "--calls", "-c", help="Total number of calls"),
    cps: float = typer.Option(1.0, "--cps", help="Calls per second launch rate"),
    max_concurrent: int = typer.Option(0, "--max-concurrent", "-m", help="Max simultaneous calls (0=no limit)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run a load test — multiple concurrent calls against your bot."""
    _setup_logging(verbose)
    settings = Settings()

    _start_server(settings)
    runner = TestRunner(settings)
    config = LoadTestConfig(calls=calls, cps=cps, max_concurrent=max_concurrent)
    load_runner = LoadTestRunner(runner, config)

    def on_progress(completed: int, total: int, cr: CallResult):
        status = "[green]PASS[/]" if cr.passed else "[red]FAIL[/]"
        console.print(f"  [{completed}/{total}] {status} {cr.id} ({cr.duration_ms:.0f}ms)" if cr.duration_ms else f"  [{completed}/{total}] {status} {cr.id}")

    async def _load():
        flow = load_flow(flow_path)
        console.print(f"\n[bold]Starting load test:[/] {calls} calls at {cps} CPS → {flow.target}")
        try:
            result = await load_runner.run(flow, on_call_complete=on_progress)
            _print_load_result(result)

            # Persist
            store = PgResultStore(settings.database_url)
            await store.save_load_result(result)

            return result
        finally:
            await runner.close()

    result = asyncio.run(_load())
    all_passed = result.passed_calls == result.total_calls
    raise typer.Exit(code=0 if all_passed else 1)


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
    store = PgResultStore(settings.database_url)

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


# Register V2 subcommand handlers. Each module decorates its handler with
# @cli_app.command() at import time; main.py is imported first so cli_app
# and the shared helpers above are already defined when these submodules
# run. Pattern keeps each subcommand in its own file (Phase 2 will extend
# this with replay / campaign without touching main.py).
from opkura.cli import bcg as _bcg_module  # noqa: F401, E402

def resolve_settings(transport: Optional[str]) -> Settings:
    """Build Settings, optionally overriding the deployment transport default.

    `--transport` sets the DEFAULT for this invocation — it does not override a
    scenario that pins `transport:` in its YAML. That pin encodes a capability
    requirement (a barge scenario needs real audio), so an operator flag must not
    be able to silently defeat it; lint enforces the impossible combinations.
    Precedence, narrowest first:

        scenario YAML  >  --transport  >  AUTOCALLS_CALL_TRANSPORT

    Passed to the constructor rather than assigned afterwards so Settings'
    model_validator re-runs — switching to sip without SIP config must fail
    loudly here, not halfway through dialling.
    """
    if transport is None:
        return Settings()
    value = transport.strip().lower()
    if value not in ALLOWED_TRANSPORTS:
        console.print(
            f"[red]--transport must be one of {', '.join(ALLOWED_TRANSPORTS)}, "
            f"got {transport!r}[/]"
        )
        raise typer.Exit(code=2)
    try:
        return Settings(call_transport=value)
    except Exception as exc:
        console.print(f"[red]--transport {value} is not usable:[/] {exc}")
        raise typer.Exit(code=2)

from opkura.cli import campaign as _campaign_module  # noqa: F401, E402
from opkura.cli import importer as _importer_module  # noqa: F401, E402
from opkura.cli import lint as _lint_module  # noqa: F401, E402
from opkura.cli import run as _run_module  # noqa: F401, E402

# Entry point aliases.
app = cli_app


def main():
    """Console-script entry point — invoked by `opkura` after `pip install -e .`."""
    cli_app()
