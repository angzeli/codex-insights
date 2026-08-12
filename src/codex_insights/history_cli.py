"""Terminal and JSON presentation for normalized session-history analytics."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from codex_insights.analytics import (
    AmbiguousSessionIdError,
    ModelSummary,
    RepositorySummary,
    SessionDetail,
    SessionFilters,
    SessionListItem,
    SessionNotFoundError,
    StatsSummary,
    TimeExpressionError,
    get_session,
    get_stats,
    list_models,
    list_repositories,
    list_sessions,
    parse_time_range,
)
from codex_insights.analytics.git import CommitAssociationItem, list_session_commits
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
JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Emit machine-readable JSON."),
]


def register_history_commands(app: typer.Typer) -> None:
    """Attach normalized history commands to the main Typer application."""

    app.command("sessions")(sessions_command)
    app.command("session")(session_command)
    app.command("repos")(repos_command)
    app.command("models")(models_command)
    app.command("stats")(stats_command)


def sessions_command(
    since: Annotated[
        str | None,
        typer.Option("--since", help="Inclusive ISO time or relative duration, such as 7d."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option(
            "--until",
            help="Exclusive ISO timestamp, whole calendar date, or relative duration.",
        ),
    ] = None,
    repository: Annotated[
        str | None,
        typer.Option("--repo", help="Repository name/path, or outside-git."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Exact model name, or unknown."),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Exact client source, such as cli or editor."),
    ] = None,
    archived: Annotated[
        bool | None,
        typer.Option(
            "--archived/--active",
            help="Show only archived or only active sessions.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=1000, help="Maximum sessions to return."),
    ] = 50,
    database: DatabaseOption = None,
    codex_home: CodexHomeOption = None,
    json_output: JsonOption = False,
) -> None:
    """List indexed sessions with practical filters."""

    try:
        parsed_since, parsed_until = parse_time_range(since, until)
        rows = list_sessions(
            resolve_index_path(database),
            codex_home=resolve_codex_home(codex_home).path,
            filters=SessionFilters(
                since=parsed_since,
                until=parsed_until,
                repository=repository,
                model=model,
                source=source,
                archived=archived,
                limit=limit,
            ),
        )
    except TimeExpressionError as exc:
        raise typer.BadParameter(str(exc), param_hint="--since/--until") from exc
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc

    if json_output:
        _emit_json([row.to_dict() for row in rows])
        return
    _render_sessions(rows)


def session_command(
    session_id: Annotated[
        str,
        typer.Argument(help="Full normalized session ID or an unambiguous prefix."),
    ],
    database: DatabaseOption = None,
    codex_home: CodexHomeOption = None,
    commits: Annotated[
        bool,
        typer.Option("--commits", help="Include provenance-aware Git associations."),
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Inspect normalized metadata for one session without transcript content."""

    try:
        database_path = resolve_index_path(database)
        source_home = resolve_codex_home(codex_home).path
        detail = get_session(
            database_path,
            session_id,
            codex_home=source_home,
        )
        commit_rows = (
            list_session_commits(database_path, session_id, codex_home=source_home)
            if commits
            else ()
        )
    except SessionNotFoundError as exc:
        raise typer.BadParameter(str(exc), param_hint="SESSION_ID") from exc
    except AmbiguousSessionIdError as exc:
        matches = ", ".join(_abbreviate_id(item, length=20) for item in exc.matches[:5])
        raise typer.BadParameter(
            f"Ambiguous session prefix {exc.prefix!r}; matches include: {matches}",
            param_hint="SESSION_ID",
        ) from exc
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc

    if json_output:
        payload = detail.to_dict()
        if commits:
            payload["commits"] = [row.to_dict() for row in commit_rows]
        _emit_json(payload)
        return
    _render_session(detail)
    if commits:
        _render_session_commits(commit_rows)


