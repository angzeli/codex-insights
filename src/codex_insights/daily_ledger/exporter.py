"""Deterministic daily-ledger export from local session caches."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from pathlib import Path
from typing import Any

from codex_insights.daily_ledger.config import LedgerConfig, ensure_state_directories
from codex_insights.daily_ledger.privacy import MAX_JSONL_RECORD_BYTES, assert_remote_safe
from codex_insights.daily_ledger.report_policy import (
    ReportingWindow,
    ensure_report_policy,
    load_reporting_policy,
    reporting_window_for_date,
)
from codex_insights.daily_ledger.schema_validation import validate_generated_document
from codex_insights.path_safety import atomic_write_text

SUMMARY_SCHEMA = "codex-daily-summary-v1"
MANIFEST_SCHEMA = "codex-manifest-v1"
_SCHEMA_FILES = (
    "codex-session-v1.schema.json",
    "codex-daily-summary-v1.schema.json",
    "codex-manifest-v1.schema.json",
)
_STATE_PRECEDENCE = (
    "completed",
    "completed_locally",
    "in_progress",
    "blocked",
    "planning_only",
    "abandoned",
    "unknown",
)


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Paths and revision produced for one calendar date."""

    date: str
    revision: int
    session_count: int
    changed: bool
    generated_paths: tuple[Path, ...]
    sync_state: str


def export_day(config: LedgerConfig, day: date) -> ExportResult:
    """Rebuild one date deterministically from all cached session slices."""

    ensure_state_directories(config.paths)
    initialize_checkout_contract(config)
    policy = load_reporting_policy(config)
    window = reporting_window_for_date(policy, day)
    day_text = day.isoformat()
    destination = _day_directory(config.ledger_checkout, day)
    sessions_directory = destination / "sessions"
    slices = _cached_slices(config, day_text)
    previous_manifest = _load_object(destination / "manifest.json")
    _validate_existing_window(previous_manifest, window)
    previous_revision = _positive_int(previous_manifest.get("revision")) or 0
    previous_records = _existing_session_records(sessions_directory)
    raw_records = {
        key: dict(_object(value.get("record")))
        for key, value in slices.items()
        if isinstance(value.get("record"), dict)
    }
    for record in raw_records.values():
        _validate_record_window(record, window)
    new_keys = sorted(set(raw_records) - set(previous_records))
    changed_keys = sorted(
        key
        for key in set(raw_records) & set(previous_records)
        if _canonical(raw_records[key], drop_revision=True)
        != _canonical(previous_records[key], drop_revision=True)
    )
    removed_keys = sorted(set(previous_records) - set(raw_records))
    legacy_events = destination / "events.jsonl"
    content_changed = (
        bool(new_keys or changed_keys or removed_keys)
        or not previous_manifest
        or legacy_events.exists()
    )
    revision = (
        1
        if previous_revision == 0
        else previous_revision + 1
        if content_changed
        else previous_revision
    )
    records: dict[str, dict[str, object]] = {}
    for key, record in sorted(raw_records.items()):
        record["revision"] = revision
        assert_remote_safe(record)
        validate_generated_document(record, "codex-session-v1.schema.json")
        records[key] = record
    generated_at = _generated_at(records)
    events = _events(slices)
    summary = _summary(
        day_text,
        window=window,
        generated_at=generated_at,
        revision=revision,
        records=records,
    )
    sync_state = _sync_state(records)
    event_lines: dict[str, list[str]] = {key: [] for key in records}
    assert_remote_safe(summary)
    validate_generated_document(summary, "codex-daily-summary-v1.schema.json")
    for event in events:
        assert_remote_safe(event)
        key = str(event.get("session_key"))
        if key not in records:
            raise ValueError("Event has no matching session aggregate")
        line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        if len(line.encode("utf-8")) > MAX_JSONL_RECORD_BYTES:
            raise ValueError("oversized_jsonl_record")
        event_lines[key].append(line)

    session_payloads = {
        sessions_directory / f"{key}.json": _pretty(record)
        for key, record in records.items()
    }
    summary_path = destination / "summary.json"
    event_payloads = {
        destination / "events" / f"{key}.jsonl": "".join(lines)
        for key, lines in sorted(event_lines.items())
    }
    hashes = {
        str(path.relative_to(destination)): _sha256_text(payload)
        for path, payload in (*session_payloads.items(), *event_payloads.items())
    }
    hashes[summary_path.name] = _sha256_text(_pretty(summary))
    first_observed, last_observed = _observed_bounds(records)
    previous_late_arrival = previous_manifest.get("late_arrival")
    late_arrival: dict[str, object]
    if not content_changed and isinstance(previous_late_arrival, dict):
        late_arrival = dict(previous_late_arrival)
    else:
        late_arrival = {
            "previous_revision": previous_revision or None,
            "new_session_keys": new_keys,
            "changed_session_keys": sorted(set(changed_keys + removed_keys)),
        }
    manifest: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA,
        "date": day_text,
        **window.identity(),
        "generated_at": generated_at,
        "revision": revision,
        "device_id": config.device_id,
        "first_observed_activity": first_observed,
        "last_observed_activity": last_observed,
        "sessions_seen": len(slices),
        "sessions_exported": len(records),
        "sessions_with_unknown_outcomes": sum(
            1 for record in records.values() if record.get("activity_state") == "unknown"
        ),
        "sync_state": sync_state,
        "late_arrival": late_arrival,
        "generated_file_hashes": dict(sorted(hashes.items())),
    }
    assert_remote_safe(manifest)
    validate_generated_document(manifest, "codex-manifest-v1.schema.json")
    manifest_path = destination / "manifest.json"
    generated_paths = [*session_payloads, summary_path, *event_payloads, manifest_path]
    changed = any(
        not path.exists() or path.read_text(encoding="utf-8") != payload
        for path, payload in (
            *session_payloads.items(),
            (summary_path, _pretty(summary)),
            *event_payloads.items(),
            (manifest_path, _pretty(manifest)),
        )
    )
    sessions_directory.mkdir(parents=True, exist_ok=True)
    for stale in sorted(sessions_directory.glob("*.json")):
        if stale.stem not in records:
            stale.unlink()
    for path, payload in session_payloads.items():
        atomic_write_text(path, payload, overwrite=True, create_parents=True)
    atomic_write_text(summary_path, _pretty(summary), overwrite=True, create_parents=True)
    for path, payload in event_payloads.items():
        if not path.exists() or path.read_text(encoding="utf-8") != payload:
            atomic_write_text(path, payload, overwrite=True, create_parents=True)
    for stale in sorted((destination / "events").glob("*.jsonl")):
        if stale not in event_payloads:
            stale.unlink()
    if legacy_events.exists():
        legacy_events.unlink()
    atomic_write_text(manifest_path, _pretty(manifest), overwrite=True, create_parents=True)
    latest_path = _write_latest_status(
        config,
        window,
        revision,
        sync_state,
        generated_at,
    )
    generated_paths.append(latest_path)
    return ExportResult(
        date=day_text,
        revision=revision,
        session_count=len(records),
        changed=changed,
        generated_paths=tuple(generated_paths),
        sync_state=sync_state,
    )


