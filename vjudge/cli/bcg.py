"""`vjudge bcg` subcommand — generate a Barge Context Document."""
from __future__ import annotations

import asyncio

import typer

from vjudge.cli.main import _setup_logging, cli_app, console


@cli_app.command()
def bcg(
    prompt_path: str = typer.Argument(..., help="Path to the bot's system prompt file."),
    output: str = typer.Option(..., "-o", "--output", help="Output BCD path (e.g. clients/foo/bargin-context.md)."),
    model: str = typer.Option("gemini-2.5-pro", "--model", help="LLM model for extraction."),
    regenerate: bool = typer.Option(False, "--regenerate", help="Overwrite existing output (default: refuse, to protect hand edits)."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Generate a Barge Context Document from a bot's system prompt."""
    _setup_logging(verbose)
    from vjudge.bcg.generator import generate_bcd

    console.print(f"Analyzing prompt with [bold]{model}[/]...")
    try:
        path = asyncio.run(
            generate_bcd(
                prompt_path=prompt_path,
                output_path=output,
                model=model,
                regenerate=regenerate,
            )
        )
    except FileExistsError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(code=2)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(code=1)
    console.print(f"[green]BCD written to[/] {path}")
