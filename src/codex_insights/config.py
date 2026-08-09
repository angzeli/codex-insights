"""Configuration and path resolution without touching Codex-owned state."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class CodexHomeSource(StrEnum):
    """Where the selected Codex home path came from."""

    EXPLICIT = "explicit option"
    ENVIRONMENT = "CODEX_HOME"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class CodexHomeResolution:
    """A resolved Codex home path and the source that selected it."""

    path: Path
    source: CodexHomeSource


def _normalized(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def resolve_codex_home(
    explicit: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> CodexHomeResolution:
    """Resolve Codex home using explicit option, environment, then ``~/.codex``."""

    environment = os.environ if environ is None else environ
    if explicit is not None:
        return CodexHomeResolution(_normalized(explicit), CodexHomeSource.EXPLICIT)

    configured = environment.get("CODEX_HOME")
    if configured:
        return CodexHomeResolution(_normalized(Path(configured)), CodexHomeSource.ENVIRONMENT)

    user_home = Path.home() if home is None else home
    return CodexHomeResolution(_normalized(user_home / ".codex"), CodexHomeSource.DEFAULT)


def default_index_path(*, home: Path | None = None) -> Path:
    """Return a default analyzer index path outside the Codex home."""

    user_home = Path.home() if home is None else home
    return _normalized(user_home / ".local" / "share" / "codex-insights" / "index.sqlite3")