def initialize_checkout_contract(config: LedgerConfig) -> tuple[Path, ...]:
    """Create safe static contract files only when they do not already exist."""

    checkout = config.ledger_checkout
    checkout.mkdir(parents=True, exist_ok=True)
    static: dict[Path, str] = {
        checkout / "README.md": _LEDGER_README,
        checkout / "SCHEMA.md": _SCHEMA_README,
        checkout / "config" / "project-aliases.yaml": _PROJECT_ALIASES,
    }
    for schema_name in _SCHEMA_FILES:
        resource = files("codex_insights.daily_ledger.schemas").joinpath(schema_name)
        static[checkout / "schema" / schema_name] = resource.read_text(encoding="utf-8")
    for path, payload in static.items():
        if path.exists():
            continue
        atomic_write_text(path, payload, overwrite=False, create_parents=True)
    policy_path = ensure_report_policy(config)
    return (*tuple(static), policy_path)


def cached_dates(config: LedgerConfig) -> tuple[date, ...]:
    """List dates available in local privacy-filtered session caches."""

    dates: set[date] = set()
    for path in _cache_files(config):
        raw = _load_object(path)
        slices = raw.get("slices")
        if not isinstance(slices, dict):
            continue
        for value in slices:
            try:
                dates.add(date.fromisoformat(str(value)))
            except ValueError:
                continue
    return tuple(sorted(dates))


def _cached_slices(config: LedgerConfig, day: str) -> dict[str, dict[str, object]]:
    slices: dict[str, dict[str, object]] = {}
    for path in _cache_files(config):
        raw = _load_object(path)
        session_key = raw.get("session_key")
        raw_slices = raw.get("slices")
        if not isinstance(session_key, str) or not isinstance(raw_slices, dict):
            continue
        value = raw_slices.get(day)
        if isinstance(value, dict):
            slices[session_key] = value
    return dict(sorted(slices.items()))


def _cache_files(config: LedgerConfig) -> tuple[Path, ...]:
    directory = config.paths.cache / "sessions"
    return tuple(sorted(directory.glob("*.json"))) if directory.is_dir() else ()


def _existing_session_records(directory: Path) -> dict[str, dict[str, object]]:
    if not directory.is_dir():
        return {}
    return {
        path.stem: value
        for path in sorted(directory.glob("*.json"))
        if (value := _load_object(path))
    }


def _summary(
    day: str,
    *,
    window: ReportingWindow,
    generated_at: str,
    revision: int,
    records: dict[str, dict[str, object]],
) -> dict[str, object]:
    groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for record in records.values():
        project = _object(record.get("project"))
        project_id = str(project.get("project_id", "unknown"))
        objective = str(record.get("objective") or "")
        groups.setdefault((project_id, objective), []).append(record)
    rollups = [
        _rollup(project_id, objective, items)
        for (project_id, objective), items in sorted(groups.items())
    ]
    all_commits = _dedupe_objects(
        item for record in records.values() for item in _objects(record.get("commits"))
    )
    all_validations = _dedupe_objects(
        item for record in records.values() for item in _objects(record.get("validation"))
    )
    state_counts = {
        state: sum(1 for record in records.values() if record.get("activity_state") == state)
        for state in _STATE_PRECEDENCE
    }
    return {
        "schema_version": SUMMARY_SCHEMA,
        "date": day,
        **window.identity(),
        "generated_at": generated_at,
        "revision": revision,
        "coverage": {
            "sessions_seen": len(records),
            "sessions_exported": len(records),
            "event_timestamp_sessions": sum(
                1 for record in records.values() if record.get("timestamp_precision") == "event"
            ),
            "capture_fallback_sessions": sum(
                1
                for record in records.values()
                if record.get("timestamp_precision") == "capture_fallback"
            ),
        },
        "totals": {
            "activities": len(rollups),
            "sessions": len(records),
            "commits": len(all_commits),
            "validation_events": len(all_validations),
            "activity_states": state_counts,
        },
        "project_rollups": rollups,
    }


def _rollup(
    project_id: str,
    objective: str,
    records: list[dict[str, object]],
) -> dict[str, object]:
    ordered = sorted(records, key=lambda item: str(item.get("last_observed_activity", "")))
    latest = ordered[-1]
    project = _object(latest.get("project"))
    states = {str(item.get("activity_state")) for item in ordered}
    state = next((value for value in _STATE_PRECEDENCE if value in states), "unknown")
    work_done = _dedupe_objects(
        item for record in ordered for item in _objects(record.get("work_done"))
    )
    commits = _dedupe_objects(
        item for record in ordered for item in _objects(record.get("commits"))
    )
    validations = _dedupe_objects(
        item for record in ordered for item in _objects(record.get("validation"))
    )
    decisions = sorted(
        {str(item) for record in ordered for item in _list(record.get("decisions"))}
    )
    open_loops = sorted(
        {str(item) for record in ordered for item in _list(record.get("open_loops"))}
    )
    git_values = [_object(record.get("git")) for record in ordered]
    return {
        "project_id": project_id,
        "display_name": str(project.get("display_name", "Unknown project")),
        "category": str(project.get("category", "unknown")),
        "activity_state": state,
        "objective": objective or None,
        "work_done": work_done,
        "git": {
            "starting_head": next(
                (item.get("starting_head") for item in git_values if item.get("starting_head")),
                None,
            ),
            "ending_head": next(
                (
                    item.get("ending_head")
                    for item in reversed(git_values)
                    if item.get("ending_head")
                ),
                None,
            ),
            "branch": next(
                (item.get("branch") for item in reversed(git_values) if item.get("branch")),
                None,
            ),
            "remote_slug": project.get("remote_slug"),
            "worktree_state": str(git_values[-1].get("worktree_state", "unknown")),
            "pushed": True if any(item.get("pushed") is True for item in git_values) else None,
        },
        "commits": commits,
        "validation": validations,
        "decisions": decisions,
        "open_loops": open_loops,
        "session_keys": sorted(str(record["session_key"]) for record in ordered),
        "evidence_level": _highest_evidence(ordered),
    }


