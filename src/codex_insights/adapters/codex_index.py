"""Version-tolerant Codex catalogue and rollout normalization."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codex_insights.adapters.base import SourceChangedDuringParseError
from codex_insights.adapters.codex_events import extract_event
from codex_insights.db import open_source_sqlite_readonly
from codex_insights.models import (
    CapabilityObservation,
    CapabilityStatus,
    ClientKind,
    EventCategory,
    EventFamily,
    NormalizedEventCount,
    NormalizedEventObservation,
    NormalizedPromptCandidate,
    NormalizedSourceSession,
    NormalizedThreadRelationship,
    NormalizedTokenEvent,
    NormalizedTokenSnapshot,
    NormalizedToolCallCandidate,
    NormalizedToolResultCandidate,
    NormalizedUsage,
    ParsedSourceSession,
    SourceCapability,
    SourceSemanticWarning,
    SourceSessionCandidate,
    SubagentSourceKind,
    UnknownSourceObservation,
    UsageSemantics,
    UsageVector,
)

PARSER_VERSION = "codex-source-parser-v11"
SOURCE_SCHEMA_FINGERPRINT_VERSION = "codex-source-schema-v1"
MAX_ROLLOUT_LINE_BYTES = 1024 * 1024
MAX_STATE_REFERENCE_CHECKS = 1_000
MAX_UNKNOWN_NAMES_PER_KIND = 128

_CLI_SOURCES = {"cli", "terminal", "command_line"}
_EDITOR_SOURCES = {
    "editor",
    "vscode",
    "visual_studio_code",
    "cursor",
    "jetbrains",
    "pycharm",
}

_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "thread_id", "session_id", "conversation_id"),
    "rollout_path": ("rollout_path", "session_path", "file_path", "path"),
    "created_at": ("created_at", "started_at", "start_time"),
    "updated_at": ("updated_at", "recency_at", "last_updated_at", "modified_at"),
    "archived_at": ("archived_at", "ended_at", "completed_at"),
    "cwd": ("cwd", "working_directory", "workdir"),
    "model": ("model", "model_name"),
    "model_provider": ("model_provider", "provider"),
    "client_source": ("source", "thread_source", "client_source"),
    "archived": ("archived", "is_archived"),
    "git_branch": ("git_branch", "branch"),
    "git_sha": ("git_sha", "git_commit", "commit_sha"),
    "git_origin_url": ("git_origin_url", "origin_url", "repository_url"),
}
_RELATIONSHIP_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "parent": ("parent_thread_id", "parent_session_id", "parent_id"),
    "child": ("child_thread_id", "child_session_id", "child_id"),
    "status": ("status", "state"),
}

_STRUCTURAL_RECORD_TYPES = {
    "session",
    "session_meta",
    "turn_context",
    "world_state",
    "compacted",
    "context_compacted",
    "inter_agent_communication_metadata",
    "task_started",
    "task_complete",
    "thread_settings_applied",
}
_NON_CONTENT_PAYLOAD_TYPES = {
    "reasoning",
    "agent_reasoning",
    "function_call_output",
    "custom_tool_call_output",
}

_KNOWN_RECORD_TYPES = _STRUCTURAL_RECORD_TYPES | {
    "event_msg",
    "response_item",
    "usage",
    "user_message",
    "tool_call",
}
_KNOWN_PAYLOAD_TYPES = _STRUCTURAL_RECORD_TYPES | _NON_CONTENT_PAYLOAD_TYPES | {
    "agent_message",
    "assistant_message",
    "custom_tool_call",
    "function_call",
    "message",
    "patch_apply_begin",
    "patch_apply_end",
    "token_count",
    "tool_call",
    "user_message",
}
_KNOWN_TOP_LEVEL_FIELDS = {
    "event",
    "id",
    "kind",
    "name",
    "payload",
    "timestamp",
    "tool_name",
    "type",
    "usage",
}
_KNOWN_PAYLOAD_FIELDS = {
    "arguments",
    "call_id",
    "cli_version",
    "codex_version",
    "content",
    "cwd",
    "duration_ms",
    "exit_code",
    "id",
    "info",
    "message",
    "model",
    "model_provider",
    "name",
    "output",
    "provider",
    "role",
    "status",
    "text",
    "tool_name",
    "type",
    "usage",
    "version",
}
_RECOGNIZED_IGNORED_FIELDS = {
    "encrypted_content",
    "input",
    "memory_citation",
    "metadata",
    "phase",
    "rate_limits",
    "stderr",
    "stdout",
    "summary",
    "turn_id",
}
_RECOGNIZED_IGNORED_PAYLOAD_TYPES = {"turn_aborted"}
_LIFECYCLE_GAP_TYPES = {
    "item-completed",
    "item_completed",
    "thread-rolled-back",
    "thread_rolled_back",
}


@dataclass(frozen=True, slots=True)
class StateDatabaseAssessment:
    """Bounded evidence used to select one Codex state database."""

    path: Path
    readable: bool
    catalogue_table: str | None
    relationship_table: str | None
    row_count: int | None
    rollout_references_checked: int
    valid_rollout_references: int
    missing_rollout_references: int
    schema_fingerprint: str
    schema_hints: tuple[str, ...]
    score: int
    modified_ns: int
    reasons: tuple[str, ...]
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class StateDatabaseSelection:
    """Deterministic state database selection plus viable alternatives."""

    selected: StateDatabaseAssessment | None
    candidates: tuple[StateDatabaseAssessment, ...]

    @property
    def explanation(self) -> str:
        if self.selected is None:
            return "No compatible readable state database was found."
        item = self.selected
        return (
            f"Selected {item.path.name} with score {item.score}: "
            + "; ".join(item.reasons)
        )


def discover_session_candidates(
    codex_home: Path,
    *,
    source_type: str,
) -> tuple[tuple[SourceSessionCandidate, ...], tuple[str, ...]]:
    """Load recognized session catalogue metadata from versioned state databases."""

    home = codex_home.expanduser().resolve(strict=False)
    if not home.is_dir():
        return (), ("Codex home does not exist; no sessions were discovered.",)

    selection = select_state_database(home)
    warnings = _selection_warnings(selection)
    selected = selection.selected
    if selected is None or selected.catalogue_table is None:
        if not selection.candidates:
            warnings.append("No versioned Codex state database was found.")
        else:
            warnings.append("No recognized Codex session catalogue was found.")
        return (), tuple(warnings)

    database_path = selected.path
    try:
        with closing(open_source_sqlite_readonly(database_path)) as connection:
            connection.row_factory = sqlite3.Row
            candidates: list[SourceSessionCandidate] = []
            for columns, row in _catalogue_rows(connection, selected.catalogue_table):
                try:
                    candidates.append(
                        _candidate_from_row(
                            row,
                            columns,
                            home=home,
                            source_type=source_type,
                            database_path=database_path,
                            table=selected.catalogue_table,
                            schema_fingerprint=selected.schema_fingerprint,
                            schema_hints=selected.schema_hints,
                        )
                    )
                except (OSError, ValueError):
                    warnings.append(f"One row in {database_path.name} could not be normalized.")
            return tuple(candidates), tuple(warnings)
    except (OSError, sqlite3.DatabaseError) as exc:
        warnings.append(f"Could not read {database_path.name}: {type(exc).__name__}.")
        return (), tuple(warnings)


def discover_thread_relationships(
    codex_home: Path,
    *,
    source_type: str,
) -> tuple[tuple[NormalizedThreadRelationship, ...], tuple[str, ...]]:
    """Read explicit spawn edges without depending on one state database version."""

    home = codex_home.expanduser().resolve(strict=False)
    if not home.is_dir():
        return (), ()
    selection = select_state_database(home)
    warnings = _selection_warnings(selection)
    selected_database = selection.selected
    if selected_database is None:
        return (), tuple(warnings)
    database_path = selected_database.path
    try:
        with closing(open_source_sqlite_readonly(database_path)) as connection:
                connection.row_factory = sqlite3.Row
                table = _recognize_relationship_table(connection)
                if table is None:
                    return (), tuple(warnings)
                declared = {
                    str(row[1]).lower(): str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
                }
                selected = {
                    concept: next(
                        (declared[alias] for alias in aliases if alias in declared),
                        "",
                    )
                    for concept, aliases in _RELATIONSHIP_COLUMN_ALIASES.items()
                }
                columns = [column for column in selected.values() if column]
                query = ", ".join(_quote(column) for column in columns)
                relationships: list[NormalizedThreadRelationship] = []
                for row in connection.execute(f"SELECT {query} FROM {_quote(table)}"):
                    parent = _short_text(row[selected["parent"]])
                    child = _short_text(row[selected["child"]])
                    if not parent or not child:
                        warnings.append(
                            f"One relationship row in {database_path.name} lacked identifiers."
                        )
                        continue
                    status_column = selected["status"]
                    relationships.append(
                        NormalizedThreadRelationship(
                            parent_source_session_id=parent,
                            child_source_session_id=child,
                            source_type=source_type,
                            source_home=home,
                            source_status=(
                                _short_text(row[status_column]) if status_column else None
                            ),
                            source_db_path=database_path,
                        )
                    )
                relationships.sort(
                    key=lambda item: (
                        item.parent_source_session_id,
                        item.child_source_session_id,
                    )
                )
                return tuple(relationships), tuple(warnings)
    except (OSError, sqlite3.DatabaseError) as exc:
        warnings.append(
            f"Could not read thread relationships from {database_path.name}: "
            f"{type(exc).__name__}."
        )
    return (), tuple(warnings)


def parse_rollout(candidate: SourceSessionCandidate) -> ParsedSourceSession:
    """Stream one rollout into normalized metadata, usage, and event counts."""

    path = candidate.session.source_path
    if path is None or not candidate.rollout_allowed or not candidate.rollout_exists:
        raise FileNotFoundError(path)

    before = path.stat()

    session = candidate.session
    event_counts: Counter[EventCategory] = Counter()
    latest_cumulative: dict[str, int | None] | None = None
    summed_deltas = _empty_usage()
    token_update_count = 0
    token_snapshots: list[NormalizedTokenSnapshot] = []
    token_events: list[NormalizedTokenEvent] = []
    event_observations: list[NormalizedEventObservation] = []
    prompt_candidates: list[NormalizedPromptCandidate] = []
    tool_call_candidates: list[NormalizedToolCallCandidate] = []
    tool_result_candidates: list[NormalizedToolResultCandidate] = []
    family_ordinals: Counter[EventFamily] = Counter()
    malformed = 0
    oversized = 0
    parsed_bytes = 0
    valid_records = 0
    partial_final_line = False
    record_types: Counter[str] = Counter()
    payload_types: Counter[str] = Counter()
    top_level_fields: set[str] = set()
    payload_fields: set[str] = set()
    unknown_counts: Counter[tuple[str, str]] = Counter()
    unknown_first: dict[tuple[str, str], datetime] = {}
    unknown_last: dict[tuple[str, str], datetime] = {}
    apparent_end = session.apparent_ended_at
    started_at = session.started_at
    source_offset = session.source_timezone_offset_minutes
    cwd = session.cwd
    model = session.model
    model_provider = session.model_provider
    codex_version = session.codex_version

    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _stat_identity(opened) != _stat_identity(before):
            raise SourceChangedDuringParseError("source_replaced_before_open")
        for source_ordinal, line in enumerate(handle):
            parsed_bytes += len(line)
            if len(line) > MAX_ROLLOUT_LINE_BYTES:
                oversized += 1
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                if not line.endswith(b"\n") and parsed_bytes == before.st_size:
                    partial_final_line = True
                    continue
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            valid_records += 1

            timestamp, offset = _parse_timestamp(record.get("timestamp"))
            if timestamp is not None:
                apparent_end = timestamp if apparent_end is None else max(apparent_end, timestamp)
                if started_at is None:
                    started_at = timestamp
                if source_offset is None:
                    source_offset = offset

            payload = record.get("payload")
            payload_map = payload if isinstance(payload, dict) else {}
            record_type = _record_type(record)
            payload_type = (_short_text(payload_map.get("type")) or "").lower()
            if record_type:
                record_types[record_type] += 1
            if payload_type:
                payload_types[payload_type] += 1
            top_level_fields.update(_safe_schema_name(str(key)) for key in record)
            payload_fields.update(_safe_schema_name(str(key)) for key in payload_map)
            _observe_unknown_shapes(
                record,
                payload_map,
                record_type=record_type,
                payload_type=payload_type,
                occurred_at=timestamp,
                counts=unknown_counts,
                first_seen=unknown_first,
                last_seen=unknown_last,
            )
            if record_type == "session_meta":
                cwd = cwd or _path_value(payload_map.get("cwd"))
                model = model or _short_text(payload_map.get("model"))
                model_provider = model_provider or _short_text(
                    payload_map.get("model_provider") or payload_map.get("provider")
                )
                codex_version = codex_version or _short_text(
                    payload_map.get("codex_version")
                    or payload_map.get("cli_version")
                    or payload_map.get("version")
                )

            extracted = extract_event(
                record,
                source_ordinal=source_ordinal,
                family_ordinal=0,
                occurred_at=timestamp,
            )
            if extracted is not None:
                family = extracted.observation.family
                extracted = replace(
                    extracted,
                    observation=replace(
                        extracted.observation,
                        family_ordinal=family_ordinals[family],
                    ),
                )
                family_ordinals[family] += 1
                event_observations.append(extracted.observation)
                if extracted.prompt is not None:
                    prompt_candidates.append(extracted.prompt)
                tool_call_candidates.extend(extracted.tool_calls)
                if extracted.tool_result is not None:
                    tool_result_candidates.append(extracted.tool_result)

            category = _event_category(record, payload_map)
            if category is not None:
                event_counts[category] += 1

            cumulative, deltas = _usage_updates(record, payload_map)
            if cumulative is not None:
                latest_cumulative = cumulative
                token_snapshots.append(
                    NormalizedTokenSnapshot(
                        cumulative=_usage_vector(cumulative),
                        last_turn=_usage_vector(deltas) if deltas is not None else None,
                        source_ordinal=source_ordinal,
                        occurred_at=timestamp,
                    )
                )
                token_events.append(
                    NormalizedTokenEvent(
                        source_ordinal=source_ordinal,
                        occurred_at=timestamp,
                        cumulative=_usage_vector(cumulative),
                        delta=_usage_vector(deltas) if deltas is not None else None,
                    )
                )
                token_update_count += 1
            elif deltas is not None:
                for key, value in deltas.items():
                    if value is not None:
                        previous = summed_deltas[key]
                        summed_deltas[key] = value + (previous or 0)
                token_events.append(
                    NormalizedTokenEvent(
                        source_ordinal=source_ordinal,
                        occurred_at=timestamp,
                        delta=_usage_vector(deltas),
                    )
                )
                token_update_count += 1

        after_handle = os.fstat(handle.fileno())

    try:
        after_path = path.stat()
    except OSError as exc:
        raise SourceChangedDuringParseError("source_disappeared_during_parse") from exc
    if (
        _stat_signature(before) != _stat_signature(after_handle)
        or _stat_signature(before) != _stat_signature(after_path)
    ):
        raise SourceChangedDuringParseError("source_changed_during_parse")

    usage_values = latest_cumulative if latest_cumulative is not None else summed_deltas
    if latest_cumulative is not None:
        semantics = UsageSemantics.CUMULATIVE_TOTAL
    elif token_update_count:
        semantics = UsageSemantics.SUMMED_EVENT_DELTAS
    else:
        semantics = UsageSemantics.UNAVAILABLE

    usage = NormalizedUsage(
        semantics=semantics,
        input_tokens=usage_values["input_tokens"],
        cached_input_tokens=usage_values["cached_input_tokens"],
        cache_write_input_tokens=usage_values["cache_write_input_tokens"],
        output_tokens=usage_values["output_tokens"],
        reasoning_output_tokens=usage_values["reasoning_output_tokens"],
        total_tokens=_total_tokens(usage_values),
        token_update_count=token_update_count,
    )
    repository_root, repository_name = _resolve_repository(cwd)
    normalized = replace(
        session,
        started_at=started_at,
        apparent_ended_at=apparent_end,
        source_timezone_offset_minutes=source_offset,
        cwd=cwd,
        repository_root=repository_root,
        repository_name=repository_name,
        model=model,
        model_provider=model_provider,
        codex_version=codex_version,
        usage=usage,
        event_counts=tuple(
            NormalizedEventCount(category=category, count=event_counts[category])
            for category in EventCategory
            if event_counts[category]
        ),
    )
    schema_hints, schema_fingerprint = _rollout_schema_identity(
        candidate,
        record_types=record_types,
        payload_types=payload_types,
        top_level_fields=top_level_fields,
        payload_fields=payload_fields,
    )
    capabilities = _parsed_capabilities(
        candidate,
        normalized,
        valid_record_count=valid_records,
        token_update_count=token_update_count,
        token_snapshot_count=len(token_snapshots),
        event_observation_count=len(event_observations),
        prompt_count=len(prompt_candidates),
        tool_call_count=len(tool_call_candidates),
        record_types=record_types,
        payload_types=payload_types,
        partial_final_line=partial_final_line,
        unknown_tool_encoding=any(kind == "tool_encoding" for kind, _ in unknown_counts),
    )
    return ParsedSourceSession(
        session=normalized,
        malformed_line_count=malformed,
        oversized_line_count=oversized,
        parsed_byte_count=parsed_bytes,
        valid_record_count=valid_records,
        partial_final_line=partial_final_line,
        source_file_identity=_file_identity(after_path),
        source_schema_fingerprint=schema_fingerprint,
        source_schema_hints=schema_hints,
        capabilities=capabilities,
        unknown_source_records=_unknown_observations(
            unknown_counts,
            unknown_first,
            unknown_last,
        ),
        semantic_warnings=_token_semantic_warnings(token_snapshots),
        token_snapshots=tuple(token_snapshots),
        token_events=tuple(token_events),
        event_observations=tuple(event_observations),
        prompt_candidates=tuple(prompt_candidates),
        tool_call_candidates=tuple(tool_call_candidates),
        tool_result_candidates=tuple(tool_result_candidates),
    )


def _stat_identity(stat: os.stat_result) -> tuple[int, int]:
    return int(stat.st_dev), int(stat.st_ino)


def _stat_signature(stat: os.stat_result) -> tuple[int, int, int, int]:
    return (*_stat_identity(stat), int(stat.st_size), int(stat.st_mtime_ns))


def _safe_schema_name(value: str) -> str:
    stripped = value.strip().lower()
    if (
        stripped
        and len(stripped) <= 128
        and all(
            character.isascii() and (character.isalnum() or character in "_.-")
            for character in stripped
        )
    ):
        return stripped
    return "<unrecognized-name>"


def _observe_unknown_shapes(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    record_type: str,
    payload_type: str,
    occurred_at: datetime | None,
    counts: Counter[tuple[str, str]],
    first_seen: dict[tuple[str, str], datetime],
    last_seen: dict[tuple[str, str], datetime],
) -> None:
    observations: list[tuple[str, str]] = []
    if record_type and record_type not in _KNOWN_RECORD_TYPES:
        observations.append(("record_type", _safe_schema_name(record_type)))
    if payload_type and payload_type not in _KNOWN_PAYLOAD_TYPES:
        observations.append(("payload_type", _safe_schema_name(payload_type)))
        if "tool" in payload_type or "function" in payload_type:
            observations.append(("tool_encoding", _safe_schema_name(payload_type)))
    observations.extend(
        ("top_level_field", _safe_schema_name(str(field)))
        for field in record
        if _safe_schema_name(str(field)) not in _KNOWN_TOP_LEVEL_FIELDS
    )
    observations.extend(
        ("payload_field", _safe_schema_name(str(field)))
        for field in payload
        if _safe_schema_name(str(field)) not in _KNOWN_PAYLOAD_FIELDS
    )
    for kind, raw_name in observations:
        name = raw_name
        known_names = {item_name for item_kind, item_name in counts if item_kind == kind}
        if name not in known_names and len(known_names) >= MAX_UNKNOWN_NAMES_PER_KIND:
            name = "<additional-unknown-names>"
        key = kind, name
        counts[key] += 1
        if occurred_at is not None:
            first_seen[key] = min(first_seen.get(key, occurred_at), occurred_at)
            last_seen[key] = max(last_seen.get(key, occurred_at), occurred_at)


def _unknown_observations(
    counts: Counter[tuple[str, str]],
    first_seen: Mapping[tuple[str, str], datetime],
    last_seen: Mapping[tuple[str, str], datetime],
) -> tuple[UnknownSourceObservation, ...]:
    return tuple(
        UnknownSourceObservation(
            kind=kind,
            name=name,
            count=count,
            diagnostic_category=_unknown_diagnostic_category(kind, name),
            capability_impact=_unknown_capability_impact(kind, name),
            first_seen_at=first_seen.get((kind, name)),
            last_seen_at=last_seen.get((kind, name)),
        )
        for (kind, name), count in sorted(counts.items())
    )


def _unknown_diagnostic_category(kind: str, name: str) -> str:
    if kind in {"payload_field", "top_level_field"}:
        return "recognized_ignored" if name in _RECOGNIZED_IGNORED_FIELDS else "field_passthrough"
    if kind == "tool_encoding" or (
        kind == "payload_type"
        and ("tool" in name or "command-end" in name or "command_end" in name)
    ):
        return "tool_result_gap"
    if kind == "payload_type" and name in _RECOGNIZED_IGNORED_PAYLOAD_TYPES:
        return "recognized_ignored"
    if kind == "payload_type" and name in _LIFECYCLE_GAP_TYPES:
        return "lifecycle_gap"
    if kind in {"record_type", "payload_type"}:
        return "semantic_gap"
    return "unclassified"


def _unknown_capability_impact(kind: str, name: str) -> str:
    category = _unknown_diagnostic_category(kind, name)
    if category == "tool_result_gap":
        return "tool_activity"
    if category == "lifecycle_gap" or name == "turn_aborted":
        return "task_lifecycle_outcomes"
    if category == "semantic_gap":
        return "event_normalization"
    return "source_compatibility"


def _rollout_schema_identity(
    candidate: SourceSessionCandidate,
    *,
    record_types: Counter[str],
    payload_types: Counter[str],
    top_level_fields: set[str],
    payload_fields: set[str],
) -> tuple[tuple[str, ...], str]:
    rollout_shape = {
        "record_types": sorted(record_types),
        "payload_types": sorted(payload_types),
        "top_level_fields": sorted(top_level_fields),
        "payload_fields": sorted(payload_fields),
    }
    encoded = json.dumps(
        {
            "version": SOURCE_SCHEMA_FINGERPRINT_VERSION,
            "state_schema": candidate.source_schema_fingerprint,
            "rollout": rollout_shape,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    hints = (
        *candidate.source_schema_hints,
        f"rollout-record-types:{','.join(rollout_shape['record_types'])}",
        f"rollout-payload-types:{','.join(rollout_shape['payload_types'])}",
        f"rollout-top-fields:{','.join(rollout_shape['top_level_fields'])}",
        f"rollout-payload-fields:{','.join(rollout_shape['payload_fields'])}",
    )
    return hints, hashlib.sha256(encoded).hexdigest()


def _parsed_capabilities(
    candidate: SourceSessionCandidate,
    session: NormalizedSourceSession,
    *,
    valid_record_count: int,
    token_update_count: int,
    token_snapshot_count: int,
    event_observation_count: int,
    prompt_count: int,
    tool_call_count: int,
    record_types: Counter[str],
    payload_types: Counter[str],
    partial_final_line: bool,
    unknown_tool_encoding: bool,
) -> tuple[CapabilityObservation, ...]:
    observed_task_lifecycle = sum(
        count
        for name, count in (*record_types.items(), *payload_types.items())
        if name in {"task_started", "task_complete", "task_failed", "task_aborted"}
    )
    command_count = sum(
        item.count
        for item in session.event_counts
        if item.category in {EventCategory.SHELL_COMMAND, EventCategory.PATCH_EDIT}
    )
    values = {
        capability: CapabilityObservation(
            capability,
            CapabilityStatus.UNKNOWN,
            evidence_type="derived_after_indexing"
            if capability
            in {
                SourceCapability.REPOSITORY_ATTRIBUTION,
                SourceCapability.MODEL_ATTRIBUTION,
                SourceCapability.PROVENANCE_MATCHING,
            }
            else "not_evaluated",
        )
        for capability in SourceCapability
    }
    values.update({item.capability: item for item in candidate.capabilities})

    def observed(
        capability: SourceCapability,
        count: int,
        evidence: str,
        *,
        degraded: bool = False,
    ) -> None:
        values[capability] = CapabilityObservation(
            capability,
            CapabilityStatus.DEGRADED
            if degraded
            else (CapabilityStatus.AVAILABLE if count else CapabilityStatus.NOT_OBSERVED),
            evidence_count=count,
            evidence_type=(
                "unrecognized_source_encoding"
                if degraded and not count
                else (evidence if count else "not_observed")
            ),
        )

    observed(
        SourceCapability.ROLLOUT_METADATA,
        valid_record_count,
        "valid_jsonl_records",
        degraded=partial_final_line,
    )
    observed(SourceCapability.TOKEN_USAGE, token_update_count, "recognized_token_updates")
    observed(SourceCapability.TOKEN_LINEAGE, token_snapshot_count, "cumulative_token_vectors")
    observed(SourceCapability.PROMPT_CONTENT, prompt_count, "recognized_user_messages")
    observed(
        SourceCapability.EVENT_PROVENANCE,
        event_observation_count,
        "fingerprintable_semantic_events",
    )
    observed(
        SourceCapability.TOOL_ACTIVITY,
        tool_call_count,
        "recognized_tool_calls",
        degraded=unknown_tool_encoding and not tool_call_count,
    )
    observed(
        SourceCapability.COMMAND_EXTRACTION,
        command_count,
        "normalized_commands",
        degraded=unknown_tool_encoding and not command_count,
    )
    observed(
        SourceCapability.TASK_LIFECYCLE,
        observed_task_lifecycle,
        "recognized_task_lifecycle_records",
    )
    values[SourceCapability.GIT_METADATA] = CapabilityObservation(
        SourceCapability.GIT_METADATA,
        CapabilityStatus.AVAILABLE
        if session.git_sha or session.git_origin_url or session.repository_root
        else CapabilityStatus.NOT_OBSERVED,
        evidence_count=int(
            bool(session.git_sha or session.git_origin_url or session.repository_root)
        ),
        evidence_type="normalized_git_or_repository_metadata"
        if session.git_sha or session.git_origin_url or session.repository_root
        else "not_observed",
    )
    values[SourceCapability.DURATION_TIMESTAMPS] = CapabilityObservation(
        SourceCapability.DURATION_TIMESTAMPS,
        CapabilityStatus.AVAILABLE
        if session.started_at and session.apparent_ended_at
        else CapabilityStatus.NOT_OBSERVED,
        evidence_count=int(bool(session.started_at and session.apparent_ended_at)),
        evidence_type="normalized_start_and_end"
        if session.started_at and session.apparent_ended_at
        else "incomplete_range",
    )
    return tuple(values[key] for key in SourceCapability)


def _token_semantic_warnings(
    snapshots: list[NormalizedTokenSnapshot],
) -> tuple[SourceSemanticWarning, ...]:
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    decreases = 0
    inconsistent_vectors = 0
    for previous, current in zip(snapshots, snapshots[1:], strict=False):
        for field in fields:
            left = getattr(previous.cumulative, field)
            right = getattr(current.cumulative, field)
            if left is not None and right is not None and right < left:
                decreases += 1
    for snapshot in snapshots:
        total = snapshot.cumulative.total_tokens
        components = (
            snapshot.cumulative.input_tokens,
            snapshot.cumulative.output_tokens,
        )
        if total is not None and any(value is not None and value > total for value in components):
            inconsistent_vectors += 1
    warnings: list[SourceSemanticWarning] = []
    if decreases:
        warnings.append(
            SourceSemanticWarning(
                code="cumulative_token_decrease",
                count=decreases,
                detail="Recognized cumulative token fields decreased between snapshots.",
            )
        )
    if inconsistent_vectors:
        warnings.append(
            SourceSemanticWarning(
                code="token_vector_relationship_changed",
                count=inconsistent_vectors,
                detail="A reported token component exceeded the reported total.",
            )
        )
    return tuple(warnings)


def select_state_database(codex_home: Path) -> StateDatabaseSelection:
    """Score compatible state databases by structure and live rollout consistency."""

    home = codex_home.expanduser().resolve(strict=False)
    assessments = tuple(
        _assess_state_database(path, home) for path in _state_database_candidates(home)
    )
    viable = [item for item in assessments if item.readable and item.catalogue_table]
    selected = max(
        viable,
        key=lambda item: (
            item.score,
            item.valid_rollout_references,
            item.row_count or -1,
            item.modified_ns,
            item.path.name,
        ),
        default=None,
    )
    return StateDatabaseSelection(selected=selected, candidates=assessments)


def _assess_state_database(path: Path, home: Path) -> StateDatabaseAssessment:
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        modified_ns = -1
    try:
        with closing(open_source_sqlite_readonly(path)) as connection:
            connection.row_factory = sqlite3.Row
            table = _recognize_catalogue_table(connection)
            relationship_table = _recognize_relationship_table(connection)
            schema_hints, fingerprint = _source_database_schema(connection)
            if table is None:
                return StateDatabaseAssessment(
                    path=path,
                    readable=True,
                    catalogue_table=None,
                    relationship_table=relationship_table,
                    row_count=None,
                    rollout_references_checked=0,
                    valid_rollout_references=0,
                    missing_rollout_references=0,
                    schema_fingerprint=fingerprint,
                    schema_hints=schema_hints,
                    score=10,
                    modified_ns=modified_ns,
                    reasons=("readable SQLite", "no compatible catalogue"),
                )
            row_count = int(
                connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
            )
            checked = valid = missing = 0
            timestamp_capable = False
            for columns, row in _catalogue_rows(connection, table):
                if checked >= MAX_STATE_REFERENCE_CHECKS:
                    break
                if columns.get("created_at") or columns.get("updated_at"):
                    timestamp_capable = True
                raw_path = _short_text(_row_value(row, columns, "rollout_path"))
                resolved, allowed = _resolve_rollout_path(home, raw_path)
                if resolved is None or not allowed:
                    continue
                checked += 1
                if resolved.is_file():
                    valid += 1
                else:
                    missing += 1
            score = 50
            reasons = ["readable SQLite", f"compatible catalogue {table}"]
            if relationship_table:
                score += 5
                reasons.append(f"relationship table {relationship_table}")
            if timestamp_capable:
                score += 5
                reasons.append("catalogue timestamps")
            if checked:
                consistency = valid / checked
                score += round(consistency * 30)
                reasons.append(f"{valid}/{checked} valid rollout references")
            if row_count:
                score += min(10, row_count.bit_length())
                reasons.append(f"{row_count} catalogue rows")
            return StateDatabaseAssessment(
                path=path,
                readable=True,
                catalogue_table=table,
                relationship_table=relationship_table,
                row_count=row_count,
                rollout_references_checked=checked,
                valid_rollout_references=valid,
                missing_rollout_references=missing,
                schema_fingerprint=fingerprint,
                schema_hints=schema_hints,
                score=score,
                modified_ns=modified_ns,
                reasons=tuple(reasons),
            )
    except (OSError, sqlite3.DatabaseError) as exc:
        return StateDatabaseAssessment(
            path=path,
            readable=False,
            catalogue_table=None,
            relationship_table=None,
            row_count=None,
            rollout_references_checked=0,
            valid_rollout_references=0,
            missing_rollout_references=0,
            schema_fingerprint="",
            schema_hints=(),
            score=0,
            modified_ns=modified_ns,
            reasons=("unreadable SQLite",),
            error_type=type(exc).__name__,
        )


def _source_database_schema(connection: sqlite3.Connection) -> tuple[tuple[str, ...], str]:
    structures: list[dict[str, object]] = []
    hints: list[str] = []
    for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        table = str(row[0])
        columns = [
            {
                "name": str(item[1]).lower(),
                "type": str(item[2]).upper(),
                "pk": int(item[5]),
            }
            for item in connection.execute(f"PRAGMA table_info({_quote(table)})")
        ]
        structures.append({"table": table.lower(), "columns": columns})
        hints.append(f"{table.lower()}({','.join(str(item['name']) for item in columns)})")
    encoded = json.dumps(
        {"version": SOURCE_SCHEMA_FINGERPRINT_VERSION, "structures": structures},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return tuple(hints), hashlib.sha256(encoded).hexdigest()


def _selection_warnings(selection: StateDatabaseSelection) -> list[str]:
    warnings = [
        f"Could not read {item.path.name}: {item.error_type}."
        for item in selection.candidates
        if not item.readable
    ]
    selected = selection.selected
    if selected is not None:
        alternatives = [
            item
            for item in selection.candidates
            if item.path != selected.path and item.readable and item.catalogue_table
        ]
        if alternatives:
            warnings.append(
                f"Selected {selected.path.name} by compatibility score {selected.score}; "
                f"{len(alternatives)} other compatible state database(s) were not combined."
            )
    return warnings


def _state_database_candidates(home: Path) -> tuple[Path, ...]:
    paths = {path for path in home.glob("state_*.sqlite") if path.is_file()}
    legacy = home / "state.sqlite"
    if legacy.is_file():
        paths.add(legacy)
    return tuple(sorted(paths, key=lambda path: path.name))


def _recognize_catalogue_table(connection: sqlite3.Connection) -> str | None:
    best: tuple[int, str] | None = None
    rows = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    for row in rows:
        table = str(row[0])
        columns = {
            str(item[1]).lower()
            for item in connection.execute(f"PRAGMA table_info({_quote(table)})")
        }
        has_id = any(alias in columns for alias in _COLUMN_ALIASES["id"])
        has_rollout = any(alias in columns for alias in _COLUMN_ALIASES["rollout_path"])
        if not has_id or not has_rollout:
            continue
        score = len(columns & {alias for aliases in _COLUMN_ALIASES.values() for alias in aliases})
        if "thread" in table.lower() or "session" in table.lower():
            score += 5
        candidate = score, table
        if best is None or candidate > best:
            best = candidate
    return best[1] if best else None


def _recognize_relationship_table(connection: sqlite3.Connection) -> str | None:
    for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ):
        table = str(row[0])
        columns = {
            str(item[1]).lower()
            for item in connection.execute(f"PRAGMA table_info({_quote(table)})")
        }
        has_parent = any(alias in columns for alias in _RELATIONSHIP_COLUMN_ALIASES["parent"])
        has_child = any(alias in columns for alias in _RELATIONSHIP_COLUMN_ALIASES["child"])
        if has_parent and has_child:
            return table
    return None


def _catalogue_rows(
    connection: sqlite3.Connection,
    table: str,
) -> Iterable[tuple[dict[str, str], sqlite3.Row]]:
    declared = {
        str(row[1]).lower(): str(row[1])
        for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
    }
    selected = {
        concept: next((declared[alias] for alias in aliases if alias in declared), "")
        for concept, aliases in _COLUMN_ALIASES.items()
    }
    columns = sorted({column for column in selected.values() if column})
    query = f"SELECT {', '.join(_quote(column) for column in columns)} FROM {_quote(table)}"
    for row in connection.execute(query):
        yield selected, row


def _candidate_from_row(
    row: sqlite3.Row,
    columns: Mapping[str, str],
    *,
    home: Path,
    source_type: str,
    database_path: Path,
    table: str,
    schema_fingerprint: str,
    schema_hints: tuple[str, ...],
) -> SourceSessionCandidate:
    source_session_id = _short_text(_row_value(row, columns, "id"))
    if not source_session_id:
        raise ValueError("Recognized catalogue row has no session identifier")

    rollout_path, allowed = _resolve_rollout_path(
        home,
        _short_text(_row_value(row, columns, "rollout_path")),
    )
    try:
        stat = rollout_path.stat() if rollout_path is not None and allowed else None
    except OSError:
        stat = None

    started_at, offset = _parse_timestamp(_row_value(row, columns, "created_at"))
    updated_at, _ = _parse_timestamp(_row_value(row, columns, "updated_at"))
    archived_at, _ = _parse_timestamp(_row_value(row, columns, "archived_at"))
    cwd = _path_value(_row_value(row, columns, "cwd"))
    repository_root, repository_name = _resolve_repository(cwd)
    client_source, client_kind, subagent_kind, source_parent_id = _normalize_client_source(
        _row_value(row, columns, "client_source")
    )
    session = NormalizedSourceSession(
        source_session_id=source_session_id,
        source_type=source_type,
        source_home=home,
        client_source=client_source,
        client_kind=client_kind,
        subagent_source_kind=subagent_kind,
        source_parent_session_id=source_parent_id,
        started_at=started_at,
        updated_at=updated_at,
        apparent_ended_at=archived_at,
        source_timezone_offset_minutes=offset,
        cwd=cwd,
        repository_root=repository_root,
        repository_name=repository_name,
        git_branch=_short_text(_row_value(row, columns, "git_branch")),
        git_sha=_short_text(_row_value(row, columns, "git_sha")),
        git_origin_url=_short_text(_row_value(row, columns, "git_origin_url")),
        model=_short_text(_row_value(row, columns, "model")),
        model_provider=_short_text(_row_value(row, columns, "model_provider")),
        archived=_truthy(_row_value(row, columns, "archived")),
        rollout_path=rollout_path,
        source_db_path=database_path,
        source_path=rollout_path,
    )
    return SourceSessionCandidate(
        session=session,
        source_schema_version=f"{database_path.stem}:{table}",
        rollout_exists=bool(stat and rollout_path and rollout_path.is_file()),
        rollout_allowed=allowed,
        size_bytes=int(stat.st_size) if stat else None,
        mtime_ns=int(stat.st_mtime_ns) if stat else None,
        file_identity=_file_identity(stat),
        source_schema_fingerprint=schema_fingerprint,
        source_schema_hints=schema_hints,
        capabilities=_catalogue_capabilities(columns, session),
    )


def _normalize_client_source(
    value: object,
) -> tuple[str | None, ClientKind, SubagentSourceKind | None, str | None]:
    """Normalize scalar or structured catalogue source metadata without retaining raw JSON."""

    source = _short_text(value)
    if source is None:
        return None, ClientKind.UNKNOWN, None, None
    try:
        decoded = json.loads(source)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        subagent = decoded.get("subagent")
        if isinstance(subagent, dict):
            spawn = subagent.get("thread_spawn")
            if isinstance(spawn, dict):
                parent = _short_text(spawn.get("parent_thread_id"))
                return None, ClientKind.SUBAGENT, SubagentSourceKind.THREAD_SPAWN, parent
            if "guardian" in subagent:
                return None, ClientKind.SUBAGENT, SubagentSourceKind.GUARDIAN, None
            return None, ClientKind.SUBAGENT, SubagentSourceKind.OTHER, None
        if "guardian" in decoded:
            return None, ClientKind.SUBAGENT, SubagentSourceKind.GUARDIAN, None
        return None, ClientKind.UNKNOWN, None, None
    if source.startswith(("{", "[")):
        return None, ClientKind.UNKNOWN, None, None
    normalized = source.casefold().replace("-", "_").replace(" ", "_")
    if normalized in _CLI_SOURCES:
        return source, ClientKind.CLI, None, None
    if normalized in _EDITOR_SOURCES:
        return source, ClientKind.EDITOR, None, None
    return source, ClientKind.OTHER, None, None


def _catalogue_capabilities(
    columns: Mapping[str, str],
    session: NormalizedSourceSession,
) -> tuple[CapabilityObservation, ...]:
    archive_available = bool(columns.get("archived") or columns.get("archived_at"))
    git_available = bool(
        session.git_sha or session.git_origin_url or session.repository_root
    )
    duration_available = bool(session.started_at and session.apparent_ended_at)
    return (
        CapabilityObservation(
            SourceCapability.SESSION_CATALOGUE,
            CapabilityStatus.AVAILABLE,
            evidence_count=1,
            evidence_type="recognized_catalogue_row",
        ),
        CapabilityObservation(
            SourceCapability.ARCHIVE_METADATA,
            CapabilityStatus.AVAILABLE if archive_available else CapabilityStatus.UNKNOWN,
            evidence_count=int(archive_available),
            evidence_type="catalogue_archive_column" if archive_available else "column_absent",
        ),
        CapabilityObservation(
            SourceCapability.GIT_METADATA,
            CapabilityStatus.AVAILABLE if git_available else CapabilityStatus.NOT_OBSERVED,
            evidence_count=int(git_available),
            evidence_type="catalogue_git_metadata" if git_available else "not_observed",
        ),
        CapabilityObservation(
            SourceCapability.DURATION_TIMESTAMPS,
            CapabilityStatus.AVAILABLE if duration_available else CapabilityStatus.NOT_OBSERVED,
            evidence_count=int(duration_available),
            evidence_type="start_and_end_timestamps" if duration_available else "incomplete_range",
        ),
    )


def _file_identity(stat: os.stat_result | None) -> str | None:
    if stat is None:
        return None
    return f"{stat.st_dev}:{stat.st_ino}"


def _row_value(row: sqlite3.Row, columns: Mapping[str, str], concept: str) -> Any:
    column = columns.get(concept)
    return row[column] if column else None


def _resolve_rollout_path(home: Path, raw_path: str | None) -> tuple[Path | None, bool]:
    if not raw_path:
        return None, False
    path = Path(raw_path)
    resolved = (path if path.is_absolute() else home / path).resolve(strict=False)
    return resolved, resolved == home or home in resolved.parents


def _resolve_repository(cwd: Path | None) -> tuple[Path | None, str | None]:
    if cwd is None or not cwd.is_dir():
        return None, None
    current = cwd.resolve(strict=False)
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            return directory, directory.name
    return None, None


def _parse_timestamp(value: Any) -> tuple[datetime | None, int | None]:
    parsed: datetime | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if abs(float(value)) > 10_000_000_000 else float(value)
        try:
            parsed = datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None, None
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.isdigit():
            return _parse_timestamp(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None, None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)

    if parsed is None:
        return None, None
    offset = parsed.utcoffset()
    offset_minutes = int(offset.total_seconds() // 60) if offset is not None else None
    return parsed.astimezone(UTC), offset_minutes


def _event_category(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> EventCategory | None:
    record_type = _record_type(record)
    payload_type = _short_text(payload.get("type"))
    normalized_payload = payload_type.lower() if payload_type else ""
    combined = f"{record_type} {normalized_payload}".lower()

    if (
        "token" in normalized_payload
        or record_type == "usage"
        or isinstance(record.get("usage"), dict)
    ):
        return EventCategory.TOKEN_UPDATE
    if normalized_payload == "user_message" or record_type == "user_message":
        return EventCategory.USER_MESSAGE
    if normalized_payload in {"agent_message", "assistant_message"}:
        return EventCategory.ASSISTANT_MESSAGE
    if normalized_payload == "message":
        role = _short_text(payload.get("role"))
        return EventCategory.USER_MESSAGE if role == "user" else EventCategory.ASSISTANT_MESSAGE
    if any(marker in combined for marker in ("error", "failed", "aborted")):
        return EventCategory.ERROR

    tool_name = _tool_name(record, payload)
    is_tool_call = (
        normalized_payload in {"function_call", "custom_tool_call", "tool_call"}
        or record_type == "tool_call"
    )
    if is_tool_call:
        lowered_name = tool_name.lower() if tool_name else ""
        if "patch" in lowered_name or "edit" in lowered_name:
            return EventCategory.PATCH_EDIT
        if any(marker in lowered_name for marker in ("exec", "shell", "command")):
            return EventCategory.SHELL_COMMAND
        return EventCategory.TOOL_CALL
    if "patch" in combined or "edit" in combined:
        return EventCategory.PATCH_EDIT
    if record_type in _STRUCTURAL_RECORD_TYPES or normalized_payload in _STRUCTURAL_RECORD_TYPES:
        return None
    if normalized_payload in _NON_CONTENT_PAYLOAD_TYPES:
        return None
    if record_type == "response_item" and not normalized_payload:
        return None
    return EventCategory.UNKNOWN if record_type or normalized_payload else None


def _usage_updates(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> tuple[dict[str, int | None] | None, dict[str, int | None] | None]:
    info = payload.get("info")
    info_map = info if isinstance(info, dict) else {}
    cumulative = info_map.get("total_token_usage")
    delta = info_map.get("last_token_usage")
    if isinstance(cumulative, dict):
        return (
            _normalized_usage_values(cumulative),
            _normalized_usage_values(delta) if isinstance(delta, dict) else None,
        )

    if isinstance(delta, dict):
        return None, _normalized_usage_values(delta)

    for possible in (record.get("usage"), payload.get("usage"), payload.get("token_usage")):
        if isinstance(possible, dict):
            return None, _normalized_usage_values(possible)
    return None, None


def _normalized_usage_values(values: Mapping[str, Any]) -> dict[str, int | None]:
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "cached_input_tokens": ("cached_input_tokens", "cache_read_input_tokens"),
        "cache_write_input_tokens": ("cache_write_input_tokens",),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "reasoning_output_tokens": ("reasoning_output_tokens", "reasoning_tokens"),
        "total_tokens": ("total_tokens",),
    }
    return {
        target: next(
            (
                int(values[source])
                for source in sources
                if isinstance(values.get(source), (int, float))
                and not isinstance(values.get(source), bool)
                and values[source] >= 0
            ),
            None,
        )
        for target, sources in aliases.items()
    }


def _empty_usage() -> dict[str, int | None]:
    return {
        "input_tokens": None,
        "cached_input_tokens": None,
        "cache_write_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
        "total_tokens": None,
    }


def _usage_vector(values: Mapping[str, int | None]) -> UsageVector:
    return UsageVector(
        input_tokens=values["input_tokens"],
        cached_input_tokens=values["cached_input_tokens"],
        cache_write_input_tokens=values["cache_write_input_tokens"],
        output_tokens=values["output_tokens"],
        reasoning_output_tokens=values["reasoning_output_tokens"],
        total_tokens=_total_tokens(values),
    )


def _total_tokens(values: Mapping[str, int | None]) -> int | None:
    reported = values["total_tokens"]
    if reported is not None:
        return reported
    input_tokens = values["input_tokens"]
    output_tokens = values["output_tokens"]
    if input_tokens is None or output_tokens is None:
        return None
    return input_tokens + output_tokens


def _record_type(record: Mapping[str, Any]) -> str:
    for key in ("type", "event", "kind"):
        value = _short_text(record.get(key))
        if value:
            return value.lower()
    return ""


def _tool_name(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    return _short_text(
        payload.get("name")
        or payload.get("tool_name")
        or record.get("tool_name")
        or record.get("name")
    )


def _short_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped and len(stripped) <= 4096 else None


def _path_value(value: Any) -> Path | None:
    text = _short_text(value)
    return Path(text).expanduser().resolve(strict=False) if text else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
