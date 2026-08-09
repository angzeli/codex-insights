"""SQLite access and migrations for the separate Codex Insights index."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 7

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

_MIGRATION_2 = """
ALTER TABLE source_sessions ADD COLUMN client_source TEXT;
CREATE INDEX source_sessions_client_source_idx ON source_sessions(client_source);
"""

_MIGRATION_3 = """
ALTER TABLE usage RENAME TO usage_v2;

CREATE TABLE usage (
    source_session_id INTEGER PRIMARY KEY REFERENCES source_sessions(id) ON DELETE CASCADE,
    usage_semantics TEXT NOT NULL CHECK (
        usage_semantics IN ('cumulative_total', 'summed_event_deltas', 'unavailable')
    ),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    cached_input_tokens INTEGER CHECK (cached_input_tokens IS NULL OR cached_input_tokens >= 0),
    cache_write_input_tokens INTEGER CHECK (
        cache_write_input_tokens IS NULL OR cache_write_input_tokens >= 0
    ),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    reasoning_output_tokens INTEGER CHECK (
        reasoning_output_tokens IS NULL OR reasoning_output_tokens >= 0
    ),
    total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
    token_update_count INTEGER NOT NULL DEFAULT 0 CHECK (token_update_count >= 0),
    updated_at TEXT NOT NULL
);

INSERT INTO usage(
    source_session_id, usage_semantics, input_tokens, cached_input_tokens,
    cache_write_input_tokens, output_tokens, reasoning_output_tokens, total_tokens,
    token_update_count, updated_at
)
SELECT source_session_id, usage_semantics,
       CASE WHEN usage_semantics = 'unavailable' THEN NULL ELSE input_tokens END,
       CASE WHEN usage_semantics = 'unavailable' THEN NULL ELSE cached_input_tokens END,
       CASE WHEN usage_semantics = 'unavailable' THEN NULL ELSE cache_write_input_tokens END,
       CASE WHEN usage_semantics = 'unavailable' THEN NULL ELSE output_tokens END,
       CASE WHEN usage_semantics = 'unavailable' THEN NULL ELSE reasoning_output_tokens END,
       CASE WHEN usage_semantics = 'unavailable' THEN NULL ELSE total_tokens END,
       token_update_count, updated_at
FROM usage_v2;

DROP TABLE usage_v2;
"""

_MIGRATION_4 = """
CREATE TABLE thread_relationships (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_home TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    parent_source_session_id TEXT NOT NULL,
    child_source_session_id TEXT NOT NULL,
    parent_session_id INTEGER REFERENCES source_sessions(id) ON DELETE SET NULL,
    child_session_id INTEGER REFERENCES source_sessions(id) ON DELETE SET NULL,
    source_status TEXT,
    source_db_path TEXT,
    last_seen_at TEXT NOT NULL,
    UNIQUE (
        source_type, source_home, relationship_type,
        parent_source_session_id, child_source_session_id
    )
);

CREATE INDEX thread_relationships_parent_idx ON thread_relationships(parent_session_id);
CREATE UNIQUE INDEX thread_relationships_child_idx
    ON thread_relationships(child_session_id)
    WHERE child_session_id IS NOT NULL;

