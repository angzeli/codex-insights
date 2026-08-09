"""Read-only adapter for a local Codex home directory."""

from __future__ import annotations

from dataclasses import dataclass

from codex_insights.config import CodexHomeResolution
from codex_insights.discovery import CodexEnvironmentReport, inspect_codex_environment


@dataclass(frozen=True, slots=True)
class CodexLocalAdapter:
    """Probe Codex local state without parsing session data."""

    resolution: CodexHomeResolution

    @property
    def name(self) -> str:
        return "codex-local"

    def probe(self) -> CodexEnvironmentReport:
        return inspect_codex_environment(self.resolution)
