"""Read-only, provenance-aware Git commit correlation."""

from __future__ import annotations

import re
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

GIT_CORRELATION_VERSION = "git-correlation-v1"
_COMMIT_HASH = re.compile(r"^[0-9a-f]{40,64}$")
_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


@dataclass(frozen=True, slots=True)
class GitCommitRecord:
    """Content-free Git commit metadata obtained through read-only commands."""

    commit_hash: str
    committed_at: datetime
    parent_count: int


@dataclass(frozen=True, slots=True)
class CommitAssociation:
    """One explainable candidate association before database persistence."""

    commit: GitCommitRecord
    confidence: str
    evidence_type: str
    explanation: str
    ambiguous: bool


def reconcile_git_commits(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Refresh derived Git evidence without mutating any user repository."""

    warnings: list[str] = []
    repositories = connection.execute(
        "SELECT id, canonical_root FROM repositories WHERE canonical_root IS NOT NULL"
    ).fetchall()
    for repository in repositories:
        repository_id = int(repository["id"])
        root = Path(str(repository["canonical_root"]))
        sessions = connection.execute(
            """
            SELECT id, source_session_id, started_at, updated_at, apparent_ended_at,
                   git_branch, git_sha
            FROM source_sessions
            WHERE repository_id = ?
            ORDER BY started_at, source_session_id
            """,
            (repository_id,),
        ).fetchall()
        bounds = _repository_bounds(sessions)
        if not root.is_dir() or bounds is None:
            continue
        commits, warning = _read_git_commits(root, *bounds)
        if warning is not None:
            warnings.append(warning)
            continue
        current_branch = _current_branch(root)
        commit_ids = _upsert_commits(connection, repository_id, commits)
        desired: set[tuple[int, int]] = set()
        for session in sessions:
            associations = _session_associations(
                connection,
                session=session,
                sessions=sessions,
                commits=commits,
                current_branch=current_branch,
            )
            with connection:
                for association in associations:
                    commit_id = commit_ids.get(association.commit.commit_hash)
                    if commit_id is None:
                        continue
                    session_id = int(session["id"])
                    desired.add((session_id, commit_id))
                    _upsert_association(
                        connection,
                        session_id=session_id,
                        commit_id=commit_id,
                        association=association,
                    )
        _delete_stale_associations(connection, repository_id, desired)
    return tuple(warnings)


def is_git_commit_command(command: str) -> bool:
    """Recognize a direct Git commit action without matching echoed text."""

    try:
        tokens = tuple(shlex.split(command, posix=True))
    except ValueError:
        tokens = tuple(command.split())
    for index in range(len(tokens) - 1):
        if tokens[index].rsplit("/", 1)[-1].casefold() == "git":
            return tokens[index + 1].casefold() == "commit"
    return False


def _session_associations(
    connection: sqlite3.Connection,
    *,
    session: sqlite3.Row,
    sessions: list[sqlite3.Row],
    commits: tuple[GitCommitRecord, ...],
    current_branch: str | None,
) -> tuple[CommitAssociation, ...]:
    session_id = int(session["id"])
    actions = [
        row
        for row in connection.execute(
            """
            SELECT occurred_at, source_ordinal, command_text, result_status,
                   result_commit_hash, result_commit_abbrev
            FROM tool_activity
            WHERE observed_session_id = ? AND origin_session_id = ?
              AND provenance_status = 'origin' AND command_category = 'git_mutation'
            ORDER BY source_ordinal, operation_ordinal
            """,
            (session_id, session_id),
        )
        if row["command_text"] is not None
        and is_git_commit_command(str(row["command_text"]))
    ]
    selected: dict[str, CommitAssociation] = {}
    for action in actions:
        exact = _exact_result_commit(action, commits)
        if exact is not None and action["result_status"] == "success":
            _prefer(
                selected,
                CommitAssociation(
                    commit=exact,
                    confidence="high",
                    evidence_type="originated_commit_result_hash",
                    explanation=(
                        "Originated successful git commit evidence exposed an exact or "
                        "uniquely resolved resulting commit hash."
                    ),
                    ambiguous=False,
                ),
            )
            continue
        action_time = _stored_datetime(action["occurred_at"]) or _stored_datetime(
            session["started_at"]
        )
        if action_time is None:
            continue
        session_end = (
            _stored_datetime(session["apparent_ended_at"])
            or _stored_datetime(session["updated_at"])
            or action_time + timedelta(minutes=30)
        )
        candidates = tuple(
            commit
            for commit in commits
            if action_time <= commit.committed_at <= session_end + timedelta(minutes=10)
            and commit.commit_hash != session["git_sha"]
        )
        for commit in candidates:
            concurrent = _has_competing_session(session_id, sessions, commit.committed_at)
            branch_matches = bool(
                session["git_branch"]
                and current_branch
                and str(session["git_branch"]) == current_branch
            )
            medium = (
                len(candidates) == 1
                and action["result_status"] == "success"
                and not concurrent
                and branch_matches
            )
            _prefer(
                selected,
                CommitAssociation(
                    commit=commit,
                    confidence="medium" if medium else "low",
                    evidence_type=(
                        "unique_compatible_commit_after_originated_action"
                        if medium
                        else "timing_candidate_after_originated_action"
                    ),
                    explanation=(
                        "One compatible commit followed an originated successful commit action "
                        "with matching current branch and no competing session."
                        if medium
                        else "Commit timing is compatible with an originated commit action, but "
                        "the available evidence is incomplete or ambiguous."
                    ),
                    ambiguous=not medium,
                ),
            )
    return tuple(
        sorted(
            selected.values(),
            key=lambda item: (item.commit.committed_at, item.commit.commit_hash),
        )
    )


def _exact_result_commit(
    action: sqlite3.Row,
    commits: tuple[GitCommitRecord, ...],
) -> GitCommitRecord | None:
    full = action["result_commit_hash"]
    if isinstance(full, str):
        return next((commit for commit in commits if commit.commit_hash == full), None)
    prefix = action["result_commit_abbrev"]
    if not isinstance(prefix, str):
        return None
    matches = tuple(commit for commit in commits if commit.commit_hash.startswith(prefix))
    return matches[0] if len(matches) == 1 else None


def _prefer(selected: dict[str, CommitAssociation], candidate: CommitAssociation) -> None:
    existing = selected.get(candidate.commit.commit_hash)
    if existing is None or _CONFIDENCE_RANK[candidate.confidence] > _CONFIDENCE_RANK[
        existing.confidence
    ]:
        selected[candidate.commit.commit_hash] = candidate


def _has_competing_session(
    session_id: int,
    sessions: list[sqlite3.Row],
    committed_at: datetime,
) -> bool:
    for other in sessions:
        if int(other["id"]) == session_id:
            continue
        start = _stored_datetime(other["started_at"])
        end = _stored_datetime(other["apparent_ended_at"]) or _stored_datetime(
            other["updated_at"]
        )
        if start is not None and start <= committed_at and (end is None or committed_at <= end):
            return True
    return False


def _repository_bounds(
    sessions: list[sqlite3.Row],
) -> tuple[datetime, datetime] | None:
    starts = [
        value
        for value in (_stored_datetime(session["started_at"]) for session in sessions)
        if value is not None
    ]
    ends = [
        value
        for value in (
            _stored_datetime(session["apparent_ended_at"])
            or _stored_datetime(session["updated_at"])
            for session in sessions
        )
        if value is not None
    ]
    if not starts:
        return None
    return min(starts) - timedelta(days=1), max(ends or starts) + timedelta(days=1)


def _read_git_commits(
    root: Path,
    since: datetime,
    until: datetime,
) -> tuple[tuple[GitCommitRecord, ...], str | None]:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "--all",
                f"--since={_format_datetime(since)}",
                f"--until={_format_datetime(until)}",
                "--format=%H%x1f%cI%x1f%P",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (), f"Read-only Git history inspection failed: {type(exc).__name__}."
    if result.returncode != 0:
        return (), "Read-only Git history inspection failed for one indexed repository."
    commits: list[GitCommitRecord] = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3 or not _COMMIT_HASH.fullmatch(parts[0]):
            continue
        timestamp = _stored_datetime(parts[1])
        if timestamp is None:
            continue
        commits.append(
            GitCommitRecord(
                commit_hash=parts[0].casefold(),
                committed_at=timestamp,
                parent_count=len(parts[2].split()) if parts[2] else 0,
            )
        )
    commits.sort(key=lambda item: (item.committed_at, item.commit_hash))
    return tuple(commits), None


def _current_branch(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _upsert_commits(
    connection: sqlite3.Connection,
    repository_id: int,
    commits: tuple[GitCommitRecord, ...],
) -> dict[str, int]:
    now = _format_datetime(datetime.now(tz=UTC))
    with connection:
        for commit in commits:
            existing = connection.execute(
                "SELECT committed_at, parent_count FROM git_commits "
                "WHERE repository_id = ? AND commit_hash = ?",
                (repository_id, commit.commit_hash),
            ).fetchone()
            values = (_format_datetime(commit.committed_at), commit.parent_count)
            if existing is not None and tuple(existing) == values:
                continue
            connection.execute(
                """
                INSERT INTO git_commits(
                    repository_id, commit_hash, committed_at, parent_count,
                    first_discovered_at, last_discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository_id, commit_hash) DO UPDATE SET
                    committed_at = excluded.committed_at,
                    parent_count = excluded.parent_count,
                    last_discovered_at = excluded.last_discovered_at
                """,
                (
                    repository_id,
                    commit.commit_hash,
                    values[0],
                    commit.parent_count,
                    now,
                    now,
                ),
            )
    return {
        str(row["commit_hash"]): int(row["id"])
        for row in connection.execute(
            "SELECT id, commit_hash FROM git_commits WHERE repository_id = ?",
            (repository_id,),
        )
    }


def _upsert_association(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    commit_id: int,
    association: CommitAssociation,
) -> None:
    values = (
        association.confidence,
        association.evidence_type,
        session_id,
        association.explanation,
        int(association.ambiguous),
        GIT_CORRELATION_VERSION,
    )
    existing = connection.execute(
        """
        SELECT confidence, evidence_type, evidence_origin_session_id,
               evidence_explanation, ambiguous, algorithm_version
        FROM session_commit_associations
        WHERE session_id = ? AND commit_id = ?
        """,
        (session_id, commit_id),
    ).fetchone()
    if existing is not None and tuple(existing) == values:
        return
    with connection:
        connection.execute(
            """
            INSERT INTO session_commit_associations(
                session_id, commit_id, confidence, evidence_type,
                evidence_origin_session_id, evidence_explanation,
                ambiguous, algorithm_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, commit_id) DO UPDATE SET
                confidence = excluded.confidence,
                evidence_type = excluded.evidence_type,
                evidence_origin_session_id = excluded.evidence_origin_session_id,
                evidence_explanation = excluded.evidence_explanation,
                ambiguous = excluded.ambiguous,
                algorithm_version = excluded.algorithm_version,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                commit_id,
                *values,
                _format_datetime(datetime.now(tz=UTC)),
            ),
        )


def _delete_stale_associations(
    connection: sqlite3.Connection,
    repository_id: int,
    desired: set[tuple[int, int]],
) -> None:
    existing = connection.execute(
        """
        SELECT associations.session_id, associations.commit_id
        FROM session_commit_associations AS associations
        JOIN source_sessions AS sessions ON sessions.id = associations.session_id
        WHERE sessions.repository_id = ?
        """,
        (repository_id,),
    ).fetchall()
    stale = [
        (int(row["session_id"]), int(row["commit_id"]))
        for row in existing
        if (int(row["session_id"]), int(row["commit_id"])) not in desired
    ]
    with connection:
        connection.executemany(
            "DELETE FROM session_commit_associations WHERE session_id = ? AND commit_id = ?",
            stale,
        )


def _stored_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _format_datetime(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")
