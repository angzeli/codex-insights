"""Typer commands for daily-ledger capture, diagnosis, export, and backfill."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from codex_insights.config import resolve_codex_home
from codex_insights.daily_ledger.config import (
    LedgerConfigurationError,
    load_ledger_config,
)
from codex_insights.daily_ledger.doctor import run_doctor
from codex_insights.daily_ledger.git_sync import GitSyncError
from codex_insights.daily_ledger.queue import run_capture_hook
from codex_insights.daily_ledger.records import parse_date
from codex_insights.daily_ledger.service import (
    backfill_range,
    export_cached_day,
    flush_queue,
)

daily_ledger_app = typer.Typer(
    help="Capture and synchronize privacy-filtered daily Codex evidence.",
    no_args_is_help=True,
)
console = Console()


def register_daily_ledger_commands(app: typer.Typer) -> None:
    """Attach the removable V1 daily-ledger command group."""

    app.add_typer(daily_ledger_app, name="daily-ledger")


@daily_ledger_app.command("capture-hook")
def capture_hook_command(
    config: Annotated[Path | None, typer.Option("--config", dir_okay=False)] = None,
) -> None:
    """Read one hook object from stdin, queue it, detach a worker, and fail open."""

    exit_code, output = run_capture_hook(sys.stdin.read(), config_path=config)
    if output:
        sys.stdout.write(output)
        sys.stdout.flush()
    if exit_code:
        raise typer.Exit(exit_code)


@daily_ledger_app.command("doctor")
def doctor_command(
    config: Annotated[Path | None, typer.Option("--config", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", file_okay=False),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect configuration, checkout, hook, queue, and leakage status."""

    try:
        resolved = load_ledger_config(config)
        report = run_doctor(resolved, codex_home=resolve_codex_home(codex_home).path)
    except LedgerConfigurationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    if json_output:
        typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return
    table = Table(title="Codex Insights daily-ledger doctor", show_header=False)
    table.add_column("Item", style="bold cyan")
    table.add_column("Value", overflow="fold")
    table.add_row("Schema", report.schema_version)
    table.add_row("Configuration", report.config_path)
    table.add_row("Reporting timezone", report.timezone or "invalid or unavailable")
    table.add_row(
        "Timezone identifier",
        "valid IANA timezone" if report.timezone_valid else "invalid",
    )
    table.add_row("Day boundary", report.day_boundary_local or "invalid or unavailable")
    table.add_row("Current report date", report.current_report_date or "unavailable")
    table.add_row(
        "Current reporting window",
        (
            f"[{report.current_window_start}, {report.current_window_end})"
            if report.current_window_start and report.current_window_end
            else "unavailable"
        ),
    )
    table.add_row("Next reporting boundary", report.next_reporting_boundary or "unavailable")
    table.add_row(
        "Historical periods",
        "valid and contiguous" if report.reporting_periods_valid else "invalid",
    )
    if report.reporting_policy_error:
        table.add_row("Reporting policy error", report.reporting_policy_error)
    table.add_row("Ledger checkout", report.checkout)
    table.add_row("Git checkout", "ready" if report.checkout_valid else "not ready")
    table.add_row("Remote", "present" if report.remote_exists else "missing")
    table.add_row("Auth readiness", report.auth_readiness)
    table.add_row("Hook helper", "installed" if report.helper_installed else "not installed")
    table.add_row("Hook configured", "yes" if report.hook_config_present else "no")
    table.add_row("Pending jobs", str(report.pending_jobs))
    table.add_row("Failed jobs", str(report.failed_jobs))
    table.add_row("Last successful push", report.last_successful_push or "none")
    table.add_row("Non-allowlisted changes", str(len(report.dirty_non_allowlisted)))
    table.add_row("Leakage findings", str(len(report.leakage_findings)))
    console.print(table)


@daily_ledger_app.command("flush")
def flush_command(
    config: Annotated[Path | None, typer.Option("--config", dir_okay=False)] = None,
    no_push: Annotated[bool, typer.Option("--no-push")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Process pending jobs and push only when the configured policy allows it."""

    try:
        result = flush_queue(load_ledger_config(config), no_push=no_push)
    except (LedgerConfigurationError, GitSyncError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = {
        "claimed_jobs": result.claimed_jobs,
        "processed_jobs": result.processed_jobs,
        "failed_jobs": result.failed_jobs,
        "retained_jobs": result.retained_jobs,
        "exported_dates": list(result.exported_dates),
        "already_running": result.already_running,
        "retry_attempts": result.retry_attempts,
        "retry_exhausted_jobs": result.retry_exhausted_jobs,
        "pushed": result.git.pushed if result.git is not None else False,
        "commit_sha": result.git.commit_sha if result.git is not None else None,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(
            f"Processed {result.processed_jobs} job(s); retained {result.retained_jobs}; "
            f"exported {len(result.exported_dates)} date(s)."
        )


@daily_ledger_app.command("export")
def export_command(
    export_date: Annotated[str, typer.Option("--date")],
    config: Annotated[Path | None, typer.Option("--config", dir_okay=False)] = None,
    no_push: Annotated[bool, typer.Option("--no-push")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Deterministically rebuild one date from the privacy-filtered cache."""

    try:
        result = export_cached_day(
            load_ledger_config(config),
            parse_date(export_date),
            no_push=no_push,
        )
    except (LedgerConfigurationError, GitSyncError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = {
        "date": result.date,
        "revision": result.revision,
        "session_count": result.session_count,
        "changed": result.changed,
        "sync_state": result.sync_state,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(
            f"Exported {result.date} revision {result.revision} "
            f"with {result.session_count} session(s)."
        )


@daily_ledger_app.command("backfill")
def backfill_command(
    since: Annotated[str, typer.Option("--since")],
    until: Annotated[str, typer.Option("--until")],
    config: Annotated[Path | None, typer.Option("--config", dir_okay=False)] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", file_okay=False),
    ] = None,
    no_push: Annotated[bool, typer.Option("--no-push")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Queue and export an inclusive range from the existing session inventory."""

    try:
        result = backfill_range(
            load_ledger_config(config),
            since=parse_date(since),
            until=parse_date(until),
            codex_home=codex_home,
            no_push=no_push,
        )
    except (LedgerConfigurationError, GitSyncError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = {
        "processed_jobs": result.processed_jobs,
        "failed_jobs": result.failed_jobs,
        "retained_jobs": result.retained_jobs,
        "exported_dates": list(result.exported_dates),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(
            f"Backfilled {len(result.exported_dates)} date(s); "
            f"retained {result.retained_jobs} job(s)."
        )
