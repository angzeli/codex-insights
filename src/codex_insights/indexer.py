"""Incremental indexing from normalized source-adapter output."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from codex_insights.db import open_index
from codex_insights.models import (
    NormalizedSourceSession,
    NormalizedUsage,
    ParsedSourceSession,
    SourceSessionCandidate,
)


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
                )
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
    return replace(
        session,
        started_at=session.started_at or _stored_datetime(existing["started_at"]),
        apparent_ended_at=(
            session.apparent_ended_at or _stored_datetime(existing["apparent_ended_at"])
        ),
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
