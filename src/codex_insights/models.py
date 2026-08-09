"""Stable internal models kept independent of Codex's local storage format."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class SessionOutcome(StrEnum):
    """A deliberately conservative outcome classification."""

    UNKNOWN = "unknown"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    ABANDONED = "abandoned"
    REWORKED = "reworked"


@dataclass(frozen=True, slots=True)
class ToolUsage:
    """Aggregate tool usage without captured stdout, stderr, or arguments."""

    tool_name: str
    invocation_count: int


@dataclass(frozen=True, slots=True)
class NormalizedSession:
    """Format-independent session metadata suitable for indexing and analytics."""

    session_id: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    working_directory: Path | None = None
    repository: Path | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    outcome: SessionOutcome = SessionOutcome.UNKNOWN
    tool_usage: tuple[ToolUsage, ...] = field(default_factory=tuple)
