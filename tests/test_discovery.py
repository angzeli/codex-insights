from __future__ import annotations

from pathlib import Path

from codex_insights.config import resolve_codex_home
from codex_insights.discovery import inspect_codex_environment


def test_missing_codex_home_is_reported_without_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing-codex-home"

    report = inspect_codex_environment(resolve_codex_home(missing))

    assert report.codex_home.path == missing
    assert report.codex_home_exists is False
    assert all(location.exists is False for location in report.locations)
