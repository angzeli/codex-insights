"""Versioned, provenance-aware session outcome classification."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from codex_insights.models import SessionOutcome

OUTCOME_CLASSIFIER_VERSION = "outcome-classifier-v2"


class OutcomeConfidence(StrEnum):
    """Confidence in one conservative session-level classification."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LifecycleStatus(StrEnum):
    """Turn lifecycle state kept separate from task-outcome evidence."""

    TURN_COMPLETED = "turn_completed"
    ABORTED = "aborted"
    UNKNOWN = "unknown"


class OutcomeEvidenceKind(StrEnum):
    """Normalized evidence kinds that never contain transcript text."""

    VALIDATION_PASS = "validation_pass"
    VALIDATION_FAIL = "validation_fail"
    EDIT = "edit"
    HIGH_COMMIT = "high_commit"
    TASK_COMPLETE = "task_complete"
    ABORT = "abort"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class OutcomeEvidence:
    """One ordered, content-free evidence observation."""

    sequence: int
    kind: OutcomeEvidenceKind
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OutcomeAssessment:
    """Explainable result from the pure classifier."""

    outcome: SessionOutcome
    confidence: OutcomeConfidence
    evidence: tuple[str, ...]
    lifecycle_status: LifecycleStatus
    classifier_version: str = OUTCOME_CLASSIFIER_VERSION

    @property
    def strongly_evidenced(self) -> bool:
        return (
            self.outcome is not SessionOutcome.UNKNOWN
            and self.confidence in {OutcomeConfidence.HIGH, OutcomeConfidence.MEDIUM}
        )


def classify_outcome(evidence: tuple[OutcomeEvidence, ...]) -> OutcomeAssessment:
    """Classify ordered originated evidence while recognizing recovery."""

    ordered = tuple(sorted(evidence, key=lambda item: item.sequence))
    if not ordered:
        return _assessment(
            SessionOutcome.UNKNOWN,
            OutcomeConfidence.LOW,
            LifecycleStatus.UNKNOWN,
            "no_originated_evidence",
        )
    kinds = tuple(item.kind for item in ordered)
    last_abort = _last_index(kinds, OutcomeEvidenceKind.ABORT)
    last_task_complete = _last_index(kinds, OutcomeEvidenceKind.TASK_COMPLETE)
    lifecycle_status = (
        LifecycleStatus.ABORTED
        if last_abort > last_task_complete
        else (
            LifecycleStatus.TURN_COMPLETED
            if last_task_complete >= 0
            else LifecycleStatus.UNKNOWN
        )
    )
    last_validation_fail = _last_index(kinds, OutcomeEvidenceKind.VALIDATION_FAIL)
    last_validation_pass = _last_index(kinds, OutcomeEvidenceKind.VALIDATION_PASS)
    last_error = _last_index(kinds, OutcomeEvidenceKind.ERROR)
    last_positive = max(
        _last_index(kinds, OutcomeEvidenceKind.VALIDATION_PASS),
        _last_index(kinds, OutcomeEvidenceKind.HIGH_COMMIT),
    )

    if lifecycle_status is LifecycleStatus.ABORTED:
        return _assessment(
            SessionOutcome.ABANDONED,
            OutcomeConfidence.HIGH,
            lifecycle_status,
            "originated_abort_without_later_completion",
        )
    if last_validation_fail >= 0 and last_validation_pass <= last_validation_fail:
        return _assessment(
            SessionOutcome.FAILED,
            OutcomeConfidence.HIGH,
            lifecycle_status,
            "final_originated_validation_failure_without_recovery",
        )
    if last_error >= 0 and last_positive <= last_error:
        return _assessment(
            SessionOutcome.FAILED,
            OutcomeConfidence.MEDIUM,
            lifecycle_status,
            "final_originated_error_without_recovery",
        )
    recovered = last_validation_fail >= 0 and last_validation_pass > last_validation_fail
    if recovered:
        return _assessment(
            SessionOutcome.SUCCESS_WITH_WARNINGS,
            OutcomeConfidence.MEDIUM,
            lifecycle_status,
            "originated_validation_failure_followed_by_pass",
            "recovery_observed",
        )
    if OutcomeEvidenceKind.HIGH_COMMIT in kinds:
        return _assessment(
            SessionOutcome.SUCCESS,
            OutcomeConfidence.MEDIUM,
            lifecycle_status,
            "high_confidence_commit_without_later_failure",
        )
    if OutcomeEvidenceKind.VALIDATION_PASS in kinds:
        return _assessment(
            SessionOutcome.SUCCESS,
            OutcomeConfidence.MEDIUM,
            lifecycle_status,
            "originated_validation_pass_without_later_failure",
        )
    if OutcomeEvidenceKind.EDIT in kinds:
        return _assessment(
            SessionOutcome.PARTIAL,
            OutcomeConfidence.LOW,
            lifecycle_status,
            "originated_edit_without_strong_outcome_evidence",
        )
    if lifecycle_status is LifecycleStatus.TURN_COMPLETED:
        return _assessment(
            SessionOutcome.UNKNOWN,
            OutcomeConfidence.LOW,
            lifecycle_status,
            "turn_completed_without_task_outcome_evidence",
        )
    return _assessment(
        SessionOutcome.UNKNOWN,
        OutcomeConfidence.LOW,
        lifecycle_status,
        "insufficient_originated_evidence",
    )


