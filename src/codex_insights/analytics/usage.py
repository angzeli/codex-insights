"""Token-coverage-aware usage analytics over the normalized index."""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from enum import StrEnum
from pathlib import Path
from statistics import fmean, median
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from codex_insights.analytics.queries import SessionFilters
from codex_insights.db import open_index

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
    reconciliation: UsageReconciliation | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "breakdown": self.breakdown.value,
            "timezone": self.timezone,
            "since": _json_datetime(self.since),
            "until": _json_datetime(self.until),
            "metrics": self.metrics.to_dict(),
            "groups": [group.to_dict() for group in self.groups],
            "reconciliation": (
                self.reconciliation.to_dict() if self.reconciliation is not None else None
            ),
        }


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
        orphan_relationships = int(
            connection.execute(
                "SELECT COUNT(*) FROM thread_relationships "
                "WHERE parent_session_id IS NULL OR child_session_id IS NULL"
            ).fetchone()[0]
        )

    scope_start, scope_end = _scope_dates(rows, selected, zone.timezone, reference)
    scope_days = _calendar_days(scope_start, scope_end)
    metrics = _metrics(rows, scope_days)
    groups = _groups(rows, breakdown, zone.timezone, scope_start, scope_end)
    if top is not None:
        groups = groups[:top]
    return UsageReport(
        breakdown=breakdown,
        timezone=zone.label,
        since=selected.since,
        until=selected.until,
        metrics=metrics,
        groups=groups,
        reconciliation=(
            _reconciliation(rows, orphan_relationships) if include_reconciliation else None
        ),
    )


def _usage_query(filters: SessionFilters) -> str:
    conditions: list[str] = []
    if filters.since is not None:
        conditions.append("s.started_at >= ?")
    if filters.until is not None:
        conditions.append("s.started_at < ?")
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
        SELECT s.source_session_id, s.started_at, s.repository_root, s.repository_name,
               s.model, s.model_provider, u.usage_semantics,
               u.aggregate_total_tokens AS total_tokens,
               u.aggregate_input_tokens AS input_tokens,
               u.aggregate_cached_input_tokens AS cached_input_tokens,
               u.aggregate_output_tokens AS output_tokens,
               u.aggregate_reasoning_output_tokens AS reasoning_output_tokens,
               u.observed_total_tokens,
               u.accounting_status,
               u.inherited_baseline_total_tokens
        FROM source_sessions AS s
        LEFT JOIN accounted_usage AS u ON u.source_session_id = s.id
        LEFT JOIN session_tasks AS tasks ON tasks.session_id = s.id
        {where}
        ORDER BY s.started_at IS NULL, s.started_at ASC, s.source_session_id ASC
    """


def _usage_parameters(filters: SessionFilters) -> tuple[object, ...]:
    parameters: list[object] = []
    if filters.since is not None:
        parameters.append(_database_datetime(filters.since))
    if filters.until is not None:
        parameters.append(_database_datetime(filters.until))
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
    rows: list[sqlite3.Row],
    breakdown: UsageBreakdown,
    timezone: tzinfo,
    scope_start: date | None,
    scope_end: date | None,
) -> tuple[UsageGroup, ...]:
    if breakdown is UsageBreakdown.SUMMARY:
        return ()

    grouped: dict[object, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[_group_key(row, breakdown, timezone)].append(row)

    results: list[UsageGroup] = []
    for key, grouped_rows in grouped.items():
        period_start, period_end = _period_dates(key, breakdown)
        days = _group_days(breakdown, period_start, period_end, scope_start, scope_end)
        results.append(
            _group_result(
                key,
                grouped_rows,
                breakdown,
                days,
                period_start,
                period_end,
            )
        )
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


def _group_key(row: sqlite3.Row, breakdown: UsageBreakdown, timezone: tzinfo) -> object:
    if breakdown is UsageBreakdown.REPOSITORY:
        return row["repository_root"]
    if breakdown is UsageBreakdown.MODEL:
        return (row["model"], row["model_provider"])
    started = _stored_datetime(row["started_at"])
    if started is None:
        return None
    local_date = started.astimezone(timezone).date()
    if breakdown is UsageBreakdown.WEEK:
        return local_date - timedelta(days=local_date.weekday())
    return local_date


def _group_result(
    key: object,
    rows: list[sqlite3.Row],
    breakdown: UsageBreakdown,
    days: int | None,
    period_start: date | None,
    period_end: date | None,
) -> UsageGroup:
    if breakdown is UsageBreakdown.REPOSITORY:
        root = Path(str(key)) if key is not None else None
        name = next((str(row["repository_name"]) for row in rows if row["repository_name"]), None)
        label = name or (root.name if root else "Outside Git repositories")
        return UsageGroup(
            key=str(root) if root else "outside-git",
            label=label,
            repository_root=root,
            metrics=_metrics(rows, days),
        )
    if breakdown is UsageBreakdown.MODEL:
        model, provider = cast(tuple[object | None, object | None], key)
        label = str(model) if model else "Unknown model"
        return UsageGroup(
            key=f"{model or 'unknown'}::{provider or 'unknown'}",
            label=label,
            model_provider=str(provider) if provider else None,
            metrics=_metrics(rows, days),
        )
    label = period_start.isoformat() if period_start else "Unknown date"
    return UsageGroup(
        key=label,
        label=label,
        period_start=period_start,
        period_end=period_end,
        metrics=_metrics(rows, days),
    )


def _metrics(rows: list[sqlite3.Row], days: int | None) -> UsageMetrics:
    values = {
        field: [int(row[field]) for row in rows if row[field] is not None] for field in TOKEN_FIELDS
    }
    totals = values["total_tokens"]
    observed_totals = [
        int(row["observed_total_tokens"])
        for row in rows
        if row["observed_total_tokens"] is not None
    ]
    coverage = UsageCoverage(
        session_count=len(rows),
        total_tokens=len(totals),
        input_tokens=len(values["input_tokens"]),
        cached_input_tokens=len(values["cached_input_tokens"]),
        output_tokens=len(values["output_tokens"]),
        reasoning_output_tokens=len(values["reasoning_output_tokens"]),
    )
    return UsageMetrics(
        session_count=len(rows),
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
        sessions_per_day=(len(rows) / days if days else None),
        coverage=coverage,
    )


def _reconciliation(
    rows: list[sqlite3.Row],
    orphan_relationships: int,
) -> UsageReconciliation:
    observed = [
        int(row["observed_total_tokens"])
        for row in rows
        if row["observed_total_tokens"] is not None
    ]
    reconciled = [int(row["total_tokens"]) for row in rows if row["total_tokens"] is not None]
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
    rows: list[sqlite3.Row],
    filters: SessionFilters,
    timezone: tzinfo,
    now: datetime,
) -> tuple[date | None, date | None]:
    dates = [
        started.astimezone(timezone).date()
        for row in rows
        if (started := _stored_datetime(row["started_at"])) is not None
    ]
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
