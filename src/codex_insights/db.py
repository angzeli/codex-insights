"""SQLite access with a hard boundary between source state and analyzer state."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class UnsafeDatabasePathError(ValueError):
    """Raised when an analyzer database would be placed inside Codex home."""


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def ensure_index_outside_codex_home(index_path: Path, codex_home: Path) -> None:
    """Reject analyzer database paths that overlap Codex-owned storage."""

    index = _resolved(index_path)
    source = _resolved(codex_home)
    if index == source or source in index.parents:
        raise UnsafeDatabasePathError(
            f"Analyzer database must be outside Codex home: {source}"
        )


def connect_index(index_path: Path, *, codex_home: Path) -> sqlite3.Connection:
    """Open the writable Codex Insights index after enforcing path separation."""

    ensure_index_outside_codex_home(index_path, codex_home)
    destination = _resolved(index_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(destination)


def open_source_sqlite_readonly(source_path: Path) -> sqlite3.Connection:
    """Open a Codex-owned SQLite file in explicit read-only and query-only modes."""

    source = _resolved(source_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    connection = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection
