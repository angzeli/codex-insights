"""Command-line interface for Codex Insights."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from codex_insights import __version__
from codex_insights.adapters import CodexLocalAdapter
from codex_insights.config import resolve_codex_home

app = typer.Typer(
    name="codex-insights",
    help="Local-first, read-only analytics and observability for Codex sessions.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def version() -> None:
    """Show the installed Codex Insights version."""

    console.print(f"Codex Insights {__version__}")


@app.command()
def doctor(
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help="Codex home to inspect (overrides CODEX_HOME and ~/.codex).",
            file_okay=False,
            dir_okay=True,
        ),
    ] = None,
) -> None:
    """Report safe runtime and Codex path metadata without reading histories."""

    report = CodexLocalAdapter(resolve_codex_home(codex_home)).probe()

    summary = Table(title="Codex Insights doctor", show_header=False)
    summary.add_column("Item", style="bold cyan")
    summary.add_column("Value")
    summary.add_row("Python", report.python_version)
    summary.add_row("Platform", report.platform)
    summary.add_row(
        f"Codex home ({report.codex_home.source})",
        str(report.codex_home.path),
    )
    summary.add_row("Codex home exists", "yes" if report.codex_home_exists else "no")
    console.print(summary)

    locations = Table(title="Likely locations (existence only)")
    locations.add_column("Location")
    locations.add_column("Path")
    locations.add_column("Exists", justify="center")
    for location in report.locations:
        locations.add_row(location.label, str(location.path), "yes" if location.exists else "no")
    console.print(locations)

    if not report.codex_home_exists:
        console.print("[yellow]Codex home was not found; no session data was inspected.[/yellow]")


def main() -> None:
    """Run the command-line application."""

    app()


if __name__ == "__main__":
    main()
