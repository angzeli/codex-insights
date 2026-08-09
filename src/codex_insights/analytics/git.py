"""Queries for provenance-aware Git commit associations."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codex_insights.db import open_index


class CommitNotFoundError(LookupError):
    """Raised when no indexed Git commit matches a hash prefix."""


class AmbiguousCommitHashError(LookupError):
    """Raised when a commit prefix is not unique across repository identities."""


@dataclass(frozen=True, slots=True)
class GitFilters:
    """Filters shared by commit list and report queries."""

    since: datetime | None = None
    until: datetime | None = None
    repository: str | None = None
    confidence: str | None = None
    model: str | None = None
    limit: int = 100


@dataclass(frozen=True, slots=True)
class CommitAssociationItem:
    """One explainable session-to-commit association."""

    commit_hash: str
    committed_at: datetime
    repository: str
    repository_identity: str
    session_id: str
    model: str | None
    confidence: str
    evidence_type: str
    evidence_explanation: str
    ambiguous: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "commit_hash": self.commit_hash,
            "committed_at": _json_datetime(self.committed_at),
            "repository": self.repository,
            "repository_identity": self.repository_identity,
            "session_id": self.session_id,
            "model": self.model,
            "confidence": self.confidence,
            "evidence_type": self.evidence_type,
            "evidence_explanation": self.evidence_explanation,
            "ambiguous": self.ambiguous,
        }


@dataclass(frozen=True, slots=True)
class CommitReport:
    """Confidence-tiered commit association summary."""

    associations: tuple[CommitAssociationItem, ...]
    high: int
    medium: int
    low: int
    ambiguous: int
    repositories_resolved: int
    sessions_with_high_confidence_commits: int
    high_confidence_commits: int
    reconciled_tokens_for_high_sessions: int | None
    high_sessions_with_token_data: int
    reconciled_tokens_per_confirmed_commit: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "associations": [item.to_dict() for item in self.associations],
            "high": self.high,
            "medium": self.medium,
            "low": self.low,
            "ambiguous": self.ambiguous,
            "repositories_resolved": self.repositories_resolved,
            "sessions_with_high_confidence_commits": self.sessions_with_high_confidence_commits,
            "high_confidence_commits": self.high_confidence_commits,
            "reconciled_tokens_for_high_sessions": self.reconciled_tokens_for_high_sessions,
            "high_sessions_with_token_data": self.high_sessions_with_token_data,
            "reconciled_tokens_per_confirmed_commit": (
                self.reconciled_tokens_per_confirmed_commit
            ),
            "ratio_semantics": "descriptive_reconciled_tokens_per_high_confidence_commit",
        }


def get_commit_report(
    database_path: Path,
    *,
    codex_home: Path,
    filters: GitFilters | None = None,
) -> CommitReport:
    """Return confidence-tiered associations and descriptive token coverage."""

    selected = filters or GitFilters()
    if selected.limit < 1:
        raise ValueError("limit must be at least 1")
    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        rows = connection.execute(*_association_query(selected)).fetchall()
        repositories_resolved = int(
            connection.execute(
                "SELECT COUNT(*) FROM repositories WHERE path_exists = 1"
            ).fetchone()[0]
        )
        high_sessions = {
            int(row["session_internal_id"])
            for row in rows
            if row["confidence"] == "high"
        }
        high_commits = {
            (int(row["repository_internal_id"]), str(row["commit_hash"]))
            for row in rows
            if row["confidence"] == "high"
        }
        unique_token_values = {
            int(row["session_internal_id"]): int(row["aggregate_total_tokens"])
            for row in rows
            if row["confidence"] == "high"
            and row["aggregate_total_tokens"] is not None
        }
    total_tokens = sum(unique_token_values.values()) if unique_token_values else None
    return CommitReport(
        associations=tuple(_association_item(row) for row in rows),
        high=sum(row["confidence"] == "high" for row in rows),
        medium=sum(row["confidence"] == "medium" for row in rows),
        low=sum(row["confidence"] == "low" for row in rows),
        ambiguous=sum(bool(row["ambiguous"]) for row in rows),
        repositories_resolved=repositories_resolved,
        sessions_with_high_confidence_commits=len(high_sessions),
        high_confidence_commits=len(high_commits),
        reconciled_tokens_for_high_sessions=total_tokens,
        high_sessions_with_token_data=len(unique_token_values),
        reconciled_tokens_per_confirmed_commit=(
            total_tokens / len(high_commits)
            if total_tokens is not None and high_commits
            else None
        ),
    )


def get_commit(
    database_path: Path,
    commit_prefix: str,
    *,
    codex_home: Path,
) -> tuple[CommitAssociationItem, ...]:
    """Resolve one commit hash prefix and return all candidate session associations."""

    prefix = commit_prefix.strip().casefold()
    if not prefix:
        raise CommitNotFoundError("Commit hash cannot be empty")
    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        matches = connection.execute(
            """
            SELECT commits.id
            FROM git_commits AS commits
            JOIN repositories AS repositories ON repositories.id = commits.repository_id
            WHERE commits.commit_hash LIKE ?
            ORDER BY repositories.identity_key, commits.commit_hash
            LIMIT 2
            """,
            (prefix + "%",),
        ).fetchall()
        if not matches:
            raise CommitNotFoundError(f"No commit matches {commit_prefix!r}")
        if len(matches) > 1:
            raise AmbiguousCommitHashError(
                f"Commit prefix {commit_prefix!r} is ambiguous across repositories"
            )
        commit_id = int(matches[0]["id"])
        rows = connection.execute(
            _ASSOCIATION_SELECT + " WHERE commits.id = ? " + _ASSOCIATION_ORDER,
            (commit_id,),
        ).fetchall()
    return tuple(_association_item(row) for row in rows)


def list_session_commits(
    database_path: Path,
    session_prefix: str,
    *,
    codex_home: Path,
) -> tuple[CommitAssociationItem, ...]:
    """Return all commit associations for an unambiguous session prefix."""

    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        sessions = connection.execute(
            "SELECT id FROM source_sessions WHERE source_session_id LIKE ? "
            "ORDER BY source_session_id LIMIT 2",
            (session_prefix.strip() + "%",),
        ).fetchall()
        if len(sessions) != 1:
            return ()
        rows = connection.execute(
            _ASSOCIATION_SELECT
            + " WHERE sessions.id = ? "
            + _ASSOCIATION_ORDER,
            (int(sessions[0]["id"]),),
        ).fetchall()
    return tuple(_association_item(row) for row in rows)


_ASSOCIATION_SELECT = """
SELECT commits.commit_hash, commits.committed_at,
       repositories.id AS repository_internal_id,
       repositories.identity_key, repositories.display_name,
       sessions.id AS session_internal_id, sessions.source_session_id,
       sessions.model, associations.confidence, associations.evidence_type,
       associations.evidence_explanation, associations.ambiguous,
       usage.aggregate_total_tokens
