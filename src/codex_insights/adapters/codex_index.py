"""Version-tolerant Codex catalogue and rollout normalization."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codex_insights.adapters.codex_events import extract_event
from codex_insights.db import open_source_sqlite_readonly
from codex_insights.models import (
    EventCategory,
    EventFamily,
    NormalizedEventCount,
    NormalizedEventObservation,
    NormalizedPromptCandidate,
    NormalizedSourceSession,
    NormalizedThreadRelationship,
    NormalizedTokenSnapshot,
    NormalizedToolCallCandidate,
    NormalizedToolResultCandidate,
    NormalizedUsage,
    ParsedSourceSession,
    SourceSessionCandidate,
    UsageSemantics,
    UsageVector,
)

PARSER_VERSION = "codex-local-v6"
MAX_ROLLOUT_LINE_BYTES = 1024 * 1024

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


def discover_session_candidates(
    codex_home: Path,
    *,
    source_type: str,
) -> tuple[tuple[SourceSessionCandidate, ...], tuple[str, ...]]:
    """Load recognized session catalogue metadata from versioned state databases."""

    home = codex_home.expanduser().resolve(strict=False)
    if not home.is_dir():
        return (), ("Codex home does not exist; no sessions were discovered.",)

    warnings: list[str] = []
    for database_path in _state_database_candidates(home):
        try:
            with closing(open_source_sqlite_readonly(database_path)) as connection:
                connection.row_factory = sqlite3.Row
                table = _recognize_catalogue_table(connection)
                if table is None:
                    warnings.append(
                        f"{database_path.name} has no recognized session catalogue table."
                    )
                    continue
                candidates: list[SourceSessionCandidate] = []
                for columns, row in _catalogue_rows(connection, table):
                    try:
                        candidates.append(
                            _candidate_from_row(
                                row,
                                columns,
                                home=home,
                                source_type=source_type,
                                database_path=database_path,
                                table=table,
                            )
                        )
                    except (OSError, ValueError):
                        warnings.append(f"One row in {database_path.name} could not be normalized.")
                return tuple(candidates), tuple(warnings)
        except (OSError, sqlite3.DatabaseError) as exc:
            warnings.append(f"Could not read {database_path.name}: {type(exc).__name__}.")

    if not _state_database_candidates(home):
        warnings.append("No versioned Codex state database was found.")
    elif not warnings:
        warnings.append("No recognized Codex session catalogue was found.")
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
    warnings: list[str] = []
    for database_path in _state_database_candidates(home):
        try:
            with closing(open_source_sqlite_readonly(database_path)) as connection:
                connection.row_factory = sqlite3.Row
                table = _recognize_relationship_table(connection)
                if table is None:
                    continue
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

    session = candidate.session
    event_counts: Counter[EventCategory] = Counter()
    latest_cumulative: dict[str, int | None] | None = None
    summed_deltas = _empty_usage()
    token_update_count = 0
    token_snapshots: list[NormalizedTokenSnapshot] = []
    event_observations: list[NormalizedEventObservation] = []
    prompt_candidates: list[NormalizedPromptCandidate] = []
    tool_call_candidates: list[NormalizedToolCallCandidate] = []
    tool_result_candidates: list[NormalizedToolResultCandidate] = []
    family_ordinals: Counter[EventFamily] = Counter()
    malformed = 0
    oversized = 0
    parsed_bytes = 0
    apparent_end = session.apparent_ended_at
    started_at = session.started_at
    source_offset = session.source_timezone_offset_minutes
    cwd = session.cwd
    model = session.model
    model_provider = session.model_provider
    codex_version = session.codex_version

    with path.open("rb") as handle:
        for source_ordinal, line in enumerate(handle):
            parsed_bytes += len(line)
            if len(line) > MAX_ROLLOUT_LINE_BYTES:
                oversized += 1
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue

            timestamp, offset = _parse_timestamp(record.get("timestamp"))
            if timestamp is not None:
                apparent_end = timestamp if apparent_end is None else max(apparent_end, timestamp)
                if started_at is None:
                    started_at = timestamp
                if source_offset is None:
                    source_offset = offset

            payload = record.get("payload")
            payload_map = payload if isinstance(payload, dict) else {}
            if _record_type(record) == "session_meta":
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
                    )
                )
                token_update_count += 1
            elif deltas is not None:
                for key, value in deltas.items():
                    if value is not None:
                        previous = summed_deltas[key]
                        summed_deltas[key] = value + (previous or 0)
                token_update_count += 1

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
    return ParsedSourceSession(
        session=normalized,
        malformed_line_count=malformed,
        oversized_line_count=oversized,
        parsed_byte_count=parsed_bytes,
        token_snapshots=tuple(token_snapshots),
        event_observations=tuple(event_observations),
        prompt_candidates=tuple(prompt_candidates),
        tool_call_candidates=tuple(tool_call_candidates),
        tool_result_candidates=tuple(tool_result_candidates),
    )


def _state_database_candidates(home: Path) -> tuple[Path, ...]:
    paths = {path for path in home.glob("state_*.sqlite") if path.is_file()}
    legacy = home / "state.sqlite"
    if legacy.is_file():
        paths.add(legacy)
    return tuple(sorted(paths, key=_database_sort_key, reverse=True))


def _database_sort_key(path: Path) -> tuple[int, int, str]:
    suffix = path.stem.removeprefix("state_")
    version = int(suffix) if suffix.isdigit() else -1
    try:
        modified = path.stat().st_mtime_ns
    except OSError:
        modified = -1
    return version, modified, path.name


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
    session = NormalizedSourceSession(
        source_session_id=source_session_id,
        source_type=source_type,
        source_home=home,
        client_source=_short_text(_row_value(row, columns, "client_source")),
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
    )


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
