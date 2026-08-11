"""Shared-query assembly for the static offline dashboard."""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from codex_insights import __version__
from codex_insights.analytics.git import (
    CommitAssociationItem,
    GitFilters,
    get_commit_report,
)
from codex_insights.analytics.outcomes import OutcomeFilters, get_outcome_report
from codex_insights.analytics.queries import SessionFilters, SessionListItem, list_sessions
from codex_insights.analytics.tasks import (
    TaskBreakdown,
    TaskFilters,
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
    UsageGroup,
    get_usage_report,
)
from codex_insights.db import open_index
from codex_insights.privacy import load_retention_policy

DASHBOARD_SCHEMA_VERSION = "codex-insights-dashboard-v1"
_QUERY_LIMIT = 100_000


@dataclass(frozen=True, slots=True)
class DashboardFilters:
    """One coherent selection applied across dashboard analytics."""

    since: datetime | None = None
    until: datetime | None = None
    repository: str | None = None
    model: str | None = None
    task_action: str | None = None
    task_domain: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "since": _json_datetime(self.since),
            "until": _json_datetime(self.until),
            "repository": self.repository,
            "model": self.model,
            "task_action": self.task_action,
            "task_domain": self.task_domain,
        }


@dataclass(frozen=True, slots=True)
class DashboardData:
    """Bounded, content-free data used by the offline HTML renderer."""

    generated_at: datetime
    timezone: str
    filters: DashboardFilters
    overview: dict[str, object]
    overview_views: dict[str, dict[str, object]]
    activity: tuple[dict[str, object], ...]
    weekly_activity: tuple[dict[str, object], ...]
    repositories: tuple[dict[str, object], ...]
    models: tuple[dict[str, object], ...]
    task_actions: dict[str, object]
    task_domains: dict[str, object]
    tools: dict[str, object]
    git: dict[str, object]
    outcomes: dict[str, object]
    interesting_sessions: tuple[dict[str, object], ...]
    data_quality: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DASHBOARD_SCHEMA_VERSION,
            "application_version": __version__,
            "generated_at": _json_datetime(self.generated_at),
            "timezone": self.timezone,
            "filters": self.filters.to_dict(),
            "overview": self.overview,
            "overview_views": self.overview_views,
            "activity": list(self.activity),
            "weekly_activity": list(self.weekly_activity),
            "repositories": list(self.repositories),
            "models": list(self.models),
            "tasks": {"actions": self.task_actions, "domains": self.task_domains},
            "tools": self.tools,
            "git": self.git,
            "outcomes": self.outcomes,
            "interesting_sessions": list(self.interesting_sessions),
            "data_quality": self.data_quality,
            "methodology": {
                "additive_tokens": "reconciled_local_contribution",
                "session_distributions": "observed_per_rollout",
                "prompts": "logical_origin_aware",
                "tools_and_commands": "originated_events",
                "git": "provenance_aware_confidence_tiers",
                "outcomes": "originated_evidence_with_unknown_retained",
                "tasks": "origin_thread_intent",
            },
        }


