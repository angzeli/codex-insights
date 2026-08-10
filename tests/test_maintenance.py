from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source
from codex_insights.maintenance import (
    UnexpectedDatabaseError,
    backup_index,
    reset_index,
    validate_expected_index,
)
from codex_insights.path_safety import UnsafeDestinationError

runner = CliRunner()


def test_sqlite_backup_is_consistent_versioned_and_outside_source(
    privacy_source_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    destination = tmp_path / "backups" / "index-backup.sqlite3"
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )

    result = backup_index(
        database,
        destination,
        codex_home=privacy_source_home,
        create_parents=True,
    )

    assert result.destination == destination
    assert result.stored_prompt_bodies == 1
    assert result.stored_command_texts == 1
    with sqlite3.connect(destination) as connection:
        metadata = connection.execute(
            "SELECT created_at, schema_version, application_version FROM backup_metadata"
        ).fetchone()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert metadata[0] == result.created_at
    assert metadata[1] == result.schema_version
    assert metadata[2]
    with pytest.raises(UnsafeDestinationError):
        backup_index(
            database,
            privacy_source_home / "forbidden.sqlite3",
            codex_home=privacy_source_home,
        )


def test_reset_rejects_unexpected_and_codex_source_database(
    privacy_source_home: Path,
    tmp_path: Path,
) -> None:
    ordinary = tmp_path / "ordinary.sqlite3"
    with sqlite3.connect(ordinary) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    with pytest.raises(UnexpectedDatabaseError):
        validate_expected_index(ordinary, codex_home=privacy_source_home)
    with pytest.raises(UnsafeDestinationError):
        reset_index(
            privacy_source_home / "state_9.sqlite",
            codex_home=privacy_source_home,
        )


def test_safe_reset_with_explicit_backup_rebuilds_without_source_mutation(
    privacy_source_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    backup = tmp_path / "backups" / "before-reset.sqlite3"
    source_database = privacy_source_home / "state_9.sqlite"
    rollout = privacy_source_home / "sessions" / "privacy.jsonl"
    source_before = (source_database.read_bytes(), rollout.read_bytes())
    adapter = CodexLocalAdapter(resolve_codex_home(privacy_source_home))
    index_source(adapter, database, codex_home=privacy_source_home)

    result = reset_index(
        database,
        codex_home=privacy_source_home,
        backup_destination=backup,
        create_backup_parents=True,
    )

    assert result.backup is not None
    assert result.backup.destination == backup
    assert not database.exists()
    assert backup.exists()
    rebuilt = index_source(adapter, database, codex_home=privacy_source_home)
    assert rebuilt.new == 1
    assert source_before == (source_database.read_bytes(), rollout.read_bytes())


def test_backup_and_reset_cli_smoke(
    privacy_source_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )

    backed_up = runner.invoke(
        app,
        [
            "backup-index",
            str(backup),
            "--db",
            str(database),
            "--codex-home",
            str(privacy_source_home),
            "--json",
        ],
    )
    reset = runner.invoke(
        app,
        [
            "reset-index",
            "--db",
            str(database),
            "--codex-home",
            str(privacy_source_home),
            "--yes",
            "--json",
        ],
    )

    assert backed_up.exit_code == 0
    assert json.loads(backed_up.stdout)["destination"] == str(backup)
    assert reset.exit_code == 0
    assert json.loads(reset.stdout)["database_path"] == str(database)
    assert not database.exists()
    assert backup.exists()
