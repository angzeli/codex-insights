"""Read-only adapter for a local Codex home directory."""

from __future__ import annotations

from dataclasses import dataclass

from codex_insights.adapters.audit_models import SourceAuditResult
from codex_insights.adapters.codex_audit import audit_codex_source
from codex_insights.adapters.codex_index import (
    PARSER_VERSION,
    discover_session_candidates,
    discover_thread_relationships,
    parse_rollout,
)
from codex_insights.config import CodexHomeResolution
from codex_insights.discovery import CodexEnvironmentReport, inspect_codex_environment
from codex_insights.models import (
    NormalizedThreadRelationship,
    ParsedSourceSession,
    SourceSessionCandidate,
)


@dataclass(frozen=True, slots=True)
class CodexLocalAdapter:
    """Probe Codex local state without parsing session data."""

    resolution: CodexHomeResolution

    @property
    def name(self) -> str:
        return "codex-local"

    @property
    def parser_version(self) -> str:
        """Version of the source-to-normalized mapping used for incrementality."""

        return PARSER_VERSION

    def probe(self) -> CodexEnvironmentReport:
        return inspect_codex_environment(self.resolution)

    def audit(self, *, sample_size: int = 5, verbose: bool = False) -> SourceAuditResult:
        """Run a bounded, read-only schema audit of this Codex home."""

        return audit_codex_source(
            self.resolution.path,
            sample_size=sample_size,
            verbose=verbose,
        )

    def discover_sessions(
        self,
    ) -> tuple[tuple[SourceSessionCandidate, ...], tuple[str, ...]]:
        """Return normalized catalogue rows without parsing rollout contents."""

        return discover_session_candidates(self.resolution.path, source_type=self.name)

    def parse_session(self, candidate: SourceSessionCandidate) -> ParsedSourceSession:
        """Stream and normalize the rollout associated with one catalogue row."""

        return parse_rollout(candidate)

    def discover_relationships(
        self,
    ) -> tuple[tuple[NormalizedThreadRelationship, ...], tuple[str, ...]]:
        """Return explicit source thread relationships without reading transcripts."""

        return discover_thread_relationships(self.resolution.path, source_type=self.name)
