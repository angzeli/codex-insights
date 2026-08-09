"""Incremental indexing from normalized source-adapter output."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from codex_insights.db import open_index
from codex_insights.git_correlation import reconcile_git_commits
from codex_insights.lineage import (
    LINEAGE_ALGORITHM_VERSION,
    analyze_thread_topology,
    assess_token_lineage,
)
from codex_insights.models import (
    EventFamily,
    EventProvenanceStatus,
    NormalizedEventObservation,
    NormalizedSourceSession,
    NormalizedThreadRelationship,
    NormalizedToolResultCandidate,
    NormalizedUsage,
    ParsedSourceSession,
    SourceSessionCandidate,
    TokenLineageAssessment,
    UsageVector,
)
from codex_insights.outcomes import reconcile_session_outcomes
from codex_insights.privacy import PROMPT_CONTENT_SCHEMA_VERSION, redact_prompt
from codex_insights.provenance import (
    PROVENANCE_ALGORITHM_VERSION,
    EventFamilyAssessment,
    assess_event_provenance,
    mirrored_user_observations,
)
from codex_insights.repository_identity import (
    REPOSITORY_IDENTITY_VERSION,
    RepositoryIdentity,
    resolve_repository_identity,
)
from codex_insights.taxonomy import reconcile_task_taxonomy


class IndexSourceAdapter(Protocol):
    """Source-independent contract used by the index orchestration layer."""

    @property
    def name(self) -> str:
        """Return the stable source adapter name."""

    @property
    def parser_version(self) -> str:
        """Return a version that changes when normalization semantics change."""

    def discover_sessions(
        self,
    ) -> tuple[tuple[SourceSessionCandidate, ...], tuple[str, ...]]:
        """Return normalized catalogue candidates and safe aggregate warnings."""

    def parse_session(self, candidate: SourceSessionCandidate) -> ParsedSourceSession:
        """Return normalized metadata and aggregate values for one source session."""


@dataclass(frozen=True, slots=True)
class IndexReport:
    """Aggregate outcome of one incremental indexing run."""

    database_path: Path
    discovered: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    warnings: tuple[str, ...] = ()


def index_source(
    adapter: IndexSourceAdapter,
    database_path: Path,
    *,
    codex_home: Path,
) -> IndexReport:
    """Incrementally upsert normalized sessions while isolating per-session failures."""

    now = _utc_now()
    counts = {name: 0 for name in ("new", "updated", "unchanged", "skipped", "failed")}
    warnings: list[str] = []
    parsed_sessions: dict[str, ParsedSourceSession] = {}
    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        run_id = _start_run(connection, codex_home=codex_home, started_at=now)
        try:
            candidates, discovery_warnings = adapter.discover_sessions()
            warnings.extend(discovery_warnings)
            for candidate in candidates:
                _index_candidate(
                    connection,
                    adapter=adapter,
                    candidate=candidate,
                    counts=counts,
                    parsed_sessions=parsed_sessions,
                )
            _reconcile_repositories(connection)
            relationships, relationship_warnings = _discover_relationships(adapter)
            warnings.extend(relationship_warnings)
            warnings.extend(
                _reconcile_relationships(
                    connection,
                    adapter=adapter,
                    candidates=candidates,
                    parsed_sessions=parsed_sessions,
                    relationships=relationships,
                    codex_home=codex_home,
                )
            )
            _reconcile_tool_activity(
                connection,
                adapter=adapter,
                codex_home=codex_home,
                parsed_sessions=parsed_sessions,
            )
            warnings.extend(reconcile_git_commits(connection))
            reconcile_session_outcomes(connection)
            _reconcile_prompts(
                connection,
                adapter=adapter,
                codex_home=codex_home,
                parsed_sessions=parsed_sessions,
            )
            reconcile_task_taxonomy(connection)
            _finish_run(
                connection,
                run_id=run_id,
                status="completed",
                discovered=len(candidates),
                counts=counts,
            )
        except Exception:
            _finish_run(
                connection,
                run_id=run_id,
                status="failed",
                discovered=0,
                counts=counts,
            )
            raise

    return IndexReport(
        database_path=database_path.expanduser().resolve(strict=False),
        discovered=len(candidates),
        new=counts["new"],
        updated=counts["updated"],
        unchanged=counts["unchanged"],
        skipped=counts["skipped"],
        failed=counts["failed"],
        warnings=tuple(warnings),
    )


def _index_candidate(
    connection: sqlite3.Connection,
    *,
    adapter: IndexSourceAdapter,
    candidate: SourceSessionCandidate,
    counts: dict[str, int],
    parsed_sessions: dict[str, ParsedSourceSession],
) -> None:
    session = candidate.session
    existing = _existing_session(connection, session)
    state = _ingestion_state(connection, candidate)
    catalogue_session = _preserve_rollout_metadata(session, existing)

    if not candidate.rollout_allowed or not candidate.rollout_exists:
        status = "outside_codex_home" if not candidate.rollout_allowed else "missing"
        with connection:
            session_id, is_new, _ = _upsert_session_metadata(connection, catalogue_session)
            if is_new:
                _replace_usage(connection, session_id, NormalizedUsage())
            if not _state_matches_status(
                state,
                candidate,
                parser_version=adapter.parser_version,
                status=status,
            ):
                _upsert_ingestion_state(
                    connection,
                    candidate,
                    parser_version=adapter.parser_version,
                    status=status,
                    error=None,
                    parsed_byte_offset=None,
                )
        counts["skipped"] += 1
        return

    if existing is not None and _source_unchanged(
        state,
        candidate,
        parser_version=adapter.parser_version,
    ):
        with connection:
            _, _, metadata_changed = _upsert_session_metadata(connection, catalogue_session)
        counts["updated" if metadata_changed else "unchanged"] += 1
        return

    try:
        parsed = adapter.parse_session(candidate)
        parsed_sessions[session.source_session_id] = parsed
        status = (
            "indexed_with_warnings"
            if parsed.malformed_line_count or parsed.oversized_line_count
            else "indexed"
        )
        warning = _parse_warning(parsed)
        with connection:
            session_id, is_new, _ = _upsert_session_metadata(connection, parsed.session)
            _replace_usage(connection, session_id, parsed.session.usage)
            _replace_event_summary(connection, session_id, parsed.session)
            _replace_event_observations(connection, session_id, parsed)
            _upsert_ingestion_state(
                connection,
                candidate,
                parser_version=adapter.parser_version,
                status=status,
                error=warning,
                parsed_byte_offset=parsed.parsed_byte_count,
            )
        counts["new" if is_new else "updated"] += 1
    except Exception as exc:
        with connection:
            session_id, is_new, _ = _upsert_session_metadata(connection, catalogue_session)
            if is_new:
                _replace_usage(connection, session_id, NormalizedUsage())
            _upsert_ingestion_state(
                connection,
                candidate,
                parser_version=adapter.parser_version,
                status="failed",
                error=type(exc).__name__,
                parsed_byte_offset=None,
            )
        counts["failed"] += 1


def _discover_relationships(
    adapter: IndexSourceAdapter,
) -> tuple[tuple[NormalizedThreadRelationship, ...], tuple[str, ...]]:
    discover = getattr(adapter, "discover_relationships", None)
    if not callable(discover):
        return (), ()
    result = cast(
        tuple[tuple[NormalizedThreadRelationship, ...], tuple[str, ...]],
        discover(),
    )
    return result


def _reconcile_repositories(connection: sqlite3.Connection) -> None:
    """Attach sessions to stable remote/common-dir/path repository identities."""

    rows = connection.execute(
        """
        SELECT id, repository_root, repository_name, git_origin_url,
               COALESCE(updated_at, started_at, first_ingested_at) AS activity_time
        FROM source_sessions
        ORDER BY activity_time, id
        """
    ).fetchall()
    session_keys: dict[int, str | None] = {}
    identities: dict[str, RepositoryIdentity] = {}
    for row in rows:
        identity = resolve_repository_identity(
            _stored_path(row["repository_root"]),
            str(row["repository_name"]) if row["repository_name"] else None,
            str(row["git_origin_url"]) if row["git_origin_url"] else None,
        )
        session_id = int(row["id"])
        session_keys[session_id] = identity.key if identity is not None else None
        if identity is None:
            continue
        previous = identities.get(identity.key)
        if previous is None or identity.path_exists or not previous.path_exists:
            identities[identity.key] = identity

    repository_ids: dict[str, int] = {}
    with connection:
        for key, identity in sorted(identities.items()):
            existing = connection.execute(
                "SELECT id FROM repositories WHERE identity_key = ?",
                (key,),
            ).fetchone()
            now = _utc_now()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO repositories(
                        identity_key, display_name, identity_method,
                        normalized_remote, canonical_root, common_git_dir,
                        path_exists, identity_version, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        identity.display_name,
                        identity.method,
                        identity.normalized_remote,
                        _format_path(identity.canonical_root),
                        _format_path(identity.common_git_dir),
                        int(identity.path_exists),
                        REPOSITORY_IDENTITY_VERSION,
                        now,
                        now,
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return a repository identifier")
                repository_ids[key] = int(cursor.lastrowid)
            else:
                repository_id = int(existing["id"])
                repository_ids[key] = repository_id
                connection.execute(
                    """
                    UPDATE repositories
                    SET display_name = ?, identity_method = ?, normalized_remote = ?,
                        canonical_root = ?, common_git_dir = ?, path_exists = ?,
                        identity_version = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        identity.display_name,
                        identity.method,
                        identity.normalized_remote,
                        _format_path(identity.canonical_root),
                        _format_path(identity.common_git_dir),
                        int(identity.path_exists),
                        REPOSITORY_IDENTITY_VERSION,
                        now,
                        repository_id,
                    ),
                )
        connection.executemany(
            "UPDATE source_sessions SET repository_id = ? WHERE id = ?",
            (
                (repository_ids.get(key) if key is not None else None, session_id)
                for session_id, key in sorted(session_keys.items())
            ),
        )


