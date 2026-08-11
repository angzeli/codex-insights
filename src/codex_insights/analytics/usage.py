"""Token-coverage-aware usage analytics over the normalized index."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from enum import StrEnum
from pathlib import Path
from statistics import fmean, median
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from codex_insights.analytics.queries import SessionFilters
from codex_insights.analytics.temporal_usage import (
    VECTOR_FIELDS,
    StoredTokenEvent,
    TemporalAttribution,
    TemporalContribution,
    attribute_session_usage,
    sum_vectors,
)
from codex_insights.db import open_index
from codex_insights.models import UsageVector

TOKEN_FIELDS = (
    "total_tokens",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


class UsageBreakdown(StrEnum):
    """Supported usage report dimensions."""

    SUMMARY = "summary"
    REPOSITORY = "repo"
    MODEL = "model"
    DAY = "day"
    WEEK = "week"


class TimezoneError(ValueError):
    """Raised when a requested timezone cannot be resolved."""


@dataclass(frozen=True, slots=True)
class TimezoneSpec:
    """Resolved timezone plus a stable display label."""

    timezone: tzinfo
    label: str


@dataclass(frozen=True, slots=True)
class UsageCoverage:
    """Per-field session coverage; missing fields are never counted as zero."""

    session_count: int
    total_tokens: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int

    def to_dict(self) -> dict[str, int]:
        return {
            "session_count": self.session_count,
            "total_tokens": self.total_tokens,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
        }


@dataclass(frozen=True, slots=True)
class UsageMetrics:
    """Aggregate token and session metrics with explicit coverage."""

    session_count: int
    total_tokens: int | None
    observed_total_tokens: int | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    mean_tokens_per_session: float | None
    median_tokens_per_session: float | None
    p90_tokens_per_session: float | None
    sessions_per_day: float | None
    coverage: UsageCoverage

    def to_dict(self) -> dict[str, object]:
        return {
            "session_count": self.session_count,
            "total_tokens": self.total_tokens,
            "observed_total_tokens": self.observed_total_tokens,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "mean_tokens_per_session": self.mean_tokens_per_session,
            "median_tokens_per_session": self.median_tokens_per_session,
            "p90_tokens_per_session": self.p90_tokens_per_session,
            "sessions_per_day": self.sessions_per_day,
            "token_semantics": "reconciled_aggregate",
            "distribution_semantics": "observed_per_rollout",
            "coverage": self.coverage.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class UsageReconciliation:
    """Aggregate evidence for observed versus lineage-adjusted token totals."""

    observed_rollout_tokens: int | None
    inherited_replayed_tokens: int | None
    reconciled_tokens: int | None
    session_count: int
    sessions_with_token_data: int
    root_threads: int
    child_threads: int
    confidently_reconciled_children: int
    independent_children: int
    ambiguous_children: int
    unavailable_children: int
    cyclic_children: int
    ambiguous_observed_tokens: int | None
    orphan_relationships: int
    child_reconciliation_coverage: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "observed_rollout_tokens": self.observed_rollout_tokens,
            "inherited_replayed_tokens": self.inherited_replayed_tokens,
            "reconciled_tokens": self.reconciled_tokens,
            "session_count": self.session_count,
            "sessions_with_token_data": self.sessions_with_token_data,
            "root_threads": self.root_threads,
            "child_threads": self.child_threads,
            "confidently_reconciled_children": self.confidently_reconciled_children,
            "independent_children": self.independent_children,
            "ambiguous_children": self.ambiguous_children,
            "unavailable_children": self.unavailable_children,
            "cyclic_children": self.cyclic_children,
            "ambiguous_observed_tokens": self.ambiguous_observed_tokens,
            "orphan_relationships": self.orphan_relationships,
            "child_reconciliation_coverage": self.child_reconciliation_coverage,
        }


@dataclass(frozen=True, slots=True)
class UsageTemporalCoverage:
    """Coverage of event-time attribution for the selected session set."""

    complete_sessions: int
    partial_sessions: int
    fallback_sessions: int
    unavailable_sessions: int
    attributed_total_tokens: int | None
    unattributed_total_tokens: int | None
    attributed_fraction: float | None
    fallback_reasons: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "complete_sessions": self.complete_sessions,
            "partial_sessions": self.partial_sessions,
            "fallback_sessions": self.fallback_sessions,
            "unavailable_sessions": self.unavailable_sessions,
            "attributed_total_tokens": self.attributed_total_tokens,
            "unattributed_total_tokens": self.unattributed_total_tokens,
            "attributed_fraction": self.attributed_fraction,
            "fallback_reasons": dict(self.fallback_reasons),
        }


@dataclass(frozen=True, slots=True)
class UsageGroup:
    """One repository, model, or local-time period in a usage report."""

    key: str
    label: str
    metrics: UsageMetrics
    repository_root: Path | None = None
    model_provider: str | None = None
    period_start: date | None = None
    period_end: date | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "repository_root": str(self.repository_root) if self.repository_root else None,
            "model_provider": self.model_provider,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "metrics": self.metrics.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class UsageReport:
    """Complete usage result suitable for terminal or JSON presentation."""

    breakdown: UsageBreakdown
    timezone: str
    since: datetime | None
    until: datetime | None
    metrics: UsageMetrics
    groups: tuple[UsageGroup, ...]
    temporal_coverage: UsageTemporalCoverage
    reconciliation: UsageReconciliation | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "breakdown": self.breakdown.value,
            "timezone": self.timezone,
            "since": _json_datetime(self.since),
            "until": _json_datetime(self.until),
            "metrics": self.metrics.to_dict(),
            "groups": [group.to_dict() for group in self.groups],
            "temporal_coverage": self.temporal_coverage.to_dict(),
            "reconciliation": (
                self.reconciliation.to_dict() if self.reconciliation is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class _TemporalSession:
    row: sqlite3.Row
    attribution: TemporalAttribution
    contributions: tuple[TemporalContribution, ...]
    scoped_usage: UsageVector


def resolve_timezone(value: str | None) -> TimezoneSpec:
    """Resolve ``local``, UTC, or an IANA timezone name."""

    requested = (value or "local").strip()
    if not requested or requested.casefold() == "local":
        local = datetime.now().astimezone().tzinfo or UTC
        return TimezoneSpec(timezone=local, label="local")
    if requested.casefold() in {"utc", "z"}:
        return TimezoneSpec(timezone=UTC, label="UTC")
    try:
        resolved = ZoneInfo(requested)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimezoneError(f"Unknown timezone {value!r}; use local, UTC, or an IANA name") from exc
    return TimezoneSpec(timezone=resolved, label=resolved.key)


def get_usage_report(
    database_path: Path,
    *,
    codex_home: Path,
    breakdown: UsageBreakdown = UsageBreakdown.SUMMARY,
    filters: SessionFilters | None = None,
    timezone: TimezoneSpec | None = None,
    top: int | None = None,
    now: datetime | None = None,
    include_reconciliation: bool = False,
) -> UsageReport:
    """Aggregate lineage-adjusted totals without reopening source rollouts."""

    selected = filters or SessionFilters(limit=1)
    zone = timezone or resolve_timezone(None)
    reference = _as_utc(now or datetime.now(tz=UTC))
    if top is not None and top < 1:
        raise ValueError("top must be at least 1")
    if top is not None and breakdown not in {
        UsageBreakdown.REPOSITORY,
        UsageBreakdown.MODEL,
    }:
        raise ValueError("top is available only for repository or model breakdowns")

    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        rows = connection.execute(
            _usage_query(selected),
            _usage_parameters(selected),
        ).fetchall()
        event_rows = connection.execute(
            """
            SELECT * FROM token_events
            ORDER BY source_session_id, event_ordinal
            """
        ).fetchall()
        orphan_relationships = int(
            connection.execute(
                "SELECT COUNT(*) FROM thread_relationships "
                "WHERE parent_session_id IS NULL OR child_session_id IS NULL"
            ).fetchone()[0]
        )

    sessions = _temporal_sessions(rows, event_rows, selected)
    scope_start, scope_end = _scope_dates(sessions, selected, zone.timezone, reference)
    scope_days = _calendar_days(scope_start, scope_end)
    metrics = _session_metrics(sessions, scope_days)
    groups = _groups(sessions, breakdown, zone.timezone, scope_start, scope_end, selected)
    if top is not None:
        groups = groups[:top]
    return UsageReport(
        breakdown=breakdown,
        timezone=zone.label,
        since=selected.since,
        until=selected.until,
        metrics=metrics,
        groups=groups,
        temporal_coverage=_temporal_coverage(sessions),
        reconciliation=(
            _reconciliation(sessions, orphan_relationships)
            if include_reconciliation
            else None
        ),
    )


def _usage_query(filters: SessionFilters) -> str:
    conditions: list[str] = []
    if filters.repository:
        if filters.repository.casefold() in {"outside-git", "non-git", "none"}:
            conditions.append("s.repository_root IS NULL")
        else:
            conditions.append("(s.repository_name = ? COLLATE NOCASE OR s.repository_root = ?)")
    if filters.model:
        if filters.model.casefold() in {"unknown", "none"}:
            conditions.append("(s.model IS NULL OR s.model = '')")
        else:
            conditions.append("s.model = ? COLLATE NOCASE")
    if filters.task_action:
        conditions.append("COALESCE(tasks.action, 'unknown') = ?")
    if filters.task_domain:
        conditions.append("COALESCE(tasks.domain, 'unknown') = ?")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return f"""
        SELECT s.id, s.source_session_id, s.started_at, s.repository_root, s.repository_name,
               s.model, s.model_provider, u.usage_semantics,
               u.aggregate_total_tokens AS total_tokens,
               u.aggregate_input_tokens AS input_tokens,
               u.aggregate_cached_input_tokens AS cached_input_tokens,
               u.aggregate_cache_write_input_tokens AS cache_write_input_tokens,
               u.aggregate_output_tokens AS output_tokens,
               u.aggregate_reasoning_output_tokens AS reasoning_output_tokens,
               u.observed_total_tokens,
               u.accounting_status,
               u.inherited_baseline_total_tokens,
               tl.baseline_input_tokens, tl.baseline_cached_input_tokens,
               tl.baseline_cache_write_input_tokens, tl.baseline_output_tokens,
               tl.baseline_reasoning_output_tokens, tl.baseline_total_tokens
        FROM source_sessions AS s
        LEFT JOIN accounted_usage AS u ON u.source_session_id = s.id
        LEFT JOIN token_lineage AS tl ON tl.child_session_id = s.id
        LEFT JOIN session_tasks AS tasks ON tasks.session_id = s.id
        {where}
        ORDER BY s.started_at IS NULL, s.started_at ASC, s.source_session_id ASC
    """


def _usage_parameters(filters: SessionFilters) -> tuple[object, ...]:
    parameters: list[object] = []
    if filters.repository and filters.repository.casefold() not in {
        "outside-git",
        "non-git",
        "none",
    }:
        parameters.extend((filters.repository, filters.repository))
    if filters.model and filters.model.casefold() not in {"unknown", "none"}:
        parameters.append(filters.model)
    if filters.task_action:
        parameters.append(filters.task_action.casefold())
    if filters.task_domain:
        parameters.append(filters.task_domain.casefold())
    return tuple(parameters)


def _groups(
    sessions: list[_TemporalSession],
    breakdown: UsageBreakdown,
    timezone: tzinfo,
    scope_start: date | None,
    scope_end: date | None,
    filters: SessionFilters,
) -> tuple[UsageGroup, ...]:
    if breakdown is UsageBreakdown.SUMMARY:
        return ()
    if breakdown in {UsageBreakdown.DAY, UsageBreakdown.WEEK}:
        results = _period_groups(
            sessions,
            breakdown,
            timezone,
            scope_start,
            scope_end,
            include_unattributed=filters.since is None and filters.until is None,
        )
    else:
        grouped: dict[object, list[_TemporalSession]] = defaultdict(list)
        for session in sessions:
            grouped[_dimension_key(session.row, breakdown)].append(session)
        results = [
            _dimension_group(key, grouped_sessions, breakdown, scope_start, scope_end)
            for key, grouped_sessions in grouped.items()
        ]
    if breakdown in {UsageBreakdown.DAY, UsageBreakdown.WEEK}:
        results.sort(key=lambda group: (group.period_start is None, group.period_start, group.key))
    else:
        results.sort(
            key=lambda group: (
                group.metrics.total_tokens is None,
                -(group.metrics.total_tokens or 0),
                -group.metrics.session_count,
                group.label.casefold(),
                group.key,
            )
        )
    return tuple(results)


def _dimension_key(row: sqlite3.Row, breakdown: UsageBreakdown) -> object:
    if breakdown is UsageBreakdown.REPOSITORY:
        return row["repository_root"]
    return (row["model"], row["model_provider"])


def _dimension_group(
    key: object,
    sessions: list[_TemporalSession],
    breakdown: UsageBreakdown,
    scope_start: date | None,
    scope_end: date | None,
) -> UsageGroup:
    days = _calendar_days(scope_start, scope_end)
    if breakdown is UsageBreakdown.REPOSITORY:
        root = Path(str(key)) if key is not None else None
        name = next(
            (
                str(session.row["repository_name"])
                for session in sessions
                if session.row["repository_name"]
            ),
            None,
        )
        label = name or (root.name if root else "Outside Git repositories")
        return UsageGroup(
            key=str(root) if root else "outside-git",
            label=label,
            repository_root=root,
            metrics=_session_metrics(sessions, days),
        )
    model, provider = key if isinstance(key, tuple) else (None, None)
    label = str(model) if model else "Unknown model"
    return UsageGroup(
        key=f"{model or 'unknown'}::{provider or 'unknown'}",
        label=label,
        model_provider=str(provider) if provider else None,
        metrics=_session_metrics(sessions, days),
    )


def _session_metrics(sessions: list[_TemporalSession], days: int | None) -> UsageMetrics:
    values = {
        field: [
            int(value)
            for session in sessions
            if (value := getattr(session.scoped_usage, field)) is not None
        ]
        for field in TOKEN_FIELDS
    }
    totals = values["total_tokens"]
    observed_totals = [
        int(session.row["observed_total_tokens"])
        for session in sessions
        if session.row["observed_total_tokens"] is not None
    ]
    coverage = UsageCoverage(
        session_count=len(sessions),
        total_tokens=len(totals),
        input_tokens=len(values["input_tokens"]),
        cached_input_tokens=len(values["cached_input_tokens"]),
        output_tokens=len(values["output_tokens"]),
        reasoning_output_tokens=len(values["reasoning_output_tokens"]),
    )
    return UsageMetrics(
        session_count=len(sessions),
        total_tokens=_sum_known(totals),
        observed_total_tokens=_sum_known(observed_totals),
        input_tokens=_sum_known(values["input_tokens"]),
        cached_input_tokens=_sum_known(values["cached_input_tokens"]),
        output_tokens=_sum_known(values["output_tokens"]),
        reasoning_output_tokens=_sum_known(values["reasoning_output_tokens"]),
        mean_tokens_per_session=fmean(observed_totals) if observed_totals else None,
        median_tokens_per_session=(
            float(median(observed_totals)) if observed_totals else None
        ),
        p90_tokens_per_session=(
            float(_nearest_rank(observed_totals, 0.90)) if observed_totals else None
        ),
        sessions_per_day=(len(sessions) / days if days else None),
        coverage=coverage,
    )


@dataclass(slots=True)
class _PeriodBucket:
    started_sessions: dict[int, _TemporalSession]
    contributing_sessions: dict[int, _TemporalSession]
    contributions: list[UsageVector]


def _period_groups(
    sessions: list[_TemporalSession],
    breakdown: UsageBreakdown,
    timezone: tzinfo,
    scope_start: date | None,
    scope_end: date | None,
    *,
    include_unattributed: bool,
) -> list[UsageGroup]:
    buckets: dict[date | None, _PeriodBucket] = {}

    def bucket(key: date | None) -> _PeriodBucket:
        return buckets.setdefault(key, _PeriodBucket({}, {}, []))

    for session in sessions:
        session_id = int(session.row["id"])
        started = _stored_datetime(session.row["started_at"])
        if started is not None:
            start_key = _period_key(started, breakdown, timezone)
            bucket(start_key).started_sessions[session_id] = session
        for contribution in session.contributions:
            if contribution.occurred_at is None:
                if include_unattributed:
                    current = bucket(None)
                else:
                    continue
            else:
                current = bucket(_period_key(contribution.occurred_at, breakdown, timezone))
            current.contributing_sessions[session_id] = session
            current.contributions.append(contribution.usage)

    results: list[UsageGroup] = []
    for key, current in buckets.items():
        period_start, period_end = _period_dates(key, breakdown)
        days = _group_days(breakdown, period_start, period_end, scope_start, scope_end)
        label = period_start.isoformat() if period_start else "Unattributed time"
        results.append(
            UsageGroup(
                key=label,
                label=label,
                period_start=period_start,
                period_end=period_end,
                metrics=_period_metrics(current, days),
            )
        )
    return results


def _period_metrics(bucket: _PeriodBucket, days: int | None) -> UsageMetrics:
    usage = sum_vectors(bucket.contributions)
    started = list(bucket.started_sessions.values())
    observed = [
        int(session.row["observed_total_tokens"])
        for session in started
        if session.row["observed_total_tokens"] is not None
    ]
    coverage_values = {
        field: sum(
            getattr(session.scoped_usage, field) is not None
            for session in bucket.contributing_sessions.values()
        )
        for field in TOKEN_FIELDS
    }
    return UsageMetrics(
        session_count=len(started),
        total_tokens=usage.total_tokens,
        observed_total_tokens=_sum_known(observed),
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_output_tokens=usage.reasoning_output_tokens,
        mean_tokens_per_session=fmean(observed) if observed else None,
        median_tokens_per_session=float(median(observed)) if observed else None,
        p90_tokens_per_session=float(_nearest_rank(observed, 0.90)) if observed else None,
        sessions_per_day=len(started) / days if days else None,
        coverage=UsageCoverage(
            session_count=len(started),
            total_tokens=coverage_values["total_tokens"],
            input_tokens=coverage_values["input_tokens"],
            cached_input_tokens=coverage_values["cached_input_tokens"],
            output_tokens=coverage_values["output_tokens"],
            reasoning_output_tokens=coverage_values["reasoning_output_tokens"],
        ),
    )


def _temporal_sessions(
    rows: list[sqlite3.Row],
    event_rows: list[sqlite3.Row],
    filters: SessionFilters,
) -> list[_TemporalSession]:
    events_by_session: dict[int, list[StoredTokenEvent]] = defaultdict(list)
    for event in event_rows:
        events_by_session[int(event["source_session_id"])].append(
            StoredTokenEvent(
                source_ordinal=int(event["source_ordinal"]),
                occurred_at=_stored_datetime(event["occurred_at"]),
                cumulative=_event_vector(event, "cumulative"),
                delta=_event_vector(event, "delta"),
            )
        )

    sessions: list[_TemporalSession] = []
    bounded = filters.since is not None or filters.until is not None
    for row in rows:
        session_id = int(row["id"])
        status = str(row["accounting_status"] or "root")
        attribution = attribute_session_usage(
            semantics=str(row["usage_semantics"] or "unavailable"),
            target=_row_vector(row, ""),
            inherited_baseline=(
                _row_vector(row, "baseline_")
                if status in {"inherited_exact", "inherited_prefix"}
                else None
            ),
            events=tuple(events_by_session.get(session_id, ())),
        )
        if bounded:
            contributions = tuple(
                contribution
                for contribution in attribution.contributions
                if contribution.occurred_at is not None
                and _in_window(contribution.occurred_at, filters)
            )
            started = _stored_datetime(row["started_at"])
            if not contributions and not (
                started is not None and _in_window(started, filters)
            ):
                continue
        else:
            contributions = attribution.contributions
        sessions.append(
            _TemporalSession(
                row=row,
                attribution=attribution,
                contributions=contributions,
                scoped_usage=sum_vectors(item.usage for item in contributions),
            )
        )
    return sessions


def _event_vector(row: sqlite3.Row, prefix: str) -> UsageVector | None:
    vector = UsageVector(
        **{field: row[f"{prefix}_{field}"] for field in VECTOR_FIELDS}
    )
    return vector if any(getattr(vector, field) is not None for field in VECTOR_FIELDS) else None


def _row_vector(row: sqlite3.Row, prefix: str) -> UsageVector:
    return UsageVector(**{field: row[f"{prefix}{field}"] for field in VECTOR_FIELDS})


def _in_window(value: datetime, filters: SessionFilters) -> bool:
    return (filters.since is None or value >= filters.since) and (
        filters.until is None or value < filters.until
    )


def _period_key(value: datetime, breakdown: UsageBreakdown, timezone: tzinfo) -> date:
    local_date = value.astimezone(timezone).date()
    if breakdown is UsageBreakdown.WEEK:
        return local_date - timedelta(days=local_date.weekday())
    return local_date


def _temporal_coverage(sessions: list[_TemporalSession]) -> UsageTemporalCoverage:
    statuses = Counter(session.attribution.status for session in sessions)
    reasons = Counter(
        session.attribution.reason
        for session in sessions
        if session.attribution.reason is not None
    )
    attributed = sum_vectors(
        contribution.usage
        for session in sessions
        for contribution in session.contributions
        if contribution.occurred_at is not None
    ).total_tokens
    unattributed = sum_vectors(
        session.attribution.unattributed_usage for session in sessions
    ).total_tokens
    has_known_tokens = any(
        session.row["total_tokens"] is not None for session in sessions
    )
    if has_known_tokens:
        attributed = attributed or 0
        unattributed = unattributed or 0
    denominator = (attributed or 0) + (unattributed or 0)
    return UsageTemporalCoverage(
        complete_sessions=statuses["complete"],
        partial_sessions=statuses["partial"],
        fallback_sessions=statuses["fallback"],
        unavailable_sessions=statuses["unavailable"],
        attributed_total_tokens=attributed,
        unattributed_total_tokens=unattributed,
        attributed_fraction=(attributed or 0) / denominator if denominator else None,
        fallback_reasons=tuple(sorted((str(key), value) for key, value in reasons.items())),
    )


def _reconciliation(
    sessions: list[_TemporalSession],
    orphan_relationships: int,
) -> UsageReconciliation:
    rows = [session.row for session in sessions]
    observed = [
        int(row["observed_total_tokens"])
        for row in rows
        if row["observed_total_tokens"] is not None
    ]
    reconciled = [
        int(session.scoped_usage.total_tokens)
        for session in sessions
        if session.scoped_usage.total_tokens is not None
    ]
    statuses = [str(row["accounting_status"] or "root") for row in rows]
    confident = {"inherited_exact", "inherited_prefix"}
    child_rows = [row for row in rows if str(row["accounting_status"] or "root") != "root"]
    children_with_observed = sum(row["observed_total_tokens"] is not None for row in child_rows)
    reconciled_children = sum(
        str(row["accounting_status"]) in confident | {"independent"}
        and row["observed_total_tokens"] is not None
        for row in child_rows
    )
    inherited = [
        int(row["inherited_baseline_total_tokens"])
        for row in rows
        if str(row["accounting_status"]) in confident
        and row["inherited_baseline_total_tokens"] is not None
    ]
    ambiguous = [
        int(row["observed_total_tokens"])
        for row in rows
        if str(row["accounting_status"]) in {"ambiguous", "cycle"}
        and row["observed_total_tokens"] is not None
    ]
    return UsageReconciliation(
        observed_rollout_tokens=_sum_known(observed),
        inherited_replayed_tokens=sum(inherited),
        reconciled_tokens=_sum_known(reconciled),
        session_count=len(rows),
        sessions_with_token_data=len(observed),
        root_threads=statuses.count("root"),
        child_threads=len(child_rows),
        confidently_reconciled_children=sum(status in confident for status in statuses),
        independent_children=statuses.count("independent"),
        ambiguous_children=statuses.count("ambiguous"),
        unavailable_children=statuses.count("unavailable"),
        cyclic_children=statuses.count("cycle"),
        ambiguous_observed_tokens=sum(ambiguous),
        orphan_relationships=orphan_relationships,
        child_reconciliation_coverage=(
            reconciled_children / children_with_observed if children_with_observed else None
        ),
    )


def _scope_dates(
    sessions: list[_TemporalSession],
    filters: SessionFilters,
    timezone: tzinfo,
    now: datetime,
) -> tuple[date | None, date | None]:
    dates: list[date] = []
    for session in sessions:
        started = _stored_datetime(session.row["started_at"])
        if started is not None:
            dates.append(started.astimezone(timezone).date())
        dates.extend(
            contribution.occurred_at.astimezone(timezone).date()
            for contribution in session.contributions
            if contribution.occurred_at is not None
        )
    start = filters.since.astimezone(timezone).date() if filters.since else min(dates, default=None)
    end: date | None
    if filters.until is not None:
        end = (filters.until - timedelta(microseconds=1)).astimezone(timezone).date()
    elif filters.since is not None:
        end = now.astimezone(timezone).date()
    else:
        end = max(dates, default=None)
    return start, end


def _period_dates(
    key: object,
    breakdown: UsageBreakdown,
) -> tuple[date | None, date | None]:
    if not isinstance(key, date):
        return None, None
    return (key, key + timedelta(days=6)) if breakdown is UsageBreakdown.WEEK else (key, key)


def _group_days(
    breakdown: UsageBreakdown,
    period_start: date | None,
    period_end: date | None,
    scope_start: date | None,
    scope_end: date | None,
) -> int | None:
    if breakdown in {UsageBreakdown.REPOSITORY, UsageBreakdown.MODEL}:
        return _calendar_days(scope_start, scope_end)
    if period_start is None or period_end is None:
        return None
    start = max(period_start, scope_start) if scope_start else period_start
    end = min(period_end, scope_end) if scope_end else period_end
    return _calendar_days(start, end)


def _calendar_days(start: date | None, end: date | None) -> int | None:
    if start is None or end is None or end < start:
        return None
    return (end - start).days + 1


def _sum_known(values: list[int]) -> int | None:
    return sum(values) if values else None


def _nearest_rank(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _stored_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _database_datetime(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _json_datetime(value: datetime | None) -> str | None:
    return _database_datetime(value) if value is not None else None


def _as_utc(value: datetime) -> datetime:
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC)
