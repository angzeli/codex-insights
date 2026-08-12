"""Stable privacy-aware JSON and CSV exports from the normalized derived index."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from codex_insights.analytics.queries import SessionFilters
from codex_insights.analytics.usage import (
    UsageBreakdown,
    UsageGroup,
    get_usage_report,
    resolve_timezone,
)
from codex_insights.db import open_index
from codex_insights.privacy import ContentRetentionPolicy, utc_now

EXPORT_SCHEMA = "codex-insights-export-v1"


class ExportDataset(StrEnum):
    """Supported normalized export datasets."""

    SESSIONS = "sessions"
    USAGE = "usage"
    PROMPTS = "prompts"
    COMMANDS = "commands"
    COMMITS = "commits"
    OUTCOMES = "outcomes"
    TASKS = "tasks"
    REPOSITORIES = "repositories"
    MODELS = "models"


class ExportFormat(StrEnum):
    JSON = "json"
    CSV = "csv"


@dataclass(frozen=True, slots=True)
class ExportFilters:
    since: datetime | None = None
    until: datetime | None = None
    repository: str | None = None
    model: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "since": _datetime_text(self.since),
            "until": _datetime_text(self.until),
            "repository": self.repository,
            "model": self.model,
        }


@dataclass(frozen=True, slots=True)
class ExportBundle:
    dataset: ExportDataset
    generated_at: str
    filters: ExportFilters
    policy: ContentRetentionPolicy
    records: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": EXPORT_SCHEMA,
            "dataset": self.dataset.value,
            "generated_at": self.generated_at,
            "filters": self.filters.to_dict(),
            "privacy_policy": self.policy.to_dict(),
            "metric_semantics": {
                "additive_tokens": "reconciled_local_contribution",
                "session_tokens": "observed_rollout_cumulative",
                "prompts": "logical_origin_aware_stored_redacted_text",
                "commands": "originated_activity_with_optional_bounded_redacted_text",
                "commits": "provenance_aware_confidence",
            },
            "record_count": len(self.records),
            "records": list(self.records),
        }


def build_export(
    database_path: Path,
    *,
    codex_home: Path,
    dataset: ExportDataset,
    policy: ContentRetentionPolicy,
    filters: ExportFilters | None = None,
) -> ExportBundle:
    """Build one bounded-semantic export without reopening Codex source rollouts."""

    selected = filters or ExportFilters()
    if dataset in {ExportDataset.REPOSITORIES, ExportDataset.MODELS}:
        records = _usage_groups(
            database_path,
            codex_home=codex_home,
            dataset=dataset,
            filters=selected,
        )
    else:
        with closing(open_index(database_path, codex_home=codex_home)) as connection:
            records = _database_records(
                connection,
                dataset=dataset,
                filters=selected,
                policy=policy,
            )
    return ExportBundle(
        dataset=dataset,
        generated_at=utc_now(),
        filters=selected,
        policy=policy,
        records=records,
    )


def render_export(bundle: ExportBundle, export_format: ExportFormat) -> str:
    """Serialize one export with stable JSON and spreadsheet-safe CSV cells."""

    if export_format is ExportFormat.JSON:
        return json.dumps(bundle.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    output = io.StringIO(newline="")
    fields = _CSV_FIELDS[bundle.dataset]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for record in bundle.records:
        writer.writerow({field: _csv_cell(record.get(field)) for field in fields})
    return output.getvalue()


def _database_records(
    connection: sqlite3.Connection,
    *,
    dataset: ExportDataset,
    filters: ExportFilters,
    policy: ContentRetentionPolicy,
) -> tuple[dict[str, object], ...]:
    if dataset is ExportDataset.SESSIONS:
        return _session_records(connection, filters)
    if dataset is ExportDataset.USAGE:
        return _usage_records(connection, filters)
    if dataset is ExportDataset.PROMPTS:
        return _prompt_records(connection, filters, include_text=policy.store_prompts)
    if dataset is ExportDataset.COMMANDS:
        return _command_records(connection, filters, include_text=policy.store_command_text)
    if dataset is ExportDataset.COMMITS:
        return _commit_records(connection, filters)
    if dataset is ExportDataset.OUTCOMES:
        return _outcome_records(connection, filters)
    if dataset is ExportDataset.TASKS:
        return _task_records(connection, filters)
    raise AssertionError(f"Unhandled export dataset: {dataset}")


def _session_records(
    connection: sqlite3.Connection,
    filters: ExportFilters,
) -> tuple[dict[str, object], ...]:
    where, parameters = _session_where(filters, "sessions")
    rows = connection.execute(
        f"""
        SELECT source_session_id, source_type, client_source, client_kind,
               subagent_source_kind, source_parent_session_id, started_at, updated_at,
               apparent_ended_at, cwd, repository_root, repository_name, git_branch,
               git_sha, model, model_provider, codex_version, archived
        FROM source_sessions AS sessions
        {where}
        ORDER BY started_at IS NULL, started_at, source_session_id
        """,
        parameters,
    )
    return tuple(
        _record(
            ExportDataset.SESSIONS,
            session_id=row["source_session_id"],
            source_type=row["source_type"],
            client_source=row["client_source"],
            client_kind=row["client_kind"],
            subagent_source_kind=row["subagent_source_kind"],
            source_parent_session_id=row["source_parent_session_id"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            apparent_ended_at=row["apparent_ended_at"],
            cwd=row["cwd"],
            repository_root=row["repository_root"],
            repository_name=row["repository_name"],
            git_branch=row["git_branch"],
            git_sha=row["git_sha"],
            model=row["model"],
            model_provider=row["model_provider"],
            codex_version=row["codex_version"],
            archived=bool(row["archived"]),
        )
        for row in rows
    )


def _usage_records(
    connection: sqlite3.Connection,
    filters: ExportFilters,
) -> tuple[dict[str, object], ...]:
    where, parameters = _session_where(filters, "sessions")
    rows = connection.execute(
        f"""
        SELECT sessions.source_session_id, sessions.started_at,
               sessions.repository_name, sessions.model,
               usage.usage_semantics, usage.accounting_status,
               usage.observed_input_tokens,
               usage.observed_cached_input_tokens,
               usage.observed_cache_write_input_tokens,
               usage.observed_output_tokens,
               usage.observed_reasoning_output_tokens,
               usage.observed_total_tokens,
               usage.aggregate_input_tokens,
               usage.aggregate_cached_input_tokens,
               usage.aggregate_cache_write_input_tokens,
               usage.aggregate_output_tokens,
               usage.aggregate_reasoning_output_tokens,
               usage.aggregate_total_tokens,
               usage.inherited_baseline_total_tokens
        FROM source_sessions AS sessions
        LEFT JOIN accounted_usage AS usage ON usage.source_session_id = sessions.id
        {where}
        ORDER BY sessions.started_at IS NULL, sessions.started_at,
                 sessions.source_session_id
        """,
        parameters,
    )
    return tuple(
        _record(
            ExportDataset.USAGE,
            session_id=row["source_session_id"],
            started_at=row["started_at"],
            repository=row["repository_name"],
            model=row["model"],
            usage_semantics=row["usage_semantics"],
            accounting_status=row["accounting_status"],
            observed_rollout_input_tokens=row["observed_input_tokens"],
            observed_rollout_cached_input_tokens=row["observed_cached_input_tokens"],
            observed_rollout_cache_write_input_tokens=row[
                "observed_cache_write_input_tokens"
            ],
            observed_rollout_output_tokens=row["observed_output_tokens"],
            observed_rollout_reasoning_output_tokens=row[
                "observed_reasoning_output_tokens"
            ],
            observed_rollout_total_tokens=row["observed_total_tokens"],
            reconciled_local_input_tokens=row["aggregate_input_tokens"],
            reconciled_local_cached_input_tokens=row["aggregate_cached_input_tokens"],
            reconciled_local_cache_write_input_tokens=row[
                "aggregate_cache_write_input_tokens"
            ],
            reconciled_local_output_tokens=row["aggregate_output_tokens"],
            reconciled_local_reasoning_output_tokens=row[
                "aggregate_reasoning_output_tokens"
            ],
            reconciled_local_total_tokens=row["aggregate_total_tokens"],
            inherited_replayed_baseline_total_tokens=row[
                "inherited_baseline_total_tokens"
            ],
        )
        for row in rows
    )


def _prompt_records(
    connection: sqlite3.Connection,
    filters: ExportFilters,
    *,
    include_text: bool,
) -> tuple[dict[str, object], ...]:
    where, parameters = _session_where(filters, "sessions")
    rows = connection.execute(
        f"""
        SELECT prompts.prompt_id, sessions.source_session_id, prompts.prompt_ordinal,
               COALESCE(prompts.occurred_at, sessions.started_at) AS occurred_at,
               sessions.repository_name, sessions.model, prompts.text,
               prompts.redaction_status, prompts.redaction_count,
               prompts.original_character_count, prompts.stored_character_count,
               prompts.provenance_status, prompts.provenance_confidence,
               prompts.user_authorship_evidence,
               COUNT(DISTINCT observations.observed_session_id) AS observation_sessions,
               COUNT(DISTINCT CASE
                   WHEN observations.observed_session_id != prompts.origin_session_id
                   THEN observations.observed_session_id END) AS replay_sessions
        FROM prompts
        JOIN source_sessions AS sessions ON sessions.id = prompts.origin_session_id
        LEFT JOIN prompt_observations AS observations ON observations.prompt_id = prompts.id
        {where}
        GROUP BY prompts.id
        ORDER BY occurred_at IS NULL, occurred_at, prompts.prompt_id
        """,
        parameters,
    )
    return tuple(
        _record(
            ExportDataset.PROMPTS,
            prompt_id=row["prompt_id"],
            origin_session_id=row["source_session_id"],
            prompt_ordinal=row["prompt_ordinal"],
            occurred_at=row["occurred_at"],
            repository=row["repository_name"],
            model=row["model"],
            stored_redacted_prompt_text=(row["text"] if include_text else None),
            text_included=include_text,
            redaction_status=row["redaction_status"],
            redaction_count=row["redaction_count"],
            original_character_count=row["original_character_count"],
            stored_character_count=row["stored_character_count"],
            provenance_status=row["provenance_status"],
            provenance_confidence=row["provenance_confidence"],
            user_authorship_evidence=row["user_authorship_evidence"],
            observation_session_count=row["observation_sessions"],
            replay_session_count=row["replay_sessions"],
        )
        for row in rows
    )


def _command_records(
    connection: sqlite3.Connection,
    filters: ExportFilters,
    *,
    include_text: bool,
) -> tuple[dict[str, object], ...]:
    where, parameters = _session_where(filters, "sessions", prefix_conditions=(
        "activity.provenance_status = 'origin'",
        "activity.origin_session_id = activity.observed_session_id",
        "activity.command_fingerprint IS NOT NULL",
    ))
    rows = connection.execute(
        f"""
        SELECT sessions.source_session_id, activity.occurred_at,
               sessions.repository_name, sessions.model, activity.tool_name,
               activity.command_category, activity.command_text,
               activity.command_fingerprint, activity.executable,
               activity.command_operation, activity.test_scope,
               activity.result_status, activity.exit_code, activity.duration_seconds,
               activity.redacted, activity.truncated, activity.provenance_status
        FROM tool_activity AS activity
        JOIN source_sessions AS sessions ON sessions.id = activity.observed_session_id
        {where}
        ORDER BY COALESCE(activity.occurred_at, sessions.started_at) IS NULL,
                 COALESCE(activity.occurred_at, sessions.started_at),
                 sessions.source_session_id, activity.source_ordinal,
                 activity.operation_ordinal
        """,
        parameters,
    )
    return tuple(
        _record(
            ExportDataset.COMMANDS,
            session_id=row["source_session_id"],
            occurred_at=row["occurred_at"],
            repository=row["repository_name"],
            model=row["model"],
            tool_name=row["tool_name"],
            command_category=row["command_category"],
            stored_redacted_bounded_command_text=(row["command_text"] if include_text else None),
            text_included=include_text,
            command_fingerprint=row["command_fingerprint"],
            executable=row["executable"],
            command_operation=row["command_operation"],
            test_scope=row["test_scope"],
            result_status=row["result_status"],
            exit_code=row["exit_code"],
            duration_seconds=row["duration_seconds"],
            redacted=bool(row["redacted"]),
            truncated=bool(row["truncated"]),
            provenance_status=row["provenance_status"],
        )
        for row in rows
    )


def _commit_records(
    connection: sqlite3.Connection,
    filters: ExportFilters,
) -> tuple[dict[str, object], ...]:
    where, parameters = _session_where(filters, "sessions")
    rows = connection.execute(
        f"""
        SELECT sessions.source_session_id, repositories.display_name AS repository,
               commits.commit_hash, commits.committed_at,
               associations.confidence, associations.evidence_type,
               associations.evidence_explanation, associations.ambiguous,
               associations.algorithm_version
        FROM session_commit_associations AS associations
        JOIN source_sessions AS sessions ON sessions.id = associations.session_id
        JOIN git_commits AS commits ON commits.id = associations.commit_id
        JOIN repositories ON repositories.id = commits.repository_id
        {where}
        ORDER BY commits.committed_at, commits.commit_hash, sessions.source_session_id
        """,
        parameters,
    )
    return tuple(
        _record(
            ExportDataset.COMMITS,
            session_id=row["source_session_id"],
            repository=row["repository"],
            commit_hash=row["commit_hash"],
            committed_at=row["committed_at"],
            confidence=row["confidence"],
            evidence_type=row["evidence_type"],
            evidence_explanation=row["evidence_explanation"],
            ambiguous=bool(row["ambiguous"]),
            algorithm_version=row["algorithm_version"],
        )
        for row in rows
    )


def _outcome_records(
    connection: sqlite3.Connection,
    filters: ExportFilters,
) -> tuple[dict[str, object], ...]:
    where, parameters = _session_where(filters, "sessions")
    rows = connection.execute(
        f"""
        SELECT sessions.source_session_id, outcomes.outcome, outcomes.confidence,
               outcomes.evidence_count, outcomes.classifier_version, outcomes.updated_at
        FROM session_outcomes AS outcomes
        JOIN source_sessions AS sessions ON sessions.id = outcomes.session_id
        {where}
        ORDER BY sessions.started_at IS NULL, sessions.started_at,
                 sessions.source_session_id
        """,
        parameters,
    )
    return tuple(
        _record(
            ExportDataset.OUTCOMES,
            session_id=row["source_session_id"],
            outcome=row["outcome"],
            confidence=row["confidence"],
            evidence_count=row["evidence_count"],
            classifier_version=row["classifier_version"],
            updated_at=row["updated_at"],
        )
        for row in rows
    )


def _task_records(
    connection: sqlite3.Connection,
    filters: ExportFilters,
) -> tuple[dict[str, object], ...]:
    where, parameters = _session_where(filters, "sessions")
    rows = connection.execute(
        f"""
        SELECT sessions.source_session_id, tasks.action, tasks.domain,
               tasks.facets_json, tasks.confidence, tasks.taxonomy_version,
               tasks.updated_at
        FROM session_tasks AS tasks
        JOIN source_sessions AS sessions ON sessions.id = tasks.session_id
        {where}
        ORDER BY sessions.started_at IS NULL, sessions.started_at,
                 sessions.source_session_id
        """,
        parameters,
    )
    return tuple(
        _record(
            ExportDataset.TASKS,
            session_id=row["source_session_id"],
            action=row["action"],
            domain=row["domain"],
            facets=_json_value(row["facets_json"]),
            confidence=row["confidence"],
            taxonomy_version=row["taxonomy_version"],
            updated_at=row["updated_at"],
        )
        for row in rows
    )


def _usage_groups(
    database_path: Path,
    *,
    codex_home: Path,
    dataset: ExportDataset,
    filters: ExportFilters,
) -> tuple[dict[str, object], ...]:
    breakdown = (
        UsageBreakdown.REPOSITORY
        if dataset is ExportDataset.REPOSITORIES
        else UsageBreakdown.MODEL
    )
    report = get_usage_report(
        database_path,
        codex_home=codex_home,
        breakdown=breakdown,
        filters=SessionFilters(
            since=filters.since,
            until=filters.until,
            repository=filters.repository,
            model=filters.model,
            limit=1,
        ),
        timezone=resolve_timezone("UTC"),
    )
    return tuple(_usage_group_record(dataset, group) for group in report.groups)


def _usage_group_record(dataset: ExportDataset, group: UsageGroup) -> dict[str, object]:
    metrics = group.metrics
    base: dict[str, object] = {
        "group_key": group.key,
        "group_label": group.label,
        "session_count": metrics.session_count,
        "sessions_with_reconciled_token_data": metrics.coverage.total_tokens,
        "reconciled_local_total_tokens": metrics.total_tokens,
        "reconciled_local_input_tokens": metrics.input_tokens,
        "reconciled_local_cached_input_tokens": metrics.cached_input_tokens,
        "reconciled_local_output_tokens": metrics.output_tokens,
        "reconciled_local_reasoning_output_tokens": metrics.reasoning_output_tokens,
        "observed_rollout_total_tokens_sum": metrics.observed_total_tokens,
        "observed_rollout_mean_tokens_per_session": metrics.mean_tokens_per_session,
        "observed_rollout_median_tokens_per_session": metrics.median_tokens_per_session,
        "observed_rollout_p90_tokens_per_session": metrics.p90_tokens_per_session,
        "sessions_per_day": metrics.sessions_per_day,
        "repository_root": str(group.repository_root) if group.repository_root else None,
        "model_provider": group.model_provider,
    }
    return _record(dataset, **base)


def _session_where(
    filters: ExportFilters,
    alias: str,
    *,
    prefix_conditions: tuple[str, ...] = (),
) -> tuple[str, tuple[object, ...]]:
    conditions = list(prefix_conditions)
    parameters: list[object] = []
    if filters.since is not None:
        conditions.append(f"{alias}.started_at >= ?")
        parameters.append(_datetime_text(filters.since))
    if filters.until is not None:
        conditions.append(f"{alias}.started_at < ?")
        parameters.append(_datetime_text(filters.until))
    if filters.repository:
        if filters.repository.casefold() in {"outside-git", "non-git", "none"}:
            conditions.append(f"{alias}.repository_root IS NULL")
        else:
            conditions.append(
                f"({alias}.repository_name = ? COLLATE NOCASE OR {alias}.repository_root = ?)"
            )
            parameters.extend((filters.repository, filters.repository))
    if filters.model:
        if filters.model.casefold() in {"unknown", "none"}:
            conditions.append(f"({alias}.model IS NULL OR {alias}.model = '')")
        else:
            conditions.append(f"{alias}.model = ? COLLATE NOCASE")
            parameters.append(filters.model)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, tuple(parameters)


def _record(dataset: ExportDataset, **values: object) -> dict[str, object]:
    return {
        "export_schema_version": EXPORT_SCHEMA,
        "dataset": dataset.value,
        **values,
    }


def _csv_cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, str):
        stripped = value.lstrip(" \t\r\n")
        if stripped.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


def _json_value(value: Any) -> object:
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, (dict, list)) else None


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


_COMMON_FIELDS = ("export_schema_version", "dataset")
_CSV_FIELDS: dict[ExportDataset, tuple[str, ...]] = {
    ExportDataset.SESSIONS: _COMMON_FIELDS
    + (
        "session_id", "source_type", "client_source", "client_kind",
        "subagent_source_kind", "source_parent_session_id", "started_at", "updated_at",
        "apparent_ended_at", "cwd", "repository_root", "repository_name", "git_branch",
        "git_sha", "model", "model_provider", "codex_version", "archived",
    ),
    ExportDataset.USAGE: _COMMON_FIELDS
    + (
        "session_id", "started_at", "repository", "model", "usage_semantics",
        "accounting_status", "observed_rollout_input_tokens",
        "observed_rollout_cached_input_tokens", "observed_rollout_cache_write_input_tokens",
        "observed_rollout_output_tokens", "observed_rollout_reasoning_output_tokens",
        "observed_rollout_total_tokens", "reconciled_local_input_tokens",
        "reconciled_local_cached_input_tokens", "reconciled_local_cache_write_input_tokens",
        "reconciled_local_output_tokens", "reconciled_local_reasoning_output_tokens",
        "reconciled_local_total_tokens", "inherited_replayed_baseline_total_tokens",
    ),
    ExportDataset.PROMPTS: _COMMON_FIELDS
    + (
        "prompt_id", "origin_session_id", "prompt_ordinal", "occurred_at", "repository",
        "model", "stored_redacted_prompt_text", "text_included", "redaction_status",
        "redaction_count", "original_character_count", "stored_character_count",
        "provenance_status", "provenance_confidence", "user_authorship_evidence",
        "observation_session_count", "replay_session_count",
    ),
    ExportDataset.COMMANDS: _COMMON_FIELDS
    + (
        "session_id", "occurred_at", "repository", "model", "tool_name",
        "command_category", "stored_redacted_bounded_command_text", "text_included",
        "command_fingerprint", "executable", "command_operation", "test_scope",
        "result_status", "exit_code", "duration_seconds", "redacted", "truncated",
        "provenance_status",
    ),
    ExportDataset.COMMITS: _COMMON_FIELDS
    + (
        "session_id", "repository", "commit_hash", "committed_at", "confidence",
        "evidence_type", "evidence_explanation", "ambiguous", "algorithm_version",
    ),
    ExportDataset.OUTCOMES: _COMMON_FIELDS
    + (
        "session_id", "outcome", "confidence", "evidence_count", "classifier_version",
        "updated_at",
    ),
    ExportDataset.TASKS: _COMMON_FIELDS
    + (
        "session_id", "action", "domain", "facets", "confidence", "taxonomy_version",
        "updated_at",
    ),
    ExportDataset.REPOSITORIES: _COMMON_FIELDS
    + (
        "group_key", "group_label", "repository_root", "session_count",
        "sessions_with_reconciled_token_data", "reconciled_local_total_tokens",
        "reconciled_local_input_tokens", "reconciled_local_cached_input_tokens",
        "reconciled_local_output_tokens", "reconciled_local_reasoning_output_tokens",
        "observed_rollout_total_tokens_sum", "observed_rollout_mean_tokens_per_session",
        "observed_rollout_median_tokens_per_session", "observed_rollout_p90_tokens_per_session",
        "sessions_per_day",
    ),
    ExportDataset.MODELS: _COMMON_FIELDS
    + (
        "group_key", "group_label", "model_provider", "session_count",
        "sessions_with_reconciled_token_data", "reconciled_local_total_tokens",
        "reconciled_local_input_tokens", "reconciled_local_cached_input_tokens",
        "reconciled_local_output_tokens", "reconciled_local_reasoning_output_tokens",
        "observed_rollout_total_tokens_sum", "observed_rollout_mean_tokens_per_session",
        "observed_rollout_median_tokens_per_session", "observed_rollout_p90_tokens_per_session",
        "sessions_per_day",
    ),
}
