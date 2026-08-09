"""Bounded, metadata-only discovery of likely Codex locations."""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from codex_insights.config import CodexHomeResolution


@dataclass(frozen=True, slots=True)
class LocationStatus:
    """Existence metadata for one known location; contents are never read here."""

    label: str
    path: Path
    exists: bool


@dataclass(frozen=True, slots=True)
class CodexEnvironmentReport:
    """Safe runtime and path metadata returned by the doctor command."""

    python_version: str
    platform: str
    codex_home: CodexHomeResolution
    codex_home_exists: bool
    locations: tuple[LocationStatus, ...]


_LIKELY_LOCATIONS = (
    ("Sessions", Path("sessions")),
    ("Archived sessions", Path("archived_sessions")),
    ("State database", Path("state_5.sqlite")),
    ("Legacy state database", Path("state.sqlite")),
)


def inspect_codex_environment(resolution: CodexHomeResolution) -> CodexEnvironmentReport:
    """Inspect only runtime metadata and existence of a fixed, bounded path list."""

    locations = tuple(
        LocationStatus(
            label=label,
            path=resolution.path / relative,
            exists=(resolution.path / relative).exists(),
        )
        for label, relative in _LIKELY_LOCATIONS
    )
    return CodexEnvironmentReport(
        python_version=".".join(str(part) for part in sys.version_info[:3]),
        platform=platform.platform(),
        codex_home=resolution,
        codex_home_exists=resolution.path.exists(),
        locations=locations,
    )
