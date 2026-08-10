"""Queries for provenance-aware session outcome classifications."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codex_insights.db import open_index


@dataclass(frozen=True, slots=True)
class OutcomeFilters:
    """Filters for classification summaries."""

    since: datetime | None = None
    until: datetime | None = None
    repository: str | None = None
    model: str | None = None
    task_action: str | None = None
    task_domain: str | None = None
    outcome: str | None = None
    confidence: str | None = None
    limit: int = 100


@dataclass(frozen=True, slots=True)
class OutcomeSessionItem:
    """One classification without transcript content."""

    session_id: str
    started_at: datetime | None
    repository: str
    model: str | None
    outcome: str
    confidence: str
    evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "started_at": _json_datetime(self.started_at),
            "repository": self.repository,
            "model": self.model,
            "outcome": self.outcome,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class OutcomeReport:
    """Outcome and confidence distributions with UNKNOWN retained."""

    session_count: int
    classifiable_count: int
    unknown_count: int
    outcomes: tuple[tuple[str, int], ...]
    confidence: tuple[tuple[str, int], ...]
    sessions: tuple[OutcomeSessionItem, ...]

    def to_dict(self) -> dict[str, object]:
        denominator = self.classifiable_count
        return {
            "session_count": self.session_count,
            "classifiable_count": self.classifiable_count,
            "unknown_count": self.unknown_count,
            "outcomes": {
                key: {
                    "count": count,
                    "fraction_of_classifiable": (
                        count / denominator if denominator and key != "unknown" else None
                    ),
                }
                for key, count in self.outcomes
            },
            "confidence": dict(self.confidence),
            "sessions": [item.to_dict() for item in self.sessions],
            "classification_semantics": "originated_evidence",
        }


def get_outcome_report(
    database_path: Path,
    *,
    codex_home: Path,
    filters: OutcomeFilters | None = None,
) -> OutcomeReport:
    """Return filtered classifications and retain every UNKNOWN session."""

    selected = filters or OutcomeFilters()
    if selected.limit < 1:
        raise ValueError("limit must be at least 1")
    query, parameters = _query(selected)
    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        rows = connection.execute(query, parameters).fetchall()
    outcomes = Counter(str(row["outcome"] or "unknown") for row in rows)
    confidence = Counter(str(row["confidence"] or "low") for row in rows)
    unknown = outcomes["unknown"]
    return OutcomeReport(
        session_count=len(rows),
        classifiable_count=len(rows) - unknown,
        unknown_count=unknown,
        outcomes=tuple(sorted(outcomes.items())),
        confidence=tuple(sorted(confidence.items())),
        sessions=tuple(_item(row) for row in rows[: selected.limit]),
    )


def _query(filters: OutcomeFilters) -> tuple[str, tuple[object, ...]]:
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
    if filters.task_action:
        conditions.append("COALESCE(tasks.action, 'unknown') = ?")
        parameters.append(filters.task_action.casefold())
    if filters.task_domain:
        conditions.append("COALESCE(tasks.domain, 'unknown') = ?")
        parameters.append(filters.task_domain.casefold())
    if filters.outcome:
        conditions.append("COALESCE(outcomes.outcome, 'unknown') = ?")
        parameters.append(filters.outcome.casefold())
    if filters.confidence:
        conditions.append("COALESCE(outcomes.confidence, 'low') = ?")
        parameters.append(filters.confidence.casefold())
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return (
        f"""
        SELECT sessions.source_session_id, sessions.started_at, sessions.repository_name,
               sessions.repository_root, sessions.model,
               COALESCE(outcomes.outcome, 'unknown') AS outcome,
               COALESCE(outcomes.confidence, 'low') AS confidence,
               COALESCE(outcomes.evidence_json, '[\"not_classified\"]') AS evidence_json
        FROM source_sessions AS sessions
        LEFT JOIN repositories AS repositories ON repositories.id = sessions.repository_id
        LEFT JOIN session_outcomes AS outcomes ON outcomes.session_id = sessions.id
        LEFT JOIN session_tasks AS tasks ON tasks.session_id = sessions.id
        {where}
        ORDER BY sessions.started_at IS NULL, sessions.started_at DESC,
                 sessions.source_session_id
        """,
        tuple(parameters),
    )


def _item(row: sqlite3.Row) -> OutcomeSessionItem:
    evidence_value = json.loads(str(row["evidence_json"]))
    evidence = (
        tuple(str(item) for item in evidence_value)
        if isinstance(evidence_value, list)
        else ("invalid_evidence",)
    )
    repository = str(row["repository_name"] or "Outside Git repositories")
    return OutcomeSessionItem(
        session_id=str(row["source_session_id"]),
        started_at=_stored_datetime(row["started_at"]),
        repository=repository,
        model=str(row["model"]) if row["model"] else None,
        outcome=str(row["outcome"]),
        confidence=str(row["confidence"]),
        evidence=evidence,
    )


def _stored_datetime(value: object) -> datetime | None:
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


def _json_datetime(value: datetime | None) -> str | None:
    return _database_datetime(value) if value is not None else None
