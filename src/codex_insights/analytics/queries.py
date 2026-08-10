"""Reusable read-only queries over the normalized Codex Insights database."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, cast

from codex_insights.db import open_index

_DURATION_PATTERN = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[mhdw])$", re.IGNORECASE)

_SESSION_CTES = """
WITH event_totals AS (
    SELECT source_session_id, SUM(event_count) AS event_count
    FROM event_summary
    GROUP BY source_session_id
),
ranked_ingestion AS (
    SELECT ingestion_state.*,
           ROW_NUMBER() OVER (
               PARTITION BY source_home, source_session_id
               ORDER BY indexed_at DESC, source_path DESC
           ) AS rank
    FROM ingestion_state
)
"""

_SESSION_SELECT = """
SELECT s.*,
       u.usage_semantics, u.input_tokens, u.cached_input_tokens,
       u.cache_write_input_tokens, u.output_tokens, u.reasoning_output_tokens,
       u.total_tokens, u.token_update_count,
       CASE
           WHEN i.status LIKE 'indexed%' THEN COALESCE(e.event_count, 0)
           ELSE NULL
       END AS event_count,
       i.status AS coverage_status, i.error AS coverage_error,
       i.parser_version AS coverage_parser_version,
       i.source_schema_version AS coverage_schema_version,
       i.size_bytes AS coverage_size_bytes, i.mtime_ns AS coverage_mtime_ns,
       i.last_parsed_byte_offset AS coverage_parsed_byte_offset,
       i.indexed_at AS coverage_indexed_at,
       o.outcome, o.confidence AS outcome_confidence,
       o.evidence_json AS outcome_evidence_json,
       o.classifier_version AS outcome_classifier_version
FROM source_sessions AS s
LEFT JOIN usage AS u ON u.source_session_id = s.id
LEFT JOIN event_totals AS e ON e.source_session_id = s.id
LEFT JOIN ranked_ingestion AS i
       ON i.source_home = s.source_home
      AND i.source_session_id = s.source_session_id
      AND i.rank = 1
