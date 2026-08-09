"""Read-only adapter for a local Codex home directory."""

from __future__ import annotations

from dataclasses import dataclass

from codex_insights.adapters.audit_models import SourceAuditResult
from codex_insights.adapters.codex_audit import audit_codex_source
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

    def audit(self, *, sample_size: int = 5, verbose: bool = False) -> SourceAuditResult:
        """Run a bounded, read-only schema audit of this Codex home."""

        return audit_codex_source(
            self.resolution.path,
            sample_size=sample_size,
            verbose=verbose,
        )