def repos_command(
    database: DatabaseOption = None,
    codex_home: CodexHomeOption = None,
    json_output: JsonOption = False,
) -> None:
    """Aggregate indexed sessions by repository, including non-Git work."""

    try:
        rows = list_repositories(
            resolve_index_path(database),
            codex_home=resolve_codex_home(codex_home).path,
        )
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    if json_output:
        _emit_json([row.to_dict() for row in rows])
        return
    _render_repositories(rows)


def models_command(
    database: DatabaseOption = None,
    codex_home: CodexHomeOption = None,
    json_output: JsonOption = False,
) -> None:
    """Aggregate indexed sessions and reconciled tokens by model."""

    try:
        rows = list_models(
            resolve_index_path(database),
            codex_home=resolve_codex_home(codex_home).path,
        )
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    if json_output:
        _emit_json([row.to_dict() for row in rows])
        return
    _render_models(rows)


def stats_command(
    database: DatabaseOption = None,
    codex_home: CodexHomeOption = None,
    json_output: JsonOption = False,
) -> None:
    """Show activity totals and token-data coverage for the index."""

    try:
        stats = get_stats(
            resolve_index_path(database),
            codex_home=resolve_codex_home(codex_home).path,
        )
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    if json_output:
        _emit_json(stats.to_dict())
        return
    _render_stats(stats)


def _render_sessions(rows: tuple[SessionListItem, ...]) -> None:
    if not rows:
        console.print("[dim]No sessions matched.[/dim]")
        return
    table = Table(box=None, pad_edge=False, collapse_padding=True)
    table.add_column("Start", no_wrap=True)
    table.add_column("Duration", width=8, no_wrap=True, justify="right")
    table.add_column("Repository", width=10, no_wrap=True, overflow="ellipsis")
    table.add_column("Model", width=10, no_wrap=True, overflow="ellipsis")
    table.add_column("Tokens", no_wrap=True, justify="right")
    table.add_column("Events", no_wrap=True, justify="right")
    table.add_column("Session", no_wrap=True)
    for row in rows:
        table.add_row(
            _format_list_timestamp(row.started_at),
            _format_duration(row.duration_seconds),
            row.repository,
            row.model or "unknown",
            _format_count(row.total_tokens),
            _format_count(row.event_count),
            _abbreviate_id(row.session_id, length=9),
        )
    console.print(table)