def build_dashboard_data(
    database_path: Path,
    *,
    codex_home: Path,
    timezone: TimezoneSpec,
    filters: DashboardFilters | None = None,
    config_path: Path | None = None,
    now: datetime | None = None,
) -> DashboardData:
    """Build an aggregated dashboard using the canonical analytics functions."""

    selected = filters or DashboardFilters()
    generated_at = _as_utc(now or datetime.now(tz=UTC))
    session_filters = SessionFilters(
        since=selected.since,
        until=selected.until,
        repository=selected.repository,
        model=selected.model,
        task_action=selected.task_action,
        task_domain=selected.task_domain,
        limit=_QUERY_LIMIT,
    )
    task_filters = TaskFilters(
        since=selected.since,
        until=selected.until,
        repository=selected.repository,
        model=selected.model,
        action=selected.task_action,
        domain=selected.task_domain,
    )
    tool_filters = ToolFilters(
        since=selected.since,
        until=selected.until,
        repository=selected.repository,
        model=selected.model,
        task_action=selected.task_action,
        task_domain=selected.task_domain,
        limit=15,
    )
    git_filters = GitFilters(
        since=selected.since,
        until=selected.until,
        repository=selected.repository,
        model=selected.model,
        task_action=selected.task_action,
        task_domain=selected.task_domain,
        limit=_QUERY_LIMIT,
    )
    outcome_filters = OutcomeFilters(
        since=selected.since,
        until=selected.until,
        repository=selected.repository,
        model=selected.model,
        task_action=selected.task_action,
        task_domain=selected.task_domain,
        limit=_QUERY_LIMIT,
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
    week_usage = get_usage_report(
        database_path,
        codex_home=codex_home,
        filters=session_filters,
        breakdown=UsageBreakdown.WEEK,
        timezone=timezone,
    )
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
    task_summary = get_task_report(
        database_path,
        codex_home=codex_home,
        filters=task_filters,
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
    commit_by_repo = Counter(
        item.repository for item in commits.associations if item.confidence == "high"
    )
    commit_by_model = Counter(
        item.model or "unknown"
        for item in commits.associations
        if item.confidence == "high"
    )
    outcomes_by_repo: dict[str, Counter[str]] = {}
    for item in outcomes.sessions:
        outcomes_by_repo.setdefault(item.repository, Counter())[item.outcome] += 1

    repositories = tuple(
        _repository_row(
            database_path,
            codex_home=codex_home,
            group=group,
            selected=selected,
            commands=command_by_repo,
            commits=commit_by_repo,
            outcomes=outcomes_by_repo,
        )
        for group in repository_usage.groups
    )
    models = tuple(
        {
            **group.to_dict(),
            "originated_commands": command_by_model.get(
                group.label if group.label != "Unknown model" else "unknown", 0
            ),
            "high_confidence_commits": commit_by_model.get(
                group.label if group.label != "Unknown model" else "unknown", 0
            ),
        }
        for group in model_usage.groups
    )

    reconciliation = usage.reconciliation
    assert reconciliation is not None
    session_count = usage.metrics.session_count
    token_sessions = usage.metrics.coverage.total_tokens
    sessions = list_sessions(database_path, codex_home=codex_home, filters=session_filters)
    policy = load_retention_policy(config_path, codex_home=codex_home)
    compatibility = _compatibility_summary(database_path, codex_home=codex_home)
    overview: dict[str, object] = {
        "sessions": session_count,
        "active_days": sum(group.period_start is not None for group in day_usage.groups),
        "repositories": len(repository_usage.groups),
        "models": len(model_usage.groups),
        "reconciled_tokens": usage.metrics.total_tokens,
        "token_coverage": {
            "sessions_with_data": token_sessions,
            "sessions": session_count,
            "fraction": token_sessions / session_count if session_count else None,
        },
        "observed_median_tokens_per_session": usage.metrics.median_tokens_per_session,
        "observed_p90_tokens_per_session": usage.metrics.p90_tokens_per_session,
        "sessions_per_day": usage.metrics.sessions_per_day,
        "high_confidence_commits": commits.high_confidence_commits,
        "classifiable_outcome_rate": (
            outcomes.classifiable_count / outcomes.session_count
            if outcomes.session_count
            else None
        ),
    }
    today_start, tomorrow_start, week_start = _local_window_bounds(
        generated_at, timezone
    )
    daily_overview = _window_overview(
        database_path,
        codex_home=codex_home,
        timezone=timezone,
        selected=selected,
        since=today_start,
        until=tomorrow_start,
        label="Today",
    )
    weekly_overview = _window_overview(
        database_path,
        codex_home=codex_home,
        timezone=timezone,
        selected=selected,
        since=week_start,
        until=tomorrow_start,
        label="This week",
    )
    overview_views = {
        "daily": daily_overview,
        "weekly": weekly_overview,
        "overall": {**overview, "label": "All time / selected range"},
    }
    tools_payload: dict[str, object] = {
        "originated_tool_calls": tools.originated_tool_calls,
        "originated_commands": tools.originated_commands,
        "commands_per_session": tools.commands_per_session,
        "test_invocations": tools.test_invocations,
        "git_inspections": tools.git_inspections,
        "patch_edits": tools.patch_edits,
        "known_results": tools.known_results,
        "failed_results": tools.failed_results,
        "failure_rate": tools.failure_rate,
        "provenance": tools.provenance.to_dict(),
        "tools": [item.to_dict() for item in tools.tools],
        "categories": [item.to_dict() for item in tools.categories],
        "executables": [item.to_dict() for item in tools.executables],
        "repeated_patterns": [
            {
                "category": item.category,
                "executable": item.executable,
                "invocation_count": item.invocation_count,
                "session_count": item.session_count,
            }
            for item in tools.repeated_commands
        ],
        "activity_semantics": "originated_events",
    }
    git_payload: dict[str, object] = {
        "high": commits.high,
        "medium": commits.medium,
        "low": commits.low,
        "ambiguous": commits.ambiguous,
        "repositories_resolved": commits.repositories_resolved,
        "sessions_with_high_confidence_commits": (
            commits.sessions_with_high_confidence_commits
        ),
        "high_confidence_commits": commits.high_confidence_commits,
        "reconciled_tokens_for_high_sessions": commits.reconciled_tokens_for_high_sessions,
        "high_sessions_with_token_data": commits.high_sessions_with_token_data,
    }
    outcome_payload: dict[str, object] = {
        "session_count": outcomes.session_count,
        "classifiable_count": outcomes.classifiable_count,
        "unknown_count": outcomes.unknown_count,
        "outcomes": dict(outcomes.outcomes),
        "confidence": dict(outcomes.confidence),
        "classification_semantics": "originated_evidence",
    }
    data_quality: dict[str, object] = {
        "token_coverage": overview["token_coverage"],
        "observed_rollout_tokens": reconciliation.observed_rollout_tokens,
        "reconciled_replay_tokens": reconciliation.inherited_replayed_tokens,
        "child_threads": reconciliation.child_threads,
        "confidently_reconciled_children": reconciliation.confidently_reconciled_children,
        "child_reconciliation_coverage": reconciliation.child_reconciliation_coverage,
        "ambiguous_lineage_threads": (
            reconciliation.ambiguous_children + reconciliation.cyclic_children
        ),
        "ambiguous_lineage_observed_tokens": reconciliation.ambiguous_observed_tokens,
        "temporal_attribution": usage.temporal_coverage.to_dict(),
        "event_provenance": tools.provenance.to_dict(),
        "logical_prompts": task_summary.metrics.logical_prompts,
        "prompt_feature_sessions": task_summary.metrics.sessions_with_prompt_features,
        "prompts_with_features": task_summary.metrics.prompts_with_features,
        "prompt_storage_enabled": policy.store_prompts,
        "command_text_storage_enabled": policy.store_command_text,
        "git_attribution": {
            "high_confidence_commits": commits.high_confidence_commits,
            "sessions_with_high_confidence_commits": (
                commits.sessions_with_high_confidence_commits
            ),
        },
        "unknown_outcomes": outcomes.unknown_count,
        "unknown_tasks": task_summary.metrics.unknown_task_count,
        "compatibility": compatibility,
    }
    return DashboardData(
        generated_at=generated_at,
        timezone=timezone.label,
        filters=selected,
        overview=overview,
        overview_views=overview_views,
        activity=tuple(group.to_dict() for group in day_usage.groups),
        weekly_activity=tuple(group.to_dict() for group in week_usage.groups),
        repositories=repositories,
        models=models,
        task_actions=task_actions.to_dict(),
        task_domains=task_domains.to_dict(),
        tools=tools_payload,
        git=git_payload,
        outcomes=outcome_payload,
        interesting_sessions=_interesting_sessions(sessions, commits.associations),
        data_quality=data_quality,
    )


def _repository_row(
    database_path: Path,
    *,
    codex_home: Path,
    group: UsageGroup,
    selected: DashboardFilters,
    commands: dict[str, int],
    commits: Counter[str],
    outcomes: dict[str, Counter[str]],
) -> dict[str, object]:
    group_dict = group.to_dict()
    key = str(group_dict["key"])
    label = str(group_dict["label"])
    task_report = get_task_report(
        database_path,
        codex_home=codex_home,
        filters=TaskFilters(
            since=selected.since,
            until=selected.until,
            repository=key,
            model=selected.model,
            action=selected.task_action,
            domain=selected.task_domain,
        ),
        breakdown=TaskBreakdown.TYPE,
    )
    return {
        **group_dict,
        "originated_commands": commands.get(key, 0),
        "high_confidence_commits": commits[label],
        "dominant_task": task_report.groups[0].key if task_report.groups else "unknown",
        "outcomes": dict(sorted(outcomes.get(label, Counter()).items())),
    }


def _interesting_sessions(
    sessions: tuple[SessionListItem, ...],
    associations: tuple[CommitAssociationItem, ...],
) -> tuple[dict[str, object], ...]:
    notes: list[dict[str, object]] = []
    with_tokens = [item for item in sessions if item.total_tokens is not None]
    if with_tokens:
        item = max(
            with_tokens,
            key=lambda value: (value.total_tokens or 0, value.session_id),
        )
        notes.append(_session_note("highest_observed_rollout_total", item))
    with_duration = [
        item for item in sessions if item.duration_seconds is not None
    ]
    if with_duration:
        item = max(
            with_duration,
            key=lambda value: (value.duration_seconds or 0, value.session_id),
        )
        note = _session_note("longest_session", item)
        if not notes or note["session_id"] != notes[0]["session_id"]:
            notes.append(note)
    confirmed = next(
        (item for item in associations if item.confidence == "high"),
        None,
    )
    if confirmed is not None:
        notes.append(
            {
                "reason": "linked_to_confirmed_commit",
                "session_id": confirmed.session_id[:12],
                "started_at": None,
                "repository": confirmed.repository,
                "model": confirmed.model,
                "observed_rollout_tokens": None,
                "duration_seconds": None,
            }
        )
    return tuple(notes[:5])


def _session_note(reason: str, item: SessionListItem) -> dict[str, object]:
    started = item.started_at
    return {
        "reason": reason,
        "session_id": item.session_id[:12],
        "started_at": _json_datetime(started) if started is not None else None,
        "repository": item.repository,
        "model": item.model,
        "observed_rollout_tokens": item.total_tokens,
        "duration_seconds": item.duration_seconds,
    }


def _compatibility_summary(database_path: Path, *, codex_home: Path) -> dict[str, object]:
    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        latest_run = connection.execute("SELECT MAX(id) FROM index_runs").fetchone()[0]
        warning_rows = (
            connection.execute(
                """
                SELECT severity, SUM(warning_count) AS warning_count
                FROM compatibility_warnings
                WHERE index_run_id = ?
                GROUP BY severity ORDER BY severity
                """,
                (latest_run,),
            ).fetchall()
            if latest_run is not None
            else ()
        )
        stale = int(
            connection.execute(
                "SELECT COUNT(*) FROM session_compatibility WHERE stale = 1"
            ).fetchone()[0]
        )
        unknown_records = int(
            connection.execute(
                "SELECT COALESCE(SUM(record_count), 0) FROM unknown_source_records"
            ).fetchone()[0]
        )
    return {
        "latest_index_run": int(latest_run) if latest_run is not None else None,
        "warnings": {str(row["severity"]): int(row["warning_count"]) for row in warning_rows},
        "stale_sessions": stale,
        "unknown_source_records": unknown_records,
    }


def _local_window_bounds(
    reference: datetime,
    timezone: TimezoneSpec,
) -> tuple[datetime, datetime, datetime]:
    local = reference.astimezone(timezone.timezone)
    today_start = datetime.combine(local.date(), time.min, tzinfo=timezone.timezone)
    tomorrow_start = today_start + timedelta(days=1)
    week_start = today_start - timedelta(days=local.date().weekday())
    return _as_utc(today_start), _as_utc(tomorrow_start), _as_utc(week_start)


def _window_overview(
    database_path: Path,
    *,
    codex_home: Path,
    timezone: TimezoneSpec,
    selected: DashboardFilters,
    since: datetime,
    until: datetime,
    label: str,
) -> dict[str, object]:
    window_since = max(value for value in (selected.since, since) if value is not None)
    window_until = min(value for value in (selected.until, until) if value is not None)
    filters = SessionFilters(
        since=window_since,
        until=window_until,
        repository=selected.repository,
        model=selected.model,
        task_action=selected.task_action,
        task_domain=selected.task_domain,
        limit=_QUERY_LIMIT,
    )
    reports = {
        breakdown: get_usage_report(
            database_path,
            codex_home=codex_home,
            filters=filters,
            breakdown=breakdown,
            timezone=timezone,
        )
        for breakdown in (
            UsageBreakdown.SUMMARY,
            UsageBreakdown.DAY,
            UsageBreakdown.REPOSITORY,
            UsageBreakdown.MODEL,
        )
    }
    summary = reports[UsageBreakdown.SUMMARY]
    coverage = summary.metrics.coverage
    return {
        "label": label,
        "sessions": summary.metrics.session_count,
        "active_days": sum(
            group.period_start is not None for group in reports[UsageBreakdown.DAY].groups
        ),
        "repositories": len(reports[UsageBreakdown.REPOSITORY].groups),
        "models": len(reports[UsageBreakdown.MODEL].groups),
        "reconciled_tokens": summary.metrics.total_tokens,
        "token_coverage": {
            "sessions_with_data": coverage.total_tokens,
            "sessions": coverage.session_count,
            "fraction": (
                coverage.total_tokens / coverage.session_count
                if coverage.session_count
                else None
            ),
        },
        "temporal_attribution": summary.temporal_coverage.to_dict(),
    }


def _as_utc(value: datetime) -> datetime:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC)


def _json_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")
