"""Origin-aware tool and command analytics over normalized metadata."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
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
        source, parameters = _activity_source(
            selected,
            resolved_session=resolved_session,
            commands_only=commands_only,
        )
        origin = _origin_condition()
        summary = connection.execute(
            f"""
            SELECT COUNT(*) AS observed,
                   COUNT(DISTINCT CASE WHEN {origin}
                                       THEN activity.observed_session_id END) AS sessions,
                   SUM(CASE WHEN {origin} THEN 1 ELSE 0 END) AS originated,
                   SUM(CASE WHEN {origin} AND activity.command_fingerprint IS NOT NULL
                            THEN 1 ELSE 0 END) AS commands,
                   SUM(CASE WHEN {origin} AND activity.result_status != 'unknown'
                            THEN 1 ELSE 0 END) AS known_results,
                   SUM(CASE WHEN {origin} AND activity.result_status = 'failure'
                            THEN 1 ELSE 0 END) AS failed_results,
                   SUM(CASE WHEN {origin} AND activity.command_category = ?
                            THEN 1 ELSE 0 END) AS test_invocations,
                   SUM(CASE WHEN {origin} AND activity.command_category = ?
                            THEN 1 ELSE 0 END) AS git_inspections,
                   SUM(CASE WHEN {origin} AND activity.command_category = ?
                            THEN 1 ELSE 0 END) AS patch_edits,
                   SUM(CASE WHEN activity.provenance_status IN
                                ('inherited_exact', 'inherited_prefix', 'observed_duplicate')
                            THEN 1 ELSE 0 END) AS inherited,
                   SUM(CASE WHEN activity.provenance_status = 'ambiguous'
                            THEN 1 ELSE 0 END) AS ambiguous,
                   SUM(CASE WHEN activity.provenance_status = 'unknown'
                            THEN 1 ELSE 0 END) AS unknown_count
            {source}
            """,
            (
                CommandCategory.TESTING.value,
                CommandCategory.GIT_INSPECTION.value,
                CommandCategory.EDITING_PATCHING.value,
                *parameters,
            ),
        ).fetchone()
        if summary is None:
            raise RuntimeError("SQLite did not return a tool activity summary")
        tools = _group_query(
            connection, source, parameters, expression="activity.tool_name", limit=selected.limit
        )
        categories = _group_query(
            connection,
            source,
            parameters,
            expression="activity.command_category",
            limit=selected.limit,
        )
        executables = _group_query(
            connection,
            source,
            parameters,
            expression="activity.executable",
            limit=selected.limit,
            additional_condition="activity.command_fingerprint IS NOT NULL "
            "AND activity.executable IS NOT NULL",
        )
        repeated_rows = []
        if repeated:
            query, repeated_parameters = _activity_query(
                selected,
                resolved_session=resolved_session,
                commands_only=True,
                originated_only=True,
            )
            repeated_rows = connection.execute(query, repeated_parameters).fetchall()

    session_count = int(summary["sessions"] or 0)
    originated_tool_calls = int(summary["originated"] or 0)
    originated_commands = int(summary["commands"] or 0)
    known_results = int(summary["known_results"] or 0)
    failed_results = int(summary["failed_results"] or 0)
    return ToolActivityReport(
        session_count=session_count,
        originated_tool_calls=originated_tool_calls,
        originated_commands=originated_commands,
        commands_per_session=(originated_commands / session_count if session_count else None),
        test_invocations=int(summary["test_invocations"] or 0),
        git_inspections=int(summary["git_inspections"] or 0),
        patch_edits=int(summary["patch_edits"] or 0),
        known_results=known_results,
        failed_results=failed_results,
        failure_rate=(failed_results / known_results if known_results else None),
        provenance=ToolProvenanceCoverage(
            observed=int(summary["observed"] or 0),
            originated=originated_tool_calls,
            inherited=int(summary["inherited"] or 0),
            ambiguous=int(summary["ambiguous"] or 0),
            unknown=int(summary["unknown_count"] or 0),
        ),
        tools=tools,
        categories=categories,
        executables=executables,
        repeated_commands=(
            _repeated_commands(repeated_rows, selected.limit) if repeated else ()
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
        source, parameters = _activity_source(
            selected,
            resolved_session=resolved_session,
            commands_only=True,
        )
        expression = {
            "repo": "COALESCE(sessions.repository_root, 'outside-git')",
            "model": "COALESCE(NULLIF(sessions.model, ''), 'unknown')",
            "category": "activity.command_category",
        }[dimension]
        rows = connection.execute(
            f"""
            SELECT {expression} AS key, COUNT(*) AS activity_count
            {source}
            AND {_origin_condition()}
            GROUP BY {expression}
            ORDER BY key
            """
            if "WHERE" in source
            else f"""
            SELECT {expression} AS key, COUNT(*) AS activity_count
            {source}
            WHERE {_origin_condition()}
            GROUP BY {expression}
            ORDER BY key
            """,
            parameters,
        ).fetchall()
    return {str(row["key"]): int(row["activity_count"]) for row in rows}


def _activity_source(
    filters: ToolFilters,
    *,
    resolved_session: int | None,
    commands_only: bool,
) -> tuple[str, tuple[object, ...]]:
    conditions: list[str] = []
    parameters: list[object] = []
    activity_time = "activity.effective_occurred_at"
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
        FROM tool_activity AS activity
        JOIN source_sessions AS sessions ON sessions.id = activity.observed_session_id
        LEFT JOIN session_tasks AS tasks ON tasks.session_id = sessions.id
        {where}
        """,
        tuple(parameters),
    )


def _activity_query(
    filters: ToolFilters,
    *,
    resolved_session: int | None,
    commands_only: bool,
    originated_only: bool = False,
) -> tuple[str, tuple[object, ...]]:
    source, parameters = _activity_source(
        filters,
        resolved_session=resolved_session,
        commands_only=commands_only,
    )
    connector = "AND" if "WHERE" in source else "WHERE"
    origin_filter = f"{connector} {_origin_condition()}" if originated_only else ""
    return (
        f"""
        SELECT activity.*, sessions.source_session_id, sessions.repository_root,
               sessions.repository_name, sessions.model, sessions.model_provider,
               sessions.started_at
        {source}
        {origin_filter}
        ORDER BY activity.effective_occurred_at IS NULL, activity.effective_occurred_at,
                 sessions.source_session_id, activity.source_ordinal,
                 activity.operation_ordinal
        """,
        parameters,
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


def _origin_condition() -> str:
    return (
        "activity.provenance_status = 'origin' "
        "AND activity.origin_session_id = activity.observed_session_id"
    )


def _group_query(
    connection: sqlite3.Connection,
    source: str,
    parameters: tuple[object, ...],
    *,
    expression: str,
    limit: int,
    additional_condition: str | None = None,
) -> tuple[ActivityGroup, ...]:
    connector = "AND" if "WHERE" in source else "WHERE"
    extra = f" AND {additional_condition}" if additional_condition else ""
    rows = connection.execute(
        f"""
        SELECT {expression} AS key, COUNT(*) AS activity_count
        {source}
        {connector} {_origin_condition()}{extra}
        GROUP BY {expression}
        ORDER BY activity_count DESC, key
        LIMIT ?
        """,
        (*parameters, limit),
    ).fetchall()
    return tuple(
        ActivityGroup(key=str(row["key"]), count=int(row["activity_count"]))
        for row in rows
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