def reconcile_session_outcomes(
    connection: sqlite3.Connection,
    session_ids: set[int] | None = None,
) -> None:
    """Build and persist assessments from normalized originated evidence only."""

    if session_ids is not None and not session_ids:
        return
    where = ""
    parameters: tuple[int, ...] = ()
    if session_ids is not None:
        placeholders = ",".join("?" for _ in session_ids)
        where = f"WHERE id IN ({placeholders})"
        parameters = tuple(sorted(session_ids))
    sessions = connection.execute(
        f"SELECT id FROM source_sessions {where} ORDER BY id",
        parameters,
    ).fetchall()
    for session in sessions:
        session_id = int(session["id"])
        evidence = _session_evidence(connection, session_id)
        assessment = classify_outcome(evidence)
        _upsert_assessment(connection, session_id, assessment, len(evidence))


def _session_evidence(
    connection: sqlite3.Connection,
    session_id: int,
) -> tuple[OutcomeEvidence, ...]:
    evidence: list[OutcomeEvidence] = []
    for row in connection.execute(
        """
        SELECT source_ordinal, occurred_at, command_category, result_status
        FROM tool_activity
        WHERE observed_session_id = ? AND origin_session_id = ?
          AND provenance_status = 'origin'
        ORDER BY source_ordinal, operation_ordinal
        """,
        (session_id, session_id),
    ):
        category = str(row["command_category"])
        result = str(row["result_status"])
        kind: OutcomeEvidenceKind | None = None
        if category in {"testing", "linting", "type_checking"}:
            if result == "success":
                kind = OutcomeEvidenceKind.VALIDATION_PASS
            elif result == "failure":
                kind = OutcomeEvidenceKind.VALIDATION_FAIL
        elif category == "editing_patching":
            kind = OutcomeEvidenceKind.EDIT
        if kind is not None:
            evidence.append(
                OutcomeEvidence(
                    sequence=int(row["source_ordinal"]) * 10,
                    kind=kind,
                    occurred_at=_stored_datetime(row["occurred_at"]),
                )
            )
    for row in connection.execute(
        """
        SELECT source_ordinal, occurred_at, event_family, source_payload_type
        FROM event_observations
        WHERE observed_session_id = ? AND origin_session_id = ?
          AND provenance_status = 'origin'
          AND (event_family IN ('task_lifecycle', 'error'))
        ORDER BY source_ordinal
        """,
        (session_id, session_id),
    ):
        payload_type = str(row["source_payload_type"])
        family = str(row["event_family"])
        if payload_type == "task_complete":
            kind = OutcomeEvidenceKind.TASK_COMPLETE
        elif payload_type == "turn_aborted":
            kind = OutcomeEvidenceKind.ABORT
        elif family == "error":
            kind = OutcomeEvidenceKind.ERROR
        else:
            continue
        evidence.append(
            OutcomeEvidence(
                sequence=int(row["source_ordinal"]) * 10 + 5,
                kind=kind,
                occurred_at=_stored_datetime(row["occurred_at"]),
            )
        )
    for row in connection.execute(
        """
        SELECT commits.committed_at
        FROM session_commit_associations AS associations
        JOIN git_commits AS commits ON commits.id = associations.commit_id
        WHERE associations.session_id = ? AND associations.confidence = 'high'
          AND associations.evidence_origin_session_id = ?
        """,
        (session_id, session_id),
    ):
        occurred_at = _stored_datetime(row["committed_at"])
        evidence.append(
            OutcomeEvidence(
                sequence=_sequence_for_time(evidence, occurred_at),
                kind=OutcomeEvidenceKind.HIGH_COMMIT,
                occurred_at=occurred_at,
            )
        )
    return tuple(sorted(evidence, key=lambda item: item.sequence))


def _sequence_for_time(
    evidence: list[OutcomeEvidence],
    occurred_at: datetime | None,
) -> int:
    if occurred_at is None:
        return max((item.sequence for item in evidence), default=0) + 1
    later = [
        item.sequence
        for item in evidence
        if item.occurred_at is not None and item.occurred_at <= occurred_at
    ]
    return max(later, default=0) + 1


def _upsert_assessment(
    connection: sqlite3.Connection,
    session_id: int,
    assessment: OutcomeAssessment,
    evidence_count: int,
) -> None:
    evidence_json = json.dumps(assessment.evidence, separators=(",", ":"))
    values = (
        assessment.outcome.value,
        assessment.confidence.value,
        evidence_json,
        evidence_count,
        assessment.lifecycle_status.value,
        int(assessment.strongly_evidenced),
        assessment.classifier_version,
    )
    existing = connection.execute(
        "SELECT outcome, confidence, evidence_json, evidence_count, lifecycle_status, "
        "strongly_evidenced, classifier_version "
        "FROM session_outcomes WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if existing is not None and tuple(existing) == values:
        return
    with connection:
        connection.execute(
            """
            INSERT INTO session_outcomes(
                session_id, outcome, confidence, evidence_json,
                evidence_count, lifecycle_status, strongly_evidenced,
                classifier_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                outcome = excluded.outcome,
                confidence = excluded.confidence,
                evidence_json = excluded.evidence_json,
                evidence_count = excluded.evidence_count,
                lifecycle_status = excluded.lifecycle_status,
                strongly_evidenced = excluded.strongly_evidenced,
                classifier_version = excluded.classifier_version,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                *values,
                _format_datetime(datetime.now(tz=UTC)),
            ),
        )


def _assessment(
    outcome: SessionOutcome,
    confidence: OutcomeConfidence,
    lifecycle_status: LifecycleStatus,
    *evidence: str,
) -> OutcomeAssessment:
    return OutcomeAssessment(
        outcome=outcome,
        confidence=confidence,
        evidence=tuple(evidence),
        lifecycle_status=lifecycle_status,
    )


def _last_index(
    kinds: tuple[OutcomeEvidenceKind, ...],
    target: OutcomeEvidenceKind,
) -> int:
    return next((index for index in range(len(kinds) - 1, -1, -1) if kinds[index] is target), -1)


def _stored_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _format_datetime(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")
