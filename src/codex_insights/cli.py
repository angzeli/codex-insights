"""Command-line interface for Codex Insights."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.filesize import decimal
from rich.table import Table
from rich.text import Text

from codex_insights import __version__
from codex_insights.adapters import CodexLocalAdapter, SourceAuditResult
from codex_insights.adapters.audit_models import FieldObservation
from codex_insights.config import resolve_codex_home, resolve_index_path
from codex_insights.db import UnsafeDatabasePathError, inspect_index
from codex_insights.git_cli import register_git_commands
from codex_insights.history_cli import register_history_commands
from codex_insights.indexer import index_source
from codex_insights.outcome_cli import register_outcome_command
from codex_insights.prompt_cli import register_prompt_commands
from codex_insights.provenance_cli import register_provenance_command
from codex_insights.task_cli import register_task_command
from codex_insights.tool_cli import register_tool_commands
from codex_insights.usage_cli import register_usage_command

app = typer.Typer(
    name="codex-insights",
    help="Local-first, read-only analytics and observability for Codex sessions.",
    no_args_is_help=True,
)
console = Console()
register_history_commands(app)
register_git_commands(app)
register_usage_command(app)
register_provenance_command(app)
register_prompt_commands(app)
register_tool_commands(app)
register_outcome_command(app)
register_task_command(app)


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


@app.command("db-info")
def db_info(
    database: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="Codex Insights database (defaults to the platform data directory).",
            dir_okay=False,
        ),
    ] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help="Codex home used to enforce database path separation.",
            file_okay=False,
            dir_okay=True,
        ),
    ] = None,
) -> None:
    """Show normalized database version and aggregate source coverage."""

    resolution = resolve_codex_home(codex_home)
    try:
        info = inspect_index(resolve_index_path(database), codex_home=resolution.path)
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc

    console.print(f"[bold cyan]DB path:[/bold cyan] {info.path}", soft_wrap=True)
    summary = Table(title="Codex Insights database", show_header=False)
    summary.add_column("Item", style="bold cyan")
    summary.add_column("Value", overflow="fold")
    summary.add_row("Schema version", str(info.schema_version))
    summary.add_row("Indexed sessions", str(info.indexed_session_count))
    summary.add_row("Latest indexing time", info.latest_indexing_time or "never")
    console.print(summary)

    coverage = Table(title="Source coverage")
    coverage.add_column("Source type")
    coverage.add_column("Sessions", justify="right")
    coverage.add_column("Codex homes", justify="right")
    if info.source_coverage:
        for item in info.source_coverage:
            coverage.add_row(
                item.source_type,
                str(item.session_count),
                str(item.source_home_count),
            )
    else:
        coverage.add_row("none", "0", "0")
    console.print(coverage)


@app.command("index")
def index_command(
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help="Codex home to index read-only (overrides CODEX_HOME and ~/.codex).",
            file_okay=False,
            dir_okay=True,
        ),
    ] = None,
    database: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="Codex Insights database (defaults to the platform data directory).",
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Incrementally index normalized session metadata and aggregate counts."""

    resolution = resolve_codex_home(codex_home)
    database_path = resolve_index_path(database)
    try:
        report = index_source(
            CodexLocalAdapter(resolution),
            database_path,
            codex_home=resolution.path,
        )
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc

    console.print(f"[bold cyan]DB path:[/bold cyan] {report.database_path}", soft_wrap=True)
    summary = Table(title="Codex session index")
    summary.add_column("Discovered", justify="right")
    summary.add_column("New", justify="right")
    summary.add_column("Updated", justify="right")
    summary.add_column("Unchanged", justify="right")
    summary.add_column("Skipped", justify="right")
    summary.add_column("Failed", justify="right")
    summary.add_row(
        str(report.discovered),
        str(report.new),
        str(report.updated),
        str(report.unchanged),
        str(report.skipped),
        str(report.failed),
    )
    console.print(summary)
    _render_messages("Warnings", report.warnings, "yellow")


@app.command("audit-source")
def audit_source(
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help="Codex home to audit (overrides CODEX_HOME and ~/.codex).",
            file_okay=False,
            dir_okay=True,
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit structured JSON instead of terminal tables."),
    ] = False,
    sample_size: Annotated[
        int,
        typer.Option(
            "--sample-size",
            min=0,
            max=100,
            help="Number of rollout files to stream; 0 disables rollout content sampling.",
        ),
    ] = 5,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Show per-file details and sample redacted SQLite field shapes.",
        ),
    ] = False,
) -> None:
    """Audit local Codex storage schemas without printing transcript content."""

    result = CodexLocalAdapter(resolve_codex_home(codex_home)).audit(
        sample_size=sample_size,
        verbose=verbose,
    )
    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return
    _render_source_audit(result, verbose=verbose)