LEFT JOIN session_outcomes AS o ON o.session_id = s.id
LEFT JOIN session_tasks AS task_filter ON task_filter.session_id = s.id
"""


class TimeExpressionError(ValueError):
    """Raised when a CLI time boundary cannot be interpreted safely."""


class SessionNotFoundError(LookupError):
    """Raised when a session ID or prefix has no match."""


class AmbiguousSessionIdError(LookupError):
    """Raised when a session ID prefix matches more than one session."""

    def __init__(self, prefix: str, matches: tuple[str, ...]) -> None:
        super().__init__(f"Session prefix {prefix!r} matches {len(matches)} sessions")
        self.prefix = prefix
        self.matches = matches


@dataclass(frozen=True, slots=True)
class SessionFilters:
    """Normalized filters for deterministic session-list queries."""

    since: datetime | None = None
    until: datetime | None = None
    repository: str | None = None
    model: str | None = None
    task_action: str | None = None
    task_domain: str | None = None
    source: str | None = None
    archived: bool | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class TokenUsageView:
    """Token values that preserve the difference between unknown and zero."""

    semantics: str
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None
    token_update_count: int

    @property
    def known(self) -> bool:
        return self.semantics != "unavailable"

    def to_dict(self) -> dict[str, object]:
        return {
            "semantics": self.semantics,
            "known": self.known,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
            "token_update_count": self.token_update_count,
        }


@dataclass(frozen=True, slots=True)
class SourceCoverageView:
    """Latest structural ingestion coverage for a normalized session."""

    status: str
    parser_version: str | None
    source_schema_version: str | None
    size_bytes: int | None
    mtime_ns: int | None
    parsed_byte_offset: int | None
    indexed_at: datetime | None
    warning: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "parser_version": self.parser_version,
            "source_schema_version": self.source_schema_version,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "parsed_byte_offset": self.parsed_byte_offset,
            "indexed_at": _json_datetime(self.indexed_at),
            "warning": self.warning,
        }


@dataclass(frozen=True, slots=True)
class SessionToolSummary:
    """Compact provenance-aware tool evidence for session detail."""

    originated: int
    inherited: int
    ambiguous: int
    unknown: int
    failed_results: int
    command_categories: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "originated": self.originated,
            "inherited": self.inherited,
            "ambiguous": self.ambiguous,
            "unknown": self.unknown,
            "failed_results": self.failed_results,
            "command_categories": dict(self.command_categories),
            "semantics": "originated_events",
        }


@dataclass(frozen=True, slots=True)
class SessionOutcomeView:
    """Explainable outcome attached to session detail."""

    outcome: str
    confidence: str
    evidence: tuple[str, ...]
    classifier_version: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "classifier_version": self.classifier_version,
            "semantics": "originated_evidence",
        }


@dataclass(frozen=True, slots=True)
class SessionListItem:
    """Compact session row for terminal lists and machine-readable output."""

    session_id: str
    started_at: datetime | None
    apparent_ended_at: datetime | None
    duration_seconds: int | None
    repository: str
    model: str | None
    source: str
    archived: bool
    total_tokens: int | None
    event_count: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "started_at": _json_datetime(self.started_at),
            "apparent_ended_at": _json_datetime(self.apparent_ended_at),
            "duration_seconds": self.duration_seconds,
            "repository": self.repository,
            "model": self.model,
            "source": self.source,
            "archived": self.archived,
            "total_tokens": self.total_tokens,
            "event_count": self.event_count,
        }


@dataclass(frozen=True, slots=True)
class SessionDetail:
    """Normalized session metadata without transcript or raw event content."""

    session_id: str
    started_at: datetime | None
    updated_at: datetime | None
    apparent_ended_at: datetime | None
    duration_seconds: int | None
    source_type: str
    client_source: str | None
    cwd: Path | None
    repository_root: Path | None
    repository_name: str | None
    git_branch: str | None
    git_sha: str | None
    model: str | None
    model_provider: str | None
    codex_version: str | None
    archived: bool
    usage: TokenUsageView
    event_counts: tuple[tuple[str, int], ...]
    tool_activity: SessionToolSummary
    outcome: SessionOutcomeView
    source_coverage: SourceCoverageView
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "started_at": _json_datetime(self.started_at),
            "updated_at": _json_datetime(self.updated_at),
            "apparent_ended_at": _json_datetime(self.apparent_ended_at),
            "duration_seconds": self.duration_seconds,
            "source_type": self.source_type,
            "source": self.client_source or self.source_type,
            "cwd": str(self.cwd) if self.cwd else None,
            "repository_root": str(self.repository_root) if self.repository_root else None,
            "repository_name": self.repository_name,
            "git_branch": self.git_branch,
            "git_sha": self.git_sha,
            "model": self.model,
            "model_provider": self.model_provider,
            "codex_version": self.codex_version,
            "archived": self.archived,
            "usage": self.usage.to_dict(),
            "event_counts": dict(self.event_counts),
            "tool_activity": self.tool_activity.to_dict(),
            "outcome": self.outcome.to_dict(),
            "source_coverage": self.source_coverage.to_dict(),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class RepositorySummary:
    """Session and reconciled-token totals grouped by normalized repository."""

    repository: str
    repository_root: Path | None
    in_git_repository: bool
    session_count: int
    first_activity: datetime | None
    latest_activity: datetime | None
    total_known_tokens: int | None
    sessions_with_token_data: int

    def to_dict(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "repository_root": str(self.repository_root) if self.repository_root else None,
            "in_git_repository": self.in_git_repository,
            "session_count": self.session_count,
            "first_activity": _json_datetime(self.first_activity),
            "latest_activity": _json_datetime(self.latest_activity),
            "total_known_tokens": self.total_known_tokens,
            "sessions_with_token_data": self.sessions_with_token_data,
            "token_semantics": "reconciled_aggregate",
        }


@dataclass(frozen=True, slots=True)
class ModelSummary:
    """Session and reconciled-token totals grouped by normalized model/provider."""

    model: str
    model_provider: str | None
    session_count: int
    first_activity: datetime | None
    latest_activity: datetime | None
    total_known_tokens: int | None
    sessions_with_token_data: int

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "model_provider": self.model_provider,
            "session_count": self.session_count,
            "first_activity": _json_datetime(self.first_activity),
            "latest_activity": _json_datetime(self.latest_activity),
            "total_known_tokens": self.total_known_tokens,
            "sessions_with_token_data": self.sessions_with_token_data,
            "token_semantics": "reconciled_aggregate",
        }


@dataclass(frozen=True, slots=True)
class StatsSummary:
    """Top-level activity and coverage metrics for the normalized index."""

    indexed_sessions: int
    active_days: int
    repositories: int
    first_activity: datetime | None
    latest_activity: datetime | None
    sessions_today: int
    sessions_last_7_days: int
    sessions_last_30_days: int
    total_known_tokens: int | None
    sessions_with_token_data: int
    token_data_fraction: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "indexed_sessions": self.indexed_sessions,
            "active_days": self.active_days,
            "repositories": self.repositories,
            "first_activity": _json_datetime(self.first_activity),
            "latest_activity": _json_datetime(self.latest_activity),
            "sessions_today": self.sessions_today,
            "sessions_last_7_days": self.sessions_last_7_days,
            "sessions_last_30_days": self.sessions_last_30_days,
            "total_known_tokens": self.total_known_tokens,
            "sessions_with_token_data": self.sessions_with_token_data,
            "token_data_fraction": self.token_data_fraction,
            "token_semantics": "reconciled_aggregate",
        }


def parse_time_range(
    since: str | None,
    until: str | None,
    *,
    now: datetime | None = None,
    timezone: tzinfo = UTC,
) -> tuple[datetime | None, datetime | None]:
    """Parse inclusive ``since`` and exclusive ``until`` UTC boundaries."""

    reference = _as_utc(now or datetime.now(tz=UTC))
    parsed_since = _parse_time_expression(
        since,
        now=reference,
        date_until=False,
        timezone=timezone,
    )
    parsed_until = _parse_time_expression(
        until,
        now=reference,
        date_until=True,
        timezone=timezone,
    )
    if parsed_since is not None and parsed_until is not None and parsed_since >= parsed_until:
        raise TimeExpressionError("--since must be earlier than --until")
    return parsed_since, parsed_until


def list_sessions(
    database_path: Path,
    *,
    codex_home: Path,
    filters: SessionFilters | None = None,
) -> tuple[SessionListItem, ...]:
    """Return sessions in stable newest-first order with normalized filters."""

    selected = filters or SessionFilters()
    if selected.limit < 1:
        raise ValueError("limit must be at least 1")

    conditions: list[str] = []
    parameters: list[object] = []
    if selected.since is not None:
        conditions.append("s.started_at >= ?")
        parameters.append(_database_datetime(selected.since))
    if selected.until is not None:
        conditions.append("s.started_at < ?")
        parameters.append(_database_datetime(selected.until))
    if selected.repository:
        if selected.repository.casefold() in {"outside-git", "non-git", "none"}:
            conditions.append("s.repository_root IS NULL")
        else:
            conditions.append("(s.repository_name = ? COLLATE NOCASE OR s.repository_root = ?)")
            parameters.extend((selected.repository, selected.repository))
    if selected.model:
        if selected.model.casefold() in {"unknown", "none"}:
            conditions.append("(s.model IS NULL OR s.model = '')")
        else:
            conditions.append("s.model = ? COLLATE NOCASE")
            parameters.append(selected.model)
    if selected.task_action:
        conditions.append("COALESCE(task_filter.action, 'unknown') = ?")
        parameters.append(selected.task_action.casefold())
    if selected.task_domain:
        conditions.append("COALESCE(task_filter.domain, 'unknown') = ?")
        parameters.append(selected.task_domain.casefold())
    if selected.source:
        conditions.append("COALESCE(s.client_source, s.source_type) = ? COLLATE NOCASE")
        parameters.append(selected.source)
    if selected.archived is not None:
        conditions.append("s.archived = ?")
        parameters.append(int(selected.archived))

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = (
        _SESSION_CTES
        + _SESSION_SELECT
        + f"\n{where}\n"
        + "ORDER BY s.started_at IS NULL, s.started_at DESC, s.source_session_id ASC\n"
        + "LIMIT ?"
    )
    parameters.append(selected.limit)
    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        rows = connection.execute(query, parameters).fetchall()
    return tuple(_list_item(row) for row in rows)


def get_session(
    database_path: Path,
    session_id_or_prefix: str,
    *,
    codex_home: Path,
) -> SessionDetail:
    """Resolve an exact session ID or unambiguous prefix and return full metadata."""

    prefix = session_id_or_prefix.strip()
    if not prefix:
        raise SessionNotFoundError("Session ID cannot be empty")

    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        session_id = _resolve_session_id(connection, prefix)
        row = connection.execute(
            _SESSION_CTES + _SESSION_SELECT + "\nWHERE s.source_session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionNotFoundError(f"No session matches {prefix!r}")
        event_counts = tuple(
            (str(event["category"]), int(event["event_count"]))
            for event in connection.execute(
                """
                SELECT category, event_count
                FROM event_summary
                WHERE source_session_id = ?
                ORDER BY category
                """,
                (int(row["id"]),),
            )
        )
        tool_activity = _session_tool_summary(connection, int(row["id"]))
    return _session_detail(cast(sqlite3.Row, row), event_counts, tool_activity)


def list_repositories(
    database_path: Path,
    *,
    codex_home: Path,
) -> tuple[RepositorySummary, ...]:
    """Aggregate every indexed session, including a non-Git category."""

    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        rows = connection.execute(
            """
            SELECT s.repository_root,
                   MAX(s.repository_name) AS repository_name,
                   COUNT(*) AS session_count,
                   MIN(s.started_at) AS first_activity,
                   MAX(s.started_at) AS latest_activity,
                   SUM(u.aggregate_total_tokens) AS total_known_tokens,
                   SUM(CASE WHEN u.aggregate_total_tokens IS NOT NULL
                            THEN 1 ELSE 0 END) AS sessions_with_token_data
            FROM source_sessions AS s
            LEFT JOIN accounted_usage AS u ON u.source_session_id = s.id
            GROUP BY s.repository_root
            ORDER BY session_count DESC, latest_activity DESC,
                     COALESCE(repository_name, '') ASC, COALESCE(s.repository_root, '') ASC
            """
        ).fetchall()
    return tuple(_repository_summary(row) for row in rows)


def list_models(
    database_path: Path,
    *,
    codex_home: Path,
) -> tuple[ModelSummary, ...]:
    """Aggregate sessions and trustworthy token totals by model/provider."""

    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        rows = connection.execute(
            """
            SELECT s.model, s.model_provider,
                   COUNT(*) AS session_count,
                   MIN(s.started_at) AS first_activity,
                   MAX(s.started_at) AS latest_activity,
                   SUM(u.aggregate_total_tokens) AS total_known_tokens,
                   SUM(CASE WHEN u.aggregate_total_tokens IS NOT NULL
                            THEN 1 ELSE 0 END) AS sessions_with_token_data
            FROM source_sessions AS s
            LEFT JOIN accounted_usage AS u ON u.source_session_id = s.id
            GROUP BY s.model, s.model_provider
            ORDER BY session_count DESC, latest_activity DESC,
                     COALESCE(s.model, '') ASC, COALESCE(s.model_provider, '') ASC
            """
        ).fetchall()
    return tuple(_model_summary(row) for row in rows)


def get_stats(
    database_path: Path,
    *,
    codex_home: Path,
    now: datetime | None = None,
) -> StatsSummary:
    """Return overview metrics with explicit token-data coverage."""

    reference = _as_utc(now or datetime.now(tz=UTC))
    today_start = datetime.combine(reference.date(), time.min, tzinfo=UTC)
    tomorrow_start = today_start + timedelta(days=1)
    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS indexed_sessions,
                   COUNT(DISTINCT CASE WHEN s.started_at IS NOT NULL
                                       THEN substr(s.started_at, 1, 10) END) AS active_days,
                   COUNT(DISTINCT s.repository_root) AS repositories,
                   MIN(s.started_at) AS first_activity,
                   MAX(s.started_at) AS latest_activity,
                   SUM(CASE WHEN s.started_at >= ? AND s.started_at < ?
                            THEN 1 ELSE 0 END) AS sessions_today,
                   SUM(CASE WHEN s.started_at >= ? AND s.started_at <= ?
                            THEN 1 ELSE 0 END) AS sessions_last_7_days,
                   SUM(CASE WHEN s.started_at >= ? AND s.started_at <= ?
                            THEN 1 ELSE 0 END) AS sessions_last_30_days,
                   SUM(u.aggregate_total_tokens) AS total_known_tokens,
                   SUM(CASE WHEN u.aggregate_total_tokens IS NOT NULL
                            THEN 1 ELSE 0 END) AS sessions_with_token_data
            FROM source_sessions AS s
            LEFT JOIN accounted_usage AS u ON u.source_session_id = s.id
            """,
            (
                _database_datetime(today_start),
                _database_datetime(tomorrow_start),
                _database_datetime(reference - timedelta(days=7)),
                _database_datetime(reference),
                _database_datetime(reference - timedelta(days=30)),
                _database_datetime(reference),
            ),
        ).fetchone()
    if row is None:
        raise RuntimeError("Statistics query returned no row")
    indexed_sessions = int(row["indexed_sessions"])
    sessions_with_tokens = int(row["sessions_with_token_data"] or 0)
    return StatsSummary(
        indexed_sessions=indexed_sessions,
        active_days=int(row["active_days"]),
        repositories=int(row["repositories"]),
        first_activity=_stored_datetime(row["first_activity"]),
        latest_activity=_stored_datetime(row["latest_activity"]),
        sessions_today=int(row["sessions_today"] or 0),
        sessions_last_7_days=int(row["sessions_last_7_days"] or 0),
        sessions_last_30_days=int(row["sessions_last_30_days"] or 0),
        total_known_tokens=_optional_int(row["total_known_tokens"]),
        sessions_with_token_data=sessions_with_tokens,
        token_data_fraction=(sessions_with_tokens / indexed_sessions if indexed_sessions else None),
    )