def _reconcile_relationships(
    connection: sqlite3.Connection,
    *,
    adapter: IndexSourceAdapter,
    candidates: tuple[SourceSessionCandidate, ...],
    parsed_sessions: dict[str, ParsedSourceSession],
    relationships: tuple[NormalizedThreadRelationship, ...],
    codex_home: Path,
) -> tuple[str, ...]:
    """Persist explicit topology and recompute only affected child accounting."""

    source_home = str(codex_home.expanduser().resolve(strict=False))
    candidates_by_id = {
        candidate.session.source_session_id: candidate for candidate in candidates
    }
    session_ids = set(candidates_by_id)
    topology = analyze_thread_topology(session_ids, relationships)
    warnings: list[str] = []
    duplicate_children = {
        child
        for child in {item.child_source_session_id for item in relationships}
        if sum(item.child_source_session_id == child for item in relationships) > 1
    }
    if duplicate_children:
        warnings.append(
            f"{len(duplicate_children)} child thread identifiers had multiple parents; "
            "their token lineage remains ambiguous."
        )

    internal_ids = {
        str(row["source_session_id"]): int(row["id"])
        for row in connection.execute(
            "SELECT id, source_session_id FROM source_sessions WHERE source_type = ? "
            "AND source_home = ?",
            (adapter.name, source_home),
        )
    }
    existing_keys = {
        (
            str(row["parent_source_session_id"]),
            str(row["child_source_session_id"]),
        )
        for row in connection.execute(
            "SELECT parent_source_session_id, child_source_session_id "
            "FROM thread_relationships WHERE source_type = ? AND source_home = ?",
            (adapter.name, source_home),
        )
    }
    current_keys = {
        (item.parent_source_session_id, item.child_source_session_id)
        for item in relationships
    }

    with connection:
        _delete_stale_relationships(
            connection,
            source_type=adapter.name,
            source_home=source_home,
            current_keys=current_keys,
        )
        for relationship in relationships:
            child_internal_id = (
                None
                if relationship.child_source_session_id in duplicate_children
                else internal_ids.get(relationship.child_source_session_id)
            )
            _upsert_relationship(
                connection,
                relationship,
                parent_session_id=internal_ids.get(relationship.parent_source_session_id),
                child_session_id=child_internal_id,
            )
        connection.execute(
            """
            DELETE FROM token_lineage
            WHERE child_session_id IN (
                SELECT id FROM source_sessions WHERE source_type = ? AND source_home = ?
            )
              AND child_session_id NOT IN (
                SELECT child_session_id FROM thread_relationships
                WHERE source_type = ? AND source_home = ? AND child_session_id IS NOT NULL
            )
            """,
            (adapter.name, source_home, adapter.name, source_home),
        )

    parse_cache = dict(parsed_sessions)
    for relationship in relationships:
        parent_id = internal_ids.get(relationship.parent_source_session_id)
        child_id = (
            None
            if relationship.child_source_session_id in duplicate_children
            else internal_ids.get(relationship.child_source_session_id)
        )
        if parent_id is None or child_id is None:
            continue
        existing = connection.execute(
            "SELECT * FROM token_lineage WHERE child_session_id = ?",
            (child_id,),
        ).fetchone()
        endpoints_changed = bool(
            relationship.parent_source_session_id in parsed_sessions
            or relationship.child_source_session_id in parsed_sessions
            or (
                relationship.parent_source_session_id,
                relationship.child_source_session_id,
            )
            not in existing_keys
        )
        if (
            existing is not None
            and int(existing["parent_session_id"]) == parent_id
            and existing["algorithm_version"] == LINEAGE_ALGORITHM_VERSION
            and not endpoints_changed
        ):
            continue

        cyclic = bool(
            relationship.parent_source_session_id in topology.cycle_nodes
            or relationship.child_source_session_id in topology.cycle_nodes
        )
        if cyclic:
            assessment = assess_token_lineage((), (), cyclic=True)
        else:
            parent = _parse_for_lineage(
                adapter,
                candidates_by_id.get(relationship.parent_source_session_id),
                parse_cache,
            )
            child = _parse_for_lineage(
                adapter,
                candidates_by_id.get(relationship.child_source_session_id),
                parse_cache,
            )
            assessment = assess_token_lineage(
                parent.token_snapshots if parent is not None else (),
                child.token_snapshots if child is not None else (),
            )
        with connection:
            _upsert_token_lineage(connection, parent_id, child_id, assessment)
    _reconcile_event_provenance(
        connection,
        internal_ids=internal_ids,
        relationships=relationships,
        duplicate_children=duplicate_children,
        cycle_source_ids=topology.cycle_nodes,
    )
    return tuple(warnings)


