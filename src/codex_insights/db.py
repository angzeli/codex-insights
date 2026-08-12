"""SQLite access and migrations for the separate Codex Insights index."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from codex_insights.path_safety import UnsafeDestinationError, validate_write_target

SCHEMA_VERSION = 21

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

_MIGRATION_8 = """
CREATE TABLE repositories (
    id INTEGER PRIMARY KEY,
    identity_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    identity_method TEXT NOT NULL CHECK (
        identity_method IN ('normalized_remote', 'common_git_dir', 'repository_path')
    ),
    normalized_remote TEXT,
    canonical_root TEXT,
    common_git_dir TEXT,
    path_exists INTEGER NOT NULL DEFAULT 0 CHECK (path_exists IN (0, 1)),
    identity_version TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

ALTER TABLE source_sessions ADD COLUMN repository_id INTEGER REFERENCES repositories(id);
CREATE INDEX source_sessions_repository_id_idx ON source_sessions(repository_id);
"""

_MIGRATION_9 = """
ALTER TABLE tool_activity ADD COLUMN result_commit_hash TEXT;
ALTER TABLE tool_activity ADD COLUMN result_commit_abbrev TEXT;

CREATE TABLE git_commits (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    commit_hash TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    parent_count INTEGER NOT NULL CHECK (parent_count >= 0),
    first_discovered_at TEXT NOT NULL,
    last_discovered_at TEXT NOT NULL,
    UNIQUE (repository_id, commit_hash)
);
CREATE INDEX git_commits_time_idx ON git_commits(repository_id, committed_at);

CREATE TABLE session_commit_associations (
    session_id INTEGER NOT NULL REFERENCES source_sessions(id) ON DELETE CASCADE,
    commit_id INTEGER NOT NULL REFERENCES git_commits(id) ON DELETE CASCADE,
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    evidence_type TEXT NOT NULL,
    evidence_origin_session_id INTEGER NOT NULL REFERENCES source_sessions(id),
    evidence_explanation TEXT NOT NULL,
    ambiguous INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous IN (0, 1)),
    algorithm_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, commit_id)
);
CREATE INDEX session_commit_confidence_idx
    ON session_commit_associations(confidence, session_id);
