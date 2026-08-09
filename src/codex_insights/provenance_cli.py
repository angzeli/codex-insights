"""CLI presentation for aggregate cross-thread event provenance."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from codex_insights.analytics.provenance import (
    AmbiguousProvenanceSessionError,
    ProvenanceSessionNotFoundError,
    ProvenanceSummary,
    get_provenance_summary,
)
from codex_insights.config import resolve_codex_home, resolve_index_path
from codex_insights.db import UnsafeDatabasePathError, open_index
from codex_insights.models import EventFamily

console = Console()


def register_provenance_command(app: typer.Typer) -> None:
    app.command("provenance")(provenance_command)


def provenance_command(
    session: Annotated[
        str | None,
        typer.Option("--session", help="Limit diagnostics to one full or unique session prefix."),
    ] = None,
    family: Annotated[
        EventFamily | None,
        typer.Option("--family", help="Limit diagnostics to one normalized event family."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit structured aggregate JSON."),
    ] = False,
    database: Annotated[
        Path | None,
        typer.Option("--db", help="Codex Insights database.", dir_okay=False),
    ] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help="Codex home used only to enforce database path separation.",
            file_okay=False,
            dir_okay=True,
        ),
    ] = None,
) -> None:
    """Summarize observed-versus-originated rollout event provenance."""

    resolution = resolve_codex_home(codex_home)
    try:
        with closing(
            open_index(resolve_index_path(database), codex_home=resolution.path)
        ) as connection:
            summary = get_provenance_summary(
                connection,
                session_prefix=session,
                family=family,
            )
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    except ProvenanceSessionNotFoundError as exc:
        raise typer.BadParameter(f"No indexed session matches {exc.args[0]!r}.") from exc
    except AmbiguousProvenanceSessionError as exc:
        raise typer.BadParameter(f"Session prefix {exc.args[0]!r} is ambiguous.") from exc

    if json_output:
        typer.echo(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
        return
    _render(summary)


def _render(summary: ProvenanceSummary) -> None:
    table = Table(title="Event provenance reconciliation", show_header=False)
    table.add_column("Metric", style="bold cyan")
    table.add_column("Value", justify="right")
    if summary.session_id is not None:
        table.add_row("Session", summary.session_id)
    table.add_row("Lineage edges", str(summary.lineage_edges))
    table.add_row("Child threads", str(summary.child_threads))
    table.add_row("Sessions with replay", str(summary.sessions_with_replay))
    table.add_row("Event families affected", str(summary.event_families_affected))
    table.add_row("Observed semantic records", str(summary.observed_events))
    table.add_row("Originated records", str(summary.originated_events))
    table.add_row("Inherited records", str(summary.inherited_events))
    table.add_row("Mirrored observations", str(summary.duplicate_observations))
    table.add_row("Ambiguous records", str(summary.ambiguous_events))
    table.add_row("Unknown records", str(summary.unknown_events))
    console.print(table)

    families = Table(title="Event-family evidence")
    families.add_column("Family")
    families.add_column("Observed", justify="right")
    families.add_column("Origin", justify="right")
    families.add_column("Inherited", justify="right")
    families.add_column("Ambiguous", justify="right")
    families.add_column("Replay children", justify="right")
    families.add_column("Ambiguous children", justify="right")
    for item in summary.families:
        families.add_row(
            item.family,
            str(item.observed_events),
            str(item.originated_events),
            str(item.inherited_events),
            str(item.ambiguous_events),
            str(item.child_threads_with_replay),
            str(item.child_threads_ambiguous),
        )
    console.print(families)