CREATE TABLE token_lineage (
    child_session_id INTEGER PRIMARY KEY REFERENCES source_sessions(id) ON DELETE CASCADE,
    parent_session_id INTEGER NOT NULL REFERENCES source_sessions(id) ON DELETE CASCADE,
    deduplication_status TEXT NOT NULL CHECK (
        deduplication_status IN (
            'inherited_exact', 'inherited_prefix', 'independent',
            'ambiguous', 'unavailable', 'cycle'
        )
    ),
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'explicit', 'none')),
    evidence_type TEXT NOT NULL,
    matched_snapshot_count INTEGER NOT NULL DEFAULT 0 CHECK (matched_snapshot_count >= 0),
    parent_sequence_start INTEGER CHECK (
        parent_sequence_start IS NULL OR parent_sequence_start >= 0
    ),
    baseline_input_tokens INTEGER CHECK (
        baseline_input_tokens IS NULL OR baseline_input_tokens >= 0
    ),
    baseline_cached_input_tokens INTEGER CHECK (
        baseline_cached_input_tokens IS NULL OR baseline_cached_input_tokens >= 0
    ),
    baseline_cache_write_input_tokens INTEGER CHECK (
        baseline_cache_write_input_tokens IS NULL OR baseline_cache_write_input_tokens >= 0
    ),
    baseline_output_tokens INTEGER CHECK (
        baseline_output_tokens IS NULL OR baseline_output_tokens >= 0
    ),
    baseline_reasoning_output_tokens INTEGER CHECK (
        baseline_reasoning_output_tokens IS NULL OR baseline_reasoning_output_tokens >= 0
    ),
    baseline_total_tokens INTEGER CHECK (
        baseline_total_tokens IS NULL OR baseline_total_tokens >= 0
    ),
    incremental_input_tokens INTEGER CHECK (
        incremental_input_tokens IS NULL OR incremental_input_tokens >= 0
    ),
    incremental_cached_input_tokens INTEGER CHECK (
        incremental_cached_input_tokens IS NULL OR incremental_cached_input_tokens >= 0
    ),
    incremental_cache_write_input_tokens INTEGER CHECK (
        incremental_cache_write_input_tokens IS NULL OR incremental_cache_write_input_tokens >= 0
    ),
    incremental_output_tokens INTEGER CHECK (
        incremental_output_tokens IS NULL OR incremental_output_tokens >= 0
    ),
    incremental_reasoning_output_tokens INTEGER CHECK (
        incremental_reasoning_output_tokens IS NULL OR incremental_reasoning_output_tokens >= 0
    ),
    incremental_total_tokens INTEGER CHECK (
        incremental_total_tokens IS NULL OR incremental_total_tokens >= 0
    ),
    delta_consistency TEXT NOT NULL CHECK (
        delta_consistency IN ('exact', 'mismatch', 'unavailable')
    ),
    algorithm_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE VIEW accounted_usage AS
SELECT u.source_session_id,
       u.usage_semantics,
       CASE WHEN u.usage_semantics != 'unavailable' THEN u.input_tokens END
           AS observed_input_tokens,
       CASE WHEN u.usage_semantics != 'unavailable' THEN u.cached_input_tokens END
           AS observed_cached_input_tokens,
       CASE WHEN u.usage_semantics != 'unavailable' THEN u.cache_write_input_tokens END
           AS observed_cache_write_input_tokens,
       CASE WHEN u.usage_semantics != 'unavailable' THEN u.output_tokens END
           AS observed_output_tokens,
       CASE WHEN u.usage_semantics != 'unavailable' THEN u.reasoning_output_tokens END
           AS observed_reasoning_output_tokens,
       CASE WHEN u.usage_semantics != 'unavailable' THEN u.total_tokens END
           AS observed_total_tokens,
       CASE
           WHEN tl.deduplication_status IS NOT NULL THEN tl.deduplication_status
           WHEN tr.child_session_id IS NOT NULL THEN 'unavailable'
           ELSE 'root'
       END AS accounting_status,
       CASE WHEN u.usage_semantics = 'unavailable' THEN NULL
            WHEN tl.deduplication_status IN ('inherited_exact', 'inherited_prefix')
            THEN tl.incremental_input_tokens ELSE u.input_tokens END AS aggregate_input_tokens,
       CASE WHEN u.usage_semantics = 'unavailable' THEN NULL
            WHEN tl.deduplication_status IN ('inherited_exact', 'inherited_prefix')
            THEN tl.incremental_cached_input_tokens ELSE u.cached_input_tokens
       END AS aggregate_cached_input_tokens,
       CASE WHEN u.usage_semantics = 'unavailable' THEN NULL
            WHEN tl.deduplication_status IN ('inherited_exact', 'inherited_prefix')
            THEN tl.incremental_cache_write_input_tokens ELSE u.cache_write_input_tokens
       END AS aggregate_cache_write_input_tokens,
       CASE WHEN u.usage_semantics = 'unavailable' THEN NULL
            WHEN tl.deduplication_status IN ('inherited_exact', 'inherited_prefix')
            THEN tl.incremental_output_tokens ELSE u.output_tokens END AS aggregate_output_tokens,
       CASE WHEN u.usage_semantics = 'unavailable' THEN NULL
            WHEN tl.deduplication_status IN ('inherited_exact', 'inherited_prefix')
            THEN tl.incremental_reasoning_output_tokens ELSE u.reasoning_output_tokens
       END AS aggregate_reasoning_output_tokens,
       CASE WHEN u.usage_semantics = 'unavailable' THEN NULL
            WHEN tl.deduplication_status IN ('inherited_exact', 'inherited_prefix')
            THEN tl.incremental_total_tokens ELSE u.total_tokens END AS aggregate_total_tokens,
       tl.baseline_total_tokens AS inherited_baseline_total_tokens
