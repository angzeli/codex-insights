"""Shared synthetic test paths; real Codex history is never used."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from codex_insights.db import open_index


@pytest.fixture
def synthetic_codex_home() -> Path:
    return Path(__file__).parent / "fixtures" / "codex_home"


@pytest.fixture
def synthetic_audit_home(tmp_path: Path) -> Path:
    """Build a disposable Codex home from committed, entirely synthetic inputs."""

    fixture_root = Path(__file__).parent / "fixtures" / "source_audit"
    codex_home = tmp_path / "codex-home"
    shutil.copytree(fixture_root / "codex_home", codex_home)
    schema = (fixture_root / "state_fixture.sql").read_text(encoding="utf-8")
    with sqlite3.connect(codex_home / "state_7.sqlite") as connection:
        connection.executescript(schema)
    return codex_home


@pytest.fixture
def privacy_source_home(tmp_path: Path) -> Path:
    """Create one synthetic source with a prompt and a Git command."""

    codex_home = tmp_path / "privacy-codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    records = (
        {
            "timestamp": "2026-08-10T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "privacy-session",
                "cwd": "/synthetic/privacy-project",
                "source": "vscode",
                "model": "synthetic-model",
            },
        },
        {
            "timestamp": "2026-08-10T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "privacy-prompt",
                "content": [
                    {
                        "type": "input_text",
                        "text": "=SUM(A1:A2) TOKEN=synthetic-secret-value",
                    }
                ],
            },
        },
        {
            "timestamp": "2026-08-10T00:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "privacy-call",
                "arguments": json.dumps(
                    {"cmd": "git commit -m '=SUM(A1:A2)' --password synthetic-secret"}
                ),
            },
        },
        {
            "timestamp": "2026-08-10T00:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "privacy-call",
                "output": json.dumps({"exit_code": 0, "output": "not persisted"}),
            },
        },
    )
    (sessions / "privacy.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    with sqlite3.connect(codex_home / "state_9.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at TEXT,
                updated_at TEXT, source TEXT, cwd TEXT, model TEXT, archived INTEGER
            );
            INSERT INTO threads VALUES (
                'privacy-session', 'sessions/privacy.jsonl',
                '2026-08-10T00:00:00Z', '2026-08-10T00:00:03Z',
                'vscode', '/synthetic/privacy-project', 'synthetic-model', 0
            );
            """
        )
    return codex_home