"""

_MIGRATION_10 = """
CREATE TABLE session_outcomes (
    session_id INTEGER PRIMARY KEY REFERENCES source_sessions(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL CHECK (
        outcome IN (
            'success', 'success_with_warnings', 'partial', 'failed',
            'abandoned', 'no_change', 'unknown'
        )
    ),
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    evidence_json TEXT NOT NULL,
    evidence_count INTEGER NOT NULL CHECK (evidence_count >= 0),
    classifier_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX session_outcomes_result_idx ON session_outcomes(outcome, confidence);
"""

_MIGRATION_11 = """
CREATE TABLE session_tasks (
    session_id INTEGER PRIMARY KEY REFERENCES source_sessions(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (
        action IN (
            'implementation', 'bug_fix', 'refactor', 'code_review',
            'repository_assessment', 'testing', 'documentation', 'ui_work',
            'scientific_status_or_diagnosis', 'research_or_exploration',
            'git_or_release', 'planning', 'question_answering', 'other', 'unknown'
        )
    ),
    domain TEXT NOT NULL CHECK (
        domain IN (
            'scientific_computing', 'software_engineering', 'developer_tooling',
            'documentation', 'data_analysis', 'ui', 'git_release', 'general', 'unknown'
        )
    ),
    facets_json TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    evidence_json TEXT NOT NULL,
    taxonomy_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX session_tasks_action_idx ON session_tasks(action, confidence);
CREATE INDEX session_tasks_domain_idx ON session_tasks(domain, confidence);
"""

_MIGRATION_12 = """
CREATE TABLE prompt_features (
    prompt_id INTEGER PRIMARY KEY REFERENCES prompts(id) ON DELETE CASCADE,
    character_length INTEGER NOT NULL CHECK (character_length >= 0),
    stored_character_length INTEGER NOT NULL CHECK (stored_character_length >= 0),
    line_count INTEGER NOT NULL CHECK (line_count >= 0),
    structured_heading_count INTEGER NOT NULL CHECK (structured_heading_count >= 0),
    has_acceptance_criteria INTEGER NOT NULL CHECK (has_acceptance_criteria IN (0, 1)),
    requests_validation INTEGER NOT NULL CHECK (requests_validation IN (0, 1)),
    path_reference_count INTEGER NOT NULL CHECK (path_reference_count >= 0),
    requests_commit INTEGER NOT NULL CHECK (requests_commit IN (0, 1)),
    requests_multiple_commits INTEGER NOT NULL CHECK (requests_multiple_commits IN (0, 1)),
    has_explicit_non_goals INTEGER NOT NULL CHECK (has_explicit_non_goals IN (0, 1)),
    has_read_only_constraint INTEGER NOT NULL CHECK (has_read_only_constraint IN (0, 1)),
    approximate_requirement_count INTEGER NOT NULL
        CHECK (approximate_requirement_count >= 0),
    source_truncated INTEGER NOT NULL CHECK (source_truncated IN (0, 1)),
    feature_version TEXT NOT NULL,
    requirement_heuristic_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX prompt_features_validation_idx ON prompt_features(requests_validation);
CREATE INDEX prompt_features_commit_idx ON prompt_features(requests_commit);
"""

_MIGRATION_13 = """
ALTER TABLE ingestion_state ADD COLUMN file_identity TEXT;
ALTER TABLE ingestion_state ADD COLUMN source_schema_fingerprint TEXT;
ALTER TABLE ingestion_state ADD COLUMN last_successful_parse_at TEXT;
ALTER TABLE ingestion_state ADD COLUMN last_successful_size_bytes INTEGER;
ALTER TABLE ingestion_state ADD COLUMN last_successful_mtime_ns INTEGER;
ALTER TABLE ingestion_state ADD COLUMN last_successful_file_identity TEXT;
ALTER TABLE ingestion_state ADD COLUMN last_successful_byte_offset INTEGER;
ALTER TABLE ingestion_state ADD COLUMN stale INTEGER NOT NULL DEFAULT 0
    CHECK (stale IN (0, 1));
ALTER TABLE ingestion_state ADD COLUMN error_at TEXT;

UPDATE ingestion_state
SET last_successful_parse_at = indexed_at,
    last_successful_size_bytes = size_bytes,
    last_successful_mtime_ns = mtime_ns,
    last_successful_byte_offset = last_parsed_byte_offset
WHERE status LIKE 'indexed%';

CREATE TABLE source_compatibility (
    source_type TEXT NOT NULL,
    source_home TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    selected_state_db_path TEXT,
    selection_reason TEXT NOT NULL,
    alternatives_json TEXT NOT NULL,
    source_schema_fingerprint TEXT,
    source_schema_hints_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_type, source_home)
);

CREATE TABLE session_compatibility (
    source_session_id INTEGER PRIMARY KEY
        REFERENCES source_sessions(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_db_path TEXT,
    source_path TEXT,
    parser_version TEXT NOT NULL,
    source_schema_fingerprint TEXT,
    source_schema_hints_json TEXT NOT NULL,
    capability_set_json TEXT NOT NULL,
    event_fingerprint_version TEXT NOT NULL,
    token_lineage_algorithm_version TEXT NOT NULL,
    provenance_algorithm_version TEXT NOT NULL,
    outcome_classifier_version TEXT NOT NULL,
    task_classifier_version TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    last_successful_parse_at TEXT,
    parse_status TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1))
);
CREATE INDEX session_compatibility_status_idx
    ON session_compatibility(parse_status, stale);

CREATE TABLE session_capabilities (
    source_session_id INTEGER NOT NULL
        REFERENCES source_sessions(id) ON DELETE CASCADE,
    capability TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('available', 'degraded', 'not_observed', 'unknown')
    ),
    evidence_count INTEGER NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    evidence_type TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_session_id, capability)
);
CREATE INDEX session_capabilities_coverage_idx
    ON session_capabilities(capability, status);

CREATE TABLE unknown_source_records (
    source_session_id INTEGER NOT NULL
        REFERENCES source_sessions(id) ON DELETE CASCADE,
    unknown_kind TEXT NOT NULL,
    unknown_name TEXT NOT NULL,
    record_count INTEGER NOT NULL CHECK (record_count >= 0),
    parser_version TEXT NOT NULL,
    source_schema_fingerprint TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_session_id, unknown_kind, unknown_name, parser_version)
);
CREATE INDEX unknown_source_records_kind_idx
    ON unknown_source_records(unknown_kind, unknown_name);

