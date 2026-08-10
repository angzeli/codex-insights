from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_insights import db as db_module
from codex_insights.cli import app
from codex_insights.config import default_index_path
from codex_insights.db import SCHEMA_VERSION, UnsafeDatabasePathError, inspect_index, open_index

runner = CliRunner()


def test_default_index_path_is_platform_aware(tmp_path: Path) -> None:
    assert default_index_path(home=tmp_path, platform_name="darwin") == (
        tmp_path / "Library" / "Application Support" / "Codex Insights" / "index.sqlite3"
    )
    assert (
        default_index_path(
            home=tmp_path,
            environ={"XDG_DATA_HOME": str(tmp_path / "xdg")},
            platform_name="linux",
        )
        == tmp_path / "xdg" / "codex-insights" / "index.sqlite3"
    )
    assert (
        default_index_path(
            home=tmp_path,
            environ={"LOCALAPPDATA": str(tmp_path / "local")},
            platform_name="win32",
        )
        == tmp_path / "local" / "Codex Insights" / "index.sqlite3"
    )


def test_index_schema_is_versioned_and_normalized(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    database = tmp_path / "data" / "index.sqlite3"

    with open_index(database, codex_home=codex_home) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        session_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(source_sessions)")
        }
        usage_columns = {
            str(row[1]): bool(row[3]) for row in connection.execute("PRAGMA table_info(usage)")
        }
        views = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'view'")
        }

    assert version == SCHEMA_VERSION
    assert "client_source" in session_columns
    assert usage_columns["total_tokens"] is False
    assert {
        "source_sessions",
        "usage",
        "event_summary",
        "ingestion_state",
        "index_runs",
        "schema_migrations",
        "thread_relationships",
        "token_lineage",
        "event_observations",
        "event_replay_summary",
        "prompts",
        "prompt_observations",
        "prompts_fts",
        "source_compatibility",
        "session_compatibility",
        "session_capabilities",
        "unknown_source_records",
        "compatibility_warnings",
        "coverage_snapshots",
    } <= tables
    assert "accounted_usage" in views


def test_index_database_cannot_be_created_under_codex_home(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    database = codex_home / "derived.sqlite3"

    with pytest.raises(UnsafeDatabasePathError):
        open_index(database, codex_home=codex_home)

    assert not database.exists()


def test_current_v01_database_migrates_to_lineage_schema_without_losing_usage(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    database = tmp_path / "index.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in (1, 2, 3, 4):
            connection.executescript(db_module._MIGRATIONS[version])
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, '2026-08-09T00:00:00Z')",
                (version,),
            )
        connection.execute(
            """
            INSERT INTO source_sessions(
                source_session_id, source_type, source_home, archived,
                first_ingested_at, last_ingested_at
            ) VALUES ('synthetic-session', 'codex-local', ?, 0,
                      '2026-08-09T00:00:00Z', '2026-08-09T00:00:00Z')
            """,
            (str(codex_home),),
        )
        session_id = connection.execute("SELECT id FROM source_sessions").fetchone()[0]
        connection.execute(
            """
            INSERT INTO usage(
                source_session_id, usage_semantics, input_tokens, output_tokens,
                total_tokens, token_update_count, updated_at
            ) VALUES (?, 'cumulative_total', 80, 20, 100, 1, '2026-08-09T00:00:00Z')
            """,
            (session_id,),
        )
        connection.commit()

    with open_index(database, codex_home=codex_home) as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        observed = connection.execute(
            "SELECT observed_total_tokens, aggregate_total_tokens FROM accounted_usage"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
        compatibility = connection.execute(
            """
            SELECT parser_version, parse_status, stale
            FROM session_compatibility
            WHERE source_session_id = ?
            """,
            (session_id,),
        ).fetchone()

    assert version == SCHEMA_VERSION
    assert tuple(observed) == (100, 100)
    assert tuple(compatibility) == ("legacy-unknown", "legacy_migrated", 0)
    assert {
        "thread_relationships",
        "token_lineage",
        "event_observations",
        "event_replay_summary",
    } <= tables


def test_event_provenance_database_migrates_to_prompt_fts(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    database = tmp_path / "index.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in (1, 2, 3, 4, 5):
            connection.executescript(db_module._MIGRATIONS[version])
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, '2026-08-09T00:00:00Z')",
                (version,),
            )
        connection.commit()

    with open_index(database, codex_home=codex_home) as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        objects = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT name, type FROM sqlite_schema WHERE name LIKE 'prompt%'"
            )
        }

    assert version == SCHEMA_VERSION
    assert ("prompts", "table") in objects
    assert ("prompt_observations", "table") in objects
    assert ("prompts_fts", "table") in objects


