"""Typed, content-safe result models for Codex source audits."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CountedValue:
    """A structural value and its observed count."""

    name: str
    count: int


@dataclass(frozen=True, slots=True)
class FieldObservation:
    """Shape information for a field without retaining its value."""

    field: str
    present_count: int
    value_types: tuple[str, ...]
    minimum_length: int | None
    maximum_length: int | None
    approximate_average_length: int | None
    values_redacted: bool = True


@dataclass(frozen=True, slots=True)
class SourceFileMetadata:
    """Non-content metadata for one discovered source file."""

    relative_path: str
    size_bytes: int
    modified_at: str | None


@dataclass(frozen=True, slots=True)
class SourceDirectoryAudit:
    """Discovery summary for one known session directory."""

    relative_path: str
    exists: bool
    rollout_file_count: int
    total_size_bytes: int


@dataclass(frozen=True, slots=True)
class HistoryAudit:
    """Bounded schema observations for ``history.jsonl``."""

    relative_path: str
    exists: bool
    size_bytes: int
    modified_at: str | None
    approximate_line_count: int | None
    sampled_line_count: int
    valid_record_count: int
    malformed_line_count: int
    fully_scanned: bool
    observed_fields: tuple[str, ...]
    text_fields: tuple[FieldObservation, ...]
    oldest_timestamp: str | None
    newest_timestamp: str | None
    records_with_session_id: int
    unique_session_ids_in_sample: int


@dataclass(frozen=True, slots=True)
class SqliteColumnAudit:
    """One observed SQLite column and its likely semantic role."""

    name: str
    declared_type: str
    nullable: bool
    primary_key: bool
    likely_role: str | None


@dataclass(frozen=True, slots=True)
class SqliteTableAudit:
    """Schema and safe aggregate observations for one SQLite table."""

    name: str
    row_count: int | None
    likely_session_table: bool
    columns: tuple[SqliteColumnAudit, ...]
    sampled_metadata_fields: tuple[FieldObservation, ...]


@dataclass(frozen=True, slots=True)
class SqliteDatabaseAudit:
    """Read-only audit of a versioned Codex state database."""

    relative_path: str
    size_bytes: int
    modified_at: str | None
    tables: tuple[SqliteTableAudit, ...]
    likely_session_tables: tuple[str, ...]
    rollout_references_checked: int
    missing_rollout_references: int
    rollout_reference_scan_truncated: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RolloutFileAudit:
    """Bounded structural observations from one streamed rollout file."""

    relative_path: str
    size_bytes: int
    modified_at: str | None
    sampled_line_count: int
    valid_record_count: int
    malformed_line_count: int
    scan_truncated: bool
    record_types: tuple[CountedValue, ...]
    payload_types: tuple[CountedValue, ...]
    event_categories: tuple[CountedValue, ...]
    observed_fields: tuple[str, ...]
    token_fields: tuple[str, ...]
    tool_names: tuple[CountedValue, ...]
    text_fields: tuple[FieldObservation, ...]
    session_metadata_records: int
    oldest_timestamp: str | None
    newest_timestamp: str | None


@dataclass(frozen=True, slots=True)
class RolloutCollectionAudit:
    """Discovery totals and aggregate schema observations for rollouts."""

    directories: tuple[SourceDirectoryAudit, ...]
    discovered_file_count: int
    total_size_bytes: int
    sampled_file_count: int
    discovery_truncated: bool
    sampled_files: tuple[RolloutFileAudit, ...]
    record_types: tuple[CountedValue, ...]
    payload_types: tuple[CountedValue, ...]
    event_categories: tuple[CountedValue, ...]
    observed_fields: tuple[str, ...]
    token_fields: tuple[str, ...]
    tool_names: tuple[CountedValue, ...]
    malformed_line_count: int
    sampled_files_without_session_metadata: int


@dataclass(frozen=True, slots=True)
class SourceAuditResult:
    """Complete serializable result of a conservative Codex source audit."""

    codex_home: str
    codex_home_exists: bool
    sample_size: int
    history: HistoryAudit
    state_databases: tuple[SqliteDatabaseAudit, ...]
    rollouts: RolloutCollectionAudit
    adjacent_metadata_files: tuple[SourceFileMetadata, ...]
    oldest_activity: str | None
    newest_activity: str | None
    schema_inconsistencies: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return only JSON-compatible primitive containers."""

        return asdict(self)
