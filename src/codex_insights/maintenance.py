"""Safe backup and reset operations for the derived Codex Insights database only."""

from __future__ import annotations

import os
import sqlite3
import stat
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


class ResetTargetChangedError(UnexpectedDatabaseError):
    """Raised when a reset target no longer has its validated filesystem identity."""


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _ValidatedIndex:
    database: Path
    schema_version: int
    database_identity: _FileIdentity
    sidecar_identities: tuple[tuple[Path, _FileIdentity | None], ...]


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

    validated = _validate_expected_index_identity(database_path, codex_home=codex_home)
    return validated.database, validated.schema_version


def _validate_expected_index_identity(
    database_path: Path,
    *,
    codex_home: Path,
) -> _ValidatedIndex:
    """Validate the schema and retain the exact filesystem object identity."""

    database = validate_write_target(
        database_path,
        codex_home=codex_home,
        operation="Derived-index reset",
    )
    user_home = resolved_path(Path.home())
    if database == Path(database.anchor) or database == user_home:
        raise UnexpectedDatabaseError(f"Refusing dangerous reset target: {database}")
    identity = _read_required_identity(database)
    with closing(_open_readonly(database)) as connection:
        _assert_identity(database, identity)
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
        _assert_identity(database, identity)
    _assert_identity(database, identity)
    sidecars: list[tuple[Path, _FileIdentity | None]] = []
    for path in _sidecar_paths(database):
        safe_sidecar = validate_write_target(
            path,
            codex_home=codex_home,
            operation="Derived-index reset",
        )
        if safe_sidecar != path:
            raise UnexpectedDatabaseError(f"Refusing non-file reset target: {path}")
        sidecars.append((path, _read_optional_identity(path)))
    return _ValidatedIndex(
        database=database,
        schema_version=schema_version,
        database_identity=identity,
        sidecar_identities=tuple(sidecars),
    )


def backup_index(
    database_path: Path,
    destination: Path,
    *,
    codex_home: Path,
    overwrite: bool = False,
    create_parents: bool = False,
) -> BackupResult:
    """Create a consistent SQLite backup containing only derived Insights data."""

    validated = _validate_expected_index_identity(database_path, codex_home=codex_home)
    return _backup_validated_index(
        validated,
        destination,
        codex_home=codex_home,
        overwrite=overwrite,
        create_parents=create_parents,
    )


def _backup_validated_index(
    validated: _ValidatedIndex,
    destination: Path,
    *,
    codex_home: Path,
    overwrite: bool,
    create_parents: bool,
) -> BackupResult:
    """Back up the already validated database object without changing its identity."""

    database = validated.database
    _assert_identity(database, validated.database_identity)
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
        _assert_identity(database, validated.database_identity)
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
                    (created_at, validated.schema_version, __version__),
                )
                target.commit()
            os.chmod(temporary, 0o600)
            _assert_identity(database, validated.database_identity)
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
        schema_version=validated.schema_version,
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

    validated = _validate_expected_index_identity(database_path, codex_home=codex_home)
    database = validated.database
    backup = (
        _backup_validated_index(
            validated,
            backup_destination,
            codex_home=codex_home,
            overwrite=backup_overwrite,
            create_parents=create_backup_parents,
        )
        if backup_destination is not None
        else None
    )
    _revalidate_reset_targets(validated)

    removed_sidecars: list[Path] = []
    for sidecar, identity in validated.sidecar_identities:
        if identity is None:
            continue
        _assert_identity(database, validated.database_identity)
        _unlink_exact_identity(sidecar, identity)
        removed_sidecars.append(sidecar)

    _assert_identity(database, validated.database_identity)
    for sidecar in _sidecar_paths(database):
        try:
            current = _read_optional_identity(sidecar)
        except (OSError, UnexpectedDatabaseError) as exc:
            raise _changed_identity_error() from exc
        if current is not None:
            raise _changed_identity_error()
    _unlink_exact_identity(database, validated.database_identity)
    return ResetResult(
        database_path=database,
        removed_files=(database, *removed_sidecars),
        backup=backup,
    )


def _revalidate_reset_targets(validated: _ValidatedIndex) -> None:
    """Fail before deletion unless the main DB and sidecar set are unchanged."""

    _assert_identity(validated.database, validated.database_identity)
    for sidecar, expected in validated.sidecar_identities:
        try:
            current = _read_optional_identity(sidecar)
        except (OSError, UnexpectedDatabaseError) as exc:
            raise _changed_identity_error() from exc
        if current != expected:
            raise _changed_identity_error()


def _unlink_exact_identity(path: Path, expected: _FileIdentity) -> None:
    """Revalidate one target immediately before its destructive unlink."""

    _assert_identity(path, expected)
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise _changed_identity_error() from exc


def _assert_identity(path: Path, expected: _FileIdentity) -> None:
    try:
        current = _read_required_identity(path)
    except (OSError, UnexpectedDatabaseError) as exc:
        raise _changed_identity_error() from exc
    if current != expected:
        raise _changed_identity_error()


def _read_required_identity(path: Path) -> _FileIdentity:
    try:
        result = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise FileNotFoundError(f"Codex Insights database does not exist: {path}") from None
    if not stat.S_ISREG(result.st_mode):
        raise UnexpectedDatabaseError(f"Refusing non-file reset target: {path}")
    if result.st_ino == 0:
        raise UnexpectedDatabaseError(
            "Refusing reset: derived index identity cannot be verified on this filesystem."
        )
    return _FileIdentity(device=result.st_dev, inode=result.st_ino)


def _read_optional_identity(path: Path) -> _FileIdentity | None:
    try:
        return _read_required_identity(path)
    except FileNotFoundError:
        return None


def _sidecar_paths(database: Path) -> tuple[Path, Path]:
    return Path(f"{database}-wal"), Path(f"{database}-shm")


def _changed_identity_error() -> ResetTargetChangedError:
    return ResetTargetChangedError(
        "Refusing reset: derived index changed identity after validation."
    )


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    connection.row_factory = sqlite3.Row
    return connection


def _count(connection: sqlite3.Connection, query: str) -> int:
    return int(connection.execute(query).fetchone()[0])
