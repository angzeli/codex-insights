"""Terminal and JSON presentation for origin-aware tool activity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from codex_insights.analytics.queries import TimeExpressionError, parse_time_range
from codex_insights.analytics.tools import (
    ActivityGroup,
    ToolActivityReport,
    ToolFilters,
    get_tool_activity_report,
)
from codex_insights.config import resolve_codex_home, resolve_index_path
from codex_insights.db import UnsafeDatabasePathError
from codex_insights.models import CommandCategory

console = Console()


def register_tool_commands(app: typer.Typer) -> None:
    """Attach tool and command analytics to the main application."""

    app.command("tools")(tools_command)
    app.command("commands")(commands_command)


def tools_command(
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    repository: Annotated[str | None, typer.Option("--repo")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    session: Annotated[str | None, typer.Option("--session")] = None,
    category: Annotated[CommandCategory | None, typer.Option("--category")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 25,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[Path | None, typer.Option("--codex-home", file_okay=False)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Summarize provenance-aware tool activity and result coverage."""

    report = _report(
        since=since,
        until=until,
        repository=repository,
        model=model,
        session=session,
        category=category,
        limit=limit,
        database=database,
        codex_home=codex_home,
        commands_only=False,
        repeated=False,
    )
    _emit(report, json_output=json_output, title="Tool activity")


def commands_command(
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    repository: Annotated[str | None, typer.Option("--repo")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    session: Annotated[str | None, typer.Option("--session")] = None,
    category: Annotated[CommandCategory | None, typer.Option("--category")] = None,
    repeated: Annotated[
        bool,
        typer.Option(
            "--repeated",
            help="Show privacy-filtered commands invoked more than once.",
        ),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 25,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[Path | None, typer.Option("--codex-home", file_okay=False)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Summarize originated shell commands and conservative repetition evidence."""

    report = _report(
        since=since,
        until=until,
        repository=repository,
        model=model,
        session=session,
        category=category,
        limit=limit,
        database=database,
        codex_home=codex_home,
        commands_only=True,
        repeated=repeated,
    )
    _emit(report, json_output=json_output, title="Command activity")


def _report(
    *,
    since: str | None,
    until: str | None,
    repository: str | None,
    model: str | None,
    session: str | None,
    category: CommandCategory | None,
    limit: int,
    database: Path | None,
    codex_home: Path | None,
    commands_only: bool,
    repeated: bool,
) -> ToolActivityReport:
    try:
        parsed_since, parsed_until = parse_time_range(since, until)
        return get_tool_activity_report(
            resolve_index_path(database),
            codex_home=resolve_codex_home(codex_home).path,
            filters=ToolFilters(
                since=parsed_since,
                until=parsed_until,
                repository=repository,
                model=model,
                session=session,
                category=category,
                limit=limit,
            ),
            commands_only=commands_only,
            repeated=repeated,
        )
    except TimeExpressionError as exc:
        raise typer.BadParameter(str(exc), param_hint="--since/--until") from exc
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _emit(report: ToolActivityReport, *, json_output: bool, title: str) -> None:
    if json_output:
        typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return
    summary = Table(title=title, show_header=False, box=None, pad_edge=False)
    summary.add_column("Metric", style="bold cyan", no_wrap=True)
    summary.add_column("Value", justify="right")
    summary.add_row("Originated tool calls", f"{report.originated_tool_calls:,}")
    summary.add_row("Originated commands", f"{report.originated_commands:,}")
    summary.add_row(
        "Commands/session",
        (
            f"{report.commands_per_session:.2f}"
            if report.commands_per_session is not None
            else "unknown"
        ),
    )
    summary.add_row("Test invocations", f"{report.test_invocations:,}")
    summary.add_row("Git inspections", f"{report.git_inspections:,}")
    summary.add_row("Patch/edit activity", f"{report.patch_edits:,}")
    failure = (
        f"{report.failed_results}/{report.known_results} ({report.failure_rate:.1%})"
        if report.failure_rate is not None
        else f"unknown (0/{report.originated_tool_calls} results classified)"
    )
    summary.add_row("Failed result rate", failure)
    console.print(summary)

    provenance = Table(title="Provenance coverage", box=None, pad_edge=False)
    provenance.add_column("Observed", justify="right")
    provenance.add_column("Originated", justify="right")
    provenance.add_column("Inherited/replayed", justify="right")
    provenance.add_column("Ambiguous", justify="right")
    provenance.add_column("Unknown", justify="right")
    provenance.add_row(
        f"{report.provenance.observed:,}",
        f"{report.provenance.originated:,}",
        f"{report.provenance.inherited:,}",
        f"{report.provenance.ambiguous:,}",
        f"{report.provenance.unknown:,}",
    )
    console.print(provenance)
    _render_groups("Command categories", report.categories)
    _render_groups("Executables", report.executables)
    _render_groups("Tools", report.tools)
    if report.repeated_commands:
        repeated = Table(title="Repeated invocations", box=None, pad_edge=False)
        repeated.add_column("Count", justify="right")
        repeated.add_column("Sessions", justify="right")
        repeated.add_column("Category")
        repeated.add_column("Command", overflow="fold")
        for item in report.repeated_commands:
            flags = []
            if item.redacted:
                flags.append("redacted")
            if item.truncated:
                flags.append("truncated")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            repeated.add_row(
                f"{item.invocation_count:,}",
                f"{item.session_count:,}",
                item.category,
                (item.command or "[command text not retained]") + suffix,
            )
        console.print(repeated)


def _render_groups(title: str, groups: tuple[ActivityGroup, ...]) -> None:
    if not groups:
        return
    table = Table(title=title, box=None, pad_edge=False)
    table.add_column("Name")
    table.add_column("Count", justify="right")
    for group in groups:
        table.add_row(group.key, f"{group.count:,}")
    console.print(table)
