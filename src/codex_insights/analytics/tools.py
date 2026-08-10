"""Origin-aware tool and command analytics over normalized metadata."""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codex_insights.db import open_index
from codex_insights.models import CommandCategory

from .prefixes import sqlite_like_prefix


@dataclass(frozen=True, slots=True)
class ToolFilters:
    """Shared filters for tool and command reports."""

    since: datetime | None = None
    until: datetime | None = None
    repository: str | None = None
    model: str | None = None
    task_action: str | None = None
    task_domain: str | None = None
    session: str | None = None
    category: CommandCategory | None = None
    limit: int = 25


@dataclass(frozen=True, slots=True)
class ToolProvenanceCoverage:
    """Physical observation counts separated from originated activity."""

    observed: int
    originated: int
    inherited: int
    ambiguous: int
    unknown: int

    def to_dict(self) -> dict[str, int]:
        return {
            "observed": self.observed,
            "originated": self.originated,
            "inherited": self.inherited,
            "ambiguous": self.ambiguous,
            "unknown": self.unknown,
        }


@dataclass(frozen=True, slots=True)
class ActivityGroup:
    """One deterministic aggregate group."""

    key: str
    count: int

    def to_dict(self) -> dict[str, object]:
        return {"key": self.key, "count": self.count}


@dataclass(frozen=True, slots=True)
class RepeatedCommand:
    """A privacy-filtered command invoked more than once in the selection."""

    command: str | None
    category: str
    executable: str | None
    invocation_count: int
    session_count: int
    first_activity: datetime | None
    latest_activity: datetime | None
    redacted: bool
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "category": self.category,
            "executable": self.executable,
            "invocation_count": self.invocation_count,
            "session_count": self.session_count,
            "first_activity": _json_datetime(self.first_activity),
            "latest_activity": _json_datetime(self.latest_activity),
            "redacted": self.redacted,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class ToolActivityReport:
    """Aggregate tool metrics with explicit output and provenance coverage."""

    session_count: int
    originated_tool_calls: int
    originated_commands: int
    commands_per_session: float | None
    test_invocations: int
    git_inspections: int
    patch_edits: int
    known_results: int
    failed_results: int
    failure_rate: float | None
    provenance: ToolProvenanceCoverage
    tools: tuple[ActivityGroup, ...]
    categories: tuple[ActivityGroup, ...]
    executables: tuple[ActivityGroup, ...]
    repeated_commands: tuple[RepeatedCommand, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "session_count": self.session_count,
            "originated_tool_calls": self.originated_tool_calls,
            "originated_commands": self.originated_commands,
            "commands_per_session": self.commands_per_session,
            "test_invocations": self.test_invocations,
            "git_inspections": self.git_inspections,
            "patch_edits": self.patch_edits,
            "known_results": self.known_results,
            "failed_results": self.failed_results,
            "failure_rate": self.failure_rate,
            "provenance": self.provenance.to_dict(),
            "tools": [item.to_dict() for item in self.tools],
            "categories": [item.to_dict() for item in self.categories],
            "executables": [item.to_dict() for item in self.executables],
            "repeated_commands": [item.to_dict() for item in self.repeated_commands],
            "activity_semantics": "originated_events",
        }