def _render_source_audit(result: SourceAuditResult, *, verbose: bool) -> None:
    directories = {item.relative_path: item for item in result.rollouts.directories}
    active = directories.get("sessions")
    archived = directories.get("archived_sessions")

    summary = Table(title="Codex source audit", show_header=False)
    summary.add_column("Item", style="bold cyan")
    summary.add_column("Value")
    summary.add_row("Codex home", result.codex_home)
    summary.add_row("Codex home exists", "yes" if result.codex_home_exists else "no")
    summary.add_row("history.jsonl", "present" if result.history.exists else "missing")
    summary.add_row("State databases", str(len(result.state_databases)))
    summary.add_row("Active session rollouts", str(active.rollout_file_count if active else 0))
    summary.add_row("Archived rollouts", str(archived.rollout_file_count if archived else 0))
    summary.add_row("Total rollout files", str(result.rollouts.discovered_file_count))
    summary.add_row("Total rollout disk usage", decimal(result.rollouts.total_size_bytes))
    summary.add_row("Rollout files sampled", str(result.rollouts.sampled_file_count))
    summary.add_row(
        "Adjacent metadata",
        ", ".join(item.relative_path for item in result.adjacent_metadata_files) or "none",
    )
    summary.add_row("Oldest apparent activity", result.oldest_activity or "unknown")
    summary.add_row("Newest apparent activity", result.newest_activity or "unknown")
    console.print(summary)

    history = Table(title="history.jsonl schema")
    history.add_column("Lines (approx.)", justify="right")
    history.add_column("Sampled", justify="right")
    history.add_column("Malformed", justify="right")
    history.add_column("Session-ID coverage")
    history.add_column("Timestamp range")
    history.add_column("Observed fields")
    coverage = (
        f"{result.history.records_with_session_id}/{result.history.valid_record_count} sampled"
        if result.history.valid_record_count
        else "n/a"
    )
    history.add_row(
        str(result.history.approximate_line_count or 0),
        str(result.history.sampled_line_count),
        str(result.history.malformed_line_count),
        coverage,
        (
            f"{result.history.oldest_timestamp or 'unknown'} → "
            f"{result.history.newest_timestamp or 'unknown'}"
        ),
        ", ".join(result.history.observed_fields) or "none",
    )
    console.print(history)

    if result.state_databases:
        sqlite_table = Table(title="Read-only SQLite schema audit")
        sqlite_table.add_column("Database")
        sqlite_table.add_column("Table")
        sqlite_table.add_column("Rows", justify="right")
        sqlite_table.add_column("Likely session table")
        sqlite_table.add_column("Useful columns")
        for database in result.state_databases:
            if not database.tables:
                sqlite_table.add_row(database.relative_path, "<none>", "?", "no", "none")
            for table in database.tables:
                useful = ", ".join(
                    f"{column.name} ({column.likely_role})"
                    for column in table.columns
                    if column.likely_role is not None
                )
                sqlite_table.add_row(
                    database.relative_path,
                    table.name,
                    str(table.row_count) if table.row_count is not None else "?",
                    "yes" if table.likely_session_table else "no",
                    useful or "none",
                )
        console.print(sqlite_table)

    rollout_types = Table(title="Observed rollout structure")
    rollout_types.add_column("Category")
    rollout_types.add_column("Observed value")
    rollout_types.add_column("Count", justify="right")
    for item in result.rollouts.record_types:
        rollout_types.add_row("record type", item.name, str(item.count))
    for item in result.rollouts.payload_types:
        rollout_types.add_row("payload type", item.name, str(item.count))
    for item in result.rollouts.event_categories:
        rollout_types.add_row("event category", item.name, str(item.count))
    if not result.rollouts.record_types:
        rollout_types.add_row("record type", "none sampled", "0")
    console.print(rollout_types)

    console.print(Text("Observed token fields: ", style="bold cyan"), end="")
    console.print(Text(", ".join(result.rollouts.token_fields) or "none"))
    console.print(Text("Observed tool names: ", style="bold cyan"), end="")
    console.print(
        Text(
            ", ".join(f"{item.name} ({item.count})" for item in result.rollouts.tool_names)
            or "none"
        )
    )

    if verbose:
        _render_verbose_audit(result)
    _render_messages("Possible schema inconsistencies", result.schema_inconsistencies, "magenta")
    _render_messages("Warnings", result.warnings, "yellow")


def _render_verbose_audit(result: SourceAuditResult) -> None:
    sampled = Table(title="Sampled rollout files (content redacted)")
    sampled.add_column("Relative path")
    sampled.add_column("Size", justify="right")
    sampled.add_column("Lines", justify="right")
    sampled.add_column("Valid", justify="right")
    sampled.add_column("Malformed", justify="right")
    sampled.add_column("Truncated")
    for audit in result.rollouts.sampled_files:
        sampled.add_row(
            audit.relative_path,
            decimal(audit.size_bytes),
            str(audit.sampled_line_count),
            str(audit.valid_record_count),
            str(audit.malformed_line_count),
            "yes" if audit.scan_truncated else "no",
        )
    console.print(sampled)

    observations = list(result.history.text_fields)
    observations.extend(
        observation
        for database in result.state_databases
        for table in database.tables
        for observation in table.sampled_metadata_fields
    )
    observations.extend(
        observation for audit in result.rollouts.sampled_files for observation in audit.text_fields
    )
    if observations:
        fields = Table(title="Redacted text-like field shapes (first 40)")
        fields.add_column("Field")
        fields.add_column("Present", justify="right")
        fields.add_column("Types")
        fields.add_column("Approx. length")
        for observation in observations[:40]:
            fields.add_row(
                observation.field,
                str(observation.present_count),
                ", ".join(observation.value_types),
                _format_length(observation),
            )
        console.print(fields)


def _format_length(observation: FieldObservation) -> str:
    if observation.minimum_length is None:
        return "n/a"
    return (
        f"{observation.minimum_length}–{observation.maximum_length} "
        f"(avg {observation.approximate_average_length})"
    )


def _render_messages(title: str, messages: tuple[str, ...], style: str) -> None:
    if not messages:
        return
    console.print(Text(title, style=f"bold {style}"))
    for message in messages:
        console.print(Text(f"- {message}"))


def main() -> None:
    """Run the command-line application."""

    app()


if __name__ == "__main__":
    main()