@pytest.fixture
def analytics_database(tmp_path: Path) -> tuple[Path, Path]:
    """Create a normalized database with deterministic synthetic history rows."""

    database = tmp_path / "analytics" / "index.sqlite3"
    codex_home = tmp_path / "synthetic-codex-home"
    source_home = str(codex_home)
    sessions = (
        (
            "session-alpha-one-1111",
            "cli",
            "2026-08-01T00:00:00Z",
            "2026-08-01T01:30:00Z",
            "/work/repo-one",
            "/repos/repo-one",
            "repo-one",
            "main",
            "model-a",
            "provider-a",
            0,
        ),
        (
            "session-alpha-two-2222",
            "editor",
            "2026-08-07T12:00:00Z",
            "2026-08-07T12:30:00Z",
            "/work/repo-one",
            "/repos/repo-one",
            "repo-one",
            "archive",
            "model-b",
            "provider-b",
            1,
        ),
        (
            "session-beta-3333",
            "cli",
            "2026-08-08T23:59:59Z",
            "2026-08-09T00:09:59Z",
            "/work/outside-git",
            None,
            None,
            None,
            "model-a",
            "provider-a",
            0,
        ),
        (
            "session-boundary-4444",
            "cli",
            "2026-08-09T00:00:00Z",
            None,
            "/work/repo-two",
            "/repos/repo-two",
            "repo-two",
            "feature/test",
            None,
            None,
            0,
        ),
    )
    with open_index(database, codex_home=codex_home) as connection:
        for row in sessions:
            connection.execute(
                """
                INSERT INTO source_sessions(
                    source_session_id, source_type, source_home, client_source,
                    started_at, updated_at, apparent_ended_at, cwd, repository_root,
                    repository_name, git_branch, model, model_provider, codex_version,
                    archived, rollout_path, source_db_path, source_path,
                    first_ingested_at, last_ingested_at
                ) VALUES (
                    ?, 'codex-local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'synthetic-1.0',
                    ?, ?, ?, ?, '2026-08-09T01:00:00Z', '2026-08-09T01:00:00Z'
                )
                """,
                (
                    row[0],
                    source_home,
                    row[1],
                    row[2],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    f"/rollouts/{row[0]}.jsonl",
                    "/state_7.sqlite",
                    f"/rollouts/{row[0]}.jsonl",
                ),
            )

        identifiers = {
            str(row["source_session_id"]): int(row["id"])
            for row in connection.execute("SELECT id, source_session_id FROM source_sessions")
        }
        usage_rows = (
            (identifiers["session-alpha-one-1111"], "cumulative_total", 80, 10, 20, 100, 1),
            (identifiers["session-alpha-two-2222"], "unavailable", 0, 0, 0, 0, 0),
            (identifiers["session-beta-3333"], "summed_event_deltas", 40, 5, 10, 50, 2),
            (identifiers["session-boundary-4444"], "unavailable", 0, 0, 0, 0, 0),
        )
        connection.executemany(
            """
            INSERT INTO usage(
                source_session_id, usage_semantics, input_tokens, cached_input_tokens,
                output_tokens, total_tokens, token_update_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '2026-08-09T01:00:00Z')
            """,
            usage_rows,
        )
        connection.executemany(
            """
            INSERT INTO token_events(
                source_session_id, event_ordinal, source_ordinal, occurred_at,
                event_kind, cumulative_input_tokens,
                cumulative_cached_input_tokens, cumulative_output_tokens,
                cumulative_total_tokens, delta_input_tokens,
                delta_cached_input_tokens, delta_output_tokens, delta_total_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    identifiers["session-alpha-one-1111"], 0, 1,
                    "2026-08-01T00:30:00Z", "cumulative_snapshot",
                    80, 10, 20, 100, None, None, None, None,
                ),
                (
                    identifiers["session-beta-3333"], 0, 1,
                    "2026-08-08T23:59:59Z", "event_delta",
                    None, None, None, None, 20, 5, 5, 25,
                ),
                (
                    identifiers["session-beta-3333"], 1, 2,
                    "2026-08-09T00:05:00Z", "event_delta",
                    None, None, None, None, 20, 0, 5, 25,
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO event_summary(source_session_id, category, event_count, updated_at)
            VALUES (?, ?, ?, '2026-08-09T01:00:00Z')
            """,
            (
                (identifiers["session-alpha-one-1111"], "user_message", 2),
                (identifiers["session-alpha-one-1111"], "assistant_message", 3),
                (identifiers["session-alpha-one-1111"], "tool_call", 1),
                (identifiers["session-alpha-two-2222"], "unknown", 4),
                (identifiers["session-beta-3333"], "shell_command", 2),
            ),
        )
        coverage_rows = (
            ("session-alpha-one-1111", "indexed", None, 1000, 1000),
            (
                "session-alpha-two-2222",
                "indexed_with_warnings",
                "malformed_lines=1;oversized_lines=0",
                2000,
                2000,
            ),
            ("session-beta-3333", "indexed", None, 3000, 3000),
            ("session-boundary-4444", "missing", None, None, None),
        )
        connection.executemany(
            """
            INSERT INTO ingestion_state(
                source_home, source_path, source_session_id, source_kind, size_bytes,
                mtime_ns, last_parsed_byte_offset, parser_version,
                source_schema_version, status, error, indexed_at
            ) VALUES (
                ?, ?, ?, 'rollout_jsonl', ?, 100, ?, 'synthetic-parser',
                'state_7:threads', ?, ?, '2026-08-09T01:00:00Z'
            )
            """,
            (
                (
                    source_home,
                    f"/rollouts/{session_id}.jsonl",
                    session_id,
                    size_bytes,
                    parsed_bytes,
                    status,
                    error,
                )
                for session_id, status, error, size_bytes, parsed_bytes in coverage_rows
            ),
        )
        connection.commit()
    return database, codex_home
