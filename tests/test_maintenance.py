from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

import codex_insights.maintenance as maintenance_module
from codex_insights.adapters import CodexLocalAdapter
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source
from codex_insights.maintenance import (
    ResetTargetChangedError,
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


def test_reset_refuses_main_database_identity_swap(
    privacy_source_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "index.sqlite3"
    validated_object = tmp_path / "validated-index.sqlite3"
    replacement_source = tmp_path / "unrelated-replacement.txt"
    replacement_source.write_text("unrelated synthetic content", encoding="utf-8")
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )
    original_revalidate = maintenance_module._revalidate_reset_targets

    def swap_then_revalidate(validated: maintenance_module._ValidatedIndex) -> None:
        database.replace(validated_object)
        replacement_source.replace(database)
        original_revalidate(validated)

    monkeypatch.setattr(
        maintenance_module,
        "_revalidate_reset_targets",
        swap_then_revalidate,
    )

    with pytest.raises(ResetTargetChangedError, match="changed identity after validation"):
        reset_index(database, codex_home=privacy_source_home)

    assert database.read_text(encoding="utf-8") == "unrelated synthetic content"
    assert validated_object.exists()


def test_reset_refuses_symlink_replacement_after_validation(
    privacy_source_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "index.sqlite3"
    validated_object = tmp_path / "validated-index.sqlite3"
    unrelated_target = tmp_path / "unrelated-target.txt"
    unrelated_target.write_text("unrelated synthetic content", encoding="utf-8")
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )
    original_revalidate = maintenance_module._revalidate_reset_targets

    def symlink_then_revalidate(validated: maintenance_module._ValidatedIndex) -> None:
        database.replace(validated_object)
        database.symlink_to(unrelated_target)
        original_revalidate(validated)

    monkeypatch.setattr(
        maintenance_module,
        "_revalidate_reset_targets",
        symlink_then_revalidate,
    )

    with pytest.raises(ResetTargetChangedError, match="changed identity after validation"):
        reset_index(database, codex_home=privacy_source_home)

    assert database.is_symlink()
    assert unrelated_target.read_text(encoding="utf-8") == "unrelated synthetic content"
    assert validated_object.exists()


def test_reset_backup_precedes_identity_swap_refusal(
    privacy_source_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "index.sqlite3"
    validated_object = tmp_path / "validated-index.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    replacement_source = tmp_path / "unrelated-replacement.txt"
    replacement_source.write_text("unrelated synthetic content", encoding="utf-8")
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE reset_identity_marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO reset_identity_marker VALUES ('validated')")
    original_revalidate = maintenance_module._revalidate_reset_targets

    def swap_after_backup(validated: maintenance_module._ValidatedIndex) -> None:
        database.replace(validated_object)
        replacement_source.replace(database)
        original_revalidate(validated)

    monkeypatch.setattr(
        maintenance_module,
        "_revalidate_reset_targets",
        swap_after_backup,
    )

    with pytest.raises(ResetTargetChangedError, match="changed identity after validation"):
        reset_index(
            database,
            codex_home=privacy_source_home,
            backup_destination=backup,
        )

    with sqlite3.connect(backup) as connection:
        marker = connection.execute("SELECT value FROM reset_identity_marker").fetchone()[0]
    assert marker == "validated"
    assert database.read_text(encoding="utf-8") == "unrelated synthetic content"
    assert validated_object.exists()


def test_reset_refuses_sidecar_created_after_validation(
    privacy_source_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "index.sqlite3"
    unexpected_sidecar = Path(f"{database}-wal")
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )
    original_revalidate = maintenance_module._revalidate_reset_targets

    def create_sidecar_then_revalidate(validated: maintenance_module._ValidatedIndex) -> None:
        unexpected_sidecar.write_text("unrelated synthetic sidecar", encoding="utf-8")
        original_revalidate(validated)

    monkeypatch.setattr(
        maintenance_module,
        "_revalidate_reset_targets",
        create_sidecar_then_revalidate,
    )

    with pytest.raises(ResetTargetChangedError, match="changed identity after validation"):
        reset_index(database, codex_home=privacy_source_home)

    assert database.exists()
    assert unexpected_sidecar.read_text(encoding="utf-8") == "unrelated synthetic sidecar"


def test_reset_refuses_captured_sidecar_identity_swap(
    privacy_source_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "index.sqlite3"
    sidecar = Path(f"{database}-shm")
    validated_sidecar = tmp_path / "validated-sidecar"
    replacement_source = tmp_path / "unrelated-sidecar"
    replacement_source.write_text("unrelated synthetic sidecar", encoding="utf-8")
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )
    sidecar.write_text("captured synthetic sidecar", encoding="utf-8")
    original_revalidate = maintenance_module._revalidate_reset_targets

    def swap_sidecar_then_revalidate(validated: maintenance_module._ValidatedIndex) -> None:
        sidecar.replace(validated_sidecar)
        replacement_source.replace(sidecar)
        original_revalidate(validated)

    monkeypatch.setattr(
        maintenance_module,
        "_revalidate_reset_targets",
        swap_sidecar_then_revalidate,
    )

    with pytest.raises(ResetTargetChangedError, match="changed identity after validation"):
        reset_index(database, codex_home=privacy_source_home)

    assert database.exists()
    assert sidecar.read_text(encoding="utf-8") == "unrelated synthetic sidecar"
    assert validated_sidecar.read_text(encoding="utf-8") == "captured synthetic sidecar"


def test_reset_fails_closed_when_identity_cannot_be_revalidated(
    privacy_source_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "index.sqlite3"
    validated_object = tmp_path / "validated-index.sqlite3"
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )
    original_revalidate = maintenance_module._revalidate_reset_targets

    def remove_path_then_revalidate(validated: maintenance_module._ValidatedIndex) -> None:
        database.replace(validated_object)
        original_revalidate(validated)

    monkeypatch.setattr(
        maintenance_module,
        "_revalidate_reset_targets",
        remove_path_then_revalidate,
    )

    with pytest.raises(ResetTargetChangedError, match="changed identity after validation"):
        reset_index(database, codex_home=privacy_source_home)

    assert not database.exists()
    assert validated_object.exists()


def test_reset_cli_reports_identity_change_without_deleting_replacement(
    privacy_source_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "index.sqlite3"
    validated_object = tmp_path / "validated-index.sqlite3"
    replacement_source = tmp_path / "unrelated-replacement.txt"
    replacement_source.write_text("unrelated synthetic content", encoding="utf-8")
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )
    original_revalidate = maintenance_module._revalidate_reset_targets

    def swap_then_revalidate(validated: maintenance_module._ValidatedIndex) -> None:
        database.replace(validated_object)
        replacement_source.replace(database)
        original_revalidate(validated)

    monkeypatch.setattr(
        maintenance_module,
        "_revalidate_reset_targets",
        swap_then_revalidate,
    )
    result = runner.invoke(
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
        env={"COLUMNS": "160"},
    )

    assert result.exit_code != 0
    assert "Refusing reset: derived index changed identity after validation." in result.output
    assert database.read_text(encoding="utf-8") == "unrelated synthetic content"
    assert validated_object.exists()


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
