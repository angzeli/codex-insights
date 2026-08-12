"""CLI presentation for conservative Git commit correlation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from codex_insights.analytics.git import (
    AmbiguousCommitHashError,
    CommitAssociationItem,
    CommitNotFoundError,
    GitFilters,
    get_commit,
    get_commit_report,
)
from codex_insights.analytics.queries import TimeExpressionError, parse_time_range
from codex_insights.config import resolve_codex_home, resolve_index_path
from codex_insights.db import UnsafeDatabasePathError

console = Console()


def register_git_commands(app: typer.Typer) -> None:
    """Attach commit list/detail commands."""

    app.command("commits")(commits_command)
    app.command("commit")(commit_command)


def commits_command(
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    repository: Annotated[str | None, typer.Option("--repo")] = None,
    confidence: Annotated[
        str | None,
        typer.Option("--confidence", help="high, medium, or low."),
    ] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[Path | None, typer.Option("--codex-home", file_okay=False)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List confidence-tiered session-to-commit associations."""

    normalized_confidence = confidence.casefold() if confidence else None
    if normalized_confidence not in {None, "high", "medium", "low"}:
        raise typer.BadParameter("Use high, medium, or low", param_hint="--confidence")
    try:
        parsed_since, parsed_until = parse_time_range(since, until)
        report = get_commit_report(
            resolve_index_path(database),
            codex_home=resolve_codex_home(codex_home).path,
            filters=GitFilters(
                since=parsed_since,
                until=parsed_until,
                repository=repository,
                confidence=normalized_confidence,
                model=model,
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

    summary = Table(title="Git correlation", show_header=False, box=None, pad_edge=False)
    summary.add_column("Metric", style="bold cyan")
    summary.add_column("Value", justify="right")
    summary.add_row("HIGH associations", f"{report.high:,}")
    summary.add_row("MEDIUM candidates", f"{report.medium:,}")
    summary.add_row("LOW candidates", f"{report.low:,}")
    summary.add_row("Ambiguous", f"{report.ambiguous:,}")
    summary.add_row("Timing candidates considered", f"{report.timing_candidates_considered:,}")
    summary.add_row("Timing candidates omitted", f"{report.timing_candidates_omitted:,}")
    summary.add_row("Resolved repositories", f"{report.repositories_resolved:,}")
    ratio = (
        f"{report.reconciled_tokens_per_confirmed_commit:,.0f}"
        if report.reconciled_tokens_per_confirmed_commit is not None
        else "unknown"
    )
    summary.add_row("Reconciled tokens/HIGH commit", ratio)
    console.print(summary)
    _render_associations(report.associations)


def commit_command(
    commit_hash: Annotated[str, typer.Argument(help="Full commit hash or unique prefix.")],
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[Path | None, typer.Option("--codex-home", file_okay=False)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect the evidence for one indexed commit without commit content."""

    try:
        rows = get_commit(
            resolve_index_path(database),
            commit_hash,
            codex_home=resolve_codex_home(codex_home).path,
        )
    except (CommitNotFoundError, AmbiguousCommitHashError) as exc:
        raise typer.BadParameter(str(exc), param_hint="HASH") from exc
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    if json_output:
        typer.echo(json.dumps([row.to_dict() for row in rows], indent=2, sort_keys=True))
        return
    _render_associations(rows)


def _render_associations(rows: tuple[CommitAssociationItem, ...]) -> None:
    if not rows:
        console.print("[dim]No commit associations matched.[/dim]")
        return
    table = Table(box=None, pad_edge=False, collapse_padding=True)
    table.add_column("Commit", no_wrap=True)
    table.add_column("Time", no_wrap=True)
    table.add_column("Repository", max_width=22, overflow="ellipsis")
    table.add_column("Confidence", no_wrap=True)
    table.add_column("Session", no_wrap=True)
    table.add_column("Evidence", max_width=34, overflow="fold")
    for row in rows:
        table.add_row(
            row.commit_hash[:10],
            row.committed_at.astimezone().strftime("%Y-%m-%d %H:%M"),
            row.repository,
            row.confidence.upper(),
            row.session_id[:10],
            row.evidence_type,
        )
    console.print(table)