def _resolve_session_id(connection: sqlite3.Connection, prefix: str) -> str:
    exact = connection.execute(
        "SELECT source_session_id FROM source_sessions WHERE source_session_id = ?",
        (prefix,),
    ).fetchone()
    if exact is not None:
        return str(exact[0])

    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    matches = tuple(
        str(row[0])
        for row in connection.execute(
            """
            SELECT source_session_id
            FROM source_sessions
            WHERE source_session_id LIKE ? ESCAPE '\\'
            ORDER BY source_session_id
            LIMIT 21
            """,
            (escaped + "%",),
        )
    )
    if not matches:
        raise SessionNotFoundError(f"No session matches {prefix!r}")
    if len(matches) > 1:
        raise AmbiguousSessionIdError(prefix, matches)
    return matches[0]


def _list_item(row: sqlite3.Row) -> SessionListItem:
    started = _stored_datetime(row["started_at"])
    ended = _stored_datetime(row["apparent_ended_at"])
    usage = _usage_view(row)
    return SessionListItem(
        session_id=str(row["source_session_id"]),
        started_at=started,
        apparent_ended_at=ended,
        duration_seconds=_duration_seconds(started, ended),
        repository=_repository_label(row["repository_name"], row["repository_root"]),
        model=_optional_str(row["model"]),
        source=_optional_str(row["client_source"]) or str(row["source_type"]),
        archived=bool(row["archived"]),
        total_tokens=usage.total_tokens,
        event_count=_optional_int(row["event_count"]),
    )


