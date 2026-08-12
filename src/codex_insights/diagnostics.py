"""Bounded, read-only compatibility diagnostics for Codex and the derived index."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from codex_insights.adapters.codex_index import PARSER_VERSION, select_state_database
from codex_insights.db import (
    SCHEMA_VERSION,
    UnsafeDatabasePathError,
    ensure_index_outside_codex_home,
)
from codex_insights.lineage import LINEAGE_ALGORITHM_VERSION
from codex_insights.outcomes import OUTCOME_CLASSIFIER_VERSION
from codex_insights.provenance import PROVENANCE_ALGORITHM_VERSION
from codex_insights.taxonomy import TASK_TAXONOMY_VERSION


@dataclass(frozen=True, slots=True)
class StateDatabaseDiagnostic:
    """One candidate state database without source row content."""

    name: str
    selected: bool
    readable: bool
    score: int
    catalogue_table: str | None
    relationship_table: str | None
    catalogue_rows: int | None
    valid_rollout_references: int
    missing_rollout_references: int
    schema_fingerprint: str | None
    reasons: tuple[str, ...]
    error_type: str | None


@dataclass(frozen=True, slots=True)
class CapabilityCoverageDiagnostic:
    """Current and previous coverage for one normalized capability."""

    capability: str
    available: int
    degraded: int
    not_observed: int
    unknown: int
    total: int
    current_ratio: float | None
    previous_ratio: float | None
    status: str


@dataclass(frozen=True, slots=True)
class UnknownSourceDiagnostic:
    """One bounded aggregate source-shape diagnostic without payload values."""

    category: str
    kind: str
    name: str
    occurrences: int
    affected_sessions: int
    first_seen_at: str | None
    last_seen_at: str | None
    newly_seen: bool
    capability_impact: str


@dataclass(frozen=True, slots=True)
class DeepDoctorReport:
    """Serializable deep diagnostics containing aggregate metadata only."""

    codex_home: str
    codex_home_exists: bool
    database_path: str
    database_path_safe: bool
    database_exists: bool
    database_integrity: str
    schema_version: int | None
    supported_schema_version: int
    parser_versions: dict[str, str]
    selected_state_database: str | None
    state_database_selection_reason: str
    state_databases: tuple[StateDatabaseDiagnostic, ...]
    source_session_count: int
    indexed_session_count: int
    missing_rollout_references: int
    stale_session_count: int
    parse_failure_count: int
    unknown_record_count: int
    unknown_record_categories: int
    unknown_diagnostic_counts: dict[str, int]
    unknown_diagnostics: tuple[UnknownSourceDiagnostic, ...]
    unknown_event_rate: float | None
    capability_coverage: tuple[CapabilityCoverageDiagnostic, ...]
    compatibility_warning_count: int
    token_lineage: dict[str, int]
    event_provenance: dict[str, int]
    source_index_difference: int | None
    latest_successful_index_at: str | None
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_deep_diagnostics(
    codex_home: Path,
    database_path: Path,
) -> DeepDoctorReport:
    """Inspect source and derived metadata without changing either database."""

    home = codex_home.expanduser().resolve(strict=False)
    database = database_path.expanduser().resolve(strict=False)
    selection = select_state_database(home) if home.is_dir() else None
    selected = selection.selected if selection is not None else None
    state_diagnostics = tuple(
        StateDatabaseDiagnostic(
            name=item.path.name,
            selected=selected is not None and item.path == selected.path,
            readable=item.readable,
            score=item.score,
            catalogue_table=item.catalogue_table,
            relationship_table=item.relationship_table,
            catalogue_rows=item.row_count,
            valid_rollout_references=item.valid_rollout_references,
            missing_rollout_references=item.missing_rollout_references,
            schema_fingerprint=item.schema_fingerprint or None,
            reasons=item.reasons,
            error_type=item.error_type,
        )
        for item in (selection.candidates if selection is not None else ())
    )
    source_sessions = selected.row_count if selected and selected.row_count is not None else 0
    missing_rollouts = selected.missing_rollout_references if selected else 0
    warnings: list[str] = []

    try:
        ensure_index_outside_codex_home(database, home)
        database_safe = True
    except UnsafeDatabasePathError as exc:
        database_safe = False
        warnings.append(str(exc))

    empty = _empty_database_diagnostics()
    if database_safe and database.is_file():
        try:
            with closing(_open_sqlite_readonly(database)) as connection:
                empty = _inspect_derived_database(connection)
        except (OSError, sqlite3.DatabaseError) as exc:
            empty["integrity"] = f"unreadable:{type(exc).__name__}"
            warnings.append(f"Derived database could not be read: {type(exc).__name__}.")
    elif database_safe:
        warnings.append("Codex Insights database does not exist yet.")

    if selected is None and home.is_dir():
        warnings.append("No compatible state database was selected.")
    if empty["schema_version"] is not None and empty["schema_version"] != SCHEMA_VERSION:
        warnings.append(
            f"Derived schema {empty['schema_version']} differs from supported schema "
            f"{SCHEMA_VERSION}."
        )
    if empty["stale_sessions"]:
        warnings.append(f"{empty['stale_sessions']} normalized session(s) are stale.")
    if empty["parse_failures"]:
        warnings.append(f"{empty['parse_failures']} source session(s) have parse failures.")
    if empty["compatibility_warnings"]:
        warnings.append(
            f"{empty['compatibility_warnings']} compatibility warning(s) are recorded."
        )

    indexed_sessions = int(empty["indexed_sessions"])
    source_difference = source_sessions - indexed_sessions if selected is not None else None
    return DeepDoctorReport(
        codex_home=str(home),
        codex_home_exists=home.is_dir(),
        database_path=str(database),
        database_path_safe=database_safe,
        database_exists=database.is_file(),
        database_integrity=str(empty["integrity"]),
        schema_version=empty["schema_version"],
        supported_schema_version=SCHEMA_VERSION,
        parser_versions={
            "source_parser": PARSER_VERSION,
            "event_provenance": PROVENANCE_ALGORITHM_VERSION,
            "token_lineage": LINEAGE_ALGORITHM_VERSION,
            "outcome_classifier": OUTCOME_CLASSIFIER_VERSION,
            "task_classifier": TASK_TAXONOMY_VERSION,
        },
        selected_state_database=selected.path.name if selected else None,
        state_database_selection_reason=(
            selection.explanation
            if selection is not None
            else "Codex home is missing; source selection was not attempted."
        ),
        state_databases=state_diagnostics,
        source_session_count=source_sessions,
        indexed_session_count=indexed_sessions,
        missing_rollout_references=missing_rollouts,
        stale_session_count=int(empty["stale_sessions"]),
        parse_failure_count=int(empty["parse_failures"]),
        unknown_record_count=int(empty["unknown_records"]),
        unknown_record_categories=int(empty["unknown_categories"]),
        unknown_diagnostic_counts=empty["unknown_diagnostic_counts"],
        unknown_diagnostics=empty["unknown_diagnostics"],
        unknown_event_rate=empty["unknown_event_rate"],
        capability_coverage=empty["capabilities"],
        compatibility_warning_count=int(empty["compatibility_warnings"]),
        token_lineage=empty["token_lineage"],
        event_provenance=empty["event_provenance"],
        source_index_difference=source_difference,
        latest_successful_index_at=empty["latest_successful_index_at"],
        warnings=tuple(warnings),
    )


def _open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _empty_database_diagnostics() -> dict[str, Any]:
    return {
        "integrity": "not_checked",
        "schema_version": None,
        "indexed_sessions": 0,
        "stale_sessions": 0,
        "parse_failures": 0,
        "unknown_records": 0,
        "unknown_categories": 0,
        "unknown_diagnostic_counts": {},
        "unknown_diagnostics": (),
        "unknown_event_rate": None,
        "compatibility_warnings": 0,
        "capabilities": (),
        "token_lineage": {},
        "event_provenance": {},
        "latest_successful_index_at": None,
    }


def _inspect_derived_database(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        )
    }
    result = _empty_database_diagnostics()
    result["integrity"] = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    if "schema_migrations" in tables:
        result["schema_version"] = int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        )
    if "source_sessions" in tables:
        result["indexed_sessions"] = int(
            connection.execute("SELECT COUNT(*) FROM source_sessions").fetchone()[0]
        )
    if "session_compatibility" in tables:
        result["stale_sessions"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM session_compatibility WHERE stale = 1"
            ).fetchone()[0]
        )
        result["parse_failures"] = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM session_compatibility
                WHERE parse_status IN ('failed', 'pending_source_change')
                """
            ).fetchone()[0]
        )
    if "unknown_source_records" in tables:
        unknown = connection.execute(
            """
            SELECT COALESCE(SUM(record_count), 0),
                   COUNT(DISTINCT unknown_kind || ':' || unknown_name)
            FROM unknown_source_records
            """
        ).fetchone()
        result["unknown_records"] = int(unknown[0])
        result["unknown_categories"] = int(unknown[1])
        unknown_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(unknown_source_records)")
        }
        if "diagnostic_category" not in unknown_columns:
            result["unknown_diagnostic_counts"] = {
                "unclassified": int(unknown[0])
            }
            return _finish_derived_diagnostics(connection, tables, result)
        result["unknown_diagnostic_counts"] = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """
                SELECT diagnostic_category, COALESCE(SUM(record_count), 0)
                FROM unknown_source_records
                GROUP BY diagnostic_category ORDER BY diagnostic_category
                """
            )
        }
        latest_run = connection.execute(
            "SELECT MAX(id) FROM index_runs WHERE status = 'completed'"
        ).fetchone()[0]
        result["unknown_diagnostics"] = tuple(
            UnknownSourceDiagnostic(
                category=str(row["diagnostic_category"]),
                kind=str(row["unknown_kind"]),
                name=str(row["unknown_name"]),
                occurrences=int(row["occurrences"]),
                affected_sessions=int(row["affected_sessions"]),
                first_seen_at=(
                    str(row["first_seen_at"]) if row["first_seen_at"] else None
                ),
                last_seen_at=str(row["last_seen_at"]) if row["last_seen_at"] else None,
                newly_seen=bool(
                    latest_run is not None
                    and row["first_index_run_id"] is not None
                    and int(row["first_index_run_id"]) == int(latest_run)
                ),
                capability_impact=str(row["capability_impact"]),
            )
            for row in connection.execute(
                """
                SELECT diagnostic_category, unknown_kind, unknown_name,
                       capability_impact, SUM(record_count) AS occurrences,
                       COUNT(DISTINCT source_session_id) AS affected_sessions,
                       MIN(first_seen_at) AS first_seen_at,
                       MAX(last_seen_at) AS last_seen_at,
                       MIN(first_index_run_id) AS first_index_run_id
                FROM unknown_source_records
                GROUP BY diagnostic_category, unknown_kind, unknown_name,
                         capability_impact
                ORDER BY CASE diagnostic_category
                    WHEN 'tool_result_gap' THEN 1
                    WHEN 'lifecycle_gap' THEN 2
                    WHEN 'semantic_gap' THEN 3
                    WHEN 'unclassified' THEN 4
                    WHEN 'field_passthrough' THEN 5
                    ELSE 6
                END, occurrences DESC, unknown_kind, unknown_name
                LIMIT 25
                """
            )
        )
    return _finish_derived_diagnostics(connection, tables, result)


def _finish_derived_diagnostics(
    connection: sqlite3.Connection,
    tables: set[str],
    result: dict[str, Any],
) -> dict[str, Any]:
    if "event_summary" in tables:
        event_totals = connection.execute(
            """
            SELECT COALESCE(SUM(event_count), 0),
                   COALESCE(SUM(CASE WHEN category = 'unknown'
                                     THEN event_count ELSE 0 END), 0)
            FROM event_summary
            """
        ).fetchone()
        total_events = int(event_totals[0])
        result["unknown_event_rate"] = (
            int(event_totals[1]) / total_events if total_events else None
        )
    if "compatibility_warnings" in tables:
        result["compatibility_warnings"] = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM compatibility_warnings
                WHERE index_run_id = (
                    SELECT MAX(id) FROM index_runs WHERE status = 'completed'
                )
                """
            ).fetchone()[0]
        )
    if "index_runs" in tables:
        latest = connection.execute(
            "SELECT MAX(completed_at) FROM index_runs WHERE status = 'completed'"
        ).fetchone()[0]
        result["latest_successful_index_at"] = str(latest) if latest else None
    if "coverage_snapshots" in tables:
        result["capabilities"] = _capability_diagnostics(connection)
    if "token_lineage" in tables:
        result["token_lineage"] = _grouped_counts(
            connection,
            "token_lineage",
            "deduplication_status",
        )
    if "event_observations" in tables:
        result["event_provenance"] = _grouped_counts(
            connection,
            "event_observations",
            "provenance_status",
        )
    return result


def _capability_diagnostics(
    connection: sqlite3.Connection,
) -> tuple[CapabilityCoverageDiagnostic, ...]:
    run_rows = connection.execute(
        """
        SELECT DISTINCT snapshots.index_run_id
        FROM coverage_snapshots AS snapshots
        JOIN index_runs AS runs ON runs.id = snapshots.index_run_id
        WHERE runs.status = 'completed'
        ORDER BY snapshots.index_run_id DESC LIMIT 2
        """
    ).fetchall()
    if not run_rows:
        return ()
    current_run = int(run_rows[0][0])
    previous_run = int(run_rows[1][0]) if len(run_rows) > 1 else None
    previous = {
        str(row["capability"]): row["coverage_ratio"]
        for row in connection.execute(
            "SELECT capability, coverage_ratio FROM coverage_snapshots WHERE index_run_id = ?",
            (previous_run,),
        )
    } if previous_run is not None else {}
    return tuple(
        CapabilityCoverageDiagnostic(
            capability=str(row["capability"]),
            available=int(row["available_count"]),
            degraded=int(row["degraded_count"]),
            not_observed=int(row["not_observed_count"]),
            unknown=int(row["unknown_count"]),
            total=int(row["total_count"]),
            current_ratio=(
                float(row["coverage_ratio"])
                if row["coverage_ratio"] is not None
                else None
            ),
            previous_ratio=(
                float(previous[str(row["capability"])])
                if previous.get(str(row["capability"])) is not None
                else None
            ),
            status=_coverage_status(
                row["coverage_ratio"],
                previous.get(str(row["capability"])),
                int(row["degraded_count"]),
            ),
        )
        for row in connection.execute(
            """
            SELECT * FROM coverage_snapshots
            WHERE index_run_id = ? ORDER BY capability
            """,
            (current_run,),
        )
    )


def _coverage_status(current: object, previous: object, degraded: int) -> str:
    if not isinstance(current, (int, float)):
        return "unavailable"
    if isinstance(previous, (int, float)) and float(previous) - float(current) >= 0.35:
        return "warning"
    if degraded:
        return "degraded"
    return "ok"


def _grouped_counts(
    connection: sqlite3.Connection,
    table: str,
    column: str,
) -> dict[str, int]:
    return {
        str(row[0]): int(row[1])
        for row in connection.execute(
            f'SELECT "{column}", COUNT(*) FROM "{table}" GROUP BY "{column}"'
        )
    }
