from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from codex_insights import db as db_module
from codex_insights.adapters import CodexLocalAdapter
from codex_insights.config import resolve_codex_home
from codex_insights.db import open_index
from codex_insights.indexer import index_source
from codex_insights.models import ClientKind, SubagentSourceKind


def test_catalogue_source_union_is_normalized_without_raw_structured_persistence(
    tmp_path: Path,
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    state = home / "state_12.sqlite"
    sources = {
        "cli": "cli",
        "editor": "vscode",
        "other": "app-server",
        "spawn": json.dumps(
            {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}
        ),
        "guardian": json.dumps({"subagent": {"guardian": {"role": "review"}}}),
        "future": json.dumps({"future_source_v2": {"private": "must-not-persist"}}),
        "malformed": "{malformed-private-source",
        "missing": None,
    }
    with sqlite3.connect(state) as connection:
        connection.execute(
            "CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT)"
        )
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?, ?)",
            ((name, f"sessions/{name}.jsonl", source) for name, source in sources.items()),
        )

    adapter = CodexLocalAdapter(resolve_codex_home(home))
    candidates, warnings = adapter.discover_sessions()
    sessions = {item.session.source_session_id: item.session for item in candidates}

    assert warnings == ()
    assert sessions["cli"].client_kind is ClientKind.CLI
    assert sessions["editor"].client_kind is ClientKind.EDITOR
    assert sessions["other"].client_kind is ClientKind.OTHER
    assert sessions["spawn"].client_kind is ClientKind.SUBAGENT
    assert sessions["spawn"].subagent_source_kind is SubagentSourceKind.THREAD_SPAWN
    assert sessions["spawn"].source_parent_session_id == "parent"
    assert sessions["guardian"].subagent_source_kind is SubagentSourceKind.GUARDIAN
    assert sessions["future"].client_kind is ClientKind.UNKNOWN
    assert sessions["malformed"].client_kind is ClientKind.UNKNOWN
    assert sessions["missing"].client_kind is ClientKind.UNKNOWN
    assert all(
        sessions[name].client_source is None
        for name in sources
        if name not in {"cli", "editor", "other"}
    )

    database = tmp_path / "index.sqlite3"
    report = index_source(adapter, database, codex_home=home)
    assert report.failed == 0
    with sqlite3.connect(database) as connection:
        rows = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT source_session_id, client_source, client_kind, "
                "subagent_source_kind, source_parent_session_id FROM source_sessions"
            )
        }
    assert rows["spawn"] == (None, "subagent", "thread_spawn", "parent")
    assert rows["future"] == (None, "unknown", None, None)
    database_bytes = database.read_bytes()
    assert b"must-not-persist" not in database_bytes
    assert b"malformed-private-source" not in database_bytes


def test_schema_17_migration_removes_legacy_structured_source_text(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    database = tmp_path / "index.sqlite3"
    structured = json.dumps({"future_source": {"private": "legacy-secret"}})
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 17):
            connection.executescript(db_module._MIGRATIONS[version])
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, '2026-08-12T00:00:00Z')",
                (version,),
            )
        connection.executemany(
            """
            INSERT INTO source_sessions(
                source_session_id, source_type, source_home, client_source,
                first_ingested_at, last_ingested_at
            ) VALUES (?, 'codex_local', ?, ?, '2026-08-12T00:00:00Z', '2026-08-12T00:00:00Z')
            """,
            (
                ("legacy-cli", str(codex_home), "cli"),
                ("legacy-editor", str(codex_home), "vscode"),
                ("legacy-structured", str(codex_home), structured),
            ),
        )
        connection.commit()

    with open_index(database, codex_home=codex_home) as connection:
        rows = connection.execute(
            "SELECT source_session_id, client_source, client_kind FROM source_sessions "
            "ORDER BY source_session_id"
        ).fetchall()

    assert [tuple(row) for row in rows] == [
        ("legacy-cli", "cli", "cli"),
        ("legacy-editor", "vscode", "editor"),
        ("legacy-structured", None, "unknown"),
    ]
    assert b"legacy-secret" not in database.read_bytes()