def _session_detail(
    row: sqlite3.Row,
    event_counts: tuple[tuple[str, int], ...],
    tool_activity: SessionToolSummary,
) -> SessionDetail:
    started = _stored_datetime(row["started_at"])
    ended = _stored_datetime(row["apparent_ended_at"])
    coverage = _coverage_view(row)
    warnings = tuple(
        warning
        for warning in (
            coverage.warning,
            (
                f"Source coverage status is {coverage.status}."
                if coverage.status not in {"indexed", "indexed_with_warnings"}
                else None
            ),
        )
        if warning
    )
    return SessionDetail(
        session_id=str(row["source_session_id"]),
        started_at=started,
        updated_at=_stored_datetime(row["updated_at"]),
        apparent_ended_at=ended,
        duration_seconds=_duration_seconds(started, ended),
        source_type=str(row["source_type"]),
        client_source=_optional_str(row["client_source"]),
        cwd=_optional_path(row["cwd"]),
        repository_root=_optional_path(row["repository_root"]),
        repository_name=_optional_str(row["repository_name"]),
        git_branch=_optional_str(row["git_branch"]),
        git_sha=_optional_str(row["git_sha"]),
        model=_optional_str(row["model"]),
        model_provider=_optional_str(row["model_provider"]),
        codex_version=_optional_str(row["codex_version"]),
        archived=bool(row["archived"]),
        usage=_usage_view(row),
        event_counts=event_counts,
        tool_activity=tool_activity,
        outcome=_outcome_view(row),
        source_coverage=coverage,
        warnings=warnings,
    )