def _render_session(detail: SessionDetail) -> None:
    metadata = Table(title="Session", show_header=False, box=None, pad_edge=False)
    metadata.add_column("Field", style="bold cyan", no_wrap=True)
    metadata.add_column("Value", overflow="fold")
    metadata.add_row("ID", detail.session_id)
    metadata.add_row(
        "Time range",
        f"{_format_timestamp(detail.started_at)} → "
        f"{_format_timestamp(detail.apparent_ended_at)} "
        f"({_format_duration(detail.duration_seconds)})",
    )
    metadata.add_row("Updated", _format_timestamp(detail.updated_at))
    source = detail.client_kind
    if detail.client_source and detail.client_source.casefold() != detail.client_kind:
        source = f"{detail.client_kind} ({detail.client_source})"
    if detail.subagent_source_kind:
        source = f"{source}: {detail.subagent_source_kind}"
    metadata.add_row("Source", source)
    metadata.add_row("CWD", str(detail.cwd) if detail.cwd else "unknown")
    metadata.add_row(
        "Repository",
        str(detail.repository_root) if detail.repository_root else "Outside Git repositories",
    )
    metadata.add_row("Branch", detail.git_branch or "unknown")
    metadata.add_row("Git SHA", detail.git_sha or "unknown")
    model = detail.model or "unknown"
    if detail.model_provider:
        model = f"{model} ({detail.model_provider})"
    metadata.add_row("Model", model)
    metadata.add_row("Codex version", detail.codex_version or "unknown")
    metadata.add_row("Archived", "yes" if detail.archived else "no")
    console.print(metadata)

    usage = Table(title="Token usage", box=None, pad_edge=False)
    usage.add_column("Semantics")
    usage.add_column("Input", justify="right")
    usage.add_column("Cached", justify="right")
    usage.add_column("Cache write", justify="right")
    usage.add_column("Output", justify="right")
    usage.add_column("Reasoning", justify="right")
    usage.add_column("Total", justify="right")
    usage.add_row(
        detail.usage.semantics,
        _format_count(detail.usage.input_tokens, unknown="unknown"),
        _format_count(detail.usage.cached_input_tokens, unknown="unknown"),
        _format_count(detail.usage.cache_write_input_tokens, unknown="unknown"),
        _format_count(detail.usage.output_tokens, unknown="unknown"),
        _format_count(detail.usage.reasoning_output_tokens, unknown="unknown"),
        _format_count(detail.usage.total_tokens, unknown="unknown"),
    )
    console.print(usage)

    outcome = Table(title="Outcome", show_header=False, box=None, pad_edge=False)
    outcome.add_column("Field", style="bold cyan", no_wrap=True)
    outcome.add_column("Value", overflow="fold")
    outcome.add_row("Classification", detail.outcome.outcome)
    outcome.add_row("Confidence", detail.outcome.confidence)
    outcome.add_row("Evidence", ", ".join(detail.outcome.evidence) or "none")
    outcome.add_row("Classifier", detail.outcome.classifier_version or "not classified")
    console.print(outcome)

    events = Table(title="Event categories", box=None, pad_edge=False)
    events.add_column("Category")
    events.add_column("Count", justify="right")
    if detail.event_counts:
        for category, count in detail.event_counts:
            events.add_row(category, f"{count:,}")
    elif detail.source_coverage.status not in {"indexed", "indexed_with_warnings"}:
        events.add_row("unavailable", "—")
    else:
        events.add_row("none observed", "—")
    console.print(events)

    tools = Table(title="Tool activity", show_header=False, box=None, pad_edge=False)
    tools.add_column("Metric", style="bold cyan", no_wrap=True)
    tools.add_column("Value", overflow="fold")
    tools.add_row("Originated", f"{detail.tool_activity.originated:,}")
    tools.add_row("Inherited/replayed", f"{detail.tool_activity.inherited:,}")
    tools.add_row("Ambiguous", f"{detail.tool_activity.ambiguous:,}")
    tools.add_row("Unknown provenance", f"{detail.tool_activity.unknown:,}")
    tools.add_row("Failed classified results", f"{detail.tool_activity.failed_results:,}")
    if detail.tool_activity.command_categories:
        tools.add_row(
            "Categories",
            ", ".join(
                f"{category}={count}"
                for category, count in detail.tool_activity.command_categories
            ),
        )
    console.print(tools)

    coverage = Table(title="Source coverage", show_header=False, box=None, pad_edge=False)
    coverage.add_column("Field", style="bold cyan", no_wrap=True)
    coverage.add_column("Value", overflow="fold")
    coverage.add_row("Status", detail.source_coverage.status)
    coverage.add_row("Parser", detail.source_coverage.parser_version or "unknown")
    coverage.add_row("Source schema", detail.source_coverage.source_schema_version or "unknown")
    coverage.add_row(
        "Source bytes",
        _format_count(detail.source_coverage.size_bytes, unknown="unknown"),
    )
    coverage.add_row(
        "Parsed bytes",
        _format_count(detail.source_coverage.parsed_byte_offset, unknown="unknown"),
    )
    coverage.add_row("Indexed", _format_timestamp(detail.source_coverage.indexed_at))
    console.print(coverage)
    if detail.warnings:
        console.print("[bold yellow]Warnings[/bold yellow]")
        for warning in detail.warnings:
            console.print(f"[yellow]- {warning}[/yellow]")