FROM session_commit_associations AS associations
JOIN git_commits AS commits ON commits.id = associations.commit_id
JOIN repositories AS repositories ON repositories.id = commits.repository_id
JOIN source_sessions AS sessions ON sessions.id = associations.session_id
LEFT JOIN accounted_usage AS usage ON usage.source_session_id = sessions.id
"""
_ASSOCIATION_ORDER = """
ORDER BY commits.committed_at DESC, repositories.identity_key,
         commits.commit_hash, sessions.source_session_id
"""


def _association_query(filters: GitFilters) -> tuple[str, tuple[object, ...]]:
    conditions: list[str] = []
    parameters: list[object] = []
    if filters.since is not None:
        conditions.append("commits.committed_at >= ?")
        parameters.append(_database_datetime(filters.since))
    if filters.until is not None:
        conditions.append("commits.committed_at < ?")
        parameters.append(_database_datetime(filters.until))
    if filters.repository:
        conditions.append(
            "(repositories.display_name = ? COLLATE NOCASE "
            "OR repositories.identity_key = ? OR repositories.canonical_root = ?)"
        )
        parameters.extend((filters.repository, filters.repository, filters.repository))
    if filters.confidence:
        conditions.append("associations.confidence = ?")
        parameters.append(filters.confidence.casefold())
    if filters.model:
        if filters.model.casefold() in {"unknown", "none"}:
            conditions.append("(sessions.model IS NULL OR sessions.model = '')")
        else:
            conditions.append("sessions.model = ? COLLATE NOCASE")
            parameters.append(filters.model)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    parameters.append(filters.limit)
    return _ASSOCIATION_SELECT + f" {where} " + _ASSOCIATION_ORDER + " LIMIT ?", tuple(
        parameters
    )


def _association_item(row: sqlite3.Row) -> CommitAssociationItem:
    committed_at = _stored_datetime(row["committed_at"])
    if committed_at is None:
        raise RuntimeError("Indexed Git commit has an invalid timestamp")
    return CommitAssociationItem(
        commit_hash=str(row["commit_hash"]),
        committed_at=committed_at,
        repository=str(row["display_name"]),
        repository_identity=str(row["identity_key"]),
        session_id=str(row["source_session_id"]),
        model=str(row["model"]) if row["model"] else None,
        confidence=str(row["confidence"]),
        evidence_type=str(row["evidence_type"]),
        evidence_explanation=str(row["evidence_explanation"]),
        ambiguous=bool(row["ambiguous"]),
    )


def _stored_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _database_datetime(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_datetime(value: datetime) -> str:
    return _database_datetime(value)
