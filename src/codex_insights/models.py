"""Stable internal models kept independent of Codex's local storage format."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class SessionOutcome(StrEnum):
    """A deliberately conservative outcome classification."""

    SUCCESS = "success"
    SUCCESS_WITH_WARNINGS = "success_with_warnings"
    PARTIAL = "partial"
    FAILED = "failed"
    ABANDONED = "abandoned"
    NO_CHANGE = "no_change"
    UNKNOWN = "unknown"


class UsageSemantics(StrEnum):
    """How per-session token totals were derived from source records."""

    CUMULATIVE_TOTAL = "cumulative_total"
    SUMMED_EVENT_DELTAS = "summed_event_deltas"
    UNAVAILABLE = "unavailable"


class DeduplicationStatus(StrEnum):
    """Explain how a child thread contributes to cross-thread aggregates."""

    INHERITED_EXACT = "inherited_exact"
    INHERITED_PREFIX = "inherited_prefix"
    INDEPENDENT = "independent"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"
    CYCLE = "cycle"


class DeduplicationConfidence(StrEnum):
    """Conservative confidence attached to one lineage assessment."""

    HIGH = "high"
    EXPLICIT = "explicit"
    NONE = "none"


class DeltaConsistency(StrEnum):
    """Whether post-baseline turn deltas corroborate a cumulative difference."""

    EXACT = "exact"
    MISMATCH = "mismatch"
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


class EventFamily(StrEnum):
    """Semantic rollout families retained for provenance, without raw payloads."""

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    INTER_AGENT_MESSAGE = "inter_agent_message"
    TOOL_CALL = "tool_call"
    TOOL_OUTPUT = "tool_output"
    SHELL_COMMAND = "shell_command"
    VALIDATION_COMMAND = "validation_command"
    GIT_COMMAND = "git_command"
    PATCH_EDIT = "patch_edit"
    PATCH_RESULT = "patch_result"
    TASK_LIFECYCLE = "task_lifecycle"
    ERROR = "error"


class EventProvenanceStatus(StrEnum):
    """Whether an observed semantic event can be attributed to a thread."""

    ORIGIN = "origin"
    INHERITED_EXACT = "inherited_exact"
    INHERITED_PREFIX = "inherited_prefix"
    OBSERVED_DUPLICATE = "observed_duplicate"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class ToolFamily(StrEnum):
    """Stable semantic families for normalized tool operations."""

    SHELL = "shell"
    PATCH = "patch"
    COLLABORATION = "collaboration"
    USER_INTERACTION = "user_interaction"
    FILE = "file"
    NETWORK = "network"
    OTHER = "other"
    UNKNOWN = "unknown"


class CommandCategory(StrEnum):
    """Deterministic command categories used by additive tool analytics."""

    GIT_INSPECTION = "git_inspection"
    GIT_MUTATION = "git_mutation"
    TESTING = "testing"
    LINTING = "linting"
    TYPE_CHECKING = "type_checking"
    BUILD_PACKAGING = "build_packaging"
    FILESYSTEM_INSPECTION = "filesystem_inspection"
    TEXT_SEARCH = "text_search"
    PYTHON_EXECUTION = "python_execution"
    DEPENDENCY_MANAGEMENT = "dependency_management"
    EDITING_PATCHING = "editing_patching"
    SCIENTIFIC_COMPUTATION = "scientific_computation"
    PROCESS_STATUS_MONITORING = "process_status_monitoring"
    WAIT_POLL = "wait_poll"
    USER_INTERACTION = "user_interaction"
    OTHER = "other"
    UNKNOWN = "unknown"


class TestScope(StrEnum):
    """Conservative syntax-derived scope for validation commands."""

    FULL_SUITE = "full_suite"
    FILE = "file"
    SUBSET = "subset"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ToolResultStatus(StrEnum):
    """Result state when a normalized output exposes enough evidence."""

    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class SourceCapability(StrEnum):
    """Source features detected without assuming that absence means zero activity."""

    SESSION_CATALOGUE = "session_catalogue_metadata"
    ROLLOUT_METADATA = "rollout_metadata"
    TOKEN_USAGE = "token_usage"
    TOKEN_LINEAGE = "token_lineage_evidence"
    PROMPT_CONTENT = "prompt_content"
    EVENT_PROVENANCE = "event_provenance"
    TOOL_ACTIVITY = "tool_call_activity"
    COMMAND_EXTRACTION = "command_extraction"
    GIT_METADATA = "git_metadata"
    REPOSITORY_ATTRIBUTION = "repository_attribution"
    MODEL_ATTRIBUTION = "model_attribution"
    PROVENANCE_MATCHING = "event_provenance_matching"
    TASK_LIFECYCLE = "task_lifecycle"
    ARCHIVE_METADATA = "archive_metadata"
    DURATION_TIMESTAMPS = "duration_timestamps"


class CapabilityStatus(StrEnum):
    """Evidence state for one source capability in one session."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    NOT_OBSERVED = "not_observed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CapabilityObservation:
    """Bounded evidence that a source feature is or is not observable."""

    capability: SourceCapability
    status: CapabilityStatus
    evidence_count: int = 0
    evidence_type: str = ""