def _reconcile_tool_activity(
    connection: sqlite3.Connection,
    *,
    adapter: IndexSourceAdapter,
    codex_home: Path,
    parsed_sessions: dict[str, ParsedSourceSession],
) -> None:
    """Persist bounded tool metadata after the shared event provenance pass."""

    source_home = str(codex_home.expanduser().resolve(strict=False))
    session_ids = {
        str(row["source_session_id"]): int(row["id"])
        for row in connection.execute(
            "SELECT id, source_session_id FROM source_sessions "
            "WHERE source_type = ? AND source_home = ?",
            (adapter.name, source_home),
        )
    }
    with connection:
        for source_session_id, parsed in sorted(parsed_sessions.items()):
            session_id = session_ids.get(source_session_id)
            if session_id is None:
                continue
            connection.execute(
                "DELETE FROM tool_activity WHERE observed_session_id = ?",
                (session_id,),
            )
            event_rows = {
                int(row["source_ordinal"]): row for row in _event_rows(connection, session_id)
            }
            calls_per_id: dict[str, int] = {}
            for candidate in parsed.tool_call_candidates:
                if candidate.call_id_digest is not None:
                    calls_per_id[candidate.call_id_digest] = (
                        calls_per_id.get(candidate.call_id_digest, 0) + 1
                    )
            results_by_id: dict[str, list[NormalizedToolResultCandidate]] = {}
            for candidate_result in parsed.tool_result_candidates:
                if candidate_result.call_id_digest is not None:
                    results_by_id.setdefault(candidate_result.call_id_digest, []).append(
                        candidate_result
                    )
            now = _utc_now()
            for candidate in parsed.tool_call_candidates:
                event_row = event_rows.get(candidate.source_ordinal)
                if event_row is None:
                    continue
                output_result: NormalizedToolResultCandidate | None = None
                if (
                    candidate.call_id_digest is not None
                    and calls_per_id.get(candidate.call_id_digest) == 1
                ):
                    matches = results_by_id.get(candidate.call_id_digest, [])
                    known = [
                        item
                        for item in matches
                        if item.status.value != "unknown"
                    ]
                    if len(known) == 1:
                        output_result = known[0]
                    elif len(matches) == 1:
                        output_result = matches[0]
                output_event_id = None
                if output_result is not None:
                    output_row = event_rows.get(output_result.source_ordinal)
                    output_event_id = int(output_row["id"]) if output_row is not None else None
                connection.execute(
                    """
                    INSERT INTO tool_activity(
                        event_observation_id, observed_session_id, origin_session_id,
                        source_ordinal, operation_ordinal, occurred_at, tool_family,
                        tool_name, command_category, command_text, command_fingerprint,
                        executable, test_scope, call_id_digest,
                        output_event_observation_id, exit_code, duration_seconds,
                        result_commit_hash, result_commit_abbrev,
                        result_status, provenance_status, redacted, truncated,
                        extraction_version, classifier_version, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        int(event_row["id"]),
                        session_id,
                        event_row["origin_session_id"],
                        candidate.source_ordinal,
                        candidate.operation_ordinal,
                        _format_datetime(candidate.occurred_at),
                        candidate.tool_family.value,
                        candidate.tool_name,
                        candidate.command_category.value,
                        candidate.command_text,
                        candidate.command_fingerprint,
                        candidate.executable,
                        candidate.test_scope.value,
                        candidate.call_id_digest,
                        output_event_id,
                        output_result.exit_code if output_result is not None else None,
                        output_result.duration_seconds if output_result is not None else None,
                        output_result.git_commit_hash if output_result is not None else None,
                        output_result.git_commit_abbrev if output_result is not None else None,
                        output_result.status.value if output_result is not None else "unknown",
                        str(event_row["provenance_status"]),
                        int(candidate.redacted),
                        int(candidate.truncated),
                        candidate.extraction_version,
                        candidate.classifier_version,
                        now,
                    ),
                )


def _parse_for_lineage(
    adapter: IndexSourceAdapter,
    candidate: SourceSessionCandidate | None,
    cache: dict[str, ParsedSourceSession],
) -> ParsedSourceSession | None:
    if candidate is None or not candidate.rollout_allowed or not candidate.rollout_exists:
        return None
    session_id = candidate.session.source_session_id
    if session_id not in cache:
        try:
            cache[session_id] = adapter.parse_session(candidate)
        except Exception:
            return None
    return cache[session_id]


def _upsert_relationship(
    connection: sqlite3.Connection,
    relationship: NormalizedThreadRelationship,
    *,
    parent_session_id: int | None,
    child_session_id: int | None,
) -> None:
    values = (
        relationship.source_type,
        str(relationship.source_home),
        relationship.relationship_type,
        relationship.parent_source_session_id,
        relationship.child_source_session_id,
        parent_session_id,
        child_session_id,
        relationship.source_status,
        str(relationship.source_db_path) if relationship.source_db_path else None,
    )
    existing = connection.execute(
        """
        SELECT parent_session_id, child_session_id, source_status, source_db_path
        FROM thread_relationships
        WHERE source_type = ? AND source_home = ? AND relationship_type = ?
          AND parent_source_session_id = ? AND child_source_session_id = ?
        """,
        values[:5],
    ).fetchone()
    comparison = values[5:]
    if existing is not None and tuple(existing) == comparison:
        return
    now = _utc_now()
    connection.execute(
        """
        INSERT INTO thread_relationships(
            source_type, source_home, relationship_type,
            parent_source_session_id, child_source_session_id,
            parent_session_id, child_session_id, source_status, source_db_path, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(
            source_type, source_home, relationship_type,
            parent_source_session_id, child_source_session_id
        ) DO UPDATE SET
            parent_session_id = excluded.parent_session_id,
            child_session_id = excluded.child_session_id,
            source_status = excluded.source_status,
            source_db_path = excluded.source_db_path,
            last_seen_at = excluded.last_seen_at
        """,
        (*values, now),
    )


def _delete_stale_relationships(
    connection: sqlite3.Connection,
    *,
    source_type: str,
    source_home: str,
    current_keys: set[tuple[str, str]],
) -> None:
    rows = connection.execute(
        "SELECT id, parent_source_session_id, child_source_session_id "
        "FROM thread_relationships WHERE source_type = ? AND source_home = ?",
        (source_type, source_home),
    ).fetchall()
    stale = [
        int(row["id"])
        for row in rows
        if (str(row["parent_source_session_id"]), str(row["child_source_session_id"]))
        not in current_keys
    ]
    connection.executemany(
        "DELETE FROM thread_relationships WHERE id = ?",
        ((identifier,) for identifier in stale),
    )


def _upsert_token_lineage(
    connection: sqlite3.Connection,
    parent_session_id: int,
    child_session_id: int,
    assessment: TokenLineageAssessment,
) -> None:
    baseline = assessment.inherited_baseline or UsageVector()
    incremental = assessment.incremental_usage or UsageVector()
    values: tuple[Any, ...] = (
        parent_session_id,
        assessment.status.value,
        assessment.confidence.value,
        assessment.evidence_type,
        assessment.matched_snapshot_count,
        assessment.parent_sequence_start,
        baseline.input_tokens,
        baseline.cached_input_tokens,
        baseline.cache_write_input_tokens,
        baseline.output_tokens,
        baseline.reasoning_output_tokens,
        baseline.total_tokens,
        incremental.input_tokens,
        incremental.cached_input_tokens,
        incremental.cache_write_input_tokens,
        incremental.output_tokens,
        incremental.reasoning_output_tokens,
        incremental.total_tokens,
        assessment.delta_consistency.value,
        LINEAGE_ALGORITHM_VERSION,
    )
    existing = connection.execute(
        "SELECT parent_session_id, deduplication_status, confidence, evidence_type, "
        "matched_snapshot_count, parent_sequence_start, baseline_input_tokens, "
        "baseline_cached_input_tokens, baseline_cache_write_input_tokens, "
        "baseline_output_tokens, baseline_reasoning_output_tokens, baseline_total_tokens, "
        "incremental_input_tokens, incremental_cached_input_tokens, "
        "incremental_cache_write_input_tokens, incremental_output_tokens, "
        "incremental_reasoning_output_tokens, incremental_total_tokens, delta_consistency, "
        "algorithm_version FROM token_lineage WHERE child_session_id = ?",
        (child_session_id,),
    ).fetchone()
    if existing is not None and tuple(existing) == values:
        return
    connection.execute(
        """
        INSERT INTO token_lineage(
            child_session_id, parent_session_id, deduplication_status, confidence,
            evidence_type, matched_snapshot_count, parent_sequence_start,
            baseline_input_tokens, baseline_cached_input_tokens,
            baseline_cache_write_input_tokens, baseline_output_tokens,
            baseline_reasoning_output_tokens, baseline_total_tokens,
            incremental_input_tokens, incremental_cached_input_tokens,
            incremental_cache_write_input_tokens, incremental_output_tokens,
            incremental_reasoning_output_tokens, incremental_total_tokens,
            delta_consistency, algorithm_version, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(child_session_id) DO UPDATE SET
            parent_session_id = excluded.parent_session_id,
            deduplication_status = excluded.deduplication_status,
            confidence = excluded.confidence,
            evidence_type = excluded.evidence_type,
            matched_snapshot_count = excluded.matched_snapshot_count,
            parent_sequence_start = excluded.parent_sequence_start,
            baseline_input_tokens = excluded.baseline_input_tokens,
            baseline_cached_input_tokens = excluded.baseline_cached_input_tokens,
            baseline_cache_write_input_tokens = excluded.baseline_cache_write_input_tokens,
            baseline_output_tokens = excluded.baseline_output_tokens,
            baseline_reasoning_output_tokens = excluded.baseline_reasoning_output_tokens,
            baseline_total_tokens = excluded.baseline_total_tokens,
            incremental_input_tokens = excluded.incremental_input_tokens,
            incremental_cached_input_tokens = excluded.incremental_cached_input_tokens,
            incremental_cache_write_input_tokens = excluded.incremental_cache_write_input_tokens,
            incremental_output_tokens = excluded.incremental_output_tokens,
            incremental_reasoning_output_tokens = excluded.incremental_reasoning_output_tokens,
            incremental_total_tokens = excluded.incremental_total_tokens,
            delta_consistency = excluded.delta_consistency,
            algorithm_version = excluded.algorithm_version,
            updated_at = excluded.updated_at
        """,
        (child_session_id, *values, _utc_now()),
    )


def _reconcile_event_provenance(
    connection: sqlite3.Connection,
    *,
    internal_ids: dict[str, int],
    relationships: tuple[NormalizedThreadRelationship, ...],
    duplicate_children: set[str],
    cycle_source_ids: frozenset[str],
) -> None:
    """Resolve event origins from explicit topology and exact ordered fingerprints."""

    events_by_session = {
        session_id: _event_rows(connection, session_id)
        for session_id in internal_ids.values()
    }
    child_source_ids = {item.child_source_session_id for item in relationships}
    processed: set[int] = set()
    with connection:
        for source_id, session_id in sorted(internal_ids.items()):
            rows = events_by_session[session_id]
            if source_id in child_source_ids:
                continue
            _mark_root_events(connection, session_id, rows)
            processed.add(session_id)

    pending = [
        item
        for item in relationships
        if item.child_source_session_id not in duplicate_children
        and item.parent_source_session_id in internal_ids
        and item.child_source_session_id in internal_ids
    ]
    while pending:
        progressed = False
        remaining: list[NormalizedThreadRelationship] = []
        for relationship in pending:
            parent_id = internal_ids[relationship.parent_source_session_id]
            child_id = internal_ids[relationship.child_source_session_id]
            if (
                relationship.parent_source_session_id not in cycle_source_ids
                and relationship.child_source_session_id not in cycle_source_ids
                and parent_id not in processed
            ):
                remaining.append(relationship)
                continue
            _reconcile_event_edge(
                connection,
                relationship=relationship,
                parent_session_id=parent_id,
                child_session_id=child_id,
                parent_rows=_event_rows(connection, parent_id),
                child_rows=_event_rows(connection, child_id),
                cyclic=(
                    relationship.parent_source_session_id in cycle_source_ids
                    or relationship.child_source_session_id in cycle_source_ids
                ),
            )
            events_by_session[child_id] = _event_rows(connection, child_id)
            processed.add(child_id)
            progressed = True
        if not progressed:
            for relationship in remaining:
                child_id = internal_ids[relationship.child_source_session_id]
                with connection:
                    _mark_events_unknown(
                        connection,
                        events_by_session[child_id],
                        evidence="unresolved_parent_order",
                    )
            break
        pending = remaining
    unresolved_children = {
        internal_ids[source_id]
        for source_id in child_source_ids
        if source_id in internal_ids and internal_ids[source_id] not in processed
    }
    with connection:
        for child_id in sorted(unresolved_children):
            _mark_events_unknown(
                connection,
                _event_rows(connection, child_id),
                evidence="unresolved_explicit_parent",
            )


def _reconcile_event_edge(
    connection: sqlite3.Connection,
    *,
    relationship: NormalizedThreadRelationship,
    parent_session_id: int,
    child_session_id: int,
    parent_rows: tuple[sqlite3.Row, ...],
    child_rows: tuple[sqlite3.Row, ...],
    cyclic: bool,
) -> None:
    parent = tuple(_event_observation(row) for row in parent_rows)
    child = tuple(_event_observation(row) for row in child_rows)
    assessment = assess_event_provenance(parent, child, cyclic=cyclic)
    mirrored = mirrored_user_observations(child)
    with connection:
        for decision in assessment.decisions:
            if decision.child_index in mirrored:
                continue
            child_row = child_rows[decision.child_index]
            status = decision.status
            origin_session_id: int | None = None
            origin_event_id: int | None = None
            evidence = decision.evidence_type
            confidence = decision.confidence
            if decision.parent_index is not None:
                parent_row = parent_rows[decision.parent_index]
                parent_status = EventProvenanceStatus(str(parent_row["provenance_status"]))
                if parent_status in {
                    EventProvenanceStatus.ORIGIN,
                    EventProvenanceStatus.INHERITED_EXACT,
                    EventProvenanceStatus.INHERITED_PREFIX,
                    EventProvenanceStatus.OBSERVED_DUPLICATE,
                }:
                    origin_session_id = int(
                        parent_row["origin_session_id"] or parent_session_id
                    )
                    origin_event_id = int(parent_row["origin_event_id"] or parent_row["id"])
                else:
                    status = EventProvenanceStatus.AMBIGUOUS
                    evidence = "matched_parent_event_has_ambiguous_origin"
                    confidence = "none"
            elif status is EventProvenanceStatus.ORIGIN:
                origin_session_id = child_session_id
                origin_event_id = int(child_row["id"])
            _update_event_provenance(
                connection,
                child_row,
                status=status,
                origin_session_id=origin_session_id,
                origin_event_id=origin_event_id,
                parent_session_id=parent_session_id,
                evidence_type=evidence,
                confidence=confidence,
            )
        _apply_mirrored_user_events(connection, child_session_id, child_rows)
        relationship_id = _relationship_id(connection, relationship)
        if relationship_id is not None:
            _replace_event_replay_summary(connection, relationship_id, assessment.families)


def _event_rows(
    connection: sqlite3.Connection,
    session_id: int,
) -> tuple[sqlite3.Row, ...]:
    return tuple(
        connection.execute(
            "SELECT * FROM event_observations WHERE observed_session_id = ? "
            "ORDER BY source_ordinal",
            (session_id,),
        )
    )


def _event_observation(row: sqlite3.Row) -> NormalizedEventObservation:
    return NormalizedEventObservation(
        source_ordinal=int(row["source_ordinal"]),
        family_ordinal=int(row["family_ordinal"]),
        family=EventFamily(str(row["event_family"])),
        fingerprint=str(row["fingerprint"]),
        source_record_type=str(row["source_record_type"]),
        source_payload_type=str(row["source_payload_type"]),
        occurred_at=_stored_datetime(row["occurred_at"]),
        stable_id_digest=(
            str(row["stable_id_digest"]) if row["stable_id_digest"] is not None else None
        ),
        approximate_content_length=(
            int(row["approximate_content_length"])
            if row["approximate_content_length"] is not None
            else None
        ),
        fingerprint_version=str(row["fingerprint_version"]),
    )


def _mark_root_events(
    connection: sqlite3.Connection,
    session_id: int,
    rows: tuple[sqlite3.Row, ...],
) -> None:
    observations = tuple(_event_observation(row) for row in rows)
    mirrored = mirrored_user_observations(observations)
    for index, row in enumerate(rows):
        if index in mirrored:
            continue
        _update_event_provenance(
            connection,
            row,
            status=EventProvenanceStatus.ORIGIN,
            origin_session_id=session_id,
            origin_event_id=int(row["id"]),
            parent_session_id=None,
            evidence_type="no_explicit_parent",
            confidence="high",
        )
    _apply_mirrored_user_events(connection, session_id, rows)


def _mark_events_unknown(
    connection: sqlite3.Connection,
    rows: tuple[sqlite3.Row, ...],
    *,
    evidence: str,
) -> None:
    for row in rows:
        _update_event_provenance(
            connection,
            row,
            status=EventProvenanceStatus.UNKNOWN,
            origin_session_id=None,
            origin_event_id=None,
            parent_session_id=None,
            evidence_type=evidence,
            confidence="none",
        )


def _apply_mirrored_user_events(
    connection: sqlite3.Connection,
    session_id: int,
    rows: tuple[sqlite3.Row, ...],
) -> None:
    observations = tuple(_event_observation(row) for row in rows)
    for duplicate_index, canonical_index in mirrored_user_observations(observations).items():
        duplicate = connection.execute(
            "SELECT * FROM event_observations WHERE id = ?",
            (int(rows[duplicate_index]["id"]),),
        ).fetchone()
        canonical = connection.execute(
            "SELECT * FROM event_observations WHERE id = ?",
            (int(rows[canonical_index]["id"]),),
        ).fetchone()
        if duplicate is None or canonical is None:
            continue
        origin_session_id = (
            int(canonical["origin_session_id"])
            if canonical["origin_session_id"] is not None
            else None
        )
        origin_event_id = (
            int(canonical["origin_event_id"])
            if canonical["origin_event_id"] is not None
            else int(canonical["id"])
        )
        _update_event_provenance(
            connection,
            duplicate,
            status=EventProvenanceStatus.OBSERVED_DUPLICATE,
            origin_session_id=origin_session_id,
            origin_event_id=origin_event_id,
            parent_session_id=(
                int(canonical["parent_session_id"])
                if canonical["parent_session_id"] is not None
                else None
            ),
            evidence_type="adjacent_mirrored_user_record",
            confidence="high",
        )


def _update_event_provenance(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    status: EventProvenanceStatus,
    origin_session_id: int | None,
    origin_event_id: int | None,
    parent_session_id: int | None,
    evidence_type: str,
    confidence: str,
) -> None:
    values = (
        status.value,
        origin_session_id,
        origin_event_id,
        parent_session_id,
        evidence_type,
        confidence,
        PROVENANCE_ALGORITHM_VERSION,
    )
    existing = (
        row["provenance_status"],
        row["origin_session_id"],
        row["origin_event_id"],
        row["parent_session_id"],
        row["evidence_type"],
        row["confidence"],
        row["provenance_algorithm_version"],
    )
    if existing == values:
        return
    connection.execute(
        """
        UPDATE event_observations
        SET provenance_status = ?, origin_session_id = ?, origin_event_id = ?,
            parent_session_id = ?, evidence_type = ?, confidence = ?,
            provenance_algorithm_version = ?, updated_at = ?
        WHERE id = ?
        """,
        (*values, _utc_now(), int(row["id"])),
    )


def _relationship_id(
    connection: sqlite3.Connection,
    relationship: NormalizedThreadRelationship,
) -> int | None:
    row = connection.execute(
        """
        SELECT id FROM thread_relationships
        WHERE source_type = ? AND source_home = ? AND relationship_type = ?
          AND parent_source_session_id = ? AND child_source_session_id = ?
        """,
        (
            relationship.source_type,
            str(relationship.source_home),
            relationship.relationship_type,
            relationship.parent_source_session_id,
            relationship.child_source_session_id,
        ),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def _replace_event_replay_summary(
    connection: sqlite3.Connection,
    relationship_id: int,
    families: tuple[EventFamilyAssessment, ...],
) -> None:
    current = {item.family.value for item in families}
    if current:
        placeholders = ", ".join("?" for _ in current)
        connection.execute(
            f"DELETE FROM event_replay_summary WHERE relationship_id = ? "
            f"AND event_family NOT IN ({placeholders})",
            (relationship_id, *sorted(current)),
        )
    else:
        connection.execute(
            "DELETE FROM event_replay_summary WHERE relationship_id = ?",
            (relationship_id,),
        )
    for item in families:
        values = (
            item.observed_child_events,
            item.originated_events,
            item.inherited_events,
            item.ambiguous_events,
            item.unknown_events,
            item.status.value,
            item.evidence_type,
            PROVENANCE_ALGORITHM_VERSION,
        )
        existing = connection.execute(
            """
            SELECT observed_child_events, originated_events, inherited_events,
                   ambiguous_events, unknown_events, provenance_status,
                   evidence_type, algorithm_version
            FROM event_replay_summary
            WHERE relationship_id = ? AND event_family = ?
            """,
            (relationship_id, item.family.value),
        ).fetchone()
        if existing is not None and tuple(existing) == values:
            continue
        connection.execute(
            """
            INSERT INTO event_replay_summary(
                relationship_id, event_family, observed_child_events,
                originated_events, inherited_events, ambiguous_events,
                unknown_events, provenance_status, evidence_type,
                algorithm_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(relationship_id, event_family) DO UPDATE SET
                observed_child_events = excluded.observed_child_events,
                originated_events = excluded.originated_events,
                inherited_events = excluded.inherited_events,
                ambiguous_events = excluded.ambiguous_events,
                unknown_events = excluded.unknown_events,
                provenance_status = excluded.provenance_status,
                evidence_type = excluded.evidence_type,
                algorithm_version = excluded.algorithm_version,
                updated_at = excluded.updated_at
            """,
            (relationship_id, item.family.value, *values, _utc_now()),
        )


def _reconcile_prompts(
    connection: sqlite3.Connection,
    *,
    adapter: IndexSourceAdapter,
    codex_home: Path,
    parsed_sessions: dict[str, ParsedSourceSession],
) -> None:
    """Persist redacted logical prompts from confidently originated user events."""

    source_home = str(codex_home.expanduser().resolve(strict=False))
    session_rows = {
        str(row["source_session_id"]): row
        for row in connection.execute(
            "SELECT id, source_session_id, client_source FROM source_sessions "
            "WHERE source_type = ? AND source_home = ?",
            (adapter.name, source_home),
        )
    }
    with connection:
        for source_session_id, parsed in sorted(parsed_sessions.items()):
            session_row = session_rows.get(source_session_id)
            if session_row is None:
                continue
            session_id = int(session_row["id"])
            allowed, authorship_evidence = _user_prompt_source(session_row["client_source"])
            event_rows = {
                int(row["source_ordinal"]): row
                for row in _event_rows(connection, session_id)
            }
            desired_ids: set[str] = set()
            prompt_ordinal = 0
            if allowed:
                for candidate in sorted(
                    parsed.prompt_candidates,
                    key=lambda item: item.source_ordinal,
                ):
                    event_row = event_rows.get(candidate.source_ordinal)
                    if not _is_origin_prompt_event(event_row, session_id):
                        continue
                    assert event_row is not None
                    prompt_id = _stable_prompt_id(
                        source_type=adapter.name,
                        source_home=source_home,
                        source_session_id=source_session_id,
                        source_ordinal=candidate.source_ordinal,
                        fingerprint=candidate.fingerprint,
                    )
                    safe = redact_prompt(candidate.text)
                    _upsert_prompt(
                        connection,
                        prompt_id=prompt_id,
                        origin_session_id=session_id,
                        origin_event_id=int(event_row["id"]),
                        source_ordinal=candidate.source_ordinal,
                        prompt_ordinal=prompt_ordinal,
                        occurred_at=_format_datetime(candidate.occurred_at),
                        text=safe.text,
                        redaction_status=safe.status,
                        redaction_count=safe.redaction_count,
                        original_character_count=safe.original_character_count,
                        stored_character_count=safe.stored_character_count,
                        authorship_evidence=authorship_evidence,
                        fingerprint=candidate.fingerprint,
                    )
                    desired_ids.add(prompt_id)
                    prompt_ordinal += 1
            _delete_stale_prompts(connection, session_id, desired_ids)
        _sync_prompt_observations(connection)


def _is_origin_prompt_event(row: sqlite3.Row | None, session_id: int) -> bool:
    return bool(
        row is not None
        and row["event_family"] == EventFamily.USER_MESSAGE.value
        and row["provenance_status"] == EventProvenanceStatus.ORIGIN.value
        and row["origin_session_id"] == session_id
        and row["origin_event_id"] == row["id"]
    )


def _user_prompt_source(value: object) -> tuple[bool, str]:
    if not isinstance(value, str) or not value.strip():
        return False, "source_authorship_unknown"
    source = value.strip()
    lowered = source.lower()
    if "guardian" in lowered:
        return False, "source_identifies_guardian"
    if "subagent" in lowered or "thread_spawn" in lowered:
        return False, "source_identifies_subagent"
    if source.startswith("{"):
        try:
            decoded = json.loads(source)
        except json.JSONDecodeError:
            return False, "structured_source_unrecognized"
        if isinstance(decoded, dict):
            return False, "structured_source_not_user_authored"
    return True, "interactive_client_source"


def _stable_prompt_id(
    *,
    source_type: str,
    source_home: str,
    source_session_id: str,
    source_ordinal: int,
    fingerprint: str,
) -> str:
    identity = "\0".join(
        (
            PROMPT_CONTENT_SCHEMA_VERSION,
            source_type,
            source_home,
            source_session_id,
            str(source_ordinal),
            fingerprint,
        )
    )
    return f"prm_{hashlib.sha256(identity.encode()).hexdigest()}"


def _upsert_prompt(
    connection: sqlite3.Connection,
    *,
    prompt_id: str,
    origin_session_id: int,
    origin_event_id: int,
    source_ordinal: int,
    prompt_ordinal: int,
    occurred_at: str | None,
    text: str,
    redaction_status: str,
    redaction_count: int,
    original_character_count: int,
    stored_character_count: int,
    authorship_evidence: str,
    fingerprint: str,
) -> None:
    values: tuple[object, ...] = (
        origin_session_id,
        origin_event_id,
        source_ordinal,
        prompt_ordinal,
        occurred_at,
        text,
        redaction_status,
        redaction_count,
        original_character_count,
        stored_character_count,
        EventProvenanceStatus.ORIGIN.value,
        "high",
        authorship_evidence,
        fingerprint,
        PROMPT_CONTENT_SCHEMA_VERSION,
    )
    existing = connection.execute(
        """
        SELECT origin_session_id, origin_event_id, source_ordinal, prompt_ordinal,
               occurred_at, text, redaction_status, redaction_count,
               original_character_count, stored_character_count, provenance_status,
               provenance_confidence, user_authorship_evidence, fingerprint,
               content_schema_version
        FROM prompts WHERE prompt_id = ?
        """,
        (prompt_id,),
    ).fetchone()
    if existing is not None and tuple(existing) == values:
        return
    now = _utc_now()
    connection.execute(
        """
        INSERT INTO prompts(
            prompt_id, origin_session_id, origin_event_id, source_ordinal,
            prompt_ordinal, occurred_at, text, redaction_status, redaction_count,
            original_character_count, stored_character_count, provenance_status,
            provenance_confidence, user_authorship_evidence, fingerprint,
            content_schema_version, first_indexed_at, last_indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(prompt_id) DO UPDATE SET
            origin_session_id = excluded.origin_session_id,
            origin_event_id = excluded.origin_event_id,
            source_ordinal = excluded.source_ordinal,
            prompt_ordinal = excluded.prompt_ordinal,
            occurred_at = excluded.occurred_at,
            text = excluded.text,
            redaction_status = excluded.redaction_status,
            redaction_count = excluded.redaction_count,
            original_character_count = excluded.original_character_count,
            stored_character_count = excluded.stored_character_count,
            provenance_status = excluded.provenance_status,
            provenance_confidence = excluded.provenance_confidence,
            user_authorship_evidence = excluded.user_authorship_evidence,
            fingerprint = excluded.fingerprint,
            content_schema_version = excluded.content_schema_version,
            last_indexed_at = excluded.last_indexed_at
        """,
        (prompt_id, *values, now, now),
    )


def _delete_stale_prompts(
    connection: sqlite3.Connection,
    origin_session_id: int,
    desired_ids: set[str],
) -> None:
    rows = connection.execute(
        "SELECT id, prompt_id FROM prompts WHERE origin_session_id = ?",
        (origin_session_id,),
    ).fetchall()
    stale = [int(row["id"]) for row in rows if str(row["prompt_id"]) not in desired_ids]
    connection.executemany("DELETE FROM prompts WHERE id = ?", ((item,) for item in stale))


def _sync_prompt_observations(connection: sqlite3.Connection) -> None:
    for prompt in connection.execute("SELECT id, origin_event_id FROM prompts ORDER BY id"):
        prompt_internal_id = int(prompt["id"])
        origin_event_id = prompt["origin_event_id"]
        desired: dict[int, sqlite3.Row] = {}
        if origin_event_id is not None:
            desired = {
                int(row["id"]): row
                for row in connection.execute(
                    """
                    SELECT id, observed_session_id, source_ordinal, provenance_status, occurred_at
                    FROM event_observations
                    WHERE origin_event_id = ? AND event_family = 'user_message'
                      AND provenance_status IN (
                          'origin', 'inherited_exact', 'inherited_prefix'
                      )
                    """,
                    (origin_event_id,),
                )
            }
        existing = {
            int(row["event_observation_id"])
            for row in connection.execute(
                "SELECT event_observation_id FROM prompt_observations WHERE prompt_id = ?",
                (prompt_internal_id,),
            )
        }
        connection.executemany(
            "DELETE FROM prompt_observations WHERE prompt_id = ? AND event_observation_id = ?",
            (
                (prompt_internal_id, event_id)
                for event_id in sorted(existing - desired.keys())
            ),
        )
        now = _utc_now()
        connection.executemany(
            """
            INSERT INTO prompt_observations(
                prompt_id, event_observation_id, observed_session_id, source_ordinal,
                provenance_status, first_observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    prompt_internal_id,
                    event_id,
                    int(row["observed_session_id"]),
                    int(row["source_ordinal"]),
                    str(row["provenance_status"]),
                    str(row["occurred_at"] or now),
                )
                for event_id, row in sorted(desired.items())
                if event_id not in existing
            ),
        )


def _start_run(
    connection: sqlite3.Connection,
    *,
    codex_home: Path,
    started_at: str,
) -> int:
    cursor = connection.execute(
        "INSERT INTO index_runs(source_home, started_at, status) VALUES (?, ?, 'running')",
        (str(codex_home.expanduser().resolve(strict=False)), started_at),
    )
    connection.commit()
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return an index run identifier")
    return int(cursor.lastrowid)


def _finish_run(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    status: str,
    discovered: int,
    counts: dict[str, int],
) -> None:
    connection.execute(
        """
        UPDATE index_runs
        SET completed_at = ?, status = ?, discovered_count = ?, new_count = ?,
            updated_count = ?, unchanged_count = ?, skipped_count = ?, failed_count = ?
        WHERE id = ?
        """,
        (
            _utc_now(),
            status,
            discovered,
            counts["new"],
            counts["updated"],
            counts["unchanged"],
            counts["skipped"],
            counts["failed"],
            run_id,
        ),
    )
    connection.commit()


def _existing_session(
    connection: sqlite3.Connection,
    session: NormalizedSourceSession,
) -> sqlite3.Row | None:
    row = connection.execute(
        """
        SELECT * FROM source_sessions
        WHERE source_type = ? AND source_home = ? AND source_session_id = ?
        """,
        (session.source_type, str(session.source_home), session.source_session_id),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def _upsert_session_metadata(
    connection: sqlite3.Connection,
    session: NormalizedSourceSession,
) -> tuple[int, bool, bool]:
    existing = _existing_session(connection, session)
    values = _session_values(session)
    now = _utc_now()
    if existing is None:
        cursor = connection.execute(
            """
            INSERT INTO source_sessions(
                source_session_id, source_type, source_home, client_source, started_at, updated_at,
                apparent_ended_at, source_timezone_offset_minutes, cwd, repository_root,
                repository_name, git_branch, git_sha, git_origin_url, model, model_provider,
                codex_version, archived, rollout_path, source_db_path, source_path,
                first_ingested_at, last_ingested_at
            ) VALUES (
                :source_session_id, :source_type, :source_home, :client_source, :started_at,
                :updated_at,
                :apparent_ended_at, :source_timezone_offset_minutes, :cwd, :repository_root,
                :repository_name, :git_branch, :git_sha, :git_origin_url, :model,
                :model_provider, :codex_version, :archived, :rollout_path, :source_db_path,
                :source_path, :first_ingested_at, :last_ingested_at
            )
            """,
            {**values, "first_ingested_at": now, "last_ingested_at": now},
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a session identifier")
        return int(cursor.lastrowid), True, True

    changed = any(existing[column] != value for column, value in values.items())
    session_id = int(existing["id"])
    if changed:
        connection.execute(
            """
            UPDATE source_sessions
            SET client_source = :client_source, started_at = :started_at,
                updated_at = :updated_at,
                apparent_ended_at = :apparent_ended_at,
                source_timezone_offset_minutes = :source_timezone_offset_minutes,
                cwd = :cwd, repository_root = :repository_root,
                repository_name = :repository_name, git_branch = :git_branch,
                git_sha = :git_sha, git_origin_url = :git_origin_url, model = :model,
                model_provider = :model_provider, codex_version = :codex_version,
                archived = :archived, rollout_path = :rollout_path,
                source_db_path = :source_db_path, source_path = :source_path,
                last_ingested_at = :last_ingested_at
            WHERE id = :id
            """,
            {**values, "last_ingested_at": now, "id": session_id},
        )
    return session_id, False, changed


def _session_values(session: NormalizedSourceSession) -> dict[str, object]:
    return {
        "source_session_id": session.source_session_id,
        "source_type": session.source_type,
        "source_home": str(session.source_home),
        "client_source": session.client_source,
        "started_at": _format_datetime(session.started_at),
        "updated_at": _format_datetime(session.updated_at),
        "apparent_ended_at": _format_datetime(session.apparent_ended_at),
        "source_timezone_offset_minutes": session.source_timezone_offset_minutes,
        "cwd": _format_path(session.cwd),
        "repository_root": _format_path(session.repository_root),
        "repository_name": session.repository_name,
        "git_branch": session.git_branch,
        "git_sha": session.git_sha,
        "git_origin_url": session.git_origin_url,
        "model": session.model,
        "model_provider": session.model_provider,
        "codex_version": session.codex_version,
        "archived": int(session.archived),
        "rollout_path": _format_path(session.rollout_path),
        "source_db_path": _format_path(session.source_db_path),
        "source_path": _format_path(session.source_path),
    }


def _replace_usage(
    connection: sqlite3.Connection,
    session_id: int,
    usage: NormalizedUsage,
) -> None:
    connection.execute(
        """
        INSERT INTO usage(
            source_session_id, usage_semantics, input_tokens, cached_input_tokens,
            cache_write_input_tokens, output_tokens, reasoning_output_tokens, total_tokens,
            token_update_count, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_session_id) DO UPDATE SET
            usage_semantics = excluded.usage_semantics,
            input_tokens = excluded.input_tokens,
            cached_input_tokens = excluded.cached_input_tokens,
            cache_write_input_tokens = excluded.cache_write_input_tokens,
            output_tokens = excluded.output_tokens,
            reasoning_output_tokens = excluded.reasoning_output_tokens,
            total_tokens = excluded.total_tokens,
            token_update_count = excluded.token_update_count,
            updated_at = excluded.updated_at
        """,
        (
            session_id,
            usage.semantics.value,
            usage.input_tokens,
            usage.cached_input_tokens,
            usage.cache_write_input_tokens,
            usage.output_tokens,
            usage.reasoning_output_tokens,
            usage.total_tokens,
            usage.token_update_count,
            _utc_now(),
        ),
    )


def _replace_event_summary(
    connection: sqlite3.Connection,
    session_id: int,
    session: NormalizedSourceSession,
) -> None:
    connection.execute(
        "DELETE FROM event_summary WHERE source_session_id = ?",
        (session_id,),
    )
    now = _utc_now()
    connection.executemany(
        """
        INSERT INTO event_summary(source_session_id, category, event_count, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        ((session_id, item.category.value, item.count, now) for item in session.event_counts),
    )


def _replace_event_observations(
    connection: sqlite3.Connection,
    session_id: int,
    parsed: ParsedSourceSession,
) -> None:
    connection.execute(
        "DELETE FROM event_observations WHERE observed_session_id = ?",
        (session_id,),
    )
    now = _utc_now()
    connection.executemany(
        """
        INSERT INTO event_observations(
            observed_session_id, source_ordinal, family_ordinal, event_family,
            source_record_type, source_payload_type, fingerprint, stable_id_digest,
            occurred_at, approximate_content_length, provenance_status,
            origin_session_id, origin_event_id, parent_session_id, evidence_type,
            confidence, fingerprint_version, provenance_algorithm_version, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', NULL, NULL, NULL,
                  'awaiting_lineage_reconciliation', 'none', ?, ?, ?)
        """,
        (
            (
                session_id,
                event.source_ordinal,
                event.family_ordinal,
                event.family.value,
                event.source_record_type,
                event.source_payload_type,
                event.fingerprint,
                event.stable_id_digest,
                _format_datetime(event.occurred_at),
                event.approximate_content_length,
                event.fingerprint_version,
                PROVENANCE_ALGORITHM_VERSION,
                now,
            )
            for event in parsed.event_observations
        ),
    )


def _ingestion_state(
    connection: sqlite3.Connection,
    candidate: SourceSessionCandidate,
) -> sqlite3.Row | None:
    row = connection.execute(
        "SELECT * FROM ingestion_state WHERE source_home = ? AND source_path = ?",
        (str(candidate.session.source_home), _ingestion_source_path(candidate)),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def _source_unchanged(
    state: sqlite3.Row | None,
    candidate: SourceSessionCandidate,
    *,
    parser_version: str,
) -> bool:
    return bool(
        state is not None
        and state["size_bytes"] == candidate.size_bytes
        and state["mtime_ns"] == candidate.mtime_ns
        and state["parser_version"] == parser_version
        and state["source_schema_version"] == candidate.source_schema_version
        and str(state["status"]).startswith("indexed")
    )


def _state_matches_status(
    state: sqlite3.Row | None,
    candidate: SourceSessionCandidate,
    *,
    parser_version: str,
    status: str,
) -> bool:
    return bool(
        state is not None
        and state["size_bytes"] == candidate.size_bytes
        and state["mtime_ns"] == candidate.mtime_ns
        and state["parser_version"] == parser_version
        and state["source_schema_version"] == candidate.source_schema_version
        and state["status"] == status
        and state["error"] is None
    )


def _upsert_ingestion_state(
    connection: sqlite3.Connection,
    candidate: SourceSessionCandidate,
    *,
    parser_version: str,
    status: str,
    error: str | None,
    parsed_byte_offset: int | None,
) -> None:
    connection.execute(
        """
        INSERT INTO ingestion_state(
            source_home, source_path, source_session_id, source_kind, size_bytes, mtime_ns,
            last_parsed_byte_offset, parser_version, source_schema_version, status, error,
            indexed_at
        ) VALUES (?, ?, ?, 'rollout_jsonl', ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_home, source_path) DO UPDATE SET
            source_session_id = excluded.source_session_id,
            source_kind = excluded.source_kind,
            size_bytes = excluded.size_bytes,
            mtime_ns = excluded.mtime_ns,
            last_parsed_byte_offset = excluded.last_parsed_byte_offset,
            parser_version = excluded.parser_version,
            source_schema_version = excluded.source_schema_version,
            status = excluded.status,
            error = excluded.error,
            indexed_at = excluded.indexed_at
        """,
        (
            str(candidate.session.source_home),
            _ingestion_source_path(candidate),
            candidate.session.source_session_id,
            candidate.size_bytes,
            candidate.mtime_ns,
            parsed_byte_offset,
            parser_version,
            candidate.source_schema_version,
            status,
            error,
            _utc_now(),
        ),
    )


def _ingestion_source_path(candidate: SourceSessionCandidate) -> str:
    path = candidate.session.source_path
    if path is not None:
        return str(path)
    return f"catalogue:{candidate.session.source_session_id}"


def _parse_warning(parsed: ParsedSourceSession) -> str | None:
    if not parsed.malformed_line_count and not parsed.oversized_line_count:
        return None
    return (
        f"malformed_lines={parsed.malformed_line_count};"
        f"oversized_lines={parsed.oversized_line_count}"
    )


def _preserve_rollout_metadata(
    session: NormalizedSourceSession,
    existing: sqlite3.Row | None,
) -> NormalizedSourceSession:
    if existing is None:
        return session
    existing_end = _stored_datetime(existing["apparent_ended_at"])
    return replace(
        session,
        started_at=session.started_at or _stored_datetime(existing["started_at"]),
        apparent_ended_at=_latest_datetime(session.apparent_ended_at, existing_end),
        source_timezone_offset_minutes=(
            session.source_timezone_offset_minutes
            if session.source_timezone_offset_minutes is not None
            else existing["source_timezone_offset_minutes"]
        ),
        cwd=session.cwd or _stored_path(existing["cwd"]),
        repository_root=(session.repository_root or _stored_path(existing["repository_root"])),
        repository_name=session.repository_name or existing["repository_name"],
        model=session.model or existing["model"],
        model_provider=session.model_provider or existing["model_provider"],
        codex_version=session.codex_version or existing["codex_version"],
    )


def _stored_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _latest_datetime(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _stored_path(value: object) -> Path | None:
    return Path(value) if isinstance(value, str) and value else None


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _format_path(value: Path | None) -> str | None:
    return str(value) if value is not None else None


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