CREATE TABLE compatibility_warnings (
    id INTEGER PRIMARY KEY,
    index_run_id INTEGER REFERENCES index_runs(id) ON DELETE CASCADE,
    source_session_id INTEGER REFERENCES source_sessions(id) ON DELETE CASCADE,
    warning_code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    warning_count INTEGER NOT NULL DEFAULT 1 CHECK (warning_count >= 1),
    message TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX compatibility_warnings_run_idx
    ON compatibility_warnings(index_run_id, warning_code);

CREATE TABLE coverage_snapshots (
    index_run_id INTEGER NOT NULL REFERENCES index_runs(id) ON DELETE CASCADE,
    capability TEXT NOT NULL,
    available_count INTEGER NOT NULL CHECK (available_count >= 0),
    degraded_count INTEGER NOT NULL CHECK (degraded_count >= 0),
    not_observed_count INTEGER NOT NULL CHECK (not_observed_count >= 0),
    unknown_count INTEGER NOT NULL CHECK (unknown_count >= 0),
    total_count INTEGER NOT NULL CHECK (total_count >= 0),
    coverage_ratio REAL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (index_run_id, capability)
);

INSERT INTO session_compatibility(
    source_session_id, source_type, source_db_path, source_path, parser_version,
    source_schema_fingerprint, source_schema_hints_json, capability_set_json,
    event_fingerprint_version, token_lineage_algorithm_version,
    provenance_algorithm_version, outcome_classifier_version,
    task_classifier_version, indexed_at, last_successful_parse_at,
    parse_status, stale
)
SELECT sessions.id, sessions.source_type, sessions.source_db_path, sessions.source_path,
       COALESCE(state.parser_version, 'legacy-unknown'), NULL, '[]', '[]',
       'event-fingerprint-v1', 'token-lineage-v1', 'event-provenance-v1',
       'outcome-classifier-v1', 'task-taxonomy-v1', sessions.last_ingested_at,
       CASE WHEN state.status LIKE 'indexed%' THEN state.indexed_at END,
       COALESCE(state.status, 'legacy_migrated'),
       CASE WHEN state.status IS NOT NULL AND state.status NOT LIKE 'indexed%' THEN 1 ELSE 0 END
FROM source_sessions AS sessions
LEFT JOIN ingestion_state AS state
  ON state.source_home = sessions.source_home
 AND state.source_session_id = sessions.source_session_id;
"""

_MIGRATION_14 = """
ALTER TABLE tool_activity ADD COLUMN command_operation TEXT;

CREATE TABLE content_retention_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    store_prompts INTEGER NOT NULL CHECK (store_prompts IN (0, 1)),
    store_command_text INTEGER NOT NULL CHECK (store_command_text IN (0, 1)),
    indexed_at TEXT NOT NULL
);

