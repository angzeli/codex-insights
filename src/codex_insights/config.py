"""Configuration and path resolution without touching Codex-owned state."""

from __future__ import annotations

import os
import sys
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


def default_index_path(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> Path:
    """Return the platform data-directory path for the analyzer database."""

    user_home = Path.home() if home is None else home
    environment = os.environ if environ is None else environ
    platform = sys.platform if platform_name is None else platform_name

    if platform == "darwin":
        data_directory = user_home / "Library" / "Application Support" / "Codex Insights"
    elif platform == "win32":
        configured = environment.get("LOCALAPPDATA")
        data_directory = (
            Path(configured) / "Codex Insights"
            if configured
            else user_home / "AppData" / "Local" / "Codex Insights"
        )
    else:
        configured = environment.get("XDG_DATA_HOME")
        data_directory = (
            Path(configured) / "codex-insights"
            if configured
            else user_home / ".local" / "share" / "codex-insights"
        )

    return _normalized(data_directory / "index.sqlite3")


def resolve_index_path(explicit: Path | None = None) -> Path:
    """Resolve an explicit database path or the platform-aware default."""

    return _normalized(explicit) if explicit is not None else default_index_path()


def default_config_path(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> Path:
    """Return the platform configuration path for Codex Insights itself."""

    user_home = Path.home() if home is None else home
    environment = os.environ if environ is None else environ
    platform = sys.platform if platform_name is None else platform_name

    if platform == "darwin":
        directory = user_home / "Library" / "Application Support" / "Codex Insights"
    elif platform == "win32":
        configured = environment.get("LOCALAPPDATA")
        directory = (
            Path(configured) / "Codex Insights"
            if configured
            else user_home / "AppData" / "Local" / "Codex Insights"
        )
    else:
        configured = environment.get("XDG_CONFIG_HOME")
        directory = (
            Path(configured) / "codex-insights"
            if configured
            else user_home / ".config" / "codex-insights"
        )
    return _normalized(directory / "config.json")


def resolve_config_path(explicit: Path | None = None) -> Path:
    """Resolve an explicit privacy-config path or the platform-aware default."""

    return _normalized(explicit) if explicit is not None else default_config_path()
