"""Explainable task taxonomy analytics with explicit metric semantics."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from statistics import median

from codex_insights.db import open_index


class TaskBreakdown(StrEnum):
    SUMMARY = "summary"
    TYPE = "type"
    DOMAIN = "domain"


@dataclass(frozen=True, slots=True)
class TaskFilters:
    since: datetime | None = None
    until: datetime | None = None
    repository: str | None = None
    model: str | None = None
    action: str | None = None
    domain: str | None = None


@dataclass(frozen=True, slots=True)
class TaskMetrics:
    session_count: int
    unknown_task_count: int
    reconciled_tokens: int | None
    sessions_with_token_data: int
    observed_median_tokens: float | None
    observed_p90_tokens: float | None
    originated_commands: int
    high_confidence_commits: int
    outcomes: tuple[tuple[str, int], ...]
    classification_confidence: tuple[tuple[str, int], ...]
    logical_prompts: int
    sessions_with_prompt_features: int
    prompts_with_features: int

    def to_dict(self) -> dict[str, object]:
        return {
            "session_count": self.session_count,
            "unknown_task_count": self.unknown_task_count,
            "reconciled_tokens": self.reconciled_tokens,
            "sessions_with_token_data": self.sessions_with_token_data,
            "observed_median_tokens": self.observed_median_tokens,
            "observed_p90_tokens": self.observed_p90_tokens,
            "originated_commands": self.originated_commands,
            "high_confidence_commits": self.high_confidence_commits,
            "outcomes": dict(self.outcomes),
            "classification_confidence": dict(self.classification_confidence),
            "logical_prompts": self.logical_prompts,
            "sessions_with_prompt_features": self.sessions_with_prompt_features,
            "prompts_with_features": self.prompts_with_features,
            "token_semantics": "reconciled_aggregate",
            "distribution_semantics": "observed_per_rollout",
            "command_semantics": "originated_events",
        }


@dataclass(frozen=True, slots=True)
class TaskGroup:
    key: str
    metrics: TaskMetrics

    def to_dict(self) -> dict[str, object]:
        return {"key": self.key, "metrics": self.metrics.to_dict()}


@dataclass(frozen=True, slots=True)
class PromptFeatureSample:
    sample_size: int
    outcomes: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict[str, object]:
        return {"sample_size": self.sample_size, "outcomes": dict(self.outcomes)}


@dataclass(frozen=True, slots=True)
class PromptFeatureCorrelation:
    feature: str
    with_feature: PromptFeatureSample
    without_feature: PromptFeatureSample
    eligible: bool
    minimum_sample_size: int = 5

    def to_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "with_feature": self.with_feature.to_dict(),
            "without_feature": self.without_feature.to_dict(),
            "eligible": self.eligible,
            "minimum_sample_size": self.minimum_sample_size,
            "interpretation": "descriptive_not_causal",
        }


@dataclass(frozen=True, slots=True)
class TaskReport:
    breakdown: TaskBreakdown
    metrics: TaskMetrics
    groups: tuple[TaskGroup, ...]
    prompt_feature_correlations: tuple[PromptFeatureCorrelation, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "breakdown": self.breakdown.value,
            "metrics": self.metrics.to_dict(),
            "groups": [group.to_dict() for group in self.groups],
            "prompt_feature_correlations": [
                comparison.to_dict() for comparison in self.prompt_feature_correlations
            ],
        }


def get_task_report(
    database_path: Path,
    *,
    codex_home: Path,
    breakdown: TaskBreakdown = TaskBreakdown.SUMMARY,
    filters: TaskFilters | None = None,
) -> TaskReport:
    """Return task/domain analytics from one shared normalized query."""

    selected = filters or TaskFilters()
    query, parameters = _query(selected)
    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        rows = connection.execute(query, parameters).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    if breakdown is not TaskBreakdown.SUMMARY:
        column = "action" if breakdown is TaskBreakdown.TYPE else "domain"
        for row in rows:
            grouped[str(row[column] or "unknown")].append(row)
    groups = tuple(
        TaskGroup(key=key, metrics=_metrics(group_rows))
        for key, group_rows in sorted(
            grouped.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )
    )
    return TaskReport(
        breakdown=breakdown,
        metrics=_metrics(rows),
        groups=groups,
        prompt_feature_correlations=_prompt_feature_correlations(rows),
    )


def _query(filters: TaskFilters) -> tuple[str, tuple[object, ...]]:
    conditions: list[str] = []
    parameters: list[object] = []
    if filters.since is not None:
        conditions.append("sessions.started_at >= ?")
        parameters.append(_database_datetime(filters.since))
    if filters.until is not None:
        conditions.append("sessions.started_at < ?")
        parameters.append(_database_datetime(filters.until))
    if filters.repository:
        if filters.repository.casefold() in {"outside-git", "non-git", "none"}:
            conditions.append("sessions.repository_id IS NULL")
        else:
            conditions.append(
                "(repositories.display_name = ? COLLATE NOCASE "
                "OR repositories.identity_key = ? OR repositories.canonical_root = ?)"
            )
            parameters.extend((filters.repository, filters.repository, filters.repository))
    if filters.model:
        if filters.model.casefold() in {"unknown", "none"}:
            conditions.append("(sessions.model IS NULL OR sessions.model = '')")
        else:
            conditions.append("sessions.model = ? COLLATE NOCASE")
            parameters.append(filters.model)
    if filters.action:
        conditions.append("COALESCE(tasks.action, 'unknown') = ?")
        parameters.append(filters.action.casefold())
    if filters.domain:
        conditions.append("COALESCE(tasks.domain, 'unknown') = ?")
        parameters.append(filters.domain.casefold())
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return (
        f"""
        WITH originated_commands AS (
            SELECT observed_session_id AS session_id, COUNT(*) AS command_count
            FROM tool_activity
            WHERE provenance_status = 'origin'
              AND origin_session_id = observed_session_id
              AND command_text IS NOT NULL
            GROUP BY observed_session_id
        ),
        confirmed_commits AS (
            SELECT session_id, COUNT(*) AS commit_count
            FROM session_commit_associations
            WHERE confidence = 'high'
            GROUP BY session_id
        ),
        prompt_counts AS (
            SELECT origin_session_id AS session_id, COUNT(*) AS prompt_count
            FROM prompts GROUP BY origin_session_id
        ),
        prompt_feature_summary AS (
            SELECT prompts.origin_session_id AS session_id,
                   COUNT(features.prompt_id) AS feature_prompt_count,
                   MAX(features.structured_heading_count > 0) AS structured_headings,
                   MAX(features.has_acceptance_criteria) AS acceptance_criteria,
                   MAX(features.requests_validation) AS validation_request,
                   MAX(features.path_reference_count > 0) AS path_references,
                   MAX(features.requests_commit) AS commit_request,
                   MAX(features.requests_multiple_commits) AS multiple_commit_request,
                   MAX(features.has_explicit_non_goals) AS explicit_non_goals,
                   MAX(features.has_read_only_constraint) AS read_only_constraint
            FROM prompts
            JOIN prompt_features AS features ON features.prompt_id = prompts.id
            GROUP BY prompts.origin_session_id
        )
        SELECT sessions.id, sessions.started_at,
               COALESCE(tasks.action, 'unknown') AS action,
               COALESCE(tasks.domain, 'unknown') AS domain,
               COALESCE(tasks.confidence, 'low') AS task_confidence,
               usage.aggregate_total_tokens, usage.observed_total_tokens,
               COALESCE(commands.command_count, 0) AS command_count,
               COALESCE(commits.commit_count, 0) AS commit_count,
               COALESCE(prompts.prompt_count, 0) AS prompt_count,
               COALESCE(features.feature_prompt_count, 0) AS feature_prompt_count,
               COALESCE(features.structured_headings, 0) AS structured_headings,
               COALESCE(features.acceptance_criteria, 0) AS acceptance_criteria,
               COALESCE(features.validation_request, 0) AS validation_request,
               COALESCE(features.path_references, 0) AS path_references,
               COALESCE(features.commit_request, 0) AS commit_request,
               COALESCE(features.multiple_commit_request, 0) AS multiple_commit_request,
               COALESCE(features.explicit_non_goals, 0) AS explicit_non_goals,
               COALESCE(features.read_only_constraint, 0) AS read_only_constraint,
               COALESCE(outcomes.outcome, 'unknown') AS outcome
        FROM source_sessions AS sessions
        LEFT JOIN repositories AS repositories ON repositories.id = sessions.repository_id
        LEFT JOIN session_tasks AS tasks ON tasks.session_id = sessions.id
        LEFT JOIN accounted_usage AS usage ON usage.source_session_id = sessions.id
        LEFT JOIN originated_commands AS commands ON commands.session_id = sessions.id
        LEFT JOIN confirmed_commits AS commits ON commits.session_id = sessions.id
        LEFT JOIN prompt_counts AS prompts ON prompts.session_id = sessions.id
        LEFT JOIN prompt_feature_summary AS features ON features.session_id = sessions.id
        LEFT JOIN session_outcomes AS outcomes ON outcomes.session_id = sessions.id
        {where}
        ORDER BY sessions.started_at IS NULL, sessions.started_at, sessions.id
        """,
        tuple(parameters),
    )


def _metrics(rows: list[sqlite3.Row]) -> TaskMetrics:
    aggregate_values = [
        int(row["aggregate_total_tokens"])
        for row in rows
        if row["aggregate_total_tokens"] is not None
    ]
    observed_values = sorted(
        int(row["observed_total_tokens"])
        for row in rows
        if row["observed_total_tokens"] is not None
    )
    return TaskMetrics(
        session_count=len(rows),
        unknown_task_count=sum(row["action"] == "unknown" for row in rows),
        reconciled_tokens=sum(aggregate_values) if aggregate_values else None,
        sessions_with_token_data=len(aggregate_values),
        observed_median_tokens=(median(observed_values) if observed_values else None),
        observed_p90_tokens=_percentile(observed_values, 0.9),
        originated_commands=sum(int(row["command_count"]) for row in rows),
        high_confidence_commits=sum(int(row["commit_count"]) for row in rows),
        outcomes=tuple(sorted(Counter(str(row["outcome"]) for row in rows).items())),
        classification_confidence=tuple(
            sorted(Counter(str(row["task_confidence"]) for row in rows).items())
        ),
        logical_prompts=sum(int(row["prompt_count"]) for row in rows),
        sessions_with_prompt_features=sum(
            int(row["feature_prompt_count"]) > 0 for row in rows
        ),
        prompts_with_features=sum(int(row["feature_prompt_count"]) for row in rows),
    )


def _prompt_feature_correlations(
    rows: list[sqlite3.Row],
) -> tuple[PromptFeatureCorrelation, ...]:
    features = (
        ("structured_headings", "structured_headings"),
        ("acceptance_criteria", "acceptance_criteria"),
        ("validation_request", "validation_request"),
        ("path_references", "path_references"),
        ("commit_request", "commit_request"),
        ("multiple_commit_request", "multiple_commit_request"),
        ("explicit_non_goals", "explicit_non_goals"),
        ("read_only_constraint", "read_only_constraint"),
    )
    eligible_rows = [row for row in rows if int(row["feature_prompt_count"]) > 0]
    comparisons: list[PromptFeatureCorrelation] = []
    for label, column in features:
        present = [row for row in eligible_rows if int(row[column]) == 1]
        absent = [row for row in eligible_rows if int(row[column]) == 0]
        eligible = len(present) >= 5 and len(absent) >= 5
        comparisons.append(
            PromptFeatureCorrelation(
                feature=label,
                with_feature=_feature_sample(present, expose=eligible),
                without_feature=_feature_sample(absent, expose=eligible),
                eligible=eligible,
            )
        )
    return tuple(comparisons)


def _feature_sample(
    rows: list[sqlite3.Row],
    *,
    expose: bool,
) -> PromptFeatureSample:
    outcomes = (
        tuple(sorted(Counter(str(row["outcome"]) for row in rows).items()))
        if expose
        else ()
    )
    return PromptFeatureSample(sample_size=len(rows), outcomes=outcomes)


def _percentile(values: list[int], quantile: float) -> float | None:
    if not values:
        return None
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(values[lower])
    fraction = position - lower
    return values[lower] + (values[upper] - values[lower]) * fraction


def _database_datetime(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")
