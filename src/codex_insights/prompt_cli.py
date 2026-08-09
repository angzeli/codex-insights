"""CLI presentation for origin-aware prompt history and FTS5 search."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from codex_insights.analytics.prompts import (
    AmbiguousPromptIdError,
    PromptDetail,
    PromptFilters,
    PromptNotFoundError,
    PromptSearchQueryError,
    get_prompt,
    list_prompts,
    search_prompts,
)
from codex_insights.analytics.queries import TimeExpressionError, parse_time_range
from codex_insights.config import resolve_codex_home, resolve_index_path
from codex_insights.db import UnsafeDatabasePathError

console = Console()
_LONG_PROMPT_THRESHOLD = 4_000


def register_prompt_commands(app: typer.Typer) -> None:
    app.command("prompts")(prompts_command)
    app.command("prompt")(prompt_command)
    app.command("search")(search_command)


def prompts_command(
    since: Annotated[str | None, typer.Option("--since", help="Inclusive time boundary.")] = None,
    until: Annotated[str | None, typer.Option("--until", help="Exclusive time boundary.")] = None,
    repository: Annotated[
        str | None, typer.Option("--repo", help="Normalized repository name or root.")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="Normalized model.")] = None,
    session: Annotated[
        str | None, typer.Option("--session", help="Origin session ID or prefix.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 50,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None, typer.Option("--codex-home", file_okay=False, dir_okay=True)
    ] = None,
) -> None:
    """List logical user prompts at their confidently known origin."""

    filters = _filters(since, until, repository, model, session, limit)
    resolution = resolve_codex_home(codex_home)
    try:
        rows = list_prompts(
            resolve_index_path(database),
            codex_home=resolution.path,
            filters=filters,
        )
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    if json_output:
        typer.echo(json.dumps([row.to_dict() for row in rows], indent=2, sort_keys=True))
        return
    table = Table(title="Origin-aware user prompts")
    table.add_column("Time", no_wrap=True)
    table.add_column("Repository", overflow="ellipsis")
    table.add_column("Model", overflow="ellipsis")
    table.add_column("Prompt ID", no_wrap=True)
    table.add_column("Replay", justify="right")
    table.add_column("Prompt", overflow="fold")
    for row in rows:
        table.add_row(
            _format_timestamp(row.occurred_at),
            row.repository,
            row.model or "unknown",
            _short_prompt_id(row.prompt_id),
            str(row.replay_session_count),
            Text(row.snippet),
        )
    console.print(table)
    console.print(f"{len(rows)} logical prompts shown; replay copies are not separate rows.")


def prompt_command(
    prompt_id: str,
    full: Annotated[
        bool,
        typer.Option("--full", help="Show stored text even when it is unusually long."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None, typer.Option("--codex-home", file_okay=False, dir_okay=True)
    ] = None,
) -> None:
    """Show one stored, redacted logical prompt by full ID or unique prefix."""

    resolution = resolve_codex_home(codex_home)
    try:
        detail = get_prompt(
            resolve_index_path(database),
            codex_home=resolution.path,
            prompt_prefix=prompt_id,
        )
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    except PromptNotFoundError as exc:
        raise typer.BadParameter(f"No prompt matches {exc.args[0]!r}.") from exc
    except AmbiguousPromptIdError as exc:
        raise typer.BadParameter(f"Prompt prefix {exc.args[0]!r} is ambiguous.") from exc
    include_text = full or detail.stored_character_count <= _LONG_PROMPT_THRESHOLD
    if json_output:
        payload = detail.to_dict(include_text=include_text)
        if not include_text:
            payload["text_preview"] = detail.text[:_LONG_PROMPT_THRESHOLD]
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    _render_prompt_detail(detail, include_text=include_text)


def search_command(
    query: str,
    since: Annotated[str | None, typer.Option("--since", help="Inclusive time boundary.")] = None,
    until: Annotated[str | None, typer.Option("--until", help="Exclusive time boundary.")] = None,
    repository: Annotated[
        str | None, typer.Option("--repo", help="Normalized repository name or root.")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="Normalized model.")] = None,
    session: Annotated[
        str | None, typer.Option("--session", help="Origin session ID or prefix.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 50,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None, typer.Option("--codex-home", file_okay=False, dir_okay=True)
    ] = None,
) -> None:
    """Search redacted origin-aware user prompts with SQLite FTS5."""

    filters = _filters(since, until, repository, model, session, limit)
    resolution = resolve_codex_home(codex_home)
    try:
        rows = search_prompts(
            resolve_index_path(database),
            codex_home=resolution.path,
            query=query,
            filters=filters,
        )
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    except PromptSearchQueryError as exc:
        raise typer.BadParameter(str(exc), param_hint="QUERY") from exc
    if json_output:
        typer.echo(json.dumps([row.to_dict() for row in rows], indent=2, sort_keys=True))
        return
    table = Table(title="Prompt search")
    table.add_column("Time", no_wrap=True)
    table.add_column("Repository", overflow="ellipsis")
    table.add_column("Model", overflow="ellipsis")
    table.add_column("Prompt ID", no_wrap=True)
    table.add_column("Origin session", no_wrap=True)
    table.add_column("Match", overflow="fold")
    for row in rows:
        table.add_row(
            _format_timestamp(row.occurred_at),
            row.repository,
            row.model or "unknown",
            _short_prompt_id(row.prompt_id),
            row.origin_session_id[:12],
            Text(row.snippet),
        )
    console.print(table)
    console.print(f"{len(rows)} origin-aware matches; replay copies are not separate hits.")


def _filters(
    since: str | None,
    until: str | None,
    repository: str | None,
    model: str | None,
    session: str | None,
    limit: int,
) -> PromptFilters:
    try:
        parsed_since, parsed_until = parse_time_range(since, until)
    except TimeExpressionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return PromptFilters(
        since=parsed_since,
        until=parsed_until,
        repository=repository,
        model=model,
        session=session,
        limit=limit,
    )


def _render_prompt_detail(detail: PromptDetail, *, include_text: bool) -> None:
    metadata = Table(title="Prompt detail", show_header=False)
    metadata.add_column("Field", style="bold cyan")
    metadata.add_column("Value", overflow="fold")
    metadata.add_row("Prompt ID", detail.prompt_id)
    metadata.add_row("Origin session", detail.origin_session_id)
    metadata.add_row("Time", _format_timestamp(detail.occurred_at))
    metadata.add_row("Repository", detail.repository)
    metadata.add_row("Model", detail.model or "unknown")
    metadata.add_row("Ordinal", str(detail.prompt_ordinal))
    metadata.add_row("Redaction", detail.redaction_status)
    metadata.add_row("Provenance", f"{detail.provenance_status} ({detail.provenance_confidence})")
    metadata.add_row("Replay sessions", str(detail.replay_session_count))
    console.print(metadata)
    if include_text:
        console.print(Text(detail.text))
    else:
        console.print(Text(detail.text[:_LONG_PROMPT_THRESHOLD]))
        console.print(
            "[yellow]Stored prompt is long; rerun with --full to display the complete "
            "redacted text."
            "[/yellow]"
        )


def _format_timestamp(value: datetime | None) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value is not None else "unknown"


def _short_prompt_id(prompt_id: str) -> str:
    return prompt_id[:16]
