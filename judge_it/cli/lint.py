"""`judge_it lint` subcommand — validate scenario YAMLs without making real calls."""
from __future__ import annotations

import asyncio

import typer
from rich.markup import escape as rich_escape

from judge_it.cli.main import _setup_logging, cli_app, console
from judge_it.core.config import Settings


@cli_app.command()
def lint(
    glob_pattern: str = typer.Argument(..., metavar="GLOB", help="Glob pattern for scenario YAMLs (e.g. 'clients/*/scenarios/*.yaml')."),
    probe: bool = typer.Option(False, "--probe", help="Also dry-run persona against a sample bot turn (costs ~$0.01 per scenario)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Validate scenario YAMLs without making real calls."""
    _setup_logging(verbose)
    import glob as _glob

    from judge_it.core.lint import (
        detect_file_kind,
        lint_campaign,
        lint_scenario,
        probe_persona,
    )

    paths = sorted(_glob.glob(glob_pattern, recursive=True))
    if not paths:
        console.print(f"[yellow]No files matched glob:[/] {glob_pattern}")
        raise typer.Exit(code=1)

    # Fast-fail when --probe is set but the API key isn't — otherwise every
    # scenario in the glob would print the same probe_no_api_key error
    # (40 identical lines for the Resurgent suite).
    api_key = ""
    if probe:
        api_key = Settings().gemini_api_key
        if not api_key:
            console.print(
                "[red]--probe requires JUDGE_IT_GEMINI_API_KEY to be set[/]"
            )
            raise typer.Exit(code=1)

    def _print_result(p: str, result, kind: str) -> bool:
        """Render one LintResult to console. Returns True if it passed."""
        if result.passed:
            tag = "CAMPAIGN" if kind == "campaign" else ""
            msg = f"[green]PASS[/] {tag + ' ' if tag else ''}{p}"
            if result.warnings:
                msg += f"  [yellow]({len(result.warnings)} warning(s))[/]"
            console.print(msg)
            for w in result.warnings:
                console.print(f"  [yellow]warning[/] {w.code}: {w.message}")
            return True
        tag = "CAMPAIGN" if kind == "campaign" else ""
        console.print(f"[red]FAIL[/] {tag + ' ' if tag else ''}{p}")
        for e in result.errors:
            # Field paths like `barge_in.triggers[0].at_turn` contain brackets
            # that Rich parses as style tags and drops. Escape preserves them.
            fld = f" ({rich_escape(e.field)})" if e.field else ""
            console.print(f"  [red]error[/] {e.code}{fld}: {e.message}")
        for w in result.warnings:
            console.print(f"  [yellow]warning[/] {w.code}: {w.message}")
        return False

    async def _run():
        from judge_it.core.campaign_loader import load as load_campaign

        all_pass = True
        failed_count = 0
        for p in paths:
            kind = detect_file_kind(p)
            if kind == "campaign":
                # Lint the campaign file itself first. If it loads, recurse
                # into each referenced scenario so per-scenario errors are
                # surfaced under each scenario's filename — much easier to
                # triage than a single rolled-up campaign error.
                campaign_result = lint_campaign(p)
                if not _print_result(p, campaign_result, "campaign"):
                    all_pass = False
                    failed_count += 1
                    continue
                # Campaign passed schema validation — load() succeeded, so
                # resolved_scenarios is populated. Lint each.
                campaign = load_campaign(p)
                for sc_path in campaign.resolved_scenarios:
                    sc_result = lint_scenario(sc_path)
                    if probe and sc_result.passed:
                        probe_errs = await probe_persona(sc_path, api_key)
                        sc_result.errors.extend(probe_errs)
                    if not _print_result(f"  ↳ {sc_path}", sc_result, "scenario"):
                        all_pass = False
                        failed_count += 1
                continue

            # Default path: treat as a scenario file.
            result = lint_scenario(p)
            if probe and result.passed:
                probe_errs = await probe_persona(p, api_key)
                result.errors.extend(probe_errs)
            if not _print_result(p, result, kind):
                all_pass = False
                failed_count += 1

        console.print()
        if all_pass:
            console.print(f"[green]{len(paths)} file(s) checked, 0 failed[/]")
        else:
            console.print(
                f"[red]{len(paths)} file(s) checked, {failed_count} failed[/]"
            )
        return 0 if all_pass else 1

    code = asyncio.run(_run())
    raise typer.Exit(code=code)
