"""SQLite access and migrations for the separate Codex Insights index."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1

_MIGRATION_1 = """
CREATE TABLE source_sessions (
    id INTEGER PRIMARY KEY,
    source_session_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_home TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT,
    apparent_ended_at TEXT,
    source_timezone_offset_minutes INTEGER,
    cwd TEXT,
    repository_root TEXT,
    repository_name TEXT,
    git_branch TEXT,
    git_sha TEXT,
    git_origin_url TEXT,
    model TEXT,
    model_provider TEXT,
    codex_version TEXT,
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    rollout_path TEXT,
    source_db_path TEXT,
    source_path TEXT,
    first_ingested_at TEXT NOT NULL,
    last_ingested_at TEXT NOT NULL,
    UNIQUE (source_type, source_home, source_session_id)
);

CREATE INDEX source_sessions_updated_at_idx ON source_sessions(updated_at);
CREATE INDEX source_sessions_repository_idx ON source_sessions(repository_root);
CREATE INDEX source_sessions_rollout_path_idx ON source_sessions(rollout_path);

CREATE TABLE usage (
    source_session_id INTEGER PRIMARY KEY REFERENCES source_sessions(id) ON DELETE CASCADE,
    usage_semantics TEXT NOT NULL CHECK (
        usage_semantics IN ('cumulative_total', 'summed_event_deltas', 'unavailable')
    ),
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    token_update_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE event_summary (
    source_session_id INTEGER NOT NULL REFERENCES source_sessions(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    event_count INTEGER NOT NULL CHECK (event_count >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_session_id, category)
);

CREATE TABLE ingestion_state (
    source_home TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_session_id TEXT,
    source_kind TEXT NOT NULL,
    size_bytes INTEGER,
    mtime_ns INTEGER,
    last_parsed_byte_offset INTEGER,
    parser_version TEXT NOT NULL,
    source_schema_version TEXT,
    status TEXT NOT NULL,
    error TEXT,
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (source_home, source_path)
);

CREATE INDEX ingestion_state_session_idx
    ON ingestion_state(source_home, source_session_id);

CREATE TABLE index_runs (
    id INTEGER PRIMARY KEY,
    source_home TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0
);
"""

_MIGRATIONS = {1: _MIGRATION_1}


class UnsafeDatabasePathError(ValueError):
    """Raised when an analyzer database would be placed inside Codex home."""


@dataclass(frozen=True, slots=True)
class SourceCoverage:
    """Indexed session coverage for one normalized source type."""

    source_type: str
    session_count: int
    source_home_count: int


@dataclass(frozen=True, slots=True)
class DatabaseInfo:
    """Small, display-safe summary of the derived analytics database."""

    path: Path
    schema_version: int
    indexed_session_count: int
    latest_indexing_time: str | None
    source_coverage: tuple[SourceCoverage, ...]


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def ensure_index_outside_codex_home(index_path: Path, codex_home: Path) -> None:
    """Reject analyzer database paths that overlap Codex-owned storage."""

    index = _resolved(index_path)
    source = _resolved(codex_home)
    if index == source or source in index.parents:
        raise UnsafeDatabasePathError(f"Analyzer database must be outside Codex home: {source}")


def connect_index(index_path: Path, *, codex_home: Path) -> sqlite3.Connection:
    """Open the writable Codex Insights index after enforcing path separation."""

    ensure_index_outside_codex_home(index_path, codex_home)
    destination = _resolved(index_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(destination)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def open_index(index_path: Path, *, codex_home: Path) -> sqlite3.Connection:
    """Open the analyzer database and apply only its own forward migrations."""

    connection = connect_index(index_path, codex_home=codex_home)
    try:
        _migrate(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    current = int(
        connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    )
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema {current} is newer than supported schema {SCHEMA_VERSION}"
        )

    for version in range(current + 1, SCHEMA_VERSION + 1):
        try:
            connection.executescript(f"BEGIN IMMEDIATE;\n{_MIGRATIONS[version]}")
            connection.execute(
                """
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (version,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def inspect_index(
    index_path: Path,
    *,
    codex_home: Path,
) -> DatabaseInfo:
    """Return aggregate database metadata without exposing indexed source values."""

    resolved = _resolved(index_path)
    with open_index(resolved, codex_home=codex_home) as connection:
        schema_version = int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        )
        session_count = int(
            connection.execute("SELECT COUNT(*) FROM source_sessions").fetchone()[0]
        )
        latest_row = connection.execute(
            "SELECT MAX(completed_at) FROM index_runs WHERE status = 'completed'"
        ).fetchone()
        coverage = tuple(
            SourceCoverage(
                source_type=str(row["source_type"]),
                session_count=int(row["session_count"]),
                source_home_count=int(row["source_home_count"]),
            )
            for row in connection.execute(
                """
                SELECT source_type, COUNT(*) AS session_count,
                       COUNT(DISTINCT source_home) AS source_home_count
                FROM source_sessions
                GROUP BY source_type
                ORDER BY source_type
                """
            )
        )

    return DatabaseInfo(
        path=resolved,
        schema_version=schema_version,
        indexed_session_count=session_count,
        latest_indexing_time=str(latest_row[0]) if latest_row and latest_row[0] else None,
        source_coverage=coverage,
    )


def open_source_sqlite_readonly(source_path: Path) -> sqlite3.Connection:
    """Open a Codex-owned SQLite file in explicit read-only and query-only modes."""

    source = _resolved(source_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    connection = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection
