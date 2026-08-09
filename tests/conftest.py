"""Shared synthetic test paths; real Codex history is never used."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def synthetic_codex_home() -> Path:
    return Path(__file__).parent / "fixtures" / "codex_home"
