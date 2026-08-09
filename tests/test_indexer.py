from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source
from codex_insights.models import ParsedSourceSession, SourceSessionCandidate

runner = CliRunner()


def test_index_normalizes_catalogue_usage_events_and_missing_rollouts(
    synthetic_audit_home: Path,
    tmp_path: Path,
) -> None:
    source_database = synthetic_audit_home / "state_7.sqlite"
    source_before = source_database.read_bytes()
    database = tmp_path / "analytics" / "index.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(synthetic_audit_home))

    report = index_source(adapter, database, codex_home=synthetic_audit_home)

    assert report.discovered == 4
    assert report.new == 3
    assert report.updated == 0
    assert report.unchanged == 0
    assert report.skipped == 1
    assert report.failed == 0

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        sessions = {
            str(row["source_session_id"]): row
            for row in connection.execute("SELECT * FROM source_sessions")
        }
        assert set(sessions) == {
            "synthetic-thread-modern",
            "synthetic-thread-legacy",
            "synthetic-thread-archived",
            "synthetic-thread-missing",
        }
        assert sessions["synthetic-thread-archived"]["archived"] == 1
        assert sessions["synthetic-thread-modern"]["repository_root"] is None
        assert sessions["synthetic-thread-modern"]["repository_name"] is None
        assert sessions["synthetic-thread-modern"]["model_provider"] == "synthetic-provider"
        assert sessions["synthetic-thread-modern"]["client_source"] == "cli"
        assert sessions["synthetic-thread-modern"]["started_at"].endswith("Z")

        modern_usage = connection.execute(
            """
            SELECT usage.* FROM usage
            JOIN source_sessions ON source_sessions.id = usage.source_session_id
            WHERE source_sessions.source_session_id = 'synthetic-thread-modern'
            """
        ).fetchone()
        assert modern_usage["usage_semantics"] == "cumulative_total"
        assert modern_usage["input_tokens"] == 120
        assert modern_usage["cached_input_tokens"] == 10
        assert modern_usage["output_tokens"] == 30
        assert modern_usage["reasoning_output_tokens"] is None
        assert modern_usage["total_tokens"] == 150

        legacy_usage = connection.execute(
            """
            SELECT usage.* FROM usage
            JOIN source_sessions ON source_sessions.id = usage.source_session_id
            WHERE source_sessions.source_session_id = 'synthetic-thread-legacy'
            """
        ).fetchone()
        assert legacy_usage["usage_semantics"] == "summed_event_deltas"
        assert legacy_usage["total_tokens"] == 100

        modern_events = dict(
            connection.execute(
                """
                SELECT event_summary.category, event_summary.event_count
                FROM event_summary
                JOIN source_sessions ON source_sessions.id = event_summary.source_session_id
                WHERE source_sessions.source_session_id = 'synthetic-thread-modern'
                """
            )
        )
        assert modern_events["token_update"] == 1
        assert modern_events["shell_command"] == 1
        assert modern_events["unknown"] == 1

        states = {
            str(row["source_session_id"]): row
            for row in connection.execute("SELECT * FROM ingestion_state")
        }
        assert states["synthetic-thread-modern"]["status"] == "indexed_with_warnings"
        assert states["synthetic-thread-modern"]["error"] == ("malformed_lines=1;oversized_lines=0")
        assert states["synthetic-thread-missing"]["status"] == "missing"

    indexed_bytes = database.read_bytes()
    assert b"SYNTHETIC PRIVATE TITLE" not in indexed_bytes
    assert b"SYNTHETIC SECRET COMMAND" not in indexed_bytes
    assert b"SYNTHETIC SECRET ARCHIVED MESSAGE" not in indexed_bytes
    assert source_database.read_bytes() == source_before


def test_index_is_idempotent_when_sources_are_unchanged(
    synthetic_audit_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(synthetic_audit_home))
    first = index_source(adapter, database, codex_home=synthetic_audit_home)
    before = _normalized_snapshot(database)

    second = index_source(adapter, database, codex_home=synthetic_audit_home)
    after = _normalized_snapshot(database)

    assert first.new == 3
    assert second.discovered == 4
    assert second.new == 0
    assert second.updated == 0
    assert second.unchanged == 3
    assert second.skipped == 1
    assert second.failed == 0
    assert before == after


def test_cumulative_token_snapshots_are_not_summed_with_each_other_or_turn_deltas(
    synthetic_audit_home: Path,
) -> None:
    adapter = CodexLocalAdapter(resolve_codex_home(synthetic_audit_home))
    candidates, _ = adapter.discover_sessions()
    candidate = next(
        item for item in candidates if item.session.source_session_id == "synthetic-thread-modern"
    )
    rollout = candidate.session.source_path
    assert rollout is not None
    records = (
        {
            "timestamp": "2026-08-09T00:01:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 20,
                        "output_tokens": 10,
                        "total_tokens": 30,
                    }
                },
            },
        },
        {
            "timestamp": "2026-08-09T00:02:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 50,
                        "output_tokens": 20,
                        "total_tokens": 70,
                    },
                    "total_token_usage": {
                        "input_tokens": 80,
                        "output_tokens": 20,
                        "total_tokens": 100,
                    },
                },
            },
        },
        {
            "timestamp": "2026-08-09T00:03:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 35,
                        "output_tokens": 15,
                        "total_tokens": 50,
                    },
                    "total_token_usage": {
                        "input_tokens": 120,
                        "output_tokens": 30,
                        "total_tokens": 150,
                    },
                },
            },
        },
    )
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    parsed = adapter.parse_session(candidate)

    assert parsed.session.usage.semantics.value == "cumulative_total"
    assert parsed.session.usage.input_tokens == 120
    assert parsed.session.usage.output_tokens == 30
    assert parsed.session.usage.total_tokens == 150
    assert parsed.session.usage.token_update_count == 3


