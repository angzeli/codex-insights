"""CLI for the generated, privacy-aware offline dashboard."""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Annotated

import typer

from codex_insights.analytics import TimeExpressionError, parse_time_range
from codex_insights.analytics.dashboard import DashboardFilters, build_dashboard_data
from codex_insights.analytics.usage import TimezoneError, resolve_timezone
from codex_insights.config import resolve_codex_home, resolve_config_path, resolve_index_path
from codex_insights.dashboard_rendering import render_dashboard
from codex_insights.db import UnsafeDatabasePathError
from codex_insights.path_safety import (
    UnsafeDestinationError,
    atomic_write_text,
    validate_write_target,
)


def register_dashboard_command(app: typer.Typer) -> None:
    """Attach the offline dashboard generator to the main application."""

    app.command("dashboard")(dashboard_command)


def dashboard_command(
    since: Annotated[
        str | None,
        typer.Option("--since", help="Inclusive ISO time or duration such as 30d."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="Exclusive ISO time or inclusive calendar date."),
    ] = None,
    repository: Annotated[str | None, typer.Option("--repo")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    task_action: Annotated[
        str | None,
        typer.Option("--task", help="Normalized task action, including unknown."),
    ] = None,
    task_domain: Annotated[
        str | None,
        typer.Option("--domain", help="Normalized task domain, including unknown."),
    ] = None,
    timezone: Annotated[
        str,
        typer.Option("--timezone", help="local, UTC, or an IANA timezone name."),
    ] = "local",
    output: Annotated[
        Path,
        typer.Option("--output", dir_okay=False, help="Destination self-contained HTML file."),
    ] = Path("codex-insights-dashboard.html"),
    database: Annotated[
        Path | None,
        typer.Option("--db", dir_okay=False, help="Codex Insights database."),
    ] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            file_okay=False,
            help="Codex home used only for source-safety enforcement.",
        ),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", dir_okay=False, help="Privacy configuration path."),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Replace an existing dashboard file."),
    ] = False,
    create_parents: Annotated[
        bool,
        typer.Option("--create-parents", help="Create missing output directories."),
    ] = False,
    open_browser: Annotated[
        bool,
        typer.Option("--open", help="Open the generated dashboard in the default browser."),
    ] = False,
) -> None:
    """Generate a static, self-contained offline analytics dashboard."""

    resolved_home = resolve_codex_home(codex_home).path
    resolved_database = resolve_index_path(database)
    try:
        zone = resolve_timezone(timezone)
        parsed_since, parsed_until = parse_time_range(
            since,
            until,
            timezone=zone.timezone,
        )
        destination = validate_write_target(
            output,
            codex_home=resolved_home,
            operation="Dashboard output",
            protected_paths=(resolved_database,),
        )
        data = build_dashboard_data(
            resolved_database,
            codex_home=resolved_home,
            timezone=zone,
            filters=DashboardFilters(
                since=parsed_since,
                until=parsed_until,
                repository=repository,
                model=model,
                task_action=task_action,
                task_domain=task_domain,
            ),
            config_path=resolve_config_path(config),
        )
        atomic_write_text(
            destination,
            render_dashboard(data),
            overwrite=overwrite,
            create_parents=create_parents,
        )
    except TimeExpressionError as exc:
        raise typer.BadParameter(str(exc), param_hint="--since/--until") from exc
    except TimezoneError as exc:
        raise typer.BadParameter(str(exc), param_hint="--timezone") from exc
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    except (UnsafeDestinationError, FileExistsError, FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--output") from exc

    typer.echo(f"Wrote offline dashboard: {destination}")
    if open_browser:
        webbrowser.open(destination.as_uri())
