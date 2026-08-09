"""Shared weekly/monthly report model assembled from canonical analytics queries."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from codex_insights.analytics.git import GitFilters, get_commit_report
from codex_insights.analytics.outcomes import OutcomeFilters, get_outcome_report
from codex_insights.analytics.queries import SessionFilters, list_sessions
from codex_insights.analytics.tasks import (
    TaskBreakdown,
    TaskFilters,
    TaskReport,
    get_task_report,
)
from codex_insights.analytics.tools import (
    ToolFilters,
    get_tool_activity_report,
    originated_command_counts,
)
from codex_insights.analytics.usage import (
    TimezoneSpec,
    UsageBreakdown,
    UsageReport,
    get_usage_report,
)

REPORT_SCHEMA_VERSION = "codex-insights-report-v1"


class ReportKind(StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass(frozen=True, slots=True)
class ReportPeriod:
    start: date
    end: date
    start_utc: datetime
    end_utc: datetime

    def to_dict(self) -> dict[str, str]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "start_utc": _json_datetime(self.start_utc),
            "end_utc_exclusive": _json_datetime(self.end_utc),
        }


@dataclass(frozen=True, slots=True)
class AnalyticsReport:
    kind: ReportKind
    generated_at: datetime
    timezone: str
    period: ReportPeriod
    repository_filter: str | None
    model_filter: str | None
    overview: dict[str, object]
    activity: tuple[dict[str, object], ...]
    weekly_activity: tuple[dict[str, object], ...]
    repositories: tuple[dict[str, object], ...]
    models: tuple[dict[str, object], ...]
    task_actions: TaskReport
    task_domains: TaskReport
    tools: dict[str, object]
    git: dict[str, object]
    outcomes: dict[str, object]
    interesting_sessions: tuple[dict[str, object], ...]
    data_quality: dict[str, object]
    previous_period: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_kind": self.kind.value,
            "generated_at": _json_datetime(self.generated_at),
            "timezone": self.timezone,
            "period": self.period.to_dict(),
            "filters": {
                "repository": self.repository_filter,
                "model": self.model_filter,
            },
            "overview": self.overview,
            "activity": list(self.activity),
            "weekly_activity": list(self.weekly_activity),
            "repositories": list(self.repositories),
            "models": list(self.models),
            "tasks": {
                "actions": self.task_actions.to_dict(),
                "domains": self.task_domains.to_dict(),
            },
            "tools": self.tools,
            "git": self.git,
            "outcomes": self.outcomes,
            "interesting_sessions": list(self.interesting_sessions),
            "data_quality": self.data_quality,
            "previous_period": self.previous_period,
            "methodology": {
                "additive_tokens": "reconciled_local_contribution",
                "session_distributions": "observed_per_rollout",
                "prompts": "logical_origin_aware",
                "tools_and_commands": "originated_events",
                "git": "provenance_aware_confidence_tiers",
                "outcomes": "originated_evidence_with_unknown_retained",
                "tasks": "origin_thread_intent",
                "local_telemetry_notice": (
                    "Local Codex telemetry is not guaranteed to reproduce server-side "
                    "billing or quota accounting."
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class _PeriodData:
    usage: UsageReport
    active_days: int
    repository_count: int
    model_count: int
    originated_commands: int
    high_confidence_commits: int
    task_report: TaskReport


def resolve_report_period(
    kind: ReportKind,
    *,
    report_date: date,
    timezone: TimezoneSpec,
) -> ReportPeriod:
    """Resolve a local calendar week/month to exclusive UTC boundaries."""

    if kind is ReportKind.WEEKLY:
        start = report_date - timedelta(days=report_date.weekday())
        end_exclusive = start + timedelta(days=7)
    else:
        start = report_date.replace(day=1)
        end_exclusive = (
            date(start.year + 1, 1, 1)
            if start.month == 12
            else date(start.year, start.month + 1, 1)
        )
    start_local = datetime.combine(start, time.min, tzinfo=timezone.timezone)
    end_local = datetime.combine(end_exclusive, time.min, tzinfo=timezone.timezone)
    return ReportPeriod(
        start=start,
        end=end_exclusive - timedelta(days=1),
        start_utc=start_local.astimezone(UTC),
        end_utc=end_local.astimezone(UTC),
    )


def build_analytics_report(
    database_path: Path,
    *,
    codex_home: Path,
    kind: ReportKind,
    timezone: TimezoneSpec,
    report_date: date | None = None,
    repository: str | None = None,
    model: str | None = None,
    now: datetime | None = None,
) -> AnalyticsReport:
    """Build one report without duplicating underlying analytics semantics."""

    generated = _as_utc(now or datetime.now(tz=UTC))
    anchor = report_date or generated.astimezone(timezone.timezone).date()
    period = resolve_report_period(kind, report_date=anchor, timezone=timezone)
    previous_period = _previous_period(kind, period, timezone)
    current = _collect_period(
        database_path,
        codex_home=codex_home,
        period=period,
        timezone=timezone,
        repository=repository,
        model=model,
    )
    previous = _collect_period(
        database_path,
        codex_home=codex_home,
        period=previous_period,
        timezone=timezone,
        repository=repository,
        model=model,
        detail=False,
    )
    return AnalyticsReport(
        kind=kind,
        generated_at=generated,
        timezone=timezone.label,
        period=period,
        repository_filter=repository,
        model_filter=model,
        overview=current["overview"],
        activity=current["activity"],
        weekly_activity=current["weekly_activity"],
        repositories=current["repositories"],
        models=current["models"],
        task_actions=current["task_actions"],
        task_domains=current["task_domains"],
        tools=current["tools"],
        git=current["git"],
        outcomes=current["outcomes"],
        interesting_sessions=current["interesting_sessions"],
        data_quality=current["data_quality"],
        previous_period=_comparison(
            current["period_data"],
            previous["period_data"],
            previous_period,
        ),
    )


def _collect_period(
    database_path: Path,
    *,
    codex_home: Path,
    period: ReportPeriod,
    timezone: TimezoneSpec,
    repository: str | None,
    model: str | None,
    detail: bool = True,
) -> dict[str, Any]:
    session_filters = SessionFilters(
        since=period.start_utc,
        until=period.end_utc,
        repository=repository,
        model=model,
        limit=100_000,
    )
    task_filters = TaskFilters(
        since=period.start_utc,
        until=period.end_utc,
        repository=repository,
        model=model,
    )
    tool_filters = ToolFilters(
        since=period.start_utc,
        until=period.end_utc,
        repository=repository,
        model=model,
        limit=10,
    )
    git_filters = GitFilters(
        since=period.start_utc,
        until=period.end_utc,
        repository=repository,
        model=model,
        limit=100_000,
    )
    outcome_filters = OutcomeFilters(
        since=period.start_utc,
        until=period.end_utc,
        repository=repository,
        model=model,
        limit=100_000,
    )
    usage = get_usage_report(
        database_path,
        codex_home=codex_home,
        filters=session_filters,
        timezone=timezone,
        include_reconciliation=True,
    )
    day_usage = get_usage_report(
        database_path,
        codex_home=codex_home,
        filters=session_filters,
        breakdown=UsageBreakdown.DAY,
        timezone=timezone,
    )
    task_summary = get_task_report(
        database_path,
        codex_home=codex_home,
        filters=task_filters,
    )
    tools = get_tool_activity_report(
        database_path,
        codex_home=codex_home,
        filters=tool_filters,
        repeated=True,
    )
    commits = get_commit_report(
        database_path,
        codex_home=codex_home,
        filters=git_filters,
    )
    period_data = _PeriodData(
        usage=usage,
        active_days=len(day_usage.groups),
        repository_count=0,
        model_count=0,
        originated_commands=tools.originated_commands,
        high_confidence_commits=commits.high_confidence_commits,
        task_report=task_summary,
    )
    if not detail:
        return {"period_data": period_data}

    repository_usage = get_usage_report(
        database_path,
        codex_home=codex_home,
        filters=session_filters,
        breakdown=UsageBreakdown.REPOSITORY,
        timezone=timezone,
    )
    model_usage = get_usage_report(
        database_path,
        codex_home=codex_home,
        filters=session_filters,
        breakdown=UsageBreakdown.MODEL,
        timezone=timezone,
    )
    period_data = _PeriodData(
        usage=usage,
        active_days=len(day_usage.groups),
        repository_count=len(repository_usage.groups),
        model_count=len(model_usage.groups),
        originated_commands=tools.originated_commands,
        high_confidence_commits=commits.high_confidence_commits,
        task_report=task_summary,
    )
    week_usage = get_usage_report(
        database_path,
        codex_home=codex_home,
        filters=session_filters,
        breakdown=UsageBreakdown.WEEK,
        timezone=timezone,
    )
    task_actions = get_task_report(
        database_path,
        codex_home=codex_home,
        filters=task_filters,
        breakdown=TaskBreakdown.TYPE,
    )
    task_domains = get_task_report(
        database_path,
        codex_home=codex_home,
        filters=task_filters,
        breakdown=TaskBreakdown.DOMAIN,
    )
    outcomes = get_outcome_report(
        database_path,
        codex_home=codex_home,
        filters=outcome_filters,
    )
    command_by_repo = originated_command_counts(
        database_path,
        codex_home=codex_home,
        filters=tool_filters,
        dimension="repo",
    )
    command_by_model = originated_command_counts(
        database_path,
        codex_home=codex_home,
        filters=tool_filters,
        dimension="model",
    )
    command_by_category = originated_command_counts(
        database_path,
        codex_home=codex_home,
        filters=tool_filters,
        dimension="category",
    )
    commit_by_repo = Counter(
        item.repository for item in commits.associations if item.confidence == "high"
    )
    outcome_by_repo: dict[str, Counter[str]] = {}
    for item in outcomes.sessions:
        outcome_by_repo.setdefault(item.repository, Counter())[item.outcome] += 1
    repositories = tuple(
        _repository_row(
            group,
            command_by_repo=command_by_repo,
            commit_by_repo=commit_by_repo,
            outcome_by_repo=outcome_by_repo,
            task_report=get_task_report(
                database_path,
                codex_home=codex_home,
                filters=TaskFilters(
                    since=period.start_utc,
                    until=period.end_utc,
                    repository=group.key,
                    model=model,
                ),
                breakdown=TaskBreakdown.TYPE,
            ),
        )
        for group in repository_usage.groups
    )
    model_commit_counts = Counter(
        item.model or "unknown"
        for item in commits.associations
        if item.confidence == "high"
    )
    models = tuple(
        {
            **group.to_dict(),
            "originated_commands": command_by_model.get(
                group.label if group.label != "Unknown model" else "unknown",
                0,
            ),
            "high_confidence_commits": model_commit_counts.get(
                group.label if group.label != "Unknown model" else "unknown",
                0,
            ),
        }
        for group in model_usage.groups
    )
    sessions = list_sessions(database_path, codex_home=codex_home, filters=session_filters)
    reconciliation = usage.reconciliation
    assert reconciliation is not None
    token_fraction = (
        usage.metrics.coverage.total_tokens / usage.metrics.session_count
        if usage.metrics.session_count
        else None
    )
    event_origin_fraction = (
        tools.provenance.originated / tools.provenance.observed
        if tools.provenance.observed
        else None
    )
    overview = {
        "sessions": usage.metrics.session_count,
        "active_days": len(day_usage.groups),
        "repositories": len(repository_usage.groups),
        "models": len(model_usage.groups),
        "reconciled_tokens": usage.metrics.total_tokens,
        "token_coverage": {
            "sessions_with_data": usage.metrics.coverage.total_tokens,
            "sessions": usage.metrics.session_count,
            "fraction": token_fraction,
        },
        "observed_median_tokens_per_session": usage.metrics.median_tokens_per_session,
        "observed_p90_tokens_per_session": usage.metrics.p90_tokens_per_session,
        "sessions_per_day": usage.metrics.sessions_per_day,
    }
    data_quality = {
        "sessions": usage.metrics.session_count,
        "token_coverage": overview["token_coverage"],
        "child_threads": reconciliation.child_threads,
        "token_reconciliation_coverage": reconciliation.child_reconciliation_coverage,
        "ambiguous_lineage_threads": (
            reconciliation.ambiguous_children + reconciliation.cyclic_children
        ),
        "ambiguous_lineage_observed_tokens": reconciliation.ambiguous_observed_tokens,
        "tool_event_provenance": {
            **tools.provenance.to_dict(),
            "originated_fraction": event_origin_fraction,
        },
        "logical_prompts": task_summary.metrics.logical_prompts,
        "prompt_feature_sessions": task_summary.metrics.sessions_with_prompt_features,
        "prompts_with_features": task_summary.metrics.prompts_with_features,
        "git_attribution": {
            "high_confidence_commits": commits.high_confidence_commits,
            "sessions_with_high_confidence_commits": (
                commits.sessions_with_high_confidence_commits
            ),
        },
        "unknown_outcomes": outcomes.unknown_count,
        "unknown_tasks": task_summary.metrics.unknown_task_count,
    }
    tools_payload = tools.to_dict()
    tools_payload["command_categories"] = [
        {"key": key, "count": count}
        for key, count in sorted(
            command_by_category.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    return {
        "period_data": period_data,
        "overview": overview,
        "activity": tuple(group.to_dict() for group in day_usage.groups),
        "weekly_activity": tuple(group.to_dict() for group in week_usage.groups),
        "repositories": repositories,
        "models": models,
        "task_actions": task_actions,
        "task_domains": task_domains,
        "tools": tools_payload,
        "git": commits.to_dict(),
        "outcomes": outcomes.to_dict(),
        "interesting_sessions": _interesting_sessions(sessions),
        "data_quality": data_quality,
    }


def _repository_row(
    group: Any,
    *,
    command_by_repo: dict[str, int],
    commit_by_repo: Counter[str],
    outcome_by_repo: dict[str, Counter[str]],
    task_report: TaskReport,
) -> dict[str, object]:
    dominant_task = task_report.groups[0].key if task_report.groups else "unknown"
    return {
        **group.to_dict(),
        "originated_commands": command_by_repo.get(group.key, 0),
        "high_confidence_commits": commit_by_repo[group.label],
        "outcomes": dict(sorted(outcome_by_repo.get(group.label, Counter()).items())),
        "dominant_task": dominant_task,
    }


def _interesting_sessions(sessions: tuple[Any, ...]) -> tuple[dict[str, object], ...]:
    if not sessions:
        return ()
    interesting: list[dict[str, object]] = []
    with_tokens = [item for item in sessions if item.total_tokens is not None]
    if with_tokens:
        item = max(with_tokens, key=lambda value: (value.total_tokens or 0, value.session_id))
        interesting.append(_session_note("highest_observed_rollout_total", item))
    with_duration = [item for item in sessions if item.duration_seconds is not None]
    if with_duration:
        item = max(
            with_duration,
            key=lambda value: (value.duration_seconds or 0, value.session_id),
        )
        note = _session_note("longest_session", item)
        if not interesting or note["session_id"] != interesting[0]["session_id"]:
            interesting.append(note)
    return tuple(interesting)


def _session_note(reason: str, item: Any) -> dict[str, object]:
    return {
        "reason": reason,
        "session_id": item.session_id[:12],
        "started_at": (
            _json_datetime(item.started_at) if item.started_at is not None else None
        ),
        "repository": item.repository,
        "model": item.model,
        "observed_rollout_tokens": item.total_tokens,
        "duration_seconds": item.duration_seconds,
    }


def _comparison(
    current: _PeriodData,
    previous: _PeriodData,
    previous_period: ReportPeriod,
) -> dict[str, object]:
    current_token_coverage = _coverage_fraction(current.usage)
    previous_token_coverage = _coverage_fraction(previous.usage)
    comparable = _coverage_comparable(current_token_coverage, previous_token_coverage)
    warning = (
        None
        if comparable
        else "Token coverage changed materially; percentage changes are suppressed."
    )
    values: dict[str, tuple[int | None, int | None]] = {
        "sessions": (
            current.usage.metrics.session_count,
            previous.usage.metrics.session_count,
        ),
        "active_days": (current.active_days, previous.active_days),
        "reconciled_tokens": (
            current.usage.metrics.total_tokens,
            previous.usage.metrics.total_tokens,
        ),
        "originated_commands": (
            current.originated_commands,
            previous.originated_commands,
        ),
        "high_confidence_commits": (
            current.high_confidence_commits,
            previous.high_confidence_commits,
        ),
    }
    return {
        "period": previous_period.to_dict(),
        "coverage_comparable": comparable,
        "warning": warning,
        "metrics": {
            key: _comparison_value(current_value, previous_value, comparable=comparable)
            for key, (current_value, previous_value) in values.items()
        },
        "token_coverage": {
            "current": current_token_coverage,
            "previous": previous_token_coverage,
        },
    }


def _comparison_value(
    current: int | None,
    previous: int | None,
    *,
    comparable: bool,
) -> dict[str, object]:
    change = current - previous if current is not None and previous is not None else None
    percentage = None
    if comparable and change is not None and previous is not None and previous != 0:
        percentage = change / previous
    return {
        "current": current,
        "previous": previous,
        "change": change,
        "percentage_change": percentage,
    }


def _coverage_fraction(report: UsageReport) -> float | None:
    sessions = report.metrics.session_count
    return report.metrics.coverage.total_tokens / sessions if sessions else None


def _coverage_comparable(current: float | None, previous: float | None) -> bool:
    if current is None and previous is None:
        return True
    if current is None or previous is None:
        return False
    return abs(current - previous) <= 0.10


def _previous_period(
    kind: ReportKind,
    period: ReportPeriod,
    timezone: TimezoneSpec,
) -> ReportPeriod:
    anchor = period.start - timedelta(days=1)
    return resolve_report_period(kind, report_date=anchor, timezone=timezone)


def _as_utc(value: datetime) -> datetime:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC)


def _json_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
