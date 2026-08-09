"""Adapter contract separating Codex storage details from internal models."""

from __future__ import annotations

from typing import Protocol

from codex_insights.discovery import CodexEnvironmentReport


class SourceAdapter(Protocol):
    """Minimal source contract; ingestion can grow behind this boundary."""

    @property
    def name(self) -> str:
        """Return a stable adapter identifier."""

    def probe(self) -> CodexEnvironmentReport:
        """Return bounded source metadata without ingesting histories."""
