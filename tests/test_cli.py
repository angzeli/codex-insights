from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from codex_insights import __version__
from codex_insights.cli import app

runner = CliRunner()


def test_help_smoke() -> None:
    result = runner.invoke(app, ["--help"], env={"CODEX_HOME": "/synthetic/not-used"})

    assert result.exit_code == 0
    assert "Local-first, read-only" in result.stdout


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
