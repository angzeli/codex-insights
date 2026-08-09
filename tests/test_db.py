from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codex_insights.db import (
    UnsafeDatabasePathError,
    ensure_index_outside_codex_home,
    open_source_sqlite_readonly,
)


def test_index_cannot_live_inside_codex_home(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"

    with pytest.raises(UnsafeDatabasePathError):
        ensure_index_outside_codex_home(codex_home / "insights.sqlite3", codex_home)


def test_source_sqlite_connection_is_query_only(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-state.sqlite"
    with sqlite3.connect(source) as writable:
        writable.execute("CREATE TABLE example (value TEXT)")

    with (
        open_source_sqlite_readonly(source) as readonly,
        pytest.raises(sqlite3.OperationalError),
    ):
        readonly.execute("INSERT INTO example VALUES ('unsafe')")
