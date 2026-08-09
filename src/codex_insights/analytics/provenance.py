"""Privacy-safe queries over persisted event provenance metadata."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass

from codex_insights.models import EventFamily


class ProvenanceSessionNotFoundError(LookupError):
    """Raised when no indexed session matches a requested identifier."""


class AmbiguousProvenanceSessionError(LookupError):
    """Raised when a session prefix is not unique."""


@dataclass(frozen=True, slots=True)
class ProvenanceFamilySummary:
    family: str
    observed_events: int
    originated_events: int
    inherited_events: int
    duplicate_observations: int
    ambiguous_events: int
    unknown_events: int
    child_threads_with_replay: int
    child_threads_ambiguous: int
    child_threads_without_replay: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProvenanceSummary:
    session_id: str | None
    lineage_edges: int
    child_threads: int
    sessions_with_replay: int
    event_families_affected: int
    observed_events: int
    originated_events: int
    inherited_events: int
    duplicate_observations: int
    ambiguous_events: int
    unknown_events: int
    fingerprint_version: str | None
    algorithm_version: str | None
    families: tuple[ProvenanceFamilySummary, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "families": [item.to_dict() for item in self.families],
        }


def get_provenance_summary(
    connection: sqlite3.Connection,
    *,
    session_prefix: str | None = None,
    family: EventFamily | None = None,
) -> ProvenanceSummary:
    """Return aggregate event-origin diagnostics without event bodies."""

    session_id: str | None = None
    internal_id: int | None = None
    if session_prefix is not None:
        internal_id, session_id = _resolve_session(connection, session_prefix)
    where = ""
    parameters: list[object] = []
    if internal_id is not None:
        where = " WHERE observed_session_id = ?"
        parameters.append(internal_id)
    if family is not None:
        where += " AND" if where else " WHERE"
        where += " event_family = ?"
        parameters.append(family.value)

    totals = connection.execute(
        f"""
        SELECT COUNT(*) AS observed_events,
               SUM(provenance_status = 'origin') AS originated_events,
               SUM(provenance_status IN ('inherited_exact', 'inherited_prefix'))
                   AS inherited_events,
               SUM(provenance_status = 'observed_duplicate') AS duplicate_observations,
               SUM(provenance_status = 'ambiguous') AS ambiguous_events,
               SUM(provenance_status = 'unknown') AS unknown_events,
               COUNT(DISTINCT CASE
                   WHEN provenance_status IN ('inherited_exact', 'inherited_prefix')
                   THEN observed_session_id END) AS sessions_with_replay,
               COUNT(DISTINCT CASE
                   WHEN provenance_status IN ('inherited_exact', 'inherited_prefix')
                   THEN event_family END) AS affected_families,
               MIN(fingerprint_version) AS fingerprint_version,
               MIN(provenance_algorithm_version) AS algorithm_version
        FROM event_observations{where}
        """,
        tuple(parameters),
    ).fetchone()

    family_where = where
    family_rows = connection.execute(
        f"""
        SELECT event_family,
               COUNT(*) AS observed_events,
               SUM(provenance_status = 'origin') AS originated_events,
               SUM(provenance_status IN ('inherited_exact', 'inherited_prefix'))
                   AS inherited_events,
               SUM(provenance_status = 'observed_duplicate') AS duplicate_observations,
               SUM(provenance_status = 'ambiguous') AS ambiguous_events,
               SUM(provenance_status = 'unknown') AS unknown_events
        FROM event_observations{family_where}
        GROUP BY event_family
        ORDER BY event_family
        """,
        tuple(parameters),
    ).fetchall()
    replay_counts = _replay_counts(
        connection,
        session_id=internal_id,
        family=family,
    )
    families = tuple(
        ProvenanceFamilySummary(
            family=str(row["event_family"]),
            observed_events=int(row["observed_events"] or 0),
            originated_events=int(row["originated_events"] or 0),
            inherited_events=int(row["inherited_events"] or 0),
            duplicate_observations=int(row["duplicate_observations"] or 0),
            ambiguous_events=int(row["ambiguous_events"] or 0),
            unknown_events=int(row["unknown_events"] or 0),
            child_threads_with_replay=replay_counts.get(str(row["event_family"]), (0, 0, 0))[0],
            child_threads_ambiguous=replay_counts.get(str(row["event_family"]), (0, 0, 0))[1],
            child_threads_without_replay=replay_counts.get(str(row["event_family"]), (0, 0, 0))[2],
        )
        for row in family_rows
    )
    relation_filter = " WHERE child_session_id = ?" if internal_id is not None else ""
    relation_parameters = (internal_id,) if internal_id is not None else ()
    relation = connection.execute(
        f"""
        SELECT COUNT(*) AS edges, COUNT(DISTINCT child_session_id) AS children
        FROM thread_relationships{relation_filter}
        """,
        relation_parameters,
    ).fetchone()
    return ProvenanceSummary(
        session_id=session_id,
        lineage_edges=int(relation["edges"] or 0),
        child_threads=int(relation["children"] or 0),
        sessions_with_replay=int(totals["sessions_with_replay"] or 0),
        event_families_affected=int(totals["affected_families"] or 0),
        observed_events=int(totals["observed_events"] or 0),
        originated_events=int(totals["originated_events"] or 0),
        inherited_events=int(totals["inherited_events"] or 0),
        duplicate_observations=int(totals["duplicate_observations"] or 0),
        ambiguous_events=int(totals["ambiguous_events"] or 0),
        unknown_events=int(totals["unknown_events"] or 0),
        fingerprint_version=(
            str(totals["fingerprint_version"]) if totals["fingerprint_version"] else None
        ),
        algorithm_version=(
            str(totals["algorithm_version"]) if totals["algorithm_version"] else None
        ),
        families=families,
    )


def _replay_counts(
    connection: sqlite3.Connection,
    *,
    session_id: int | None,
    family: EventFamily | None,
) -> dict[str, tuple[int, int, int]]:
    conditions: list[str] = []
    parameters: list[object] = []
    if session_id is not None:
        conditions.append("relationships.child_session_id = ?")
        parameters.append(session_id)
    if family is not None:
        conditions.append("summary.event_family = ?")
        parameters.append(family.value)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = connection.execute(
        f"""
        SELECT summary.event_family,
               SUM(summary.inherited_events > 0) AS replay,
               SUM(summary.inherited_events = 0 AND summary.ambiguous_events > 0) AS ambiguous,
               SUM(summary.inherited_events = 0 AND summary.ambiguous_events = 0) AS no_replay
        FROM event_replay_summary AS summary
        JOIN thread_relationships AS relationships
          ON relationships.id = summary.relationship_id
        {where}
        GROUP BY summary.event_family
        """,
        tuple(parameters),
    )
    return {
        str(row["event_family"]): (
            int(row["replay"] or 0),
            int(row["ambiguous"] or 0),
            int(row["no_replay"] or 0),
        )
        for row in rows
    }


def _resolve_session(connection: sqlite3.Connection, prefix: str) -> tuple[int, str]:
    rows = connection.execute(
        """
        SELECT id, source_session_id FROM source_sessions
        WHERE source_session_id LIKE ?
        ORDER BY source_session_id
        LIMIT 2
        """,
        (f"{prefix}%",),
    ).fetchall()
    if not rows:
        raise ProvenanceSessionNotFoundError(prefix)
    if len(rows) > 1:
        raise AmbiguousProvenanceSessionError(prefix)
    return int(rows[0]["id"]), str(rows[0]["source_session_id"])
