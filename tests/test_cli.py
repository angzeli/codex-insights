from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_insights import __version__
from codex_insights.adapters import CodexLocalAdapter
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home
from codex_insights.db import SCHEMA_VERSION
from codex_insights.indexer import index_source

runner = CliRunner()


def test_help_smoke() -> None:
    result = runner.invoke(app, ["--help"], env={"CODEX_HOME": "/synthetic/not-used"})

    assert result.exit_code == 0
    assert "Local-first, read-only" in result.stdout


@pytest.mark.parametrize(
    "arguments",
    (
        ("version", "--help"),
        ("doctor", "--help"),
        ("audit-source", "--help"),
        ("db-info", "--help"),
        ("index", "--help"),
        ("stats", "--help"),
        ("usage", "--help"),
        ("sessions", "--help"),
        ("session", "--help"),
        ("repos", "--help"),
        ("models", "--help"),
        ("prompts", "--help"),
        ("prompt", "--help"),
        ("search", "--help"),
        ("tools", "--help"),
        ("commands", "--help"),
        ("commits", "--help"),
        ("commit", "--help"),
        ("outcomes", "--help"),
        ("tasks", "--help"),
        ("report", "--help"),
        ("dashboard", "--help"),
        ("privacy", "--help"),
        ("export", "--help"),
        ("backup-index", "--help"),
        ("reset-index", "--help"),
    ),
)
def test_public_command_help_is_available(arguments: tuple[str, ...]) -> None:
    result = runner.invoke(app, list(arguments), env={"CODEX_HOME": "/synthetic/not-used"})

    assert result.exit_code == 0
    assert "Usage:" in result.stdout


def test_version_smoke() -> None:
    result = runner.invoke(app, ["version"], env={"CODEX_HOME": "/synthetic/not-used"})

    assert result.exit_code == 0
    assert f"Codex Insights {__version__}" in result.stdout


def test_doctor_uses_synthetic_codex_home(synthetic_codex_home: Path) -> None:
    result = runner.invoke(
        app,
        ["doctor", "--codex-home", str(synthetic_codex_home)],
        env={"CODEX_HOME": "/synthetic/lower-precedence"},
    )

    assert result.exit_code == 0
    assert "tests/fixtures" in result.stdout
    assert "Codex home (explicit option)" in result.stdout
    assert "Codex home exists" in result.stdout
    assert "Sessions" in result.stdout


def test_doctor_handles_missing_home(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    result = runner.invoke(
        app,
        ["doctor", "--codex-home", str(missing)],
        env={"CODEX_HOME": "/synthetic/lower-precedence"},
    )

    assert result.exit_code == 0
    assert "Codex home was not found" in result.stdout


def test_doctor_deep_reports_bounded_compatibility_diagnostics(
    synthetic_audit_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    source_database = synthetic_audit_home / "state_7.sqlite"
    source_before = source_database.read_bytes()
    index_source(
        CodexLocalAdapter(resolve_codex_home(synthetic_audit_home)),
        database,
        codex_home=synthetic_audit_home,
    )
    database_before = database.read_bytes()

    result = runner.invoke(
        app,
        [
            "doctor",
            "--deep",
            "--json",
            "--codex-home",
            str(synthetic_audit_home),
            "--db",
            str(database),
        ],
        env={"HOME": str(tmp_path / "unused-home")},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["database_integrity"] == "ok"
    assert payload["selected_state_database"] == "state_7.sqlite"
    assert payload["source_session_count"] == 4
    assert payload["indexed_session_count"] == 4
    assert payload["capability_coverage"]
    assert payload["parser_versions"]["source_parser"].startswith(
        "codex-source-parser-"
    )
    assert source_database.read_bytes() == source_before
    assert database.read_bytes() == database_before


def test_doctor_deep_rejects_derived_database_inside_codex_home(
    synthetic_audit_home: Path,
) -> None:
    unsafe_database = synthetic_audit_home / "derived.sqlite3"

    result = runner.invoke(
        app,
        [
            "doctor",
            "--deep",
            "--json",
            "--codex-home",
            str(synthetic_audit_home),
            "--db",
            str(unsafe_database),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["database_path_safe"] is False
    assert not unsafe_database.exists()
