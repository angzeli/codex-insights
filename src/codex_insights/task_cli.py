"""CLI presentation for explainable task taxonomy analytics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from codex_insights.analytics.queries import TimeExpressionError, parse_time_range
from codex_insights.analytics.tasks import (
    TaskBreakdown,
    TaskFilters,
    TaskMetrics,
    get_task_report,
)
from codex_insights.config import resolve_codex_home, resolve_index_path
from codex_insights.db import UnsafeDatabasePathError

console = Console()


def register_task_command(app: typer.Typer) -> None:
    """Attach task taxonomy analytics."""

    app.command("tasks")(tasks_command)


def tasks_command(
    breakdown: Annotated[
        TaskBreakdown,
        typer.Option("--by", help="Break down by type or domain."),
    ] = TaskBreakdown.SUMMARY,
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    repository: Annotated[str | None, typer.Option("--repo")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    action: Annotated[str | None, typer.Option("--type")] = None,
    domain: Annotated[str | None, typer.Option("--domain")] = None,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[Path | None, typer.Option("--codex-home", file_okay=False)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report origin-intent task types, domains, and explainable coverage."""

    try:
        parsed_since, parsed_until = parse_time_range(since, until)
        report = get_task_report(
            resolve_index_path(database),
            codex_home=resolve_codex_home(codex_home).path,
            breakdown=breakdown,
            filters=TaskFilters(
                since=parsed_since,
                until=parsed_until,
                repository=repository,
                model=model,
                action=action,
                domain=domain,
            ),
        )
    except TimeExpressionError as exc:
        raise typer.BadParameter(str(exc), param_hint="--since/--until") from exc
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    if json_output:
        typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return

    _render_metrics("Task taxonomy", report.metrics)
    if report.groups:
        table = Table(box=None, pad_edge=False, collapse_padding=True)
        table.add_column("Task type" if breakdown is TaskBreakdown.TYPE else "Domain")
        table.add_column("Sessions", justify="right")
        table.add_column("Reconciled tokens", justify="right")
        table.add_column("Token data", justify="right")
        table.add_column("Commands", justify="right")
        table.add_column("HIGH commits", justify="right")
        for group in report.groups:
            metrics = group.metrics
            table.add_row(
                group.key,
                f"{metrics.session_count:,}",
                _count(metrics.reconciled_tokens),
                f"{metrics.sessions_with_token_data}/{metrics.session_count}",
                f"{metrics.originated_commands:,}",
                f"{metrics.high_confidence_commits:,}",
            )
        console.print(table)


def _render_metrics(title: str, metrics: TaskMetrics) -> None:
    table = Table(title=title, show_header=False, box=None, pad_edge=False)
    table.add_column("Metric", style="bold cyan")
    table.add_column("Value", justify="right")
    table.add_row("Sessions", f"{metrics.session_count:,}")
    table.add_row("Reconciled tokens", _count(metrics.reconciled_tokens))
    table.add_row(
        "Token coverage", f"{metrics.sessions_with_token_data}/{metrics.session_count}"
    )
    table.add_row("Observed median/session", _number(metrics.observed_median_tokens))
    table.add_row("Observed P90/session", _number(metrics.observed_p90_tokens))
    table.add_row("Originated commands", f"{metrics.originated_commands:,}")
    table.add_row("Logical prompts", f"{metrics.logical_prompts:,}")
    table.add_row("UNKNOWN tasks", f"{metrics.unknown_task_count:,}")
    console.print(table)


def _count(value: int | None) -> str:
    return f"{value:,}" if value is not None else "unknown"


def _number(value: float | None) -> str:
    return f"{value:,.0f}" if value is not None else "unknown"