def test_changed_rollout_refreshes_only_that_session(
    synthetic_audit_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(synthetic_audit_home))
    index_source(adapter, database, codex_home=synthetic_audit_home)
    rollout = synthetic_audit_home / "sessions" / "2026" / "08" / "09" / "rollout-modern.jsonl"
    original_mtime = rollout.stat().st_mtime_ns
    additions = [
        {
            "timestamp": "2026-08-09T00:04:00+08:00",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "SYNTHETIC NEVER STORE"},
        },
        {
            "timestamp": "2026-08-09T00:05:00+08:00",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 200,
                        "cached_input_tokens": 20,
                        "output_tokens": 40,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 240,
                    }
                },
            },
        },
    ]
    with rollout.open("a", encoding="utf-8") as handle:
        for record in additions:
            handle.write(json.dumps(record) + "\n")
    os.utime(rollout, ns=(original_mtime + 1_000_000, original_mtime + 1_000_000))

    report = index_source(adapter, database, codex_home=synthetic_audit_home)

    assert report.updated == 1
    assert report.unchanged == 2
    assert report.skipped == 1
    with sqlite3.connect(database) as connection:
        usage = connection.execute(
            """
            SELECT input_tokens, output_tokens, reasoning_output_tokens, total_tokens,
                   token_update_count
            FROM usage JOIN source_sessions ON source_sessions.id = usage.source_session_id
            WHERE source_sessions.source_session_id = 'synthetic-thread-modern'
            """
        ).fetchone()
        assert usage == (200, 40, 5, 240, 2)
        user_messages = connection.execute(
            """
            SELECT event_count FROM event_summary
            JOIN source_sessions ON source_sessions.id = event_summary.source_session_id
            WHERE source_sessions.source_session_id = 'synthetic-thread-modern'
              AND category = 'user_message'
            """
        ).fetchone()[0]
        assert user_messages == 1
    assert b"SYNTHETIC NEVER STORE" not in database.read_bytes()


def test_index_cli_never_falls_back_to_real_home(
    synthetic_audit_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "index.sqlite3"

    def fail_if_called() -> Path:
        raise AssertionError("Path.home() must not be used with explicit paths")

    monkeypatch.setattr(Path, "home", fail_if_called)
    result = runner.invoke(
        app,
        [
            "index",
            "--codex-home",
            str(synthetic_audit_home),
            "--db",
            str(database),
        ],
        env={"CODEX_HOME": "/must/not/be/used"},
    )

    assert result.exit_code == 0
    assert "Codex session index" in result.stdout
    assert "Discovered" in result.stdout
    assert "Failed" in result.stdout
    assert "SYNTHETIC SECRET" not in result.stdout

    info = runner.invoke(
        app,
        ["db-info", "--codex-home", str(synthetic_audit_home), "--db", str(database)],
        env={"CODEX_HOME": "/must/not/be/used"},
    )
    assert info.exit_code == 0
    assert "Indexed sessions" in info.stdout
    assert "codex-local" in info.stdout
    assert "Latest indexing time" in info.stdout


def test_one_session_failure_does_not_abort_other_sessions(
    synthetic_audit_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    base = CodexLocalAdapter(resolve_codex_home(synthetic_audit_home))

    class OneFailureAdapter:
        @property
        def name(self) -> str:
            return base.name

        @property
        def parser_version(self) -> str:
            return base.parser_version

        def discover_sessions(
            self,
        ) -> tuple[tuple[SourceSessionCandidate, ...], tuple[str, ...]]:
            return base.discover_sessions()

        def parse_session(self, candidate: SourceSessionCandidate) -> ParsedSourceSession:
            if candidate.session.source_session_id == "synthetic-thread-legacy":
                raise ValueError("synthetic parse failure")
            return base.parse_session(candidate)

    report = index_source(OneFailureAdapter(), database, codex_home=synthetic_audit_home)

    assert report.discovered == 4
    assert report.new == 2
    assert report.failed == 1
    assert report.skipped == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_sessions").fetchone()[0] == 4
        failure = connection.execute(
            """
            SELECT status, error FROM ingestion_state
            WHERE source_session_id = 'synthetic-thread-legacy'
            """
        ).fetchone()
        assert failure == ("failed", "ValueError")


def _normalized_snapshot(database: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(database) as connection:
        rows: list[tuple[object, ...]] = []
        for table, order_by in (
            ("source_sessions", "id"),
            ("usage", "source_session_id"),
            ("event_summary", "source_session_id, category"),
            ("ingestion_state", "source_path"),
            ("thread_relationships", "id"),
            ("token_lineage", "child_session_id"),
            ("event_observations", "observed_session_id, source_ordinal"),
            ("event_replay_summary", "relationship_id, event_family"),
        ):
            rows.extend(connection.execute(f"SELECT * FROM {table} ORDER BY {order_by}"))
        return tuple(rows)
