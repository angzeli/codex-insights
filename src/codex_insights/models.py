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


class UsageSemantics(StrEnum):
    """How per-session token totals were derived from source records."""

    CUMULATIVE_TOTAL = "cumulative_total"
    SUMMED_EVENT_DELTAS = "summed_event_deltas"
    UNAVAILABLE = "unavailable"


class EventCategory(StrEnum):
    """Stable event categories that intentionally omit raw event payloads."""

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    SHELL_COMMAND = "shell_command"
    PATCH_EDIT = "patch_edit"
    TOKEN_UPDATE = "token_update"
    ERROR = "error"
    UNKNOWN = "unknown"


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


@dataclass(frozen=True, slots=True)
class NormalizedUsage:
    """Per-session totals with explicit cumulative-versus-delta semantics."""

    semantics: UsageSemantics = UsageSemantics.UNAVAILABLE
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    token_update_count: int = 0


@dataclass(frozen=True, slots=True)
class NormalizedEventCount:
    """Count of one normalized event category without raw content."""

    category: EventCategory
    count: int


@dataclass(frozen=True, slots=True)
class NormalizedSourceSession:
    """Stable source-session representation consumed by the indexer and database."""

    source_session_id: str
    source_type: str
    source_home: Path
    started_at: datetime | None = None
    updated_at: datetime | None = None
    apparent_ended_at: datetime | None = None
    source_timezone_offset_minutes: int | None = None
    cwd: Path | None = None
    repository_root: Path | None = None
    repository_name: str | None = None
    git_branch: str | None = None
    git_sha: str | None = None
    git_origin_url: str | None = None
    model: str | None = None
    model_provider: str | None = None
    codex_version: str | None = None
    archived: bool = False
    rollout_path: Path | None = None
    source_db_path: Path | None = None
    source_path: Path | None = None
    usage: NormalizedUsage = field(default_factory=NormalizedUsage)
    event_counts: tuple[NormalizedEventCount, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SourceSessionCandidate:
    """Adapter-neutral catalogue row plus inexpensive source file identity."""

    session: NormalizedSourceSession
    source_schema_version: str
    rollout_exists: bool
    rollout_allowed: bool
    size_bytes: int | None = None
    mtime_ns: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedSourceSession:
    """Normalized parse result with only structural warning counts."""

    session: NormalizedSourceSession
    malformed_line_count: int = 0
    oversized_line_count: int = 0
    parsed_byte_count: int = 0