FROM usage AS u
LEFT JOIN thread_relationships AS tr ON tr.child_session_id = u.source_session_id
LEFT JOIN token_lineage AS tl ON tl.child_session_id = u.source_session_id;
"""

_MIGRATION_5 = """
CREATE TABLE event_observations (
    id INTEGER PRIMARY KEY,
    observed_session_id INTEGER NOT NULL REFERENCES source_sessions(id) ON DELETE CASCADE,
    source_ordinal INTEGER NOT NULL CHECK (source_ordinal >= 0),
    family_ordinal INTEGER NOT NULL CHECK (family_ordinal >= 0),
    event_family TEXT NOT NULL CHECK (
        event_family IN (
            'user_message', 'assistant_message', 'inter_agent_message',
            'tool_call', 'tool_output', 'shell_command', 'validation_command',
            'git_command', 'patch_edit', 'patch_result', 'task_lifecycle', 'error'
        )
    ),
    source_record_type TEXT NOT NULL,
    source_payload_type TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    stable_id_digest TEXT,
    occurred_at TEXT,
    approximate_content_length INTEGER CHECK (
        approximate_content_length IS NULL OR approximate_content_length >= 0
    ),
    provenance_status TEXT NOT NULL CHECK (
        provenance_status IN (
            'origin', 'inherited_exact', 'inherited_prefix',
            'observed_duplicate', 'ambiguous', 'unknown'
        )
    ),
    origin_session_id INTEGER REFERENCES source_sessions(id) ON DELETE SET NULL,
    origin_event_id INTEGER REFERENCES event_observations(id) ON DELETE SET NULL,
    parent_session_id INTEGER REFERENCES source_sessions(id) ON DELETE SET NULL,
    evidence_type TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'none')),
    fingerprint_version TEXT NOT NULL,
    provenance_algorithm_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (observed_session_id, source_ordinal, fingerprint_version)
);

CREATE INDEX event_observations_session_idx
    ON event_observations(observed_session_id, source_ordinal);
CREATE INDEX event_observations_origin_idx
    ON event_observations(origin_event_id);
CREATE INDEX event_observations_family_idx
    ON event_observations(event_family, provenance_status);

CREATE TABLE event_replay_summary (
    relationship_id INTEGER NOT NULL REFERENCES thread_relationships(id) ON DELETE CASCADE,
    event_family TEXT NOT NULL,
    observed_child_events INTEGER NOT NULL CHECK (observed_child_events >= 0),
    originated_events INTEGER NOT NULL CHECK (originated_events >= 0),
    inherited_events INTEGER NOT NULL CHECK (inherited_events >= 0),
    ambiguous_events INTEGER NOT NULL CHECK (ambiguous_events >= 0),
    unknown_events INTEGER NOT NULL CHECK (unknown_events >= 0),
    provenance_status TEXT NOT NULL CHECK (
        provenance_status IN (
            'origin', 'inherited_exact', 'inherited_prefix',
            'observed_duplicate', 'ambiguous', 'unknown'
        )
    ),
    evidence_type TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (relationship_id, event_family)
);
"""

_MIGRATION_6 = """
CREATE TABLE prompts (
    id INTEGER PRIMARY KEY,
    prompt_id TEXT NOT NULL UNIQUE,
    origin_session_id INTEGER NOT NULL REFERENCES source_sessions(id) ON DELETE CASCADE,
    origin_event_id INTEGER REFERENCES event_observations(id) ON DELETE SET NULL,
    source_ordinal INTEGER NOT NULL CHECK (source_ordinal >= 0),
    prompt_ordinal INTEGER NOT NULL CHECK (prompt_ordinal >= 0),
    occurred_at TEXT,
    text TEXT NOT NULL,
    redaction_status TEXT NOT NULL CHECK (
        redaction_status IN ('none', 'redacted', 'truncated', 'redacted_and_truncated')
    ),
    redaction_count INTEGER NOT NULL DEFAULT 0 CHECK (redaction_count >= 0),
    original_character_count INTEGER NOT NULL CHECK (original_character_count >= 0),
    stored_character_count INTEGER NOT NULL CHECK (stored_character_count >= 0),
    provenance_status TEXT NOT NULL CHECK (
        provenance_status IN (
            'origin', 'inherited_exact', 'inherited_prefix',
            'observed_duplicate', 'ambiguous', 'unknown'
        )
    ),
    provenance_confidence TEXT NOT NULL CHECK (provenance_confidence IN ('high', 'none')),
    user_authorship_evidence TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    content_schema_version TEXT NOT NULL,
    first_indexed_at TEXT NOT NULL,
    last_indexed_at TEXT NOT NULL,
    UNIQUE (origin_session_id, source_ordinal, content_schema_version)
);