def _events(
    slices: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for session_key, value in sorted(slices.items()):
        for raw in _objects(value.get("events")):
            event_id = raw.get("event_id")
            if not isinstance(event_id, str):
                continue
            row = dict(raw)
            if row.get("session_key") != session_key:
                raise ValueError("Event shard session identity mismatch")
            # Event provenance revision is independent of the daily manifest.
            # Updating another session must not churn this session's shard.
            row.setdefault("revision", 1)
            if event_id in rows and rows[event_id] != row:
                raise ValueError("Conflicting event identity across session shards")
            rows[event_id] = row
    return sorted(rows.values(), key=lambda row: (str(row.get("timestamp") or ""),
                                                  str(row["event_id"])))


def _sync_state(records: dict[str, dict[str, object]]) -> str:
    if not records:
        return "unknown"
    if all(record.get("timestamp_precision") == "event" for record in records.values()):
        return "complete_through_last_observed_activity"
    return "partial"


def _generated_at(records: dict[str, dict[str, object]]) -> str:
    values = [str(record.get("last_observed_activity", "")) for record in records.values()]
    return max(values) if values else "1970-01-01T00:00:00Z"


def _observed_bounds(records: dict[str, dict[str, object]]) -> tuple[str | None, str | None]:
    first = [str(record["first_observed_activity"]) for record in records.values()]
    last = [str(record["last_observed_activity"]) for record in records.values()]
    return (min(first), max(last)) if first and last else (None, None)


def _write_latest_status(
    config: LedgerConfig,
    window: ReportingWindow,
    revision: int,
    sync_state: str,
    generated_at: str,
) -> Path:
    path = config.ledger_checkout / "status" / "latest.json"
    existing = _load_object(path)
    existing_date = existing.get("date")
    if isinstance(existing_date, str) and existing_date > window.report_date.isoformat():
        return path
    payload = {
        "schema_version": "codex-latest-status-v1",
        "date": window.report_date.isoformat(),
        **window.identity(),
        "generated_at": generated_at,
        "revision": revision,
        "sync_state": sync_state,
        "manifest": f"ledger/codex/{window.report_date:%Y/%m/%d}/manifest.json",
    }
    assert_remote_safe(payload)
    atomic_write_text(path, _pretty(payload), overwrite=True, create_parents=True)
    return path


def _day_directory(checkout: Path, day: date) -> Path:
    return checkout / "ledger" / "codex" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"


def _validate_existing_window(
    previous_manifest: dict[str, object],
    window: ReportingWindow,
) -> None:
    if not previous_manifest or "report_date" not in previous_manifest:
        return
    expected = window.identity()
    if any(previous_manifest.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "Existing manifest reporting window differs from the effective historical policy"
        )


def _validate_record_window(record: dict[str, object], window: ReportingWindow) -> None:
    expected = window.identity()
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("Cached session slice does not match the effective reporting window")


def _load_object(path: Path) -> dict[str, object]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _pretty(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _canonical(value: dict[str, object], *, drop_revision: bool) -> str:
    selected = dict(value)
    if drop_revision:
        selected.pop("revision", None)
    return json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _object(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _objects(value: object) -> list[dict[str, object]]:
    return [item for item in _list(value) if isinstance(item, dict)]


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _dedupe_objects(values: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for value in values:
        rows[_canonical(value, drop_revision=False)] = value
    return [rows[key] for key in sorted(rows)]


def _highest_evidence(records: list[dict[str, object]]) -> str:
    levels = {
        str(_object(record.get("evidence")).get("level", "low")) for record in records
    }
    return next((level for level in ("high", "medium", "low") if level in levels), "low")


_LEDGER_README = """# Angze Daily Ledger

This private repository contains privacy-filtered, evidence-grounded Codex daily activity records.
It intentionally excludes full prompts, transcripts, tool output, credentials, and absolute paths.
Generated Codex data lives under `ledger/codex/`; `status/latest.json` points to the newest
manifest.
"""

_SCHEMA_README = """# Ledger schema

The V1 JSON Schemas in `schema/` describe per-session slices, deterministic daily summaries, and
daily manifests. `events/<session-key>.jsonl` contains only event identity, type,
time, source hash, stable session key, and revision. `config/report-policy.yaml` is the committed
source of truth for reporting windows. Manifests permanently record the policy and half-open window
used for each report date. Unknown evidence remains explicit.
"""

_PROJECT_ALIASES = """# Optional display-name overrides. Keep aliases free of local paths.
aliases: {}
"""
