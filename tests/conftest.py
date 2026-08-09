"""Shared synthetic test paths; real Codex history is never used."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest


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