CREATE INDEX prompts_origin_session_idx ON prompts(origin_session_id, prompt_ordinal);
CREATE INDEX prompts_occurred_at_idx ON prompts(occurred_at);

CREATE TABLE prompt_observations (
    prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    event_observation_id INTEGER NOT NULL REFERENCES event_observations(id) ON DELETE CASCADE,
    observed_session_id INTEGER NOT NULL REFERENCES source_sessions(id) ON DELETE CASCADE,
    source_ordinal INTEGER NOT NULL CHECK (source_ordinal >= 0),
    provenance_status TEXT NOT NULL CHECK (
        provenance_status IN ('origin', 'inherited_exact', 'inherited_prefix')
    ),
    first_observed_at TEXT NOT NULL,
    PRIMARY KEY (prompt_id, event_observation_id)
);

CREATE INDEX prompt_observations_session_idx
    ON prompt_observations(observed_session_id, prompt_id);

CREATE VIRTUAL TABLE prompts_fts USING fts5(
    text,
    content='prompts',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER prompts_fts_after_insert AFTER INSERT ON prompts BEGIN
    INSERT INTO prompts_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER prompts_fts_after_delete AFTER DELETE ON prompts BEGIN
    INSERT INTO prompts_fts(prompts_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER prompts_fts_after_update AFTER UPDATE OF text ON prompts BEGIN
    INSERT INTO prompts_fts(prompts_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO prompts_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

_MIGRATION_7 = """
CREATE TABLE tool_activity (
    id INTEGER PRIMARY KEY,
    event_observation_id INTEGER NOT NULL
        REFERENCES event_observations(id) ON DELETE CASCADE,
    observed_session_id INTEGER NOT NULL REFERENCES source_sessions(id) ON DELETE CASCADE,
    origin_session_id INTEGER REFERENCES source_sessions(id) ON DELETE SET NULL,
    source_ordinal INTEGER NOT NULL CHECK (source_ordinal >= 0),
    operation_ordinal INTEGER NOT NULL CHECK (operation_ordinal >= 0),
    occurred_at TEXT,
    tool_family TEXT NOT NULL CHECK (
        tool_family IN (
            'shell', 'patch', 'collaboration', 'user_interaction',
            'file', 'network', 'other', 'unknown'
        )
    ),
    tool_name TEXT NOT NULL,
    command_category TEXT NOT NULL CHECK (
        command_category IN (
            'git_inspection', 'git_mutation', 'testing', 'linting',
            'type_checking', 'build_packaging', 'filesystem_inspection',
            'text_search', 'python_execution', 'dependency_management',
            'editing_patching', 'scientific_computation',
            'process_status_monitoring', 'wait_poll', 'user_interaction',
            'other', 'unknown'
        )
    ),
    command_text TEXT,
    command_fingerprint TEXT,
    executable TEXT,
    test_scope TEXT NOT NULL CHECK (
        test_scope IN ('full_suite', 'file', 'subset', 'unknown', 'not_applicable')
    ),
    call_id_digest TEXT,
    output_event_observation_id INTEGER
        REFERENCES event_observations(id) ON DELETE SET NULL,
    exit_code INTEGER,
    duration_seconds REAL CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    result_status TEXT NOT NULL CHECK (result_status IN ('success', 'failure', 'unknown')),
    provenance_status TEXT NOT NULL CHECK (
        provenance_status IN (
            'origin', 'inherited_exact', 'inherited_prefix',
            'observed_duplicate', 'ambiguous', 'unknown'
        )
    ),
    redacted INTEGER NOT NULL DEFAULT 0 CHECK (redacted IN (0, 1)),
    truncated INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0, 1)),
    extraction_version TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (
        observed_session_id, source_ordinal, operation_ordinal, extraction_version
    )
);

CREATE INDEX tool_activity_origin_idx
    ON tool_activity(origin_session_id, provenance_status, occurred_at);
CREATE INDEX tool_activity_category_idx
    ON tool_activity(command_category, provenance_status);
CREATE INDEX tool_activity_command_idx
    ON tool_activity(command_fingerprint, provenance_status);
CREATE INDEX tool_activity_call_idx
    ON tool_activity(observed_session_id, call_id_digest);
"""

_MIGRATIONS = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
    5: _MIGRATION_5,
    6: _MIGRATION_6,
    7: _MIGRATION_7,
}


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
    with closing(open_index(resolved, codex_home=codex_home)) as connection:
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
