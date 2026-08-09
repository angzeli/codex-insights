"""Bounded, read-only schema audit for an installed Codex home."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlparse

from codex_insights.adapters.audit_models import (
    CountedValue,
    FieldObservation,
    HistoryAudit,
    RolloutCollectionAudit,
    RolloutFileAudit,
    SourceAuditResult,
    SourceDirectoryAudit,
    SourceFileMetadata,
    SqliteColumnAudit,
    SqliteDatabaseAudit,
    SqliteTableAudit,
)
from codex_insights.db import open_source_sqlite_readonly

_MAX_DISCOVERED_ROLLOUTS = 100_000
_MAX_JSONL_LINE_BYTES = 1_048_576
_MAX_ROLLOUT_SCAN_BYTES = 4_194_304
_MAX_ROLLOUT_SCAN_LINES = 20_000
_MAX_HISTORY_HEAD_BYTES = 4_194_304
_MAX_HISTORY_TAIL_BYTES = 1_048_576
_MAX_HISTORY_SCAN_LINES = 50_000
_MAX_SQLITE_REFERENCE_ROWS = 10_000
_MAX_SQLITE_PROGRESS_CALLBACKS = 100
_MAX_OBSERVED_FIELDS = 512
_MAX_NESTING_DEPTH = 6
_MAX_CONTAINER_ITEMS = 200

_SESSION_DIRECTORIES = ("sessions", "archived_sessions")
_ADJACENT_METADATA_TOKENS = ("config", "global", "metadata", "model", "version")
_SENSITIVE_METADATA_TOKENS = ("auth", "credential", "secret", "token")
_SAFE_LABEL_PATTERN = re.compile(r"[A-Za-z0-9_.:/-]{1,100}\Z")
_TIMESTAMP_FIELD_NAMES = {
    "created_at",
    "ended_at",
    "started_at",
    "timestamp",
    "ts",
    "updated_at",
}
_SESSION_ID_FIELD_NAMES = {
    "conversation_id",
    "session_id",
    "sessionid",
    "thread_id",
    "threadid",
}


@dataclass(slots=True)
class _FieldStat:
    present_count: int = 0
    value_types: set[str] = field(default_factory=set)
    minimum_length: int | None = None
    maximum_length: int | None = None
    total_length: int = 0
    length_count: int = 0


class _FieldCollector:
    def __init__(self) -> None:
        self._fields: dict[str, _FieldStat] = {}

    def observe(self, name: str, value: Any) -> None:
        safe_name = _sanitize_field_path(name)
        if safe_name not in self._fields and len(self._fields) >= _MAX_OBSERVED_FIELDS:
            return
        stat = self._fields.setdefault(safe_name, _FieldStat())
        stat.present_count += 1
        stat.value_types.add(_value_type(value))
        length = _approximate_length(value)
        if length is None:
            return
        stat.minimum_length = (
            length if stat.minimum_length is None else min(stat.minimum_length, length)
        )
        stat.maximum_length = (
            length if stat.maximum_length is None else max(stat.maximum_length, length)
        )
        stat.total_length += length
        stat.length_count += 1

    def results(self) -> tuple[FieldObservation, ...]:
        observations = []
        for name, stat in sorted(self._fields.items()):
            average = (
                round(stat.total_length / stat.length_count) if stat.length_count else None
            )
            observations.append(
                FieldObservation(
                    field=name,
                    present_count=stat.present_count,
                    value_types=tuple(sorted(stat.value_types)),
                    minimum_length=stat.minimum_length,
                    maximum_length=stat.maximum_length,
                    approximate_average_length=average,
                )
            )
        return tuple(observations)


@dataclass(slots=True)
class _JsonlScan:
    sampled_lines: int = 0
    valid_records: int = 0
    malformed_lines: int = 0
    scanned_bytes: int = 0
    reached_eof: bool = False
    truncated: bool = False

    def merge(self, other: _JsonlScan) -> None:
        self.sampled_lines += other.sampled_lines
        self.valid_records += other.valid_records
        self.malformed_lines += other.malformed_lines
        self.scanned_bytes += other.scanned_bytes
        self.reached_eof = self.reached_eof or other.reached_eof
        self.truncated = self.truncated or other.truncated


@dataclass(frozen=True, slots=True)
class _DiscoveredRollout:
    path: Path
    metadata: SourceFileMetadata
    modified_ns: int
    directory: str


def audit_codex_source(
    codex_home: Path,
    *,
    sample_size: int = 5,
    verbose: bool = False,
) -> SourceAuditResult:
    """Audit Codex source layout without retaining or returning raw record content."""

    if not 0 <= sample_size <= 100:
        raise ValueError("sample_size must be between 0 and 100")

    home = codex_home.expanduser().resolve(strict=False)
    if not home.is_dir():
        history = _empty_history()
        rollouts = _empty_rollouts()
        warning = f"Codex home does not exist or is not a directory: {home}"
        return SourceAuditResult(
            codex_home=str(home),
            codex_home_exists=False,
            sample_size=sample_size,
            history=history,
            state_databases=(),
            rollouts=rollouts,
            adjacent_metadata_files=(),
            oldest_activity=None,
            newest_activity=None,
            schema_inconsistencies=(),
            warnings=(warning,),
        )

    warnings: list[str] = []
    inconsistencies: list[str] = []
    history = _audit_history(home, warnings)
    state_databases = _audit_state_databases(
        home,
        sample_size=sample_size,
        verbose=verbose,
        warnings=warnings,
    )
    rollouts, rollout_activity = _audit_rollouts(
        home,
        sample_size=sample_size,
        warnings=warnings,
    )
    adjacent = _discover_adjacent_metadata(home, warnings)

    _identify_schema_inconsistencies(state_databases, rollouts, inconsistencies)
    if history.malformed_line_count:
        warnings.append(
            f"history.jsonl contained {history.malformed_line_count} malformed sampled line(s)."
        )
    if rollouts.malformed_line_count:
        warnings.append(
            "Sampled rollout files contained "
            f"{rollouts.malformed_line_count} malformed line(s)."
        )

    activity = list(rollout_activity)
    activity.extend(
        value
        for value in (
            history.oldest_timestamp,
            history.newest_timestamp,
            history.modified_at,
        )
        if value is not None
    )
    return SourceAuditResult(
        codex_home=str(home),
        codex_home_exists=True,
        sample_size=sample_size,
        history=history,
        state_databases=state_databases,
        rollouts=rollouts,
        adjacent_metadata_files=adjacent,
        oldest_activity=min(activity) if activity else None,
        newest_activity=max(activity) if activity else None,
        schema_inconsistencies=tuple(dict.fromkeys(inconsistencies)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _audit_history(home: Path, warnings: list[str]) -> HistoryAudit:
    path = home / "history.jsonl"
    if not path.is_file():
        return _empty_history()

    try:
        metadata = _file_metadata(path, home)
    except OSError as error:
        warnings.append(f"Could not stat history.jsonl: {error}")
        return _empty_history()

    observed_fields: set[str] = set()
    text_fields = _FieldCollector()
    session_ids: set[str] = set()
    records_with_session_id = 0
    oldest: str | None = None
    newest: str | None = None

    def observe(record: Any) -> None:
        nonlocal newest, oldest, records_with_session_id
        if not isinstance(record, dict):
            return
        record_has_session = False
        for raw_name in record:
            observed_fields.add(_sanitize_component(raw_name))
        for path_name, leaf_name, value in _iter_fields(record):
            if isinstance(value, str):
                text_fields.observe(path_name, value)
            normalized_leaf = leaf_name.lower()
            if normalized_leaf in _SESSION_ID_FIELD_NAMES and isinstance(value, (str, int)):
                record_has_session = True
                if len(session_ids) < _MAX_HISTORY_SCAN_LINES:
                    session_ids.add(str(value))
            if normalized_leaf in _TIMESTAMP_FIELD_NAMES:
                timestamp = _normalize_timestamp(value)
                if timestamp is not None:
                    oldest = timestamp if oldest is None else min(oldest, timestamp)
                    newest = timestamp if newest is None else max(newest, timestamp)
        if record_has_session:
            records_with_session_id += 1

    size = metadata.size_bytes
    combined = _JsonlScan()
    if size <= _MAX_HISTORY_HEAD_BYTES + _MAX_HISTORY_TAIL_BYTES:
        combined = _scan_jsonl_segment(
            path,
            max_bytes=max(size + 1, 1),
            max_lines=_MAX_HISTORY_SCAN_LINES,
            observe=observe,
        )
        fully_scanned = combined.reached_eof and not combined.truncated
    else:
        head = _scan_jsonl_segment(
            path,
            max_bytes=_MAX_HISTORY_HEAD_BYTES,
            max_lines=_MAX_HISTORY_SCAN_LINES // 2,
            observe=observe,
        )
        tail = _scan_jsonl_segment(
            path,
            max_bytes=_MAX_HISTORY_TAIL_BYTES,
            max_lines=_MAX_HISTORY_SCAN_LINES // 2,
            observe=observe,
            start_offset=max(0, size - _MAX_HISTORY_TAIL_BYTES),
        )
        combined.merge(head)
        combined.merge(tail)
        fully_scanned = False
        warnings.append(
            "history.jsonl exceeded the bounded scan window; line count and schema are sampled."
        )

    approximate_lines: int | None
    if fully_scanned:
        approximate_lines = combined.sampled_lines
    elif combined.sampled_lines and combined.scanned_bytes:
        average_line_bytes = combined.scanned_bytes / combined.sampled_lines
        approximate_lines = round(size / average_line_bytes)
    else:
        approximate_lines = None

    return HistoryAudit(
        relative_path="history.jsonl",
        exists=True,
        size_bytes=size,
        modified_at=metadata.modified_at,
        approximate_line_count=approximate_lines,
        sampled_line_count=combined.sampled_lines,
        valid_record_count=combined.valid_records,
        malformed_line_count=combined.malformed_lines,
        fully_scanned=fully_scanned,
        observed_fields=tuple(sorted(observed_fields)),
        text_fields=text_fields.results(),
        oldest_timestamp=oldest,
        newest_timestamp=newest,
        records_with_session_id=records_with_session_id,
        unique_session_ids_in_sample=len(session_ids),
    )


def _audit_rollouts(
    home: Path,
    *,
    sample_size: int,
    warnings: list[str],
) -> tuple[RolloutCollectionAudit, tuple[str, ...]]:
    discovered, directories, truncated = _discover_rollouts(home, warnings)
    selected = _select_rollout_sample(discovered, sample_size)
    audits = tuple(_audit_rollout_file(item, warnings) for item in selected)

    record_types: Counter[str] = Counter()
    payload_types: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    tool_names: Counter[str] = Counter()
    observed_fields: set[str] = set()
    token_fields: set[str] = set()
    malformed = 0
    without_metadata = 0
    for audit in audits:
        record_types.update({item.name: item.count for item in audit.record_types})
        payload_types.update({item.name: item.count for item in audit.payload_types})
        categories.update({item.name: item.count for item in audit.event_categories})
        tool_names.update({item.name: item.count for item in audit.tool_names})
        observed_fields.update(audit.observed_fields)
        token_fields.update(audit.token_fields)
        malformed += audit.malformed_line_count
        without_metadata += int(audit.session_metadata_records == 0)

    activity = tuple(
        value
        for item in discovered
        for value in (item.metadata.modified_at,)
        if value is not None
    ) + tuple(
        value
        for audit in audits
        for value in (audit.oldest_timestamp, audit.newest_timestamp)
        if value is not None
    )
    return (
        RolloutCollectionAudit(
            directories=directories,
            discovered_file_count=len(discovered),
            total_size_bytes=sum(item.metadata.size_bytes for item in discovered),
            sampled_file_count=len(audits),
            discovery_truncated=truncated,
            sampled_files=audits,
            record_types=_counted(record_types),
            payload_types=_counted(payload_types),
            event_categories=_counted(categories),
            observed_fields=tuple(sorted(observed_fields)),
            token_fields=tuple(sorted(token_fields)),
            tool_names=_counted(tool_names),
            malformed_line_count=malformed,
            sampled_files_without_session_metadata=without_metadata,
        ),
        activity,
    )


def _discover_rollouts(
    home: Path,
    warnings: list[str],
) -> tuple[list[_DiscoveredRollout], tuple[SourceDirectoryAudit, ...], bool]:
    discovered: list[_DiscoveredRollout] = []
    summaries: list[SourceDirectoryAudit] = []
    discovery_truncated = False

    for directory_name in _SESSION_DIRECTORIES:
        root = home / directory_name
        directory_count = 0
        directory_size = 0
        if not root.is_dir():
            summaries.append(
                SourceDirectoryAudit(
                    relative_path=directory_name,
                    exists=False,
                    rollout_file_count=0,
                    total_size_bytes=0,
                )
            )
            continue

        def on_error(error: OSError, directory: str = directory_name) -> None:
            warnings.append(f"Could not inspect part of {directory}: {error}")

        for raw_root, directory_names, file_names in os.walk(
            root,
            followlinks=False,
            onerror=on_error,
        ):
            current_root = Path(raw_root)
            directory_names[:] = [
                name for name in directory_names if not (current_root / name).is_symlink()
            ]
            for file_name in file_names:
                if not file_name.lower().endswith(".jsonl"):
                    continue
                if len(discovered) >= _MAX_DISCOVERED_ROLLOUTS:
                    discovery_truncated = True
                    break
                path = current_root / file_name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    stat = path.stat()
                    metadata = SourceFileMetadata(
                        relative_path=path.relative_to(home).as_posix(),
                        size_bytes=stat.st_size,
                        modified_at=_datetime_from_timestamp(stat.st_mtime),
                    )
                except (OSError, ValueError) as error:
                    warnings.append(f"Could not stat a rollout candidate: {error}")
                    continue
                discovered.append(
                    _DiscoveredRollout(
                        path=path,
                        metadata=metadata,
                        modified_ns=stat.st_mtime_ns,
                        directory=directory_name,
                    )
                )
                directory_count += 1
                directory_size += stat.st_size
            if discovery_truncated:
                break
        summaries.append(
            SourceDirectoryAudit(
                relative_path=directory_name,
                exists=True,
                rollout_file_count=directory_count,
                total_size_bytes=directory_size,
            )
        )
        if discovery_truncated:
            warnings.append(
                f"Rollout discovery stopped at {_MAX_DISCOVERED_ROLLOUTS:,} files."
            )
            break

    discovered.sort(key=lambda item: (item.modified_ns, item.metadata.relative_path))
    return discovered, tuple(summaries), discovery_truncated


def _select_rollout_sample(
    discovered: list[_DiscoveredRollout],
    sample_size: int,
) -> tuple[_DiscoveredRollout, ...]:
    if sample_size == 0 or not discovered:
        return ()
    if sample_size >= len(discovered):
        return tuple(discovered)
    if sample_size == 1:
        return (discovered[-1],)

    indices = {
        round(index * (len(discovered) - 1) / (sample_size - 1))
        for index in range(sample_size)
    }
    if len(indices) < sample_size:
        for index in range(len(discovered) - 1, -1, -1):
            indices.add(index)
            if len(indices) == sample_size:
                break
    return tuple(discovered[index] for index in sorted(indices))


def _audit_rollout_file(
    discovered: _DiscoveredRollout,
    warnings: list[str],
) -> RolloutFileAudit:
    record_types: Counter[str] = Counter()
    payload_types: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    tool_names: Counter[str] = Counter()
    observed_fields: set[str] = set()
    token_fields: set[str] = set()
    text_fields = _FieldCollector()
    session_metadata_records = 0
    oldest: str | None = None
    newest: str | None = None

    def observe(record: Any) -> None:
        nonlocal newest, oldest, session_metadata_records
        if not isinstance(record, dict):
            record_types["<non-object>"] += 1
            categories["unknown"] += 1
            return

        for raw_name in record:
            observed_fields.add(_sanitize_component(raw_name))

        record_type = _safe_label(record.get("type"))
        if record_type is None:
            record_type = _safe_label(record.get("event")) or _safe_label(record.get("kind"))
        record_type = record_type or "<missing>"
        record_types[record_type] += 1

        payload = record.get("payload")
        payload_type: str | None = None
        if isinstance(payload, dict):
            payload_type = _safe_label(payload.get("type"))
            if payload_type is not None:
                payload_types[payload_type] += 1

        local_token_fields: set[str] = set()
        field_names: set[str] = set()
        for field_path, leaf_name, value in _iter_fields(record):
            lowered = leaf_name.lower()
            field_names.add(lowered)
            if isinstance(value, str):
                text_fields.observe(field_path, value)
            if "token" in lowered:
                safe_path = _sanitize_field_path(field_path)
                token_fields.add(safe_path)
                local_token_fields.add(safe_path)
            if lowered in _TIMESTAMP_FIELD_NAMES:
                timestamp = _normalize_timestamp(value)
                if timestamp is not None:
                    oldest = timestamp if oldest is None else min(oldest, timestamp)
                    newest = timestamp if newest is None else max(newest, timestamp)
            if lowered in {"name", "tool_name"} and _looks_like_tool_record(
                record_type,
                payload_type,
                field_names,
            ):
                tool_name = _safe_label(value)
                if tool_name is not None:
                    tool_names[tool_name] += 1

        category = _event_category(
            record_type=record_type,
            payload_type=payload_type,
            field_names=field_names,
            has_token_fields=bool(local_token_fields),
        )
        categories[category] += 1
        if category == "session_metadata":
            session_metadata_records += 1

    scan = _scan_jsonl_segment(
        discovered.path,
        max_bytes=_MAX_ROLLOUT_SCAN_BYTES,
        max_lines=_MAX_ROLLOUT_SCAN_LINES,
        observe=observe,
    )
    if scan.truncated:
        warnings.append(
            "A rollout sample reached its byte, line, or line-length limit: "
            f"{discovered.metadata.relative_path}"
        )

    return RolloutFileAudit(
        relative_path=discovered.metadata.relative_path,
        size_bytes=discovered.metadata.size_bytes,
        modified_at=discovered.metadata.modified_at,
        sampled_line_count=scan.sampled_lines,
        valid_record_count=scan.valid_records,
        malformed_line_count=scan.malformed_lines,
        scan_truncated=scan.truncated,
        record_types=_counted(record_types),
        payload_types=_counted(payload_types),
        event_categories=_counted(categories),
        observed_fields=tuple(sorted(observed_fields)),
        token_fields=tuple(sorted(token_fields)),
        tool_names=_counted(tool_names),
        text_fields=text_fields.results(),
        session_metadata_records=session_metadata_records,
        oldest_timestamp=oldest,
        newest_timestamp=newest,
    )


def _audit_state_databases(
    home: Path,
    *,
    sample_size: int,
    verbose: bool,
    warnings: list[str],
) -> tuple[SqliteDatabaseAudit, ...]:
    candidates: set[Path] = set()
    try:
        candidates.update(path for path in home.glob("state_*.sqlite") if path.is_file())
    except OSError as error:
        warnings.append(f"Could not discover versioned state databases: {error}")
    legacy = home / "state.sqlite"
    if legacy.is_file():
        candidates.add(legacy)

    audits = [
        _audit_state_database(
            path,
            home=home,
            sample_size=min(sample_size, 5),
            verbose=verbose,
        )
        for path in sorted(candidates, key=lambda item: item.name)
    ]
    for audit in audits:
        warnings.extend(audit.warnings)
    return tuple(audits)


def _audit_state_database(
    path: Path,
    *,
    home: Path,
    sample_size: int,
    verbose: bool,
) -> SqliteDatabaseAudit:
    metadata = _file_metadata(path, home)
    warnings: list[str] = []
    table_audits: list[SqliteTableAudit] = []
    likely_tables: list[str] = []
    references_checked = 0
    missing_references = 0
    reference_truncated = False

    try:
        connection = open_source_sqlite_readonly(path)
    except (OSError, sqlite3.Error) as error:
        return SqliteDatabaseAudit(
            relative_path=metadata.relative_path,
            size_bytes=metadata.size_bytes,
            modified_at=metadata.modified_at,
            tables=(),
            likely_session_tables=(),
            rollout_references_checked=0,
            missing_rollout_references=0,
            rollout_reference_scan_truncated=False,
            warnings=(f"Could not open {metadata.relative_path} read-only: {error}",),
        )

    try:
        raw_tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for raw_table_name in raw_tables:
            raw_columns = _table_columns(connection, raw_table_name)
            columns = tuple(item[1] for item in raw_columns)
            likely_session = _likely_session_table(raw_table_name, columns)
            if likely_session:
                likely_tables.append(_sanitize_component(raw_table_name))
            row_count = _safe_row_count(connection, raw_table_name)
            if row_count is None:
                warnings.append(
                    f"Skipped an expensive or unreadable row count in {metadata.relative_path}."
                )
            sampled_fields = (
                _sample_sqlite_metadata(
                    connection,
                    raw_table_name,
                    raw_columns,
                    sample_size,
                )
                if verbose and likely_session and sample_size
                else ()
            )
            checked, missing, truncated = _check_rollout_references(
                connection,
                raw_table_name,
                raw_columns,
                home,
            )
            references_checked += checked
            missing_references += missing
            reference_truncated = reference_truncated or truncated
            table_audits.append(
                SqliteTableAudit(
                    name=_sanitize_component(raw_table_name),
                    row_count=row_count,
                    likely_session_table=likely_session,
                    columns=columns,
                    sampled_metadata_fields=sampled_fields,
                )
            )
    except sqlite3.Error as error:
        warnings.append(f"Schema audit failed for {metadata.relative_path}: {error}")
    finally:
        connection.close()

    if missing_references:
        warnings.append(
            f"{metadata.relative_path} references {missing_references} missing rollout path(s) "
            f"among {references_checked} checked."
        )
    return SqliteDatabaseAudit(
        relative_path=metadata.relative_path,
        size_bytes=metadata.size_bytes,
        modified_at=metadata.modified_at,
        tables=tuple(table_audits),
        likely_session_tables=tuple(likely_tables),
        rollout_references_checked=references_checked,
        missing_rollout_references=missing_references,
        rollout_reference_scan_truncated=reference_truncated,
        warnings=tuple(warnings),
    )


def _table_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> list[tuple[str, SqliteColumnAudit]]:
    columns = []
    for row in connection.execute(
        "SELECT name, type, \"notnull\", pk FROM pragma_table_info(?)",
        (table_name,),
    ):
        raw_name = str(row[0])
        columns.append(
            (
                raw_name,
                SqliteColumnAudit(
                    name=_sanitize_component(raw_name),
                    declared_type=str(row[1] or ""),
                    nullable=not bool(row[2]),
                    primary_key=bool(row[3]),
                    likely_role=_likely_column_role(raw_name, table_name=table_name),
                ),
            )
        )
    return columns


def _safe_row_count(connection: sqlite3.Connection, table_name: str) -> int | None:
    callbacks = 0

    def progress() -> int:
        nonlocal callbacks
        callbacks += 1
        return int(callbacks > _MAX_SQLITE_PROGRESS_CALLBACKS)

    connection.set_progress_handler(progress, 100_000)
    try:
        row = connection.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}"
        ).fetchone()
        return int(row[0]) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        connection.set_progress_handler(None, 0)


def _sample_sqlite_metadata(
    connection: sqlite3.Connection,
    table_name: str,
    columns: list[tuple[str, SqliteColumnAudit]],
    limit: int,
) -> tuple[FieldObservation, ...]:
    selected = [raw for raw, column in columns if column.likely_role is not None][
        :_MAX_CONTAINER_ITEMS
    ]
    if not selected:
        return ()
    collector = _FieldCollector()
    selection = ", ".join(_quote_identifier(name) for name in selected)
    try:
        rows = connection.execute(
            f"SELECT {selection} FROM {_quote_identifier(table_name)} LIMIT ?",
            (limit,),
        )
        for row in rows:
            for index, raw_name in enumerate(selected):
                collector.observe(raw_name, row[index])
    except sqlite3.Error:
        return ()
    return collector.results()


def _check_rollout_references(
    connection: sqlite3.Connection,
    table_name: str,
    columns: list[tuple[str, SqliteColumnAudit]],
    home: Path,
) -> tuple[int, int, bool]:
    path_columns = [raw for raw, column in columns if column.likely_role == "rollout_path"]
    checked = 0
    missing = 0
    truncated = False
    for raw_column in path_columns:
        try:
            rows = connection.execute(
                f"SELECT {_quote_identifier(raw_column)} "
                f"FROM {_quote_identifier(table_name)} "
                f"WHERE {_quote_identifier(raw_column)} IS NOT NULL LIMIT ?",
                (_MAX_SQLITE_REFERENCE_ROWS + 1,),
            )
            for row_index, row in enumerate(rows):
                if row_index >= _MAX_SQLITE_REFERENCE_ROWS:
                    truncated = True
                    break
                value = row[0]
                if not isinstance(value, str) or not value:
                    continue
                referenced = _referenced_path(value, home)
                if referenced is None:
                    continue
                checked += 1
                try:
                    if not referenced.is_file():
                        missing += 1
                except OSError:
                    missing += 1
        except sqlite3.Error:
            continue
    return checked, missing, truncated


def _referenced_path(value: str, home: Path) -> Path | None:
    if value.startswith("file://"):
        parsed = urlparse(value)
        if parsed.scheme != "file":
            return None
        return Path(unquote(parsed.path)).expanduser().resolve(strict=False)
    path = Path(value).expanduser()
    return (path if path.is_absolute() else home / path).resolve(strict=False)


def _discover_adjacent_metadata(
    home: Path,
    warnings: list[str],
) -> tuple[SourceFileMetadata, ...]:
    discovered: list[SourceFileMetadata] = []
    try:
        entries = list(home.iterdir())
    except OSError as error:
        warnings.append(f"Could not list adjacent Codex metadata: {error}")
        return ()

    for path in entries:
        lower = path.name.lower()
        if path.name == "history.jsonl" or lower.startswith("state_"):
            continue
        if path.suffix.lower() not in {".json", ".toml"}:
            continue
        if not any(token in lower for token in _ADJACENT_METADATA_TOKENS):
            continue
        if any(token in lower for token in _SENSITIVE_METADATA_TOKENS):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            discovered.append(_file_metadata(path, home))
        except OSError as error:
            warnings.append(f"Could not stat an adjacent metadata candidate: {error}")
    return tuple(sorted(discovered, key=lambda item: item.relative_path))


def _identify_schema_inconsistencies(
    databases: tuple[SqliteDatabaseAudit, ...],
    rollouts: RolloutCollectionAudit,
    inconsistencies: list[str],
) -> None:
    if len(databases) > 1:
        signatures = {
            tuple(
                (table.name, tuple(column.name for column in table.columns))
                for table in database.tables
            )
            for database in databases
        }
        if len(signatures) > 1:
            inconsistencies.append(
                "Versioned state databases expose different table or column layouts."
            )
    sampled_type_sets = {
        tuple(item.name for item in audit.record_types) for audit in rollouts.sampled_files
    }
    if len(sampled_type_sets) > 1:
        inconsistencies.append(
            "Sampled rollout files expose different record-type sets."
        )
    if rollouts.sampled_files_without_session_metadata:
        inconsistencies.append(
            f"{rollouts.sampled_files_without_session_metadata} sampled rollout file(s) "
            "did not expose an expected session metadata record."
        )


def _scan_jsonl_segment(
    path: Path,
    *,
    max_bytes: int,
    max_lines: int,
    observe: Callable[[Any], None],
    start_offset: int = 0,
) -> _JsonlScan:
    result = _JsonlScan()
    try:
        with path.open("rb") as stream:
            if start_offset:
                stream.seek(start_offset)
                skipped = stream.readline(_MAX_JSONL_LINE_BYTES + 1)
                result.scanned_bytes += len(skipped)
                if len(skipped) > _MAX_JSONL_LINE_BYTES and not skipped.endswith(b"\n"):
                    result.truncated = True
                    return result
            _scan_jsonl_stream(
                stream,
                result=result,
                max_bytes=max_bytes,
                max_lines=max_lines,
                observe=observe,
            )
    except OSError:
        result.truncated = True
    return result


def _scan_jsonl_stream(
    stream: BinaryIO,
    *,
    result: _JsonlScan,
    max_bytes: int,
    max_lines: int,
    observe: Callable[[Any], None],
) -> None:
    while result.sampled_lines < max_lines and result.scanned_bytes < max_bytes:
        raw_line = stream.readline(_MAX_JSONL_LINE_BYTES + 1)
        if not raw_line:
            result.reached_eof = True
            return
        result.scanned_bytes += len(raw_line)
        result.sampled_lines += 1
        if len(raw_line) > _MAX_JSONL_LINE_BYTES:
            result.malformed_lines += 1
            result.truncated = True
            return
        try:
            record = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            result.malformed_lines += 1
            continue
        result.valid_records += 1
        observe(record)
    result.truncated = not result.reached_eof


def _iter_fields(
    value: Any,
    prefix: str = "",
    *,
    depth: int = 0,
) -> Iterator[tuple[str, str, Any]]:
    if depth > _MAX_NESTING_DEPTH:
        return
    if isinstance(value, dict):
        for index, (raw_name, child) in enumerate(value.items()):
            if index >= _MAX_CONTAINER_ITEMS:
                return
            component = _sanitize_component(raw_name)
            path = f"{prefix}.{component}" if prefix else component
            if isinstance(child, (dict, list)):
                yield from _iter_fields(child, path, depth=depth + 1)
            else:
                yield path, component, child
    elif isinstance(value, list):
        for child in value[:_MAX_CONTAINER_ITEMS]:
            path = f"{prefix}[]" if prefix else "[]"
            if isinstance(child, (dict, list)):
                yield from _iter_fields(child, path, depth=depth + 1)
            else:
                yield path, "[]", child


def _event_category(
    *,
    record_type: str,
    payload_type: str | None,
    field_names: set[str],
    has_token_fields: bool,
) -> str:
    labels = f"{record_type} {payload_type or ''}".lower()
    if "session_meta" in labels or (
        any(name in field_names for name in _SESSION_ID_FIELD_NAMES)
        and ("cwd" in field_names or "model" in field_names)
    ):
        return "session_metadata"
    if has_token_fields or "token" in labels or "usage" in labels:
        return "token_usage"
    if any(marker in labels for marker in ("command", "exec", "function_call", "tool")):
        return "tool_or_command"
    if any(marker in labels for marker in ("event", "message", "response")):
        return "message_or_event"
    return "unknown"


def _looks_like_tool_record(
    record_type: str,
    payload_type: str | None,
    field_names: set[str],
) -> bool:
    labels = f"{record_type} {payload_type or ''}".lower()
    return any(marker in labels for marker in ("command", "exec", "function", "tool")) or (
        "arguments" in field_names and "name" in field_names
    )


def _likely_column_role(name: str, *, table_name: str) -> str | None:
    lowered = name.lower()
    normalized_table = table_name.lower().rstrip("s")
    if lowered == "id":
        return (
            "session_id"
            if normalized_table in {"conversation", "session", "thread"}
            else "identifier"
        )
    if lowered in {"session_id", "thread_id", "conversation_id"} or lowered.endswith(
        ("_session_id", "_thread_id")
    ):
        return "session_id"
    if "title" in lowered or lowered in {"name", "summary"}:
        return "title_or_summary"
    if lowered in {"cwd", "working_directory", "workspace"} or "worktree" in lowered:
        return "working_directory"
    if lowered in {"source", "origin"} or lowered.endswith("_source"):
        return "source"
    if "model" in lowered:
        return "model"
    if "rollout" in lowered and ("path" in lowered or "file" in lowered):
        return "rollout_path"
    if "git" in lowered or lowered in {"branch", "commit", "commit_hash", "sha"}:
        return "git_metadata"
    if lowered in _TIMESTAMP_FIELD_NAMES or lowered.endswith(("_at", "_timestamp")):
        return "timestamp"
    if "archive" in lowered:
        return "archived_state"
    if any(marker in lowered for marker in ("content", "message", "prompt", "text")):
        return "free_text"
    return None


def _likely_session_table(name: str, columns: tuple[SqliteColumnAudit, ...]) -> bool:
    lowered = name.lower()
    roles = {column.likely_role for column in columns}
    return any(marker in lowered for marker in ("session", "thread", "conversation")) or (
        "session_id" in roles
        and bool(roles & {"working_directory", "rollout_path", "timestamp"})
    )


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _counted(counter: Counter[str]) -> tuple[CountedValue, ...]:
    return tuple(
        CountedValue(name=name, count=count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _file_metadata(path: Path, home: Path) -> SourceFileMetadata:
    stat = path.stat()
    return SourceFileMetadata(
        relative_path=path.relative_to(home).as_posix(),
        size_bytes=stat.st_size,
        modified_at=_datetime_from_timestamp(stat.st_mtime),
    )


def _datetime_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _normalize_timestamp(value: Any) -> str | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            timestamp = float(value)
            while timestamp > 10_000_000_000:
                timestamp /= 1000
            parsed = datetime.fromtimestamp(timestamp, tz=UTC)
        elif isinstance(value, str) and len(value) <= 100:
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            parsed = parsed.astimezone(UTC)
        else:
            return None
    except (OSError, OverflowError, ValueError):
        return None
    if not 2000 <= parsed.year <= 2200:
        return None
    return parsed.isoformat()


def _safe_label(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_LABEL_PATTERN.fullmatch(value):
        return value
    return None


def _sanitize_component(value: Any) -> str:
    text = str(value)
    cleaned = "".join(character if 32 <= ord(character) < 127 else "?" for character in text)
    return cleaned[:100] or "<empty>"


def _sanitize_field_path(value: str) -> str:
    return _sanitize_component(value)[:200]


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bytes):
        return "bytes"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _approximate_length(value: Any) -> int | None:
    if isinstance(value, (str, bytes, list, dict, tuple)):
        return len(value)
    return None


def _empty_history() -> HistoryAudit:
    return HistoryAudit(
        relative_path="history.jsonl",
        exists=False,
        size_bytes=0,
        modified_at=None,
        approximate_line_count=None,
        sampled_line_count=0,
        valid_record_count=0,
        malformed_line_count=0,
        fully_scanned=False,
        observed_fields=(),
        text_fields=(),
        oldest_timestamp=None,
        newest_timestamp=None,
        records_with_session_id=0,
        unique_session_ids_in_sample=0,
    )


def _empty_rollouts() -> RolloutCollectionAudit:
    return RolloutCollectionAudit(
        directories=tuple(
            SourceDirectoryAudit(
                relative_path=name,
                exists=False,
                rollout_file_count=0,
                total_size_bytes=0,
            )
            for name in _SESSION_DIRECTORIES
        ),
        discovered_file_count=0,
        total_size_bytes=0,
        sampled_file_count=0,
        discovery_truncated=False,
        sampled_files=(),
        record_types=(),
        payload_types=(),
        event_categories=(),
        observed_fields=(),
        token_fields=(),
        tool_names=(),
        malformed_line_count=0,
        sampled_files_without_session_metadata=0,
    )
