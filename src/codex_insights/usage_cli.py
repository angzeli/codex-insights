"""Terminal and JSON presentation for normalized token usage analytics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from codex_insights.analytics import (
    SessionFilters,
    TimeExpressionError,
    TimezoneError,
    UsageBreakdown,
    UsageGroup,
    UsageMetrics,
    UsageReconciliation,
    get_usage_report,
    parse_time_range,
    resolve_timezone,
)
from codex_insights.config import resolve_codex_home, resolve_index_path
from codex_insights.db import UnsafeDatabasePathError

console = Console()

DatabaseOption = Annotated[
    Path | None,
    typer.Option(
        "--db",
        help="Codex Insights database (defaults to the platform data directory).",
        dir_okay=False,
    ),
]
CodexHomeOption = Annotated[
    Path | None,
    typer.Option(
        "--codex-home",
        help="Codex home used only to enforce database path separation.",
        file_okay=False,
        dir_okay=True,
    ),
]


def register_usage_command(app: typer.Typer) -> None:
    """Attach the usage analytics command to the main application."""

    app.command("usage")(usage_command)


def usage_command(
    breakdown: Annotated[
        UsageBreakdown,
        typer.Option("--by", help="Break down usage by repo, model, day, or week."),
    ] = UsageBreakdown.SUMMARY,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Inclusive ISO time or duration such as 7d."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="Exclusive ISO time or inclusive calendar date."),
    ] = None,
    repository: Annotated[
        str | None,
        typer.Option("--repo", help="Normalized repository name/path, or outside-git."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Exact normalized model name, or unknown."),
    ] = None,
    timezone: Annotated[
        str,
        typer.Option(
            "--timezone",
            help="Timezone for dates and day/week buckets: local, UTC, or an IANA name.",
        ),
    ] = "local",
    top: Annotated[
        int | None,
        typer.Option("--top", min=1, help="Limit repository/model groups."),
    ] = None,
    database: DatabaseOption = None,
    codex_home: CodexHomeOption = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
    reconciliation: Annotated[
        bool,
        typer.Option(
            "--reconciliation",
            help="Show observed, inherited/replayed, reconciled, and ambiguous totals.",
        ),
    ] = False,
) -> None:
    """Report token usage, rates, percentiles, and source-data coverage."""

    try:
        zone = resolve_timezone(timezone)
        parsed_since, parsed_until = parse_time_range(
            since,
            until,
            timezone=zone.timezone,
        )
        report = get_usage_report(
            resolve_index_path(database),
            codex_home=resolve_codex_home(codex_home).path,
            breakdown=breakdown,
            filters=SessionFilters(
                since=parsed_since,
                until=parsed_until,
                repository=repository,
                model=model,
                limit=1,
            ),
            timezone=zone,
            top=top,
            include_reconciliation=reconciliation,
        )
    except TimeExpressionError as exc:
        raise typer.BadParameter(str(exc), param_hint="--since/--until") from exc
    except TimezoneError as exc:
        raise typer.BadParameter(str(exc), param_hint="--timezone") from exc
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--top") from exc

    if json_output:
        typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return
    _render_usage(
        report.metrics,
        report.groups,
        report.timezone,
        report.breakdown,
        report.reconciliation,
    )


def _render_usage(
    metrics: UsageMetrics,
    groups: tuple[UsageGroup, ...],
    timezone: str,
    breakdown: UsageBreakdown,
    reconciliation: UsageReconciliation | None,
) -> None:
    coverage = metrics.coverage.total_tokens
    fraction = coverage / metrics.session_count if metrics.session_count else None
    if metrics.total_tokens is None:
        covered_sessions = f"{coverage}/{metrics.session_count}"
        headline = f"Known token totals unavailable across {covered_sessions} sessions"
    else:
        headline = (
            f"{_format_compact(metrics.total_tokens)} reconciled tokens across "
            f"{coverage}/{metrics.session_count} sessions with token records"
        )
    if fraction is not None:
        headline += f" ({fraction:.1%})"
    console.print(f"[bold cyan]{headline}[/bold cyan]")
    console.print(f"[dim]Timezone: {timezone}[/dim]")

    summary = Table(show_header=False, box=None, pad_edge=False)
    summary.add_column("Metric", style="bold", no_wrap=True)
    summary.add_column("Value", justify="right")
    summary.add_column("Coverage", justify="right")
    for label, value, field_coverage in (
        ("Reconciled total", metrics.total_tokens, metrics.coverage.total_tokens),
        ("Input tokens", metrics.input_tokens, metrics.coverage.input_tokens),
        ("Cached input", metrics.cached_input_tokens, metrics.coverage.cached_input_tokens),
        ("Output tokens", metrics.output_tokens, metrics.coverage.output_tokens),
        (
            "Reasoning output",
            metrics.reasoning_output_tokens,
            metrics.coverage.reasoning_output_tokens,
        ),
    ):
        summary.add_row(
            label,
            _format_compact(value),
            f"{field_coverage}/{metrics.session_count}",
        )
    if metrics.observed_total_tokens != metrics.total_tokens:
        summary.add_row(
            "Observed rollout total",
            _format_compact(metrics.observed_total_tokens),
            f"{metrics.coverage.total_tokens}/{metrics.session_count}",
        )
    summary.add_row(
        "Observed mean tokens/session",
        _format_number(metrics.mean_tokens_per_session),
        "known",
    )
    summary.add_row(
        "Observed median tokens/session",
        _format_number(metrics.median_tokens_per_session),
        "known",
    )
    summary.add_row(
        "Observed P90 tokens/session",
        _format_number(metrics.p90_tokens_per_session),
        "known",
    )
    summary.add_row("Sessions/day", _format_rate(metrics.sessions_per_day), "all")
    console.print(summary)

    if reconciliation is not None:
        _render_reconciliation(reconciliation)

    if breakdown is UsageBreakdown.SUMMARY:
        return
    if not groups:
        console.print("[dim]No matching sessions.[/dim]")
        return
    table = Table(box=None, pad_edge=False, collapse_padding=True)
    table.add_column(_group_heading(breakdown), max_width=24, overflow="ellipsis")
    if breakdown is UsageBreakdown.MODEL:
        table.add_column("Provider", max_width=14, overflow="ellipsis")
    table.add_column("Sessions", justify="right")
    if reconciliation is not None:
        table.add_column("Observed tokens", justify="right")
    table.add_column("Reconciled tokens", justify="right")
    table.add_column("Token data", justify="right")
    table.add_column("Mean/session", justify="right")
    table.add_column("P90", justify="right")
    table.add_column("Sessions/day", justify="right")
    for group in groups:
        row = [
            group.label,
            f"{group.metrics.session_count:,}",
            _format_compact(group.metrics.total_tokens),
            f"{group.metrics.coverage.total_tokens}/{group.metrics.session_count}",
            _format_number(group.metrics.mean_tokens_per_session),
            _format_number(group.metrics.p90_tokens_per_session),
            _format_rate(group.metrics.sessions_per_day),
        ]
        if reconciliation is not None:
            row.insert(2, _format_compact(group.metrics.observed_total_tokens))
        if breakdown is UsageBreakdown.MODEL:
            row.insert(1, group.model_provider or "unknown")
        table.add_row(*row)
    console.print(table)


def _render_reconciliation(reconciliation: UsageReconciliation) -> None:
    table = Table(title="Token reconciliation", show_header=False, box=None, pad_edge=False)
    table.add_column("Metric", style="bold", no_wrap=True)
    table.add_column("Value", justify="right")
    for label, value in (
        ("Observed rollout sum", reconciliation.observed_rollout_tokens),
        ("Inherited/replayed usage", reconciliation.inherited_replayed_tokens),
        ("Reconciled aggregate", reconciliation.reconciled_tokens),
        ("Ambiguous observed usage", reconciliation.ambiguous_observed_tokens),
    ):
        table.add_row(label, _format_integer(value))
    for label, value in (
        ("Root threads", reconciliation.root_threads),
        ("Child threads", reconciliation.child_threads),
        ("Confidently reconciled", reconciliation.confidently_reconciled_children),
        ("Independent", reconciliation.independent_children),
        ("Ambiguous", reconciliation.ambiguous_children),
        ("Unavailable", reconciliation.unavailable_children),
        ("Cyclic", reconciliation.cyclic_children),
        ("Orphan relationships", reconciliation.orphan_relationships),
    ):
        table.add_row(label, f"{value:,}")
    coverage = reconciliation.child_reconciliation_coverage
    table.add_row(
        "Child reconciliation coverage",
        f"{coverage:.1%}" if coverage is not None else "n/a",
    )
    console.print(table)


def _group_heading(breakdown: UsageBreakdown) -> str:
    return {
        UsageBreakdown.REPOSITORY: "Repository",
        UsageBreakdown.MODEL: "Model",
        UsageBreakdown.DAY: "Day",
        UsageBreakdown.WEEK: "Week starting",
    }.get(breakdown, "Group")


def _format_compact(value: int | None) -> str:
    if value is None:
        return "unknown"
    scales = (
        (1_000_000_000_000, "T"),
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    )
    for divisor, suffix in scales:
        if abs(value) >= divisor:
            scaled = value / divisor
            precision = 1 if scaled >= 10 else 2
            return f"{scaled:.{precision}f}{suffix}"
    return f"{value:,}"


def _format_number(value: float | None) -> str:
    return f"{value:,.1f}" if value is not None else "unknown"


def _format_integer(value: int | None) -> str:
    return f"{value:,}" if value is not None else "unknown"


def _format_rate(value: float | None) -> str:
    return f"{value:,.2f}" if value is not None else "unknown"