def get_tool_activity_report(
    database_path: Path,
    *,
    codex_home: Path,
    filters: ToolFilters | None = None,
    commands_only: bool = False,
    repeated: bool = False,
) -> ToolActivityReport:
    """Return origin-aware tool metrics without reopening source rollouts."""

    selected = filters or ToolFilters()
    if selected.limit < 1:
        raise ValueError("limit must be at least 1")
    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        resolved_session = _resolve_session(connection, selected.session)
        query, parameters = _activity_query(
            selected,
            resolved_session=resolved_session,
            commands_only=commands_only,
        )
        rows = connection.execute(query, parameters).fetchall()

    provenance_counts: Counter[str] = Counter(str(row["provenance_status"]) for row in rows)
    origin_rows = [
        row
        for row in rows
        if row["provenance_status"] == "origin"
        and row["origin_session_id"] == row["observed_session_id"]
    ]
    session_count = len({int(row["observed_session_id"]) for row in origin_rows})
    command_rows = [row for row in origin_rows if row["command_fingerprint"] is not None]
    known_results = sum(row["result_status"] != "unknown" for row in origin_rows)
    failed_results = sum(row["result_status"] == "failure" for row in origin_rows)
    categories = Counter(str(row["command_category"]) for row in origin_rows)
    tools = Counter(str(row["tool_name"]) for row in origin_rows)
    executables = Counter(
        str(row["executable"]) for row in command_rows if row["executable"] is not None
    )
    return ToolActivityReport(
        session_count=session_count,
        originated_tool_calls=len(origin_rows),
        originated_commands=len(command_rows),
        commands_per_session=(len(command_rows) / session_count if session_count else None),
        test_invocations=categories[CommandCategory.TESTING.value],
        git_inspections=categories[CommandCategory.GIT_INSPECTION.value],
        patch_edits=categories[CommandCategory.EDITING_PATCHING.value],
        known_results=known_results,
        failed_results=failed_results,
        failure_rate=(failed_results / known_results if known_results else None),
        provenance=ToolProvenanceCoverage(
            observed=len(rows),
            originated=len(origin_rows),
            inherited=(
                provenance_counts["inherited_exact"]
                + provenance_counts["inherited_prefix"]
                + provenance_counts["observed_duplicate"]
            ),
            ambiguous=provenance_counts["ambiguous"],
            unknown=provenance_counts["unknown"],
        ),
        tools=_groups(tools, selected.limit),
        categories=_groups(categories, selected.limit),
        executables=_groups(executables, selected.limit),
        repeated_commands=(
            _repeated_commands(command_rows, selected.limit) if repeated else ()
        ),
    )


def originated_command_counts(
    database_path: Path,
    *,
    codex_home: Path,
    filters: ToolFilters | None = None,
    dimension: str,
) -> dict[str, int]:
    """Support reconciliation tests and reports with one shared additive query."""

    if dimension not in {"repo", "model", "category"}:
        raise ValueError("dimension must be repo, model, or category")
    selected = filters or ToolFilters(limit=1_000_000)
    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        resolved_session = _resolve_session(connection, selected.session)
        query, parameters = _activity_query(
            selected,
            resolved_session=resolved_session,
            commands_only=True,
        )
        rows = connection.execute(query, parameters).fetchall()
    counts: Counter[str] = Counter()
    for row in rows:
        if (
            row["provenance_status"] != "origin"
            or row["origin_session_id"] != row["observed_session_id"]
        ):
            continue
        if dimension == "repo":
            key = str(row["repository_root"] or "outside-git")
        elif dimension == "model":
            key = str(row["model"] or "unknown")
        else:
            key = str(row["command_category"])
        counts[key] += 1
    return dict(sorted(counts.items()))