def _outcome_view(row: sqlite3.Row) -> SessionOutcomeView:
    raw = row["outcome_evidence_json"]
    try:
        decoded = json.loads(str(raw)) if raw is not None else []
    except json.JSONDecodeError:
        decoded = ["invalid_evidence"]
    evidence = (
        tuple(str(item) for item in decoded)
        if isinstance(decoded, list)
        else ("invalid_evidence",)
    )
    return SessionOutcomeView(
        outcome=_optional_str(row["outcome"]) or "unknown",
        confidence=_optional_str(row["outcome_confidence"]) or "low",
        evidence=evidence,
        classifier_version=_optional_str(row["outcome_classifier_version"]),
    )


def _session_tool_summary(
    connection: sqlite3.Connection,
    session_id: int,
) -> SessionToolSummary:
    rows = connection.execute(
        "SELECT provenance_status, origin_session_id, result_status, command_category "
        "FROM tool_activity WHERE observed_session_id = ?",
        (session_id,),
    ).fetchall()
    originated = [
        row
        for row in rows
        if row["provenance_status"] == "origin" and row["origin_session_id"] == session_id
    ]
    categories: dict[str, int] = {}
    for activity in originated:
        category = str(activity["command_category"])
        categories[category] = categories.get(category, 0) + 1
    return SessionToolSummary(
        originated=len(originated),
        inherited=sum(
            row["provenance_status"]
            in {"inherited_exact", "inherited_prefix", "observed_duplicate"}
            for row in rows
        ),
        ambiguous=sum(row["provenance_status"] == "ambiguous" for row in rows),
        unknown=sum(row["provenance_status"] == "unknown" for row in rows),
        failed_results=sum(row["result_status"] == "failure" for row in originated),
        command_categories=tuple(sorted(categories.items())),
    )