def test_schema_12_migration_preserves_phase_two_derived_rows(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    database = tmp_path / "index.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 13):
            connection.executescript(db_module._MIGRATIONS[version])
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, '2026-08-09T00:00:00Z')",
                (version,),
            )
        connection.executescript(
            f"""
            INSERT INTO source_sessions(
                id, source_session_id, source_type, source_home, archived,
                first_ingested_at, last_ingested_at
            ) VALUES
                (1, 'parent', 'codex-local', '{codex_home}', 0, 't', 't'),
                (2, 'child', 'codex-local', '{codex_home}', 0, 't', 't');
            INSERT INTO usage(
                source_session_id, usage_semantics, total_tokens,
                token_update_count, updated_at
            ) VALUES (2, 'cumulative_total', 120, 1, 't');
            INSERT INTO thread_relationships(
                id, source_type, source_home, relationship_type,
                parent_source_session_id, child_source_session_id,
                parent_session_id, child_session_id, last_seen_at
            ) VALUES (1, 'codex-local', '{codex_home}', 'spawn',
                      'parent', 'child', 1, 2, 't');
            INSERT INTO token_lineage(
                child_session_id, parent_session_id, deduplication_status,
                confidence, evidence_type, matched_snapshot_count,
                delta_consistency, algorithm_version, updated_at
            ) VALUES (2, 1, 'inherited_exact', 'high', 'synthetic', 1,
                      'exact', 'token-lineage-v1', 't');
            INSERT INTO event_observations(
                id, observed_session_id, source_ordinal, family_ordinal,
                event_family, source_record_type, source_payload_type,
                fingerprint, provenance_status, origin_session_id,
                evidence_type, confidence, fingerprint_version,
                provenance_algorithm_version, updated_at
            ) VALUES (1, 1, 0, 0, 'user_message', 'event_msg', 'user_message',
                      'fingerprint', 'origin', 1, 'synthetic', 'high',
                      'event-fingerprint-v1', 'event-provenance-v1', 't');
            INSERT INTO prompts(
                id, prompt_id, origin_session_id, origin_event_id, source_ordinal,
                prompt_ordinal, text, redaction_status, redaction_count,
                original_character_count, stored_character_count, provenance_status,
                provenance_confidence, user_authorship_evidence, fingerprint,
                content_schema_version, first_indexed_at, last_indexed_at
            ) VALUES (1, 'prompt', 1, 1, 0, 0, 'synthetic', 'none', 0, 9, 9,
                      'origin', 'high', 'synthetic', 'fingerprint',
                      'prompt-content-v1', 't', 't');
            INSERT INTO tool_activity(
                id, event_observation_id, observed_session_id, origin_session_id,
                source_ordinal, operation_ordinal, tool_family, tool_name,
                command_category, test_scope, result_status, provenance_status,
                redacted, truncated, extraction_version, classifier_version,
                updated_at
            ) VALUES (1, 1, 1, 1, 0, 0, 'shell', 'exec', 'testing',
                      'subset', 'success', 'origin', 0, 0, 'tool-v1',
                      'command-v1', 't');
            INSERT INTO repositories(
                id, identity_key, display_name, identity_method, path_exists,
                identity_version, first_seen_at, last_seen_at
            ) VALUES (1, 'repo', 'repo', 'repository_path', 0, 'repo-v1', 't', 't');
            INSERT INTO git_commits(
                id, repository_id, commit_hash, committed_at, parent_count,
                first_discovered_at, last_discovered_at
            ) VALUES (1, 1, 'abc', 't', 1, 't', 't');
            INSERT INTO session_commit_associations(
                session_id, commit_id, confidence, evidence_type,
                evidence_origin_session_id, evidence_explanation, ambiguous,
                algorithm_version, updated_at
            ) VALUES (1, 1, 'high', 'synthetic', 1, 'synthetic', 0, 'git-v1', 't');
            INSERT INTO session_outcomes(
                session_id, outcome, confidence, evidence_json, evidence_count,
                classifier_version, updated_at
            ) VALUES (1, 'success', 'high', '[]', 1, 'outcome-classifier-v1', 't');
            INSERT INTO session_tasks(
                session_id, action, domain, facets_json, confidence,
                evidence_json, taxonomy_version, updated_at
            ) VALUES (1, 'testing', 'software_engineering', '[]', 'high', '[]',
                      'task-taxonomy-v1', 't');
            INSERT INTO prompt_features(
                prompt_id, character_length, stored_character_length, line_count,
                structured_heading_count, has_acceptance_criteria,
                requests_validation, path_reference_count, requests_commit,
                requests_multiple_commits, has_explicit_non_goals,
                has_read_only_constraint, approximate_requirement_count,
                source_truncated, feature_version, requirement_heuristic_version,
                updated_at
            ) VALUES (1, 9, 9, 1, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0,
                      'prompt-features-v1', 'approx-v1', 't');
            """
        )
        before = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in (
                "source_sessions",
                "usage",
                "token_lineage",
                "event_observations",
                "prompts",
                "tool_activity",
                "session_commit_associations",
                "session_outcomes",
                "session_tasks",
                "prompt_features",
            )
        }
        connection.commit()

    with open_index(database, codex_home=codex_home) as connection:
        after = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in before
        }
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]

    assert version == SCHEMA_VERSION
    assert after == before


def test_db_info_reports_empty_database(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    database = tmp_path / "index.sqlite3"

    info = inspect_index(database, codex_home=codex_home)

    assert info.path == database
    assert info.schema_version == SCHEMA_VERSION
    assert info.indexed_session_count == 0
    assert info.latest_indexing_time is None
    assert info.source_coverage == ()

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_db_info_cli_uses_explicit_safe_paths(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    database = tmp_path / "index.sqlite3"

    result = runner.invoke(
        app,
        ["db-info", "--codex-home", str(codex_home), "--db", str(database)],
        env={"CODEX_HOME": "/must/not/be/used"},
    )

    assert result.exit_code == 0
    assert str(database) in result.stdout.replace("\n", "")
    assert "Schema version" in result.stdout
    assert "Indexed sessions" in result.stdout
    assert "never" in result.stdout


def test_db_info_cli_rejects_database_under_codex_home(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    database = codex_home / "index.sqlite3"

    result = runner.invoke(
        app,
        ["db-info", "--codex-home", str(codex_home), "--db", str(database)],
        env={"CODEX_HOME": "/must/not/be/used"},
    )

    assert result.exit_code == 2
    assert "Analyzer database must be outside Codex home" in result.stderr
    assert not database.exists()
