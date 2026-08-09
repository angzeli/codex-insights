"""Adapter contract separating Codex storage details from internal models."""

from __future__ import annotations

from typing import Protocol

from codex_insights.adapters.audit_models import SourceAuditResult
from codex_insights.discovery import CodexEnvironmentReport


class SourceAdapter(Protocol):
    """Minimal source contract; ingestion can grow behind this boundary."""

    @property
    def name(self) -> str:
        """Return a stable adapter identifier."""

    def probe(self) -> CodexEnvironmentReport:
        """Return bounded source metadata without ingesting histories."""

    def audit(self, *, sample_size: int = 5, verbose: bool = False) -> SourceAuditResult:
        """Return bounded source schema observations without raw content."""
