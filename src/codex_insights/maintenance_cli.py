"""CLI entry points for safe backup and reset of the derived Insights index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from codex_insights.config import resolve_codex_home, resolve_index_path
from codex_insights.maintenance import backup_index, reset_index, validate_expected_index

console = Console()


def register_maintenance_commands(app: typer.Typer) -> None:
    """Attach derived-database backup and reset commands to the main CLI."""

    app.command("backup-index")(backup_index_command)
    app.command("reset-index")(reset_index_command)


def backup_index_command(
    destination: Path,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", file_okay=False),
    ] = None,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
    create_parents: Annotated[bool, typer.Option("--create-parents")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Back up the derived Insights SQLite database with explicit content counts."""

    source_home = resolve_codex_home(codex_home).path
    try:
        result = backup_index(
            resolve_index_path(database),
            destination,
            codex_home=source_home,
            overwrite=overwrite,
            create_parents=create_parents,
        )
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc), param_hint="DESTINATION") from exc
    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return
    console.print(f"Backup written: {result.destination}")
    console.print(
        f"Schema {result.schema_version}; contains {result.stored_prompt_bodies:,} stored "
        f"prompt bodies and {result.stored_command_texts:,} stored command texts."
    )


def reset_index_command(
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", file_okay=False),
    ] = None,
    backup: Annotated[Path | None, typer.Option("--backup", dir_okay=False)] = None,
    overwrite_backup: Annotated[bool, typer.Option("--overwrite-backup")] = False,
    create_backup_parents: Annotated[
        bool,
        typer.Option("--create-parents"),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Delete only a verified derived index, optionally after an explicit backup."""

    source_home = resolve_codex_home(codex_home).path
    database_path = resolve_index_path(database)
    try:
        resolved_database, schema_version = validate_expected_index(
            database_path, codex_home=source_home
        )
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    if not yes:
        console.print(f"Derived DB to reset: {resolved_database} (schema {schema_version})")
        if backup is not None:
            console.print(f"Explicit backup destination: {backup.expanduser()}")
        typer.confirm(
            "Delete this Codex Insights derived index? Codex source history is not affected.",
            abort=True,
        )
    try:
        result = reset_index(
            resolved_database,
            codex_home=source_home,
            backup_destination=backup,
            backup_overwrite=overwrite_backup,
            create_backup_parents=create_backup_parents,
        )
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--db/--backup") from exc
    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return
    if result.backup is not None:
        console.print(f"Backup written: {result.backup.destination}")
    console.print(f"Reset derived index: {result.database_path}")
    console.print("Codex source history was not modified.")
