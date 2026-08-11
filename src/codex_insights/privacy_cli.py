"""CLI controls for privacy policy, safe export, backup, purge, and reset."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from codex_insights.analytics.queries import TimeExpressionError, parse_time_range
from codex_insights.config import (
    resolve_codex_home,
    resolve_config_path,
    resolve_index_path,
)
from codex_insights.db import UnsafeDatabasePathError
from codex_insights.exporting import (
    ExportDataset,
    ExportFilters,
    ExportFormat,
    build_export,
    render_export,
)
from codex_insights.path_safety import (
    UnsafeDestinationError,
    atomic_write_text,
    validate_write_target,
)
from codex_insights.privacy import (
    ContentRetentionPolicy,
    PurgeTarget,
    inspect_privacy,
    load_retention_policy,
    purge_derived_content,
    save_retention_policy,
)

console = Console()
privacy_app = typer.Typer(
    help="Inspect and control content retained in the derived Insights database.",
    invoke_without_command=True,
    no_args_is_help=False,
)


class Toggle(StrEnum):
    ON = "on"
    OFF = "off"

    @property
    def enabled(self) -> bool:
        return self is Toggle.ON


@dataclass(frozen=True, slots=True)
class _PrivacyGroupOptions:
    database: Path | None = None
    codex_home: Path | None = None
    config: Path | None = None
    json_output: bool = False


def register_privacy_commands(app: typer.Typer) -> None:
    """Attach privacy controls and derived-data operations to the main CLI."""

    app.add_typer(privacy_app, name="privacy")
    app.command("export")(export_command)


@privacy_app.callback(invoke_without_command=True)
def privacy_overview(
    ctx: typer.Context,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", file_okay=False),
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", dir_okay=False)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Explain local derived storage and the active future-retention policy."""

    if ctx.invoked_subcommand is not None:
        ctx.obj = _PrivacyGroupOptions(
            database=database,
            codex_home=codex_home,
            config=config,
            json_output=json_output,
        )
        return
    source_home = resolve_codex_home(codex_home).path
    try:
        policy = load_retention_policy(config, codex_home=source_home)
        config_path = validate_write_target(
            resolve_config_path(config),
            codex_home=source_home,
            operation="Privacy configuration",
        )
        database_path = resolve_index_path(database)
    except (ValueError, UnsafeDestinationError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = {
        "database_path": str(database_path),
        "config_path": str(config_path),
        "policy": policy.to_dict(),
        "stores": [
            "normalized session/project metadata",
            "token and event-provenance metadata",
            "derived Git, outcome, and task metadata",
            "redacted logical prompt text when enabled",
            "redacted bounded command text when enabled",
        ],
        "does_not_store": [
            "raw tool output or stdout/stderr",
            "hidden reasoning",
            "raw rollout records",
            "unredacted secrets reconstructed from source",
        ],
        "codex_source_access": "read_only",
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    table = Table(title="Codex Insights privacy", show_header=False, box=None)
    table.add_column("Item", style="bold cyan")
    table.add_column("Value", overflow="fold")
    table.add_row("Derived DB", str(database_path))
    table.add_row("Configuration", str(config_path))
    table.add_row("Future prompt storage", "on" if policy.store_prompts else "off")
    table.add_row(
        "Future command-text storage", "on" if policy.store_command_text else "off"
    )
    table.add_row("Raw tool output", "never stored")
    table.add_row("Codex source", "strictly read-only")
    console.print(table)
    console.print(
        "Stores normalized metadata and derived analytics; optional text is redacted and bounded."
    )
    console.print(
        "Does not store hidden reasoning, raw rollout records, patches, or raw tool stdout/stderr."
    )


@privacy_app.command("inspect")
def privacy_inspect_command(
    ctx: typer.Context,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", file_okay=False),
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", dir_okay=False)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report retained content categories and counts without sensitive values."""

    shared = _privacy_group_options(ctx)
    database = database if database is not None else shared.database
    codex_home = codex_home if codex_home is not None else shared.codex_home
    config = config if config is not None else shared.config
    json_output = json_output or shared.json_output
    source_home = resolve_codex_home(codex_home).path
    try:
        inspection = inspect_privacy(
            resolve_index_path(database),
            codex_home=source_home,
            config_path=config,
        )
    except (ValueError, UnsafeDatabasePathError, UnsafeDestinationError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        typer.echo(json.dumps(inspection.to_dict(), indent=2, sort_keys=True))
        return
    summary = Table(title="Privacy inspection", show_header=False, box=None)
    summary.add_column("Item", style="bold cyan")
    summary.add_column("Value", overflow="fold")
    summary.add_row("Derived DB", str(inspection.database_path))
    summary.add_row("Configuration", str(inspection.config_path))
    summary.add_row(
        "Future prompt storage", "on" if inspection.policy.store_prompts else "off"
    )
    summary.add_row(
        "Future command-text storage",
        "on" if inspection.policy.store_command_text else "off",
    )
    summary.add_row("Raw tool output", "no")
    console.print(summary)
    counts = Table(title="Retained derived categories", box=None)
    counts.add_column("Category")
    counts.add_column("Count", justify="right")
    for key, value in sorted(inspection.counts.items()):
        counts.add_row(key.replace("_", " "), f"{value:,}")
    console.print(counts)


@privacy_app.command("config")
def privacy_config_command(
    ctx: typer.Context,
    store_prompts: Annotated[Toggle | None, typer.Option("--store-prompts")] = None,
    store_command_text: Annotated[
        Toggle | None,
        typer.Option("--store-command-text"),
    ] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", file_okay=False),
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", dir_okay=False)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show or change persistent policy for text stored by future indexing."""

    shared = _privacy_group_options(ctx)
    codex_home = codex_home if codex_home is not None else shared.codex_home
    config = config if config is not None else shared.config
    json_output = json_output or shared.json_output
    source_home = resolve_codex_home(codex_home).path
    try:
        current = load_retention_policy(config, codex_home=source_home)
        policy = ContentRetentionPolicy(
            store_prompts=(
                store_prompts.enabled if store_prompts is not None else current.store_prompts
            ),
            store_command_text=(
                store_command_text.enabled
                if store_command_text is not None
                else current.store_command_text
            ),
        )
        changed = policy != current
        path = (
            save_retention_policy(policy, config, codex_home=source_home)
            if changed
            else validate_write_target(
                resolve_config_path(config),
                codex_home=source_home,
                operation="Privacy configuration",
            )
        )
    except (ValueError, UnsafeDestinationError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    payload = {"config_path": str(path), "changed": changed, **policy.to_dict()}
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    console.print(f"[bold cyan]Config:[/bold cyan] {path}", soft_wrap=True)
    console.print(f"Future prompt storage: {'on' if policy.store_prompts else 'off'}")
    console.print(
        "Future command-text storage: "
        f"{'on' if policy.store_command_text else 'off'}"
    )
    if changed:
        console.print("Existing stored content is unchanged; use privacy purge to remove it.")


@privacy_app.command("purge")
def privacy_purge_command(
    ctx: typer.Context,
    target: PurgeTarget,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", file_okay=False),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Purge selected text from the derived DB while preserving analytic metadata."""

    shared = _privacy_group_options(ctx)
    database = database if database is not None else shared.database
    codex_home = codex_home if codex_home is not None else shared.codex_home
    json_output = json_output or shared.json_output
    source_home = resolve_codex_home(codex_home).path
    database_path = resolve_index_path(database)
    if not yes:
        console.print(f"Derived DB: {database_path}")
        typer.confirm(
            f"Purge stored {target.value} from this Codex Insights database?",
            abort=True,
        )
    try:
        result = purge_derived_content(
            database_path,
            codex_home=source_home,
            target=target,
        )
    except (ValueError, UnsafeDatabasePathError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        console.print(
            f"Purged {result.affected_items:,} {target.value} item(s) from {result.database_path}."
        )


def _privacy_group_options(ctx: typer.Context) -> _PrivacyGroupOptions:
    return ctx.obj if isinstance(ctx.obj, _PrivacyGroupOptions) else _PrivacyGroupOptions()


def export_command(
    output: Annotated[Path, typer.Option("--output", dir_okay=False)],
    dataset: Annotated[ExportDataset, typer.Option("--dataset")] = ExportDataset.SESSIONS,
    export_format: Annotated[ExportFormat, typer.Option("--format")] = ExportFormat.JSON,
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    repository: Annotated[str | None, typer.Option("--repo")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
    create_parents: Annotated[bool, typer.Option("--create-parents")] = False,
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", file_okay=False),
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", dir_okay=False)] = None,
) -> None:
    """Export one normalized derived dataset as stable JSON or safe CSV."""

    source_home = resolve_codex_home(codex_home).path
    database_path = resolve_index_path(database)
    config_path = resolve_config_path(config)
    try:
        parsed_since, parsed_until = parse_time_range(since, until)
        policy = load_retention_policy(config, codex_home=source_home)
        destination = validate_write_target(
            output,
            codex_home=source_home,
            operation="Export",
            protected_paths=(database_path, config_path),
        )
        bundle = build_export(
            database_path,
            codex_home=source_home,
            dataset=dataset,
            policy=policy,
            filters=ExportFilters(
                since=parsed_since,
                until=parsed_until,
                repository=repository,
                model=model,
            ),
        )
        atomic_write_text(
            destination,
            render_export(bundle, export_format),
            overwrite=overwrite,
            create_parents=create_parents,
        )
    except TimeExpressionError as exc:
        raise typer.BadParameter(str(exc), param_hint="--since/--until") from exc
    except (ValueError, OSError, UnsafeDatabasePathError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--output") from exc
    console.print(
        f"Exported {len(bundle.records):,} {dataset.value} record(s) to {destination}."
    )
