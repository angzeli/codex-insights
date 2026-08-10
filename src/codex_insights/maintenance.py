"""Safe backup and reset operations for the derived Codex Insights database only."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from codex_insights import __version__
from codex_insights.path_safety import (
    prepare_output_parent,
    resolved_path,
    validate_write_target,
)
from codex_insights.privacy import utc_now


class UnexpectedDatabaseError(ValueError):
    """Raised when a destructive target is not recognizably an Insights database."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    destination: Path
    created_at: str
    schema_version: int
    stored_prompt_bodies: int
    stored_command_texts: int

    def to_dict(self) -> dict[str, object]:
        return {
            "destination": str(self.destination),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "stored_prompt_bodies": self.stored_prompt_bodies,
            "stored_command_texts": self.stored_command_texts,
        }


@dataclass(frozen=True, slots=True)
class ResetResult:
    database_path: Path
    removed_files: tuple[Path, ...]
    backup: BackupResult | None

    def to_dict(self) -> dict[str, object]:
        return {
            "database_path": str(self.database_path),
            "removed_files": [str(path) for path in self.removed_files],
            "backup": self.backup.to_dict() if self.backup is not None else None,
        }


def validate_expected_index(database_path: Path, *, codex_home: Path) -> tuple[Path, int]:
    """Verify a real file has the minimum normalized Insights schema, read-only."""

    database = validate_write_target(
        database_path,
        codex_home=codex_home,
        operation="Derived-index reset",
    )
    user_home = resolved_path(Path.home())
    if database == Path(database.anchor) or database == user_home or database.is_dir():
        raise UnexpectedDatabaseError(f"Refusing dangerous reset target: {database}")
    if not database.is_file():
        raise FileNotFoundError(f"Codex Insights database does not exist: {database}")
    with closing(_open_readonly(database)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        required = {"schema_migrations", "source_sessions", "index_runs"}
        if not required <= tables:
            raise UnexpectedDatabaseError(
                f"Refusing reset: file is not a recognized Codex Insights database: {database}"
            )
        schema_version = int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        )
    return database, schema_version


def backup_index(
    database_path: Path,
    destination: Path,
    *,
    codex_home: Path,
    overwrite: bool = False,
    create_parents: bool = False,
) -> BackupResult:
    """Create a consistent SQLite backup containing only derived Insights data."""

    database, schema_version = validate_expected_index(database_path, codex_home=codex_home)
    backup_path = validate_write_target(
        destination,
        codex_home=codex_home,
        operation="Derived-index backup",
        protected_paths=(database,),
    )
    if backup_path.exists() and not overwrite:
        raise FileExistsError(
            f"Backup already exists: {backup_path}; pass --overwrite to replace it"
        )
    prepare_output_parent(backup_path, create_parents=create_parents)
    created_at = utc_now()
    with closing(_open_readonly(database)) as source:
        stored_prompt_bodies = _count(
            source, "SELECT COUNT(*) FROM prompts WHERE length(text) > 0"
        )
        stored_command_texts = _count(
            source, "SELECT COUNT(*) FROM tool_activity WHERE command_text IS NOT NULL"
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{backup_path.name}.", suffix=".tmp", dir=backup_path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                target.execute(
                    """
                    CREATE TABLE IF NOT EXISTS backup_metadata (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        created_at TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        application_version TEXT NOT NULL
                    )
                    """
                )
                target.execute("DELETE FROM backup_metadata")
                target.execute(
                    "INSERT INTO backup_metadata VALUES (1, ?, ?, ?)",
                    (created_at, schema_version, __version__),
                )
                target.commit()
            os.chmod(temporary, 0o600)
            if backup_path.exists() and not overwrite:
                raise FileExistsError(
                    f"Backup already exists: {backup_path}; pass --overwrite to replace it"
                )
            os.replace(temporary, backup_path)
        finally:
            if temporary.exists():
                temporary.unlink()
    return BackupResult(
        destination=backup_path,
        created_at=created_at,
        schema_version=schema_version,
        stored_prompt_bodies=stored_prompt_bodies,
        stored_command_texts=stored_command_texts,
    )


def reset_index(
    database_path: Path,
    *,
    codex_home: Path,
    backup_destination: Path | None = None,
    backup_overwrite: bool = False,
    create_backup_parents: bool = False,
) -> ResetResult:
    """Delete only a verified derived index and its SQLite sidecars."""

    database, _ = validate_expected_index(database_path, codex_home=codex_home)
    backup = (
        backup_index(
            database,
            backup_destination,
            codex_home=codex_home,
            overwrite=backup_overwrite,
            create_parents=create_backup_parents,
        )
        if backup_destination is not None
        else None
    )
    candidates = (database, Path(f"{database}-wal"), Path(f"{database}-shm"))
    removed: list[Path] = []
    for candidate in candidates:
        safe = validate_write_target(
            candidate,
            codex_home=codex_home,
            operation="Derived-index reset",
            protected_paths=((backup.destination,) if backup is not None else ()),
        )
        if safe.exists():
            if not safe.is_file():
                raise UnexpectedDatabaseError(f"Refusing non-file reset target: {safe}")
            safe.unlink()
            removed.append(safe)
    return ResetResult(database_path=database, removed_files=tuple(removed), backup=backup)


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    connection.row_factory = sqlite3.Row
    return connection


def _count(connection: sqlite3.Connection, query: str) -> int:
    return int(connection.execute(query).fetchone()[0])