INSERT INTO content_retention_state(
    singleton, store_prompts, store_command_text, indexed_at
) VALUES (1, 1, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
"""

_MIGRATION_15 = """
CREATE INDEX tool_activity_event_idx
    ON tool_activity(event_observation_id);
CREATE INDEX tool_activity_output_event_idx
    ON tool_activity(output_event_observation_id);
CREATE INDEX prompts_origin_event_idx
    ON prompts(origin_event_id);
CREATE INDEX prompt_observations_event_idx
    ON prompt_observations(event_observation_id);
"""

_MIGRATION_16 = """
CREATE TABLE token_events (
    source_session_id INTEGER NOT NULL REFERENCES source_sessions(id) ON DELETE CASCADE,
    event_ordinal INTEGER NOT NULL CHECK (event_ordinal >= 0),
    source_ordinal INTEGER NOT NULL CHECK (source_ordinal >= 0),
    occurred_at TEXT,
    event_kind TEXT NOT NULL CHECK (
        event_kind IN ('cumulative_snapshot', 'event_delta')
    ),
    cumulative_input_tokens INTEGER CHECK (
        cumulative_input_tokens IS NULL OR cumulative_input_tokens >= 0
    ),
    cumulative_cached_input_tokens INTEGER CHECK (
        cumulative_cached_input_tokens IS NULL OR cumulative_cached_input_tokens >= 0
    ),
    cumulative_cache_write_input_tokens INTEGER CHECK (
        cumulative_cache_write_input_tokens IS NULL OR cumulative_cache_write_input_tokens >= 0
    ),
    cumulative_output_tokens INTEGER CHECK (
        cumulative_output_tokens IS NULL OR cumulative_output_tokens >= 0
    ),
    cumulative_reasoning_output_tokens INTEGER CHECK (
        cumulative_reasoning_output_tokens IS NULL OR cumulative_reasoning_output_tokens >= 0
    ),
    cumulative_total_tokens INTEGER CHECK (
        cumulative_total_tokens IS NULL OR cumulative_total_tokens >= 0
    ),
    delta_input_tokens INTEGER CHECK (
        delta_input_tokens IS NULL OR delta_input_tokens >= 0
    ),
    delta_cached_input_tokens INTEGER CHECK (
        delta_cached_input_tokens IS NULL OR delta_cached_input_tokens >= 0
    ),
    delta_cache_write_input_tokens INTEGER CHECK (
        delta_cache_write_input_tokens IS NULL OR delta_cache_write_input_tokens >= 0
    ),
    delta_output_tokens INTEGER CHECK (
        delta_output_tokens IS NULL OR delta_output_tokens >= 0
    ),
    delta_reasoning_output_tokens INTEGER CHECK (
        delta_reasoning_output_tokens IS NULL OR delta_reasoning_output_tokens >= 0
    ),
    delta_total_tokens INTEGER CHECK (
        delta_total_tokens IS NULL OR delta_total_tokens >= 0
    ),
    PRIMARY KEY (source_session_id, event_ordinal)
);

CREATE INDEX token_events_occurred_at_idx ON token_events(occurred_at);
CREATE INDEX token_events_source_order_idx
    ON token_events(source_session_id, source_ordinal);
"""

_MIGRATION_17 = """
ALTER TABLE source_sessions ADD COLUMN client_kind TEXT NOT NULL DEFAULT 'unknown'
    CHECK (client_kind IN ('cli', 'editor', 'subagent', 'other', 'unknown'));
ALTER TABLE source_sessions ADD COLUMN subagent_source_kind TEXT
    CHECK (subagent_source_kind IS NULL OR subagent_source_kind IN (
        'thread_spawn', 'guardian', 'other'
    ));
ALTER TABLE source_sessions ADD COLUMN source_parent_session_id TEXT;

UPDATE source_sessions
SET client_kind = CASE
    WHEN client_source IS NULL OR TRIM(client_source) = '' THEN 'unknown'
    WHEN SUBSTR(LTRIM(client_source), 1, 1) IN ('{', '[') THEN 'unknown'
    WHEN LOWER(REPLACE(REPLACE(client_source, '-', '_'), ' ', '_'))
         IN ('cli', 'terminal', 'command_line') THEN 'cli'
    WHEN LOWER(REPLACE(REPLACE(client_source, '-', '_'), ' ', '_'))
         IN ('editor', 'vscode', 'visual_studio_code', 'cursor', 'jetbrains', 'pycharm')
         THEN 'editor'
    ELSE 'other'
END;

UPDATE source_sessions
SET client_source = NULL
WHERE SUBSTR(LTRIM(client_source), 1, 1) IN ('{', '[');

CREATE INDEX source_sessions_client_kind_idx ON source_sessions(client_kind);
CREATE INDEX source_sessions_source_parent_idx ON source_sessions(source_parent_session_id);
"""

_MIGRATION_18 = """
ALTER TABLE session_outcomes ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'unknown'
    CHECK (lifecycle_status IN ('turn_completed', 'aborted', 'unknown'));
ALTER TABLE session_outcomes ADD COLUMN strongly_evidenced INTEGER NOT NULL DEFAULT 0
    CHECK (strongly_evidenced IN (0, 1));

UPDATE session_outcomes
SET strongly_evidenced = CASE
    WHEN outcome <> 'unknown' AND confidence IN ('high', 'medium') THEN 1
    ELSE 0
END;

CREATE INDEX session_outcomes_strong_idx
    ON session_outcomes(strongly_evidenced, outcome);
"""

_MIGRATION_19 = """
ALTER TABLE unknown_source_records ADD COLUMN diagnostic_category TEXT NOT NULL
    DEFAULT 'unclassified' CHECK (diagnostic_category IN (
        'field_passthrough', 'recognized_ignored', 'semantic_gap',
        'tool_result_gap', 'lifecycle_gap', 'unclassified'
    ));
ALTER TABLE unknown_source_records ADD COLUMN capability_impact TEXT NOT NULL
    DEFAULT 'source_compatibility';
ALTER TABLE unknown_source_records ADD COLUMN first_index_run_id INTEGER
    REFERENCES index_runs(id) ON DELETE SET NULL;
ALTER TABLE unknown_source_records ADD COLUMN last_index_run_id INTEGER
    REFERENCES index_runs(id) ON DELETE SET NULL;

UPDATE unknown_source_records
SET diagnostic_category = CASE
    WHEN unknown_kind IN ('payload_field', 'top_level_field') THEN 'field_passthrough'
    WHEN unknown_kind = 'tool_encoding' THEN 'tool_result_gap'
    WHEN unknown_kind IN ('record_type', 'payload_type') THEN 'semantic_gap'
    ELSE 'unclassified'
END,
capability_impact = CASE
    WHEN unknown_kind = 'tool_encoding' THEN 'tool_activity'
    WHEN unknown_kind IN ('record_type', 'payload_type') THEN 'event_normalization'
    ELSE 'source_compatibility'
END;

CREATE INDEX unknown_source_records_diagnostic_idx
    ON unknown_source_records(diagnostic_category, unknown_name);
CREATE INDEX unknown_source_records_first_run_idx
    ON unknown_source_records(first_index_run_id);
"""

_MIGRATION_20 = """
CREATE TABLE git_candidate_summaries (
    session_id INTEGER PRIMARY KEY REFERENCES source_sessions(id) ON DELETE CASCADE,
    timing_candidates_considered INTEGER NOT NULL CHECK (timing_candidates_considered >= 0),
    timing_candidates_persisted INTEGER NOT NULL CHECK (timing_candidates_persisted >= 0),
    timing_candidates_omitted INTEGER NOT NULL CHECK (timing_candidates_omitted >= 0),
    algorithm_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE git_reconciliation_state (
    repository_id INTEGER PRIMARY KEY REFERENCES repositories(id) ON DELETE CASCADE,
    algorithm_version TEXT NOT NULL,
    ref_state_fingerprint TEXT NOT NULL,
    reconciled_at TEXT NOT NULL
);
"""

_MIGRATION_21 = """
ALTER TABLE tool_activity ADD COLUMN effective_occurred_at TEXT;

UPDATE tool_activity
SET effective_occurred_at = COALESCE(
    occurred_at,
    (SELECT started_at FROM source_sessions
     WHERE source_sessions.id = tool_activity.observed_session_id)
);

CREATE INDEX source_sessions_started_at_idx
    ON source_sessions(started_at, id);
CREATE INDEX tool_activity_effective_time_idx
    ON tool_activity(effective_occurred_at, observed_session_id);
CREATE INDEX tool_activity_category_time_idx
    ON tool_activity(command_category, effective_occurred_at);
CREATE INDEX git_commits_global_time_idx
    ON git_commits(committed_at, repository_id);
"""

_MIGRATIONS = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
    5: _MIGRATION_5,
    6: _MIGRATION_6,
    7: _MIGRATION_7,
    8: _MIGRATION_8,
    9: _MIGRATION_9,
    10: _MIGRATION_10,
    11: _MIGRATION_11,
    12: _MIGRATION_12,
    13: _MIGRATION_13,
    14: _MIGRATION_14,
    15: _MIGRATION_15,
    16: _MIGRATION_16,
    17: _MIGRATION_17,
    18: _MIGRATION_18,
    19: _MIGRATION_19,
    20: _MIGRATION_20,
    21: _MIGRATION_21,
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
    try:
        validate_write_target(
            index,
            codex_home=codex_home,
            operation="Analyzer database",
        )
    except UnsafeDestinationError as exc:
        raise UnsafeDatabasePathError(str(exc)) from exc


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
