"""CLI entry points for weekly and monthly offline reports."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from codex_insights.analytics.reports import ReportKind, build_analytics_report
from codex_insights.analytics.usage import TimezoneError, resolve_timezone
from codex_insights.config import resolve_codex_home, resolve_index_path
from codex_insights.db import UnsafeDatabasePathError
from codex_insights.path_safety import atomic_write_text, validate_write_target
from codex_insights.reporting import ReportFormat, render_report

report_app = typer.Typer(help="Generate privacy-safe periodic analytics reports.")


def register_report_commands(app: typer.Typer) -> None:
    app.add_typer(report_app, name="report")


@report_app.command("weekly")
def weekly_command(
    report_date: Annotated[
        str | None,
        typer.Option("--date", help="Any local calendar date in the requested week."),
    ] = None,
    repository: Annotated[str | None, typer.Option("--repo")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    report_format: Annotated[ReportFormat, typer.Option("--format")] = ReportFormat.MARKDOWN,
    output: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
    timezone: Annotated[str, typer.Option("--timezone")] = "local",
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", file_okay=False),
    ] = None,
) -> None:
    """Generate a calendar-week report (Monday through Sunday)."""

    _report_command(
        ReportKind.WEEKLY,
        report_date=report_date,
        repository=repository,
        model=model,
        report_format=report_format,
        output=output,
        timezone=timezone,
        database=database,
        codex_home=codex_home,
    )


@report_app.command("monthly")
def monthly_command(
    report_date: Annotated[
        str | None,
        typer.Option("--date", help="Any local calendar date in the requested month."),
    ] = None,
    repository: Annotated[str | None, typer.Option("--repo")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    report_format: Annotated[ReportFormat, typer.Option("--format")] = ReportFormat.MARKDOWN,
    output: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
    timezone: Annotated[str, typer.Option("--timezone")] = "local",
    database: Annotated[Path | None, typer.Option("--db", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", file_okay=False),
    ] = None,
) -> None:
    """Generate a compact calendar-month report."""

    _report_command(
        ReportKind.MONTHLY,
        report_date=report_date,
        repository=repository,
        model=model,
        report_format=report_format,
        output=output,
        timezone=timezone,
        database=database,
        codex_home=codex_home,
    )


def _report_command(
    kind: ReportKind,
    *,
    report_date: str | None,
    repository: str | None,
    model: str | None,
    report_format: ReportFormat,
    output: Path | None,
    timezone: str,
    database: Path | None,
    codex_home: Path | None,
) -> None:
    resolved_home = resolve_codex_home(codex_home).path
    try:
        zone = resolve_timezone(timezone)
        anchor = date.fromisoformat(report_date) if report_date else None
        resolved_database = resolve_index_path(database)
        destination = (
            validate_write_target(
                output,
                codex_home=resolved_home,
                operation="Report output",
                protected_paths=(resolved_database,),
            )
            if output is not None
            else None
        )
        report = build_analytics_report(
            resolved_database,
            codex_home=resolved_home,
            kind=kind,
            timezone=zone,
            report_date=anchor,
            repository=repository,
            model=model,
        )
    except UnsafeDatabasePathError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    except TimezoneError as exc:
        raise typer.BadParameter(str(exc), param_hint="--timezone") from exc
    except ValueError as exc:
        hint = "--date" if report_date else "--output"
        raise typer.BadParameter(str(exc), param_hint=hint) from exc
    rendered = render_report(report, report_format)
    if output is None:
        typer.echo(rendered, nl=False)
        return
    assert destination is not None
    atomic_write_text(destination, rendered, overwrite=True, create_parents=True)
    typer.echo(f"Wrote {report_format.value} report: {destination}")