def _usage_view(row: sqlite3.Row) -> TokenUsageView:
    semantics = _optional_str(row["usage_semantics"]) or "unavailable"
    known = semantics != "unavailable"
    return TokenUsageView(
        semantics=semantics,
        input_tokens=_optional_int(row["input_tokens"]) if known else None,
        cached_input_tokens=_optional_int(row["cached_input_tokens"]) if known else None,
        cache_write_input_tokens=_optional_int(row["cache_write_input_tokens"]) if known else None,
        output_tokens=_optional_int(row["output_tokens"]) if known else None,
        reasoning_output_tokens=(_optional_int(row["reasoning_output_tokens"]) if known else None),
        total_tokens=_optional_int(row["total_tokens"]) if known else None,
        token_update_count=int(row["token_update_count"] or 0),
    )


def _coverage_view(row: sqlite3.Row) -> SourceCoverageView:
    return SourceCoverageView(
        status=_optional_str(row["coverage_status"]) or "untracked",
        parser_version=_optional_str(row["coverage_parser_version"]),
        source_schema_version=_optional_str(row["coverage_schema_version"]),
        size_bytes=_optional_int(row["coverage_size_bytes"]),
        mtime_ns=_optional_int(row["coverage_mtime_ns"]),
        parsed_byte_offset=_optional_int(row["coverage_parsed_byte_offset"]),
        indexed_at=_stored_datetime(row["coverage_indexed_at"]),
        warning=_optional_str(row["coverage_error"]),
    )