def _render_repositories(rows: tuple[RepositorySummary, ...]) -> None:
    if not rows:
        console.print("[dim]No indexed sessions.[/dim]")
        return
    table = Table(box=None, pad_edge=False, collapse_padding=True)
    table.add_column("Repository", max_width=28, overflow="ellipsis")
    table.add_column("Sessions", justify="right")
    table.add_column("First", no_wrap=True)
    table.add_column("Latest", no_wrap=True)
    table.add_column("Reconciled tokens", justify="right")
    table.add_column("Token data", justify="right")
    for row in rows:
        table.add_row(
            row.repository,
            f"{row.session_count:,}",
            _format_date(row.first_activity),
            _format_date(row.latest_activity),
            _format_count(row.total_known_tokens, unknown="unknown"),
            f"{row.sessions_with_token_data}/{row.session_count}",
        )
    console.print(table)


def _render_session_commits(rows: tuple[CommitAssociationItem, ...]) -> None:
    table = Table(title="Commit associations", box=None, pad_edge=False)
    table.add_column("Commit")
    table.add_column("Confidence")
    table.add_column("Evidence", overflow="fold")
    if not rows:
        table.add_row("none", "—", "No association evidence")
    for row in rows:
        table.add_row(row.commit_hash[:12], row.confidence.upper(), row.evidence_type)
    console.print(table)


def _render_models(rows: tuple[ModelSummary, ...]) -> None:
    if not rows:
        console.print("[dim]No indexed sessions.[/dim]")
        return
    table = Table(box=None, pad_edge=False, collapse_padding=True)
    table.add_column("Model", max_width=24, overflow="ellipsis")
    table.add_column("Provider", max_width=14, overflow="ellipsis")
    table.add_column("Sessions", justify="right")
    table.add_column("First", no_wrap=True)
    table.add_column("Latest", no_wrap=True)
    table.add_column("Reconciled tokens", justify="right")
    table.add_column("Token data", justify="right")
    for row in rows:
        table.add_row(
            row.model,
            row.model_provider or "unknown",
            f"{row.session_count:,}",
            _format_date(row.first_activity),
            _format_date(row.latest_activity),
            _format_count(row.total_known_tokens, unknown="unknown"),
            f"{row.sessions_with_token_data}/{row.session_count}",
        )
    console.print(table)


def _render_stats(stats: StatsSummary) -> None:
    table = Table(title="Codex Insights stats", show_header=False, box=None, pad_edge=False)
    table.add_column("Metric", style="bold cyan", no_wrap=True)
    table.add_column("Value", justify="right")
    table.add_row("Indexed sessions", f"{stats.indexed_sessions:,}")
    table.add_row("Session-start active days", f"{stats.active_days:,}")
    table.add_row("Repositories", f"{stats.repositories:,}")
    table.add_row("First activity", _format_timestamp(stats.first_activity))
    table.add_row("Latest activity", _format_timestamp(stats.latest_activity))
    table.add_row("Sessions today", f"{stats.sessions_today:,}")
    table.add_row("Sessions last 7 days", f"{stats.sessions_last_7_days:,}")
    table.add_row("Sessions last 30 days", f"{stats.sessions_last_30_days:,}")
    table.add_row(
        "Reconciled known tokens",
        _format_count(stats.total_known_tokens, unknown="unknown"),
    )
    coverage = (
        f"{stats.sessions_with_token_data}/{stats.indexed_sessions} "
        f"({stats.token_data_fraction:.1%})"
        if stats.token_data_fraction is not None
        else "unknown"
    )
    table.add_row("Sessions with token data", coverage)
    console.print(table)


def _emit_json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True))


def _format_timestamp(value: datetime | None) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M%z") if value else "unknown"


def _format_list_timestamp(value: datetime | None) -> str:
    return value.astimezone().strftime("%m-%d %H:%M") if value else "unknown"


def _format_date(value: datetime | None) -> str:
    return value.astimezone().strftime("%Y-%m-%d") if value else "unknown"


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{remaining_seconds}s"
    return f"{remaining_seconds}s"


def _format_count(value: int | None, *, unknown: str = "?") -> str:
    return f"{value:,}" if value is not None else unknown


def _abbreviate_id(session_id: str, *, length: int = 12) -> str:
    return session_id if len(session_id) <= length else session_id[:length] + "…"
