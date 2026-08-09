from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

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

    assert version == SCHEMA_VERSION
    assert {
        "source_sessions",
        "usage",
        "event_summary",
        "ingestion_state",
        "index_runs",
        "schema_migrations",
    } <= tables


def test_index_database_cannot_be_created_under_codex_home(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    database = codex_home / "derived.sqlite3"

    with pytest.raises(UnsafeDatabasePathError):
        open_index(database, codex_home=codex_home)

    assert not database.exists()


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
