"""Adapter contract separating Codex storage details from internal models."""

from __future__ import annotations

from typing import Protocol

from codex_insights.adapters.audit_models import SourceAuditResult
from codex_insights.discovery import CodexEnvironmentReport
from codex_insights.models import ParsedSourceSession, SourceSessionCandidate


class SourceChangedDuringParseError(RuntimeError):
    """A source adapter detected that a live input changed during parsing."""


class SourceAdapter(Protocol):
    """Minimal source contract; ingestion can grow behind this boundary."""

    @property
    def name(self) -> str:
        """Return a stable adapter identifier."""

    @property
    def parser_version(self) -> str:
        """Return the normalization parser version for incremental invalidation."""

    def probe(self) -> CodexEnvironmentReport:
        """Return bounded source metadata without ingesting histories."""

    def audit(self, *, sample_size: int = 5, verbose: bool = False) -> SourceAuditResult:
        """Return bounded source schema observations without raw content."""

    def discover_sessions(
        self,
    ) -> tuple[tuple[SourceSessionCandidate, ...], tuple[str, ...]]:
        """Return normalized catalogue candidates and aggregate warnings."""

    def parse_session(self, candidate: SourceSessionCandidate) -> ParsedSourceSession:
        """Stream one candidate into normalized metadata and aggregate counts."""