@dataclass(frozen=True, slots=True)
class UnknownSourceObservation:
    """Counted source-format metadata that deliberately excludes raw payloads."""

    kind: str
    name: str
    count: int
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SourceSemanticWarning:
    """A conservative warning that a recognized source shape changed behavior."""

    code: str
    count: int
    detail: str


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
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    token_update_count: int = 0


@dataclass(frozen=True, slots=True)
class UsageVector:
    """One content-free vector from Codex token telemetry."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class NormalizedTokenSnapshot:
    """A cumulative vector and optional same-record last-turn delta."""

    cumulative: UsageVector
    last_turn: UsageVector | None = None


@dataclass(frozen=True, slots=True)
class NormalizedThreadRelationship:
    """Explicit source relationship kept independent of source table names."""

    parent_source_session_id: str
    child_source_session_id: str
    source_type: str
    source_home: Path
    relationship_type: str = "spawn"
    source_status: str | None = None
    source_db_path: Path | None = None


@dataclass(frozen=True, slots=True)
class TokenLineageAssessment:
    """Explainable accounting result for one explicit parent-child edge."""

    status: DeduplicationStatus
    confidence: DeduplicationConfidence
    evidence_type: str
    matched_snapshot_count: int = 0
    parent_sequence_start: int | None = None
    inherited_baseline: UsageVector | None = None
    incremental_usage: UsageVector | None = None
    delta_consistency: DeltaConsistency = DeltaConsistency.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class NormalizedEventCount:
    """Count of one normalized event category without raw content."""

    category: EventCategory
    count: int


@dataclass(frozen=True, slots=True)
class NormalizedEventObservation:
    """Content-free identity for one selected semantic record in a rollout."""

    source_ordinal: int
    family_ordinal: int
    family: EventFamily
    fingerprint: str
    source_record_type: str
    source_payload_type: str
    occurred_at: datetime | None = None
    stable_id_digest: str | None = None
    approximate_content_length: int | None = None
    fingerprint_version: str = ""


@dataclass(frozen=True, slots=True)
class NormalizedPromptCandidate:
    """Transient user-message content; persisted only after privacy filtering."""

    source_ordinal: int
    fingerprint: str
    occurred_at: datetime | None
    text: str


@dataclass(frozen=True, slots=True)
class NormalizedToolCallCandidate:
    """Privacy-filtered tool metadata derived behind the source adapter boundary."""

    source_ordinal: int
    operation_ordinal: int
    occurred_at: datetime | None
    call_id_digest: str | None
    tool_family: ToolFamily
    tool_name: str
    command_category: CommandCategory
    command_text: str | None = None
    command_fingerprint: str | None = None
    executable: str | None = None
    test_scope: TestScope = TestScope.NOT_APPLICABLE
    redacted: bool = False
    truncated: bool = False
    extraction_version: str = ""
    classifier_version: str = ""


@dataclass(frozen=True, slots=True)
class NormalizedToolResultCandidate:
    """Bounded result metadata correlated by a digested source call identifier."""

    source_ordinal: int
    call_id_digest: str | None
    status: ToolResultStatus
    exit_code: int | None = None
    duration_seconds: float | None = None
    git_commit_hash: str | None = None
    git_commit_abbrev: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedSourceSession:
    """Stable source-session representation consumed by the indexer and database."""

    source_session_id: str
    source_type: str
    source_home: Path
    client_source: str | None = None
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
    file_identity: str | None = None
    source_schema_fingerprint: str = ""
    source_schema_hints: tuple[str, ...] = field(default_factory=tuple)
    capabilities: tuple[CapabilityObservation, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ParsedSourceSession:
    """Normalized parse result with only structural warning counts."""

    session: NormalizedSourceSession
    malformed_line_count: int = 0
    oversized_line_count: int = 0
    parsed_byte_count: int = 0
    valid_record_count: int = 0
    partial_final_line: bool = False
    source_file_identity: str | None = None
    source_schema_fingerprint: str = ""
    source_schema_hints: tuple[str, ...] = field(default_factory=tuple)
    capabilities: tuple[CapabilityObservation, ...] = field(default_factory=tuple)
    unknown_source_records: tuple[UnknownSourceObservation, ...] = field(
        default_factory=tuple
    )
    semantic_warnings: tuple[SourceSemanticWarning, ...] = field(default_factory=tuple)
    token_snapshots: tuple[NormalizedTokenSnapshot, ...] = field(default_factory=tuple)
    event_observations: tuple[NormalizedEventObservation, ...] = field(default_factory=tuple)
    prompt_candidates: tuple[NormalizedPromptCandidate, ...] = field(default_factory=tuple)
    tool_call_candidates: tuple[NormalizedToolCallCandidate, ...] = field(default_factory=tuple)
    tool_result_candidates: tuple[NormalizedToolResultCandidate, ...] = field(
        default_factory=tuple
    )
