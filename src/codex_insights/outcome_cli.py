"""CLI presentation for conservative session outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from codex_insights.analytics.outcomes import OutcomeFilters, get_outcome_report
from codex_insights.analytics.queries import TimeExpressionError, parse_time_range
from codex_insights.config import resolve_codex_home, resolve_index_path
from codex_insights.db import UnsafeDatabasePathError

console = Console()


def register_outcome_command(app: typer.Typer) -> None:
    """Attach outcome analytics to the main application."""

    app.command("outcomes")(outcomes_command)


def outcomes_command(
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    repository: Annotated[str | None, typer.Option("--repo")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    outcome: Annotated[str | None, typer.Option("--outcome")] = None,
    confidence: Annotated[str | None, typer.Option("--confidence")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[Path | None, typer.Option("--codex-home", file_okay=False)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report provenance-aware outcomes, confidence, and UNKNOWN coverage."""

    valid_outcomes = {
        "success",
        "success_with_warnings",
        "partial",
        "failed",
        "abandoned",
        "no_change",
        "unknown",
    }
    if outcome is not None and outcome.casefold() not in valid_outcomes:
        raise typer.BadParameter("Unknown outcome label", param_hint="--outcome")
    if confidence is not None and confidence.casefold() not in {"high", "medium", "low"}:
        raise typer.BadParameter("Use high, medium, or low", param_hint="--confidence")
    try:
        parsed_since, parsed_until = parse_time_range(since, until)
        report = get_outcome_report(
            resolve_index_path(database),
            codex_home=resolve_codex_home(codex_home).path,
            filters=OutcomeFilters(
                since=parsed_since,
                until=parsed_until,
                repository=repository,
                model=model,
                outcome=outcome,
                confidence=confidence,
                limit=limit,
            ),
        )
    except TimeExpressionError as exc:
        raise typer.BadParameter(str(exc), param_hint="--since/--until") from exc
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    if json_output:
        typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return

    summary = Table(title="Session outcomes", show_header=False, box=None, pad_edge=False)
    summary.add_column("Metric", style="bold cyan")
    summary.add_column("Value", justify="right")
    summary.add_row("Sessions", f"{report.session_count:,}")
    summary.add_row("Classifiable", f"{report.classifiable_count:,}")
    summary.add_row("UNKNOWN", f"{report.unknown_count:,}")
    console.print(summary)

    outcomes = Table(title="Outcome distribution", box=None, pad_edge=False)
    outcomes.add_column("Outcome")
    outcomes.add_column("Count", justify="right")
    outcomes.add_column("Among classifiable", justify="right")
    for label, count in report.outcomes:
        fraction = (
            f"{count / report.classifiable_count:.1%}"
            if report.classifiable_count and label != "unknown"
            else "—"
        )
        outcomes.add_row(label, f"{count:,}", fraction)
    console.print(outcomes)

    confidence_table = Table(title="Confidence", box=None, pad_edge=False)
    confidence_table.add_column("Level")
    confidence_table.add_column("Count", justify="right")
    for label, count in report.confidence:
        confidence_table.add_row(label, f"{count:,}")
    console.print(confidence_table)
