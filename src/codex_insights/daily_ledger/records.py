"""Evidence-grounded session records built from the existing rollout parser."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from codex_insights.adapters.codex_index import PARSER_VERSION, parse_rollout
from codex_insights.daily_ledger.config import LedgerConfig
from codex_insights.daily_ledger.privacy import (
    assert_remote_safe,
    safe_repository_name,
    sanitize_remote_text,
)
from codex_insights.models import (
    CommandCategory,
    EventFamily,
    NormalizedSourceSession,
    NormalizedToolCallCandidate,
    NormalizedToolResultCandidate,
    ParsedSourceSession,
    SessionOutcome,
    SourceSessionCandidate,
    ToolResultStatus,
)
from codex_insights.outcomes import (
    LifecycleStatus,
    OutcomeAssessment,
    OutcomeEvidence,
    OutcomeEvidenceKind,
    classify_outcome,
)

SESSION_CACHE_SCHEMA = "daily-ledger-session-cache-v1"
SESSION_RECORD_SCHEMA = "codex-session-v1"
_FIRST_SENTENCE = re.compile(r"^(.{1,180}?[.!?。！？])(?:\s|$)")
_SAFE_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ActivityState(StrEnum):
    """Human-facing state kept separate from the existing outcome model."""

    COMPLETED = "completed"
    COMPLETED_LOCALLY = "completed_locally"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    PLANNING_ONLY = "planning_only"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    """Read-only Git facts collected after transcript parsing."""

    repository_root: Path | None
    repository_name: str | None
    remote_slug: str | None
    branch: str | None
    ending_head: str | None
    worktree_state: str
    pushed: bool | None


@dataclass(frozen=True, slots=True)
class RecordBuildResult:
    """One idempotent cache value and the dates it covers."""

    session_key: str
    source_hash: str
    cache: dict[str, object]
    dates: tuple[str, ...]


def build_session_cache(job: object, config: LedgerConfig) -> RecordBuildResult:
    """Parse one local job into privacy-safe daily slices."""

    session_id = _job_string(job, "session_id")
    transcript_path = Path(_job_string(job, "transcript_path"))
    cwd = Path(_job_string(job, "cwd"))
    captured_at = _parse_datetime(_job_string(job, "captured_at"))
    session_key = stable_session_key(session_id, config.device_id)
    source_hash = _sha256_file(transcript_path)
    parsed = _parse_transcript(session_id, transcript_path, cwd)
    snapshot = inspect_git_snapshot(cwd, remote=config.remote)
    slices = _daily_slices(
        parsed,
        config=config,
        session_key=session_key,
        source_hash=source_hash,
        captured_at=captured_at,
        assistant_excerpt=_job_optional_string(job, "assistant_outcome_excerpt"),
        hook_event_name=_job_string(job, "hook_event_name"),
        hook_event_id=_job_string(job, "event_id"),
        snapshot=snapshot,
    )
    cache: dict[str, object] = {
        "schema": SESSION_CACHE_SCHEMA,
        "session_key": session_key,
        "source_hash": source_hash,
        "parser_version": PARSER_VERSION,
        "latest_captured_at": _format_datetime(captured_at),
        "event_ids": [_job_string(job, "event_id")],
        "slices": slices,
    }
    assert_remote_safe(
        {
            day: value["record"]
            for day, value in slices.items()
            if isinstance(value, dict)
        }
    )
    return RecordBuildResult(
        session_key=session_key,
        source_hash=source_hash,
        cache=cache,
        dates=tuple(sorted(slices)),
    )


def merge_session_cache(
    previous: dict[str, object] | None,
    current: RecordBuildResult,
) -> dict[str, object]:
    """Retain event identities while replacing a session with its latest full parse."""

    merged = dict(current.cache)
    previous_ids = previous.get("event_ids", []) if previous is not None else []
    current_ids = current.cache.get("event_ids", [])
    identifiers = {
        str(value)
        for value in (*_as_list(previous_ids), *_as_list(current_ids))
        if isinstance(value, str)
    }
    merged["event_ids"] = sorted(identifiers)
    return merged


def stable_session_key(session_id: str, device_id: str) -> str:
    """Hash a local session id with a device-scoped domain separator."""

    digest = hashlib.sha256(
        f"codex-daily-ledger-v1\0{device_id}\0{session_id}".encode()
    ).hexdigest()
    return digest[:32]


def inspect_git_snapshot(cwd: Path, *, remote: str) -> GitSnapshot:
    """Read persisted local Git state without fetching or mutating the repository."""

    root_text = _git(cwd, "rev-parse", "--show-toplevel")
    if root_text is None:
        return GitSnapshot(None, None, None, None, None, "unknown", None)
    root = Path(root_text).resolve(strict=False)
    repository_name = safe_repository_name(root.name)
    branch = sanitize_remote_text(_git(root, "branch", "--show-current"), maximum_characters=128)
    head = _full_hash(_git(root, "rev-parse", "HEAD"))
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    worktree_state = "unknown" if status is None else "clean" if not status else "dirty"
    remote_url = _git(root, "remote", "get-url", remote)
    remote_slug = _remote_slug(remote_url)
    pushed: bool | None = None
    if head is not None and branch:
        tracking = _full_hash(
            _git(root, "rev-parse", "--verify", f"refs/remotes/{remote}/{branch}")
        )
        if tracking == head:
            pushed = True
    return GitSnapshot(
        repository_root=root,
        repository_name=repository_name,
        remote_slug=remote_slug,
        branch=branch,
        ending_head=head,
        worktree_state=worktree_state,
        pushed=pushed,
    )


def _parse_transcript(
    session_id: str,
    transcript_path: Path,
    cwd: Path,
) -> ParsedSourceSession:
    stat = transcript_path.stat()
    session = NormalizedSourceSession(
        source_session_id=session_id,
        source_type="daily-ledger-hook",
        source_home=transcript_path.parent,
        cwd=cwd,
        rollout_path=transcript_path,
        source_path=transcript_path,
    )
    candidate = SourceSessionCandidate(
        session=session,
        source_schema_version="hook-v1",
        rollout_exists=True,
        rollout_allowed=True,
        size_bytes=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
    )
    return parse_rollout(candidate)


def _daily_slices(
    parsed: ParsedSourceSession,
    *,
    config: LedgerConfig,
    session_key: str,
    source_hash: str,
    captured_at: datetime,
    assistant_excerpt: str | None,
    hook_event_name: str,
    hook_event_id: str,
    snapshot: GitSnapshot,
) -> dict[str, dict[str, object]]:
    calls = tuple(parsed.tool_call_candidates)
    results = _result_map(parsed.tool_result_candidates)
    observations = tuple(parsed.event_observations)
    objective = _clean_objective(parsed)
    precise = bool(observations or calls) and all(
        event.occurred_at is not None for event in observations
    ) and all(call.occurred_at is not None for call in calls)
    fallback = parsed.session.started_at or captured_at
    day_keys = {
        _local_date(timestamp or fallback, config)
        for timestamp in (
            *(event.occurred_at for event in observations),
            *(call.occurred_at for call in calls),
        )
    }
    if not day_keys:
        day_keys = {_local_date(fallback, config)}
    captured_day = _local_date(captured_at, config)
    hook_day = captured_day if captured_day in day_keys or not precise else max(day_keys)
    slices: dict[str, dict[str, object]] = {}
    for day in sorted(day_keys):
        day_calls = tuple(
            call
            for call in calls
            if _local_date(call.occurred_at or fallback, config) == day
        )
        day_observations = tuple(
            event
            for event in observations
            if _local_date(event.occurred_at or fallback, config) == day
        )
        assessment = _assessment(day_calls, day_observations, results)
        validations = _validations(day_calls, results, fallback=fallback)
        commits = _commits(day_calls, results, snapshot=snapshot, config=config, fallback=fallback)
        state = _activity_state(
            assessment,
            day_calls=day_calls,
            validations=validations,
            commits=commits,
            pushed=snapshot.pushed,
        )
        first_observed, last_observed = _bounds(
            tuple(
                item
                for item in (
                    *(call.occurred_at for call in day_calls),
                    *(event.occurred_at for event in day_observations),
                )
                if item is not None
            ),
            fallback=fallback,
        )
        is_last_slice = day == max(day_keys)
        git_value = _git_value(snapshot, is_last_slice=is_last_slice)
        work_done = _work_done(day_calls, validations, commits)
        open_loops = _open_loops(assessment, validations)
        event_rows = _event_rows(
            session_key,
            source_hash,
            day_observations,
            day_calls,
            fallback=fallback,
        )
        if day == hook_day:
            event_rows.append(
                {
                    "event_id": hook_event_id,
                    "session_key": session_key,
                    "event_type": hook_event_name.casefold(),
                    "timestamp": _format_datetime(captured_at),
                    "source_hash": source_hash,
                    "revision": 1,
                }
            )
        project_id = _project_id(snapshot, parsed.session.repository_name, session_key)
        record: dict[str, object] = {
            "schema_version": SESSION_RECORD_SCHEMA,
            "date": day,
            "timezone": config.timezone,
            "session_key": session_key,
            "device_id": config.device_id,
            "first_observed_activity": _format_datetime(first_observed),
            "last_observed_activity": _format_datetime(last_observed),
            "timestamp_precision": "event" if precise else "capture_fallback",
            "project": {
                "project_id": project_id,
                "display_name": snapshot.repository_name
                or safe_repository_name(parsed.session.repository_name)
                or "Unknown project",
                "category": "software",
                "remote_slug": snapshot.remote_slug,
            },
            "activity_state": state.value,
            "objective": objective,
            "work_done": work_done,
            "git": git_value,
            "commits": commits,
            "validation": validations,
            "decisions": [],
            "open_loops": open_loops,
            "assistant_outcome_excerpt": sanitize_remote_text(
                assistant_excerpt, maximum_characters=320
            ),
            "underlying_outcome": {
                "outcome": assessment.outcome.value,
                "confidence": assessment.confidence.value,
                "lifecycle_status": assessment.lifecycle_status.value,
                "evidence": list(assessment.evidence),
                "classifier_version": assessment.classifier_version,
            },
            "transcript_sha256": source_hash,
            "evidence": {
                "level": _evidence_level(assessment, commits, validations),
                "sources": _evidence_sources(day_calls, commits, validations),
                "assistant_claim_used_for_completion": False,
                "parser_version": PARSER_VERSION,
            },
            "event_ids": sorted({str(row["event_id"]) for row in event_rows}),
        }
        slices[day] = {"record": record, "events": event_rows}
    return slices


def _assessment(
    calls: tuple[NormalizedToolCallCandidate, ...],
    observations: tuple[Any, ...],
    results: dict[str, NormalizedToolResultCandidate],
) -> OutcomeAssessment:
    evidence: list[OutcomeEvidence] = []
    for call in calls:
        result = results.get(call.call_id_digest or "")
        status = result.status if result is not None else ToolResultStatus.UNKNOWN
        kind: OutcomeEvidenceKind | None = None
        if call.command_category in {
            CommandCategory.TESTING,
            CommandCategory.LINTING,
            CommandCategory.TYPE_CHECKING,
        }:
            if status is ToolResultStatus.SUCCESS:
                kind = OutcomeEvidenceKind.VALIDATION_PASS
            elif status is ToolResultStatus.FAILURE:
                kind = OutcomeEvidenceKind.VALIDATION_FAIL
        elif call.command_category is CommandCategory.EDITING_PATCHING:
            kind = OutcomeEvidenceKind.EDIT
        if kind is not None:
            evidence.append(
                OutcomeEvidence(
                    sequence=call.source_ordinal * 10 + call.operation_ordinal,
                    kind=kind,
                    occurred_at=call.occurred_at,
                )
            )
        if call.command_operation == "git_commit" and result is not None and (
            result.git_commit_hash or result.git_commit_abbrev
        ):
            evidence.append(
                OutcomeEvidence(
                    sequence=call.source_ordinal * 10 + 8,
                    kind=OutcomeEvidenceKind.HIGH_COMMIT,
                    occurred_at=call.occurred_at,
                )
            )
    for event in observations:
        if event.family is EventFamily.TASK_LIFECYCLE:
            if event.source_payload_type == "task_complete":
                kind = OutcomeEvidenceKind.TASK_COMPLETE
            elif event.source_payload_type == "turn_aborted":
                kind = OutcomeEvidenceKind.ABORT
            else:
                continue
        elif event.family is EventFamily.ERROR:
            kind = OutcomeEvidenceKind.ERROR
        else:
            continue
        evidence.append(
            OutcomeEvidence(
                sequence=event.source_ordinal * 10 + 5,
                kind=kind,
                occurred_at=event.occurred_at,
            )
        )
    return classify_outcome(tuple(evidence))


def _activity_state(
    assessment: OutcomeAssessment,
    *,
    day_calls: tuple[NormalizedToolCallCandidate, ...],
    validations: list[dict[str, object]],
    commits: list[dict[str, object]],
    pushed: bool | None,
) -> ActivityState:
    if assessment.lifecycle_status is LifecycleStatus.ABORTED:
        return ActivityState.ABANDONED
    if assessment.outcome is SessionOutcome.FAILED:
        return ActivityState.BLOCKED
    if assessment.outcome in {SessionOutcome.SUCCESS, SessionOutcome.SUCCESS_WITH_WARNINGS}:
        pushed_task_commit = bool(commits) and all(
            commit.get("pushed") is True for commit in commits
        )
        return (
            ActivityState.COMPLETED
            if pushed is True and pushed_task_commit
            else ActivityState.COMPLETED_LOCALLY
        )
    if assessment.outcome is SessionOutcome.PARTIAL:
        return ActivityState.IN_PROGRESS
    execution = bool(commits or validations) or any(
        call.command_category
        in {
            CommandCategory.EDITING_PATCHING,
            CommandCategory.GIT_MUTATION,
            CommandCategory.SCIENTIFIC_COMPUTATION,
            CommandCategory.BUILD_PACKAGING,
        }
        for call in day_calls
    )
    if not execution:
        return ActivityState.PLANNING_ONLY
    return ActivityState.UNKNOWN


def _validations(
    calls: tuple[NormalizedToolCallCandidate, ...],
    results: dict[str, NormalizedToolResultCandidate],
    *,
    fallback: datetime,
) -> list[dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for call in calls:
        if call.command_category not in {
            CommandCategory.TESTING,
            CommandCategory.LINTING,
            CommandCategory.TYPE_CHECKING,
        }:
            continue
        result = results.get(call.call_id_digest or "")
        status = result.status.value if result is not None else ToolResultStatus.UNKNOWN.value
        identifier = hashlib.sha256(
            (
                f"{call.command_fingerprint or call.tool_name}:"
                f"{call.source_ordinal}:{call.operation_ordinal}"
            ).encode()
        ).hexdigest()
        rows[identifier] = {
            "validation_id": identifier,
            "category": call.command_category.value,
            "command": sanitize_remote_text(call.command_text, maximum_characters=240),
            "scope": call.test_scope.value,
            "result": status,
            "timestamp": _format_datetime(call.occurred_at or fallback),
            "evidence_type": "structured_command_result",
        }
    return [rows[key] for key in sorted(rows)]


def _commits(
    calls: tuple[NormalizedToolCallCandidate, ...],
    results: dict[str, NormalizedToolResultCandidate],
    *,
    snapshot: GitSnapshot,
    config: LedgerConfig,
    fallback: datetime,
) -> list[dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for call in calls:
        if call.command_operation != "git_commit":
            continue
        result = results.get(call.call_id_digest or "")
        if result is None:
            continue
        raw_hash = result.git_commit_hash or result.git_commit_abbrev
        commit_hash = (
            _resolve_commit_hash(snapshot.repository_root, raw_hash)
            if snapshot.repository_root is not None
            else None
        )
        if commit_hash is None:
            commit_hash = _full_hash(raw_hash)
        if commit_hash is None and raw_hash and re.fullmatch(r"[0-9a-f]{7,39}", raw_hash):
            commit_hash = raw_hash
        if commit_hash is None:
            continue
        subject = (
            sanitize_remote_text(
                _git(snapshot.repository_root, "show", "-s", "--format=%s", commit_hash),
                maximum_characters=180,
            )
            if snapshot.repository_root is not None
            else None
        )
        rows[commit_hash] = {
            "sha": commit_hash,
            "subject": subject,
            "timestamp": _format_datetime(call.occurred_at or fallback),
            "pushed": snapshot.pushed if commit_hash == snapshot.ending_head else None,
            "evidence_type": "structured_git_command_result",
        }
    return [rows[key] for key in sorted(rows)]


def _resolve_commit_hash(cwd: Path, value: str | None) -> str | None:
    if value is None:
        return None
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    return _full_hash(_git(cwd, "rev-parse", "--verify", f"{value}^{{commit}}"))


def _work_done(
    calls: tuple[NormalizedToolCallCandidate, ...],
    validations: list[dict[str, object]],
    commits: list[dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if any(call.command_category is CommandCategory.EDITING_PATCHING for call in calls):
        rows.append(
            {
                "summary": "Repository edits were observed",
                "evidence_type": "structured_edit_event",
                "confidence": "medium",
            }
        )
    for commit in commits:
        rows.append(
            {
                "summary": f"Created local Git commit {str(commit['sha'])[:12]}",
                "evidence_type": "structured_git_command_result",
                "confidence": "high",
            }
        )
    passed = sum(1 for item in validations if item["result"] == "success")
    if passed:
        rows.append(
            {
                "summary": f"Observed {passed} successful validation command(s)",
                "evidence_type": "structured_command_result",
                "confidence": "high",
            }
        )
    return rows


def _open_loops(
    assessment: OutcomeAssessment,
    validations: list[dict[str, object]],
) -> list[str]:
    if any(item["result"] == "failure" for item in validations):
        return ["A validation failure remained in the latest evidenced state"]
    if assessment.outcome is SessionOutcome.UNKNOWN:
        return ["Workload outcome remains unknown because evidence is insufficient"]
    if assessment.outcome is SessionOutcome.PARTIAL:
        return ["Repository edits were observed without strong completion evidence"]
    return []


def _event_rows(
    session_key: str,
    source_hash: str,
    observations: tuple[Any, ...],
    calls: tuple[NormalizedToolCallCandidate, ...],
    *,
    fallback: datetime,
) -> list[dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for event in observations:
        identifier = hashlib.sha256(
            f"{session_key}:{event.fingerprint}:{event.source_ordinal}".encode()
        ).hexdigest()
        rows[identifier] = {
            "event_id": identifier,
            "session_key": session_key,
            "event_type": event.family.value,
            "timestamp": _format_datetime(event.occurred_at or fallback),
            "source_hash": source_hash,
            "revision": 1,
        }
    for call in calls:
        identifier = hashlib.sha256(
            (
                f"{session_key}:{call.command_fingerprint or call.tool_name}:"
                f"{call.source_ordinal}:{call.operation_ordinal}"
            ).encode()
        ).hexdigest()
        rows[identifier] = {
            "event_id": identifier,
            "session_key": session_key,
            "event_type": call.command_category.value,
            "timestamp": _format_datetime(call.occurred_at or fallback),
            "source_hash": source_hash,
            "revision": 1,
        }
    return [rows[key] for key in sorted(rows)]


def _git_value(snapshot: GitSnapshot, *, is_last_slice: bool) -> dict[str, object]:
    if not is_last_slice:
        return {
            "starting_head": None,
            "ending_head": None,
            "branch": None,
            "remote_slug": snapshot.remote_slug,
            "worktree_state": "unknown",
            "pushed": None,
            "evidence_type": "not_attributed_to_earlier_slice",
        }
    return {
        "starting_head": None,
        "ending_head": snapshot.ending_head,
        "branch": snapshot.branch,
        "remote_slug": snapshot.remote_slug,
        "worktree_state": snapshot.worktree_state,
        "pushed": snapshot.pushed,
        "evidence_type": "persisted_local_git_state",
    }


def _project_id(
    snapshot: GitSnapshot,
    parsed_repository_name: str | None,
    session_key: str,
) -> str:
    if snapshot.remote_slug is not None:
        return snapshot.remote_slug
    name = snapshot.repository_name or safe_repository_name(parsed_repository_name)
    return f"local/{name}" if name is not None else f"unknown/{session_key[:12]}"


def _clean_objective(parsed: ParsedSourceSession) -> str | None:
    for prompt in parsed.prompt_candidates:
        safe = sanitize_remote_text(prompt.text, maximum_characters=600)
        if safe is None:
            continue
        match = _FIRST_SENTENCE.match(safe)
        if match is not None:
            return match.group(1)
        if len(safe) <= 180:
            return safe
        return safe[:179].rstrip() + "…"
    return None


def _result_map(
    results: tuple[NormalizedToolResultCandidate, ...],
) -> dict[str, NormalizedToolResultCandidate]:
    return {
        item.call_id_digest: item
        for item in results
        if item.call_id_digest is not None
    }


def _evidence_level(
    assessment: OutcomeAssessment,
    commits: list[dict[str, object]],
    validations: list[dict[str, object]],
) -> str:
    if commits or any(item["result"] in {"success", "failure"} for item in validations):
        return "high"
    if assessment.evidence and assessment.evidence != ("no_originated_evidence",):
        return "medium"
    return "low"


def _evidence_sources(
    calls: tuple[NormalizedToolCallCandidate, ...],
    commits: list[dict[str, object]],
    validations: list[dict[str, object]],
) -> list[str]:
    sources = {"structured_parser"}
    if calls:
        sources.add("command_metadata")
    if validations:
        sources.add("command_results")
    if commits:
        sources.add("git_command_results")
    return sorted(sources)


def _bounds(
    timestamps: tuple[datetime, ...],
    *,
    fallback: datetime,
) -> tuple[datetime, datetime]:
    if not timestamps:
        return fallback, fallback
    return min(timestamps), max(timestamps)


def _local_date(value: datetime, config: LedgerConfig) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(config.zone).date().isoformat()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _format_datetime(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(cwd: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _remote_slug(value: str | None) -> str | None:
    if not value or "@" in value and ":" not in value:
        return None
    candidate: str
    if value.startswith("git@") and ":" in value:
        candidate = value.split(":", 1)[1]
    else:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https", "ssh", "git"}:
            return None
        candidate = parsed.path.lstrip("/")
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    return candidate if _SAFE_SLUG.fullmatch(candidate) else None


def _full_hash(value: str | None) -> str | None:
    return value if value is not None and re.fullmatch(r"[0-9a-f]{40}", value) else None


def _job_string(job: object, name: str) -> str:
    value = getattr(job, name, None)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Job field {name} is missing")
    return value


def _job_optional_string(job: object, name: str) -> str | None:
    value = getattr(job, name, None)
    return value if isinstance(value, str) and value else None


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def parse_date(value: str) -> date:
    """Parse a strict ISO calendar date for CLI operations."""

    return date.fromisoformat(value)