def _activity_query(
    filters: ToolFilters,
    *,
    resolved_session: int | None,
    commands_only: bool,
) -> tuple[str, tuple[object, ...]]:
    conditions: list[str] = []
    parameters: list[object] = []
    activity_time = "COALESCE(activity.occurred_at, sessions.started_at)"
    if filters.since is not None:
        conditions.append(f"{activity_time} >= ?")
        parameters.append(_database_datetime(filters.since))
    if filters.until is not None:
        conditions.append(f"{activity_time} < ?")
        parameters.append(_database_datetime(filters.until))
    if filters.repository:
        if filters.repository.casefold() in {"outside-git", "non-git", "none"}:
            conditions.append("sessions.repository_root IS NULL")
        else:
            conditions.append(
                "(sessions.repository_name = ? COLLATE NOCASE OR sessions.repository_root = ?)"
            )
            parameters.extend((filters.repository, filters.repository))
    if filters.model:
        if filters.model.casefold() in {"unknown", "none"}:
            conditions.append("(sessions.model IS NULL OR sessions.model = '')")
        else:
            conditions.append("sessions.model = ? COLLATE NOCASE")
            parameters.append(filters.model)
    if filters.task_action:
        conditions.append("COALESCE(tasks.action, 'unknown') = ?")
        parameters.append(filters.task_action.casefold())
    if filters.task_domain:
        conditions.append("COALESCE(tasks.domain, 'unknown') = ?")
        parameters.append(filters.task_domain.casefold())
    if resolved_session is not None:
        conditions.append("activity.observed_session_id = ?")
        parameters.append(resolved_session)
    if filters.category is not None:
        conditions.append("activity.command_category = ?")
        parameters.append(filters.category.value)
    if commands_only:
        conditions.append("activity.command_fingerprint IS NOT NULL")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return (
        f"""
        SELECT activity.*, sessions.source_session_id, sessions.repository_root,
               sessions.repository_name, sessions.model, sessions.model_provider,
               sessions.started_at
        FROM tool_activity AS activity
        JOIN source_sessions AS sessions ON sessions.id = activity.observed_session_id
        LEFT JOIN session_tasks AS tasks ON tasks.session_id = sessions.id
        {where}
        ORDER BY {activity_time} IS NULL, {activity_time},
                 sessions.source_session_id, activity.source_ordinal,
                 activity.operation_ordinal
        """,
        tuple(parameters),
    )


def _resolve_session(connection: sqlite3.Connection, prefix: str | None) -> int | None:
    if prefix is None:
        return None
    value = prefix.strip()
    if not value:
        raise ValueError("session prefix cannot be empty")
    exact = connection.execute(
        "SELECT id FROM source_sessions WHERE source_session_id = ?",
        (value,),
    ).fetchone()
    if exact is not None:
        return int(exact["id"])
    rows = connection.execute(
        "SELECT id FROM source_sessions WHERE source_session_id LIKE ? ESCAPE '\\' "
        "ORDER BY source_session_id LIMIT 2",
        (sqlite_like_prefix(value),),
    ).fetchall()
    if len(rows) != 1:
        label = "No session matches" if not rows else "Session prefix is ambiguous"
        raise ValueError(f"{label}: {value!r}")
    return int(rows[0]["id"])


def _groups(counts: Counter[str], limit: int) -> tuple[ActivityGroup, ...]:
    return tuple(
        ActivityGroup(key=key, count=count)
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    )


def _repeated_commands(
    rows: list[sqlite3.Row],
    limit: int,
) -> tuple[RepeatedCommand, ...]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        fingerprint = row["command_fingerprint"]
        if fingerprint is not None:
            grouped[str(fingerprint)].append(row)
    results: list[RepeatedCommand] = []
    for grouped_rows in grouped.values():
        if len(grouped_rows) < 2:
            continue
        first = grouped_rows[0]
        times = [
            value
            for value in (_stored_datetime(row["occurred_at"]) for row in grouped_rows)
            if value is not None
        ]
        results.append(
            RepeatedCommand(
                command=(str(first["command_text"]) if first["command_text"] is not None else None),
                category=str(first["command_category"]),
                executable=(str(first["executable"]) if first["executable"] else None),
                invocation_count=len(grouped_rows),
                session_count=len(
                    {int(row["observed_session_id"]) for row in grouped_rows}
                ),
                first_activity=min(times) if times else None,
                latest_activity=max(times) if times else None,
                redacted=any(bool(row["redacted"]) for row in grouped_rows),
                truncated=any(bool(row["truncated"]) for row in grouped_rows),
            )
        )
    results.sort(
        key=lambda item: (
            -item.invocation_count,
            (item.command or "").casefold(),
            item.category,
        )
    )
    return tuple(results[:limit])


def _database_datetime(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _stored_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _json_datetime(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None