def _repository_summary(row: sqlite3.Row) -> RepositorySummary:
    root = _optional_path(row["repository_root"])
    return RepositorySummary(
        repository=_repository_label(row["repository_name"], row["repository_root"]),
        repository_root=root,
        in_git_repository=root is not None,
        session_count=int(row["session_count"]),
        first_activity=_stored_datetime(row["first_activity"]),
        latest_activity=_stored_datetime(row["latest_activity"]),
        total_known_tokens=_optional_int(row["total_known_tokens"]),
        sessions_with_token_data=int(row["sessions_with_token_data"] or 0),
    )


def _model_summary(row: sqlite3.Row) -> ModelSummary:
    return ModelSummary(
        model=_optional_str(row["model"]) or "Unknown model",
        model_provider=_optional_str(row["model_provider"]),
        session_count=int(row["session_count"]),
        first_activity=_stored_datetime(row["first_activity"]),
        latest_activity=_stored_datetime(row["latest_activity"]),
        total_known_tokens=_optional_int(row["total_known_tokens"]),
        sessions_with_token_data=int(row["sessions_with_token_data"] or 0),
    )


def _parse_time_expression(
    expression: str | None,
    *,
    now: datetime,
    date_until: bool,
    timezone: tzinfo,
) -> datetime | None:
    if expression is None:
        return None
    value = expression.strip()
    if not value:
        raise TimeExpressionError("Time boundary cannot be empty")

    duration = _DURATION_PATTERN.fullmatch(value)
    if duration:
        amount = int(duration.group("amount"))
        unit = duration.group("unit").lower()
        multipliers = {
            "m": timedelta(minutes=1),
            "h": timedelta(hours=1),
            "d": timedelta(days=1),
            "w": timedelta(weeks=1),
        }
        return now - amount * multipliers[unit]

    try:
        parsed_date = date.fromisoformat(value)
    except ValueError:
        parsed_date = None
    if parsed_date is not None:
        boundary = datetime.combine(parsed_date, time.min, tzinfo=timezone)
        if date_until:
            boundary += timedelta(days=1)
        return _as_utc(boundary)

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimeExpressionError(
            f"Unsupported time {expression!r}; use ISO 8601 or a duration such as 7d or 24h"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return _as_utc(parsed)


def _duration_seconds(started: datetime | None, ended: datetime | None) -> int | None:
    if started is None or ended is None or ended < started:
        return None
    return int((ended - started).total_seconds())


def _repository_label(name: Any, root: Any) -> str:
    normalized_name = _optional_str(name)
    if normalized_name:
        return normalized_name
    normalized_root = _optional_str(root)
    return Path(normalized_root).name if normalized_root else "Outside Git repositories"


def _database_datetime(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _stored_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _json_datetime(value: datetime | None) -> str | None:
    return _database_datetime(value) if value is not None else None


def _as_utc(value: datetime) -> datetime:
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC)


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _optional_path(value: Any) -> Path | None:
    text = _optional_str(value)
    return Path(text) if text else None
