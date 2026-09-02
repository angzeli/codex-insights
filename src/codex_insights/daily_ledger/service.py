"""Queue worker, export, backfill, and retention semantics."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.config import resolve_codex_home
from codex_insights.daily_ledger.config import (
    LedgerConfig,
    ensure_state_directories,
    validate_checkout_separation,
)
from codex_insights.daily_ledger.exporter import (
    ExportResult,
    cached_dates,
    export_day,
    initialize_checkout_contract,
)
from codex_insights.daily_ledger.git_sync import (
    GitSyncError,
    GitSyncResult,
    assert_allowlisted_content_safe,
    sync_checkout,
)
from codex_insights.daily_ledger.locking import LockUnavailableError, ProcessLock
from codex_insights.daily_ledger.queue import (
    capture_hook_payload,
    load_hook_job,
)
from codex_insights.daily_ledger.records import (
    RecordBuildResult,
    build_session_cache,
    merge_session_cache,
)
from codex_insights.daily_ledger.report_policy import (
    load_reporting_policy,
    report_date_for_timestamp,
)
from codex_insights.path_safety import atomic_write_text


@dataclass(frozen=True, slots=True)
class FlushResult:
    """One worker batch result suitable for CLI output."""

    claimed_jobs: int
    processed_jobs: int
    failed_jobs: int
    retained_jobs: int
    exported_dates: tuple[str, ...]
    already_running: bool = False
    git: GitSyncResult | None = None


def flush_queue(config: LedgerConfig, *, no_push: bool = False) -> FlushResult:
    """Process a queue batch under one lock and retain jobs until push succeeds."""

    ensure_state_directories(config.paths)
    try:
        with ProcessLock(config.paths.locks / "worker.lock"):
            return _flush_locked(config, no_push=no_push)
    except LockUnavailableError:
        return FlushResult(0, 0, 0, 0, (), already_running=True)


def export_cached_day(config: LedgerConfig, day: date, *, no_push: bool) -> ExportResult:
    """Export one cached date and optionally synchronize the dedicated checkout."""

    result = export_day(config, day)
    if not no_push and config.push_enabled:
        sync_checkout(config, lambda: export_day(config, day))
    return result


def backfill_range(
    config: LedgerConfig,
    *,
    since: date,
    until: date,
    codex_home: Path | None,
    no_push: bool,
) -> FlushResult:
    """Queue catalogue sessions intersecting an inclusive local calendar range."""

    if until < since:
        raise ValueError("--until must not be earlier than --since")
    initialize_checkout_contract(config)
    policy = load_reporting_policy(config)
    resolution = resolve_codex_home(codex_home)
    validate_checkout_separation(config, codex_home=resolution.path)
    adapter = CodexLocalAdapter(resolution)
    candidates, _ = adapter.discover_sessions()
    for candidate in candidates:
        session = candidate.session
        if (
            session.source_path is None
            or not candidate.rollout_exists
            or not candidate.rollout_allowed
        ):
            continue
        start = session.started_at or session.updated_at
        end = session.apparent_ended_at or session.updated_at or session.started_at
        if start is not None and end is not None:
            first_report_date = report_date_for_timestamp(policy, start)
            last_report_date = report_date_for_timestamp(policy, end)
            if last_report_date < since or first_report_date > until:
                continue
        payload = {
            "session_id": session.source_session_id,
            "transcript_path": str(session.source_path),
            "cwd": str(session.cwd or resolution.path.parent),
            "hook_event_name": "SessionEnd",
            "reason": "backfill",
        }
        capture_hook_payload(
            payload,
            config=config,
            now=end or start or datetime.now(tz=UTC),
            launch_worker=False,
        )
    return flush_queue(config, no_push=no_push)


def _flush_locked(config: LedgerConfig, *, no_push: bool) -> FlushResult:
    initialize_checkout_contract(config)
    assert_allowlisted_content_safe(
        config.ledger_checkout, ("config/report-policy.yaml",)
    )
    policy = load_reporting_policy(config)
    claimed = _claim_pending(config)
    failed = 0
    processed = 0
    affected_dates: set[date] = set()
    valid_jobs: list[Path] = []
    for path in claimed:
        try:
            job = load_hook_job(path)
            validate_checkout_separation(
                config,
                audited_working_directories=(Path(job.cwd),),
            )
            result = build_session_cache(job, config, policy=policy)
            previous = _load_cache(config, result.session_key)
            affected_dates.update(_cache_dates(previous))
            affected_dates.update(date.fromisoformat(value) for value in result.dates)
            _save_cache(config, result, previous)
            processed += 1
            valid_jobs.append(path)
        except Exception as exc:
            failed += 1
            _move_failed(config, path, exc)
    exported: list[ExportResult] = [
        export_day(config, day) for day in sorted(affected_dates)
    ]
    should_push = config.push_enabled and not no_push
    if not should_push:
        _return_to_pending(config, valid_jobs)
        return FlushResult(
            claimed_jobs=len(claimed),
            processed_jobs=processed,
            failed_jobs=failed,
            retained_jobs=len(valid_jobs),
            exported_dates=tuple(item.date for item in exported),
        )
    try:
        git_result = sync_checkout(
            config,
            lambda: tuple(export_day(config, day) for day in sorted(affected_dates)),
        )
    except GitSyncError:
        _return_to_pending(config, valid_jobs)
        raise
    _mark_processed(config, valid_jobs)
    return FlushResult(
        claimed_jobs=len(claimed),
        processed_jobs=processed,
        failed_jobs=failed,
        retained_jobs=0,
        exported_dates=tuple(item.date for item in exported),
        git=git_result,
    )


def _claim_pending(config: LedgerConfig) -> tuple[Path, ...]:
    # Owning the worker lock means any files left here are from an interrupted worker.
    claimed: list[Path] = list(sorted(config.paths.processing.glob("*.json")))
    for source in sorted(config.paths.pending.glob("*.json")):
        destination = config.paths.processing / source.name
        if destination.exists():
            continue
        try:
            os.replace(source, destination)
        except FileNotFoundError:
            continue
        claimed.append(destination)
    return tuple(claimed)


def _load_cache(config: LedgerConfig, session_key: str) -> dict[str, object] | None:
    path = config.paths.cache / "sessions" / f"{session_key}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _save_cache(
    config: LedgerConfig,
    result: RecordBuildResult,
    previous: dict[str, object] | None,
) -> None:
    payload = merge_session_cache(previous, result)
    path = config.paths.cache / "sessions" / f"{result.session_key}.json"
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        overwrite=True,
        create_parents=True,
    )


def _cache_dates(cache: dict[str, object] | None) -> set[date]:
    if cache is None:
        return set()
    slices = cache.get("slices")
    if not isinstance(slices, dict):
        return set()
    dates: set[date] = set()
    for value in slices:
        try:
            dates.add(date.fromisoformat(str(value)))
        except ValueError:
            continue
    return dates


def _move_failed(config: LedgerConfig, path: Path, exc: Exception) -> None:
    destination = config.paths.failed / path.name
    os.replace(path, destination)
    metadata = {
        "schema": "daily-ledger-failure-v1",
        "job": path.name,
        "error_type": type(exc).__name__,
        "failed_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
    }
    atomic_write_text(
        destination.with_suffix(".error.json"),
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        overwrite=True,
        create_parents=True,
    )


def _return_to_pending(config: LedgerConfig, paths: list[Path]) -> None:
    for path in paths:
        if path.exists():
            os.replace(path, config.paths.pending / path.name)


def _mark_processed(config: LedgerConfig, paths: list[Path]) -> None:
    for path in paths:
        if path.exists():
            os.replace(path, config.paths.processed / path.name)


def all_cached_dates(config: LedgerConfig) -> tuple[date, ...]:
    """Public helper used by doctor and CLI status output."""

    return cached_dates(config)
