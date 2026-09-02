"""Fail-open hook capture and atomic pending-job queue."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codex_insights.daily_ledger.config import (
    LedgerConfig,
    LedgerPaths,
    default_ledger_paths,
    ensure_state_directories,
    load_ledger_config,
)
from codex_insights.daily_ledger.privacy import sanitize_remote_text
from codex_insights.path_safety import atomic_write_text

HOOK_JOB_SCHEMA = "daily-ledger-hook-job-v1"
STOP_STDOUT = '{"continue":true,"suppressOutput":true}'
SUPPORTED_EVENTS = frozenset({"Stop", "SessionEnd"})


@dataclass(frozen=True, slots=True)
class HookJob:
    """Selected local-only hook fields; never written to the remote ledger."""

    schema: str
    event_id: str
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str
    captured_at: str
    turn_id: str | None = None
    assistant_outcome_excerpt: str | None = None
    reason: str | None = None
    transcript_size: int | None = None
    transcript_mtime_ns: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> HookJob:
        if raw.get("schema") != HOOK_JOB_SCHEMA:
            raise ValueError("Unsupported pending-job schema")
        required = {
            key: raw.get(key)
            for key in (
                "event_id",
                "session_id",
                "transcript_path",
                "cwd",
                "hook_event_name",
                "captured_at",
            )
        }
        if not all(isinstance(value, str) and value for value in required.values()):
            raise ValueError("Pending job is missing required string fields")
        return cls(
            schema=HOOK_JOB_SCHEMA,
            event_id=str(required["event_id"]),
            session_id=str(required["session_id"]),
            transcript_path=str(required["transcript_path"]),
            cwd=str(required["cwd"]),
            hook_event_name=str(required["hook_event_name"]),
            captured_at=str(required["captured_at"]),
            turn_id=_optional_string(raw.get("turn_id")),
            assistant_outcome_excerpt=_optional_string(
                raw.get("assistant_outcome_excerpt")
            ),
            reason=_optional_string(raw.get("reason")),
            transcript_size=_optional_int(raw.get("transcript_size")),
            transcript_mtime_ns=_optional_int(raw.get("transcript_mtime_ns")),
        )


def capture_hook_payload(
    raw: object,
    *,
    config: LedgerConfig,
    now: datetime | None = None,
    launch_worker: bool = True,
) -> Path | None:
    """Validate common fields, atomically queue one job, and detach a worker."""

    if not isinstance(raw, dict):
        return None
    session_id = _required_hook_string(raw.get("session_id"))
    transcript_path = _required_hook_string(raw.get("transcript_path"))
    cwd = _required_hook_string(raw.get("cwd"))
    event_name = _required_hook_string(raw.get("hook_event_name"))
    if None in {session_id, transcript_path, cwd, event_name}:
        return None
    assert session_id is not None
    assert transcript_path is not None
    assert cwd is not None
    assert event_name is not None
    if event_name not in SUPPORTED_EVENTS:
        return None
    captured = (now or datetime.now(tz=UTC)).astimezone(UTC)
    transcript = Path(transcript_path).expanduser().resolve(strict=False)
    try:
        stat = transcript.stat()
        size = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)
    except OSError:
        size = None
        mtime_ns = None
    turn_id = _optional_string(raw.get("turn_id"))
    reason = sanitize_remote_text(raw.get("reason"), maximum_characters=120)
    assistant_excerpt = sanitize_remote_text(
        raw.get("last_assistant_message"), maximum_characters=320
    )
    event_id = _event_id(
        session_id=session_id,
        event_name=event_name,
        turn_id=turn_id,
        reason=reason,
        transcript_path=transcript,
        transcript_size=size,
        transcript_mtime_ns=mtime_ns,
    )
    job = HookJob(
        schema=HOOK_JOB_SCHEMA,
        event_id=event_id,
        session_id=session_id,
        transcript_path=str(transcript),
        cwd=str(Path(cwd).expanduser().resolve(strict=False)),
        hook_event_name=event_name,
        captured_at=captured.isoformat().replace("+00:00", "Z"),
        turn_id=turn_id,
        assistant_outcome_excerpt=assistant_excerpt,
        reason=reason,
        transcript_size=size,
        transcript_mtime_ns=mtime_ns,
    )
    ensure_state_directories(config.paths)
    destination = config.paths.pending / f"{event_id}.json"
    payload = json.dumps(asdict(job), indent=2, sort_keys=True) + "\n"
    atomic_write_text(destination, payload, overwrite=True, create_parents=True)
    if launch_worker:
        launch_detached_worker(config.paths, config.paths.config)
    return destination


def run_capture_hook(
    stdin_text: str,
    *,
    config_path: Path | None = None,
    launch_worker: bool = True,
) -> tuple[int, str]:
    """Run the event-specific fail-open hook contract without ordinary stdout."""

    event_name: str | None = None
    paths = default_ledger_paths()
    try:
        raw: Any = json.loads(stdin_text)
        if isinstance(raw, dict):
            event_name = _optional_string(raw.get("hook_event_name"))
        config = load_ledger_config(config_path)
        paths = config.paths
        capture_hook_payload(raw, config=config, launch_worker=launch_worker)
    except Exception as exc:  # the hook must never fail a Codex turn
        log_local_failure(paths, "hook", exc)
    return 0, STOP_STDOUT if event_name == "Stop" else ""


def launch_detached_worker(paths: LedgerPaths, config_path: Path) -> None:
    """Start one lock-protected worker without a continuously running daemon."""

    ensure_state_directories(paths)
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "codex_insights.daily_ledger.worker",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=os.name != "nt",
    )


def load_hook_job(path: Path) -> HookJob:
    """Load one selected-field local job."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Pending job must be a JSON object")
    return HookJob.from_dict(raw)


def _event_id(
    *,
    session_id: str,
    event_name: str,
    turn_id: str | None,
    reason: str | None,
    transcript_path: Path,
    transcript_size: int | None,
    transcript_mtime_ns: int | None,
) -> str:
    canonical = json.dumps(
        {
            "session_id": session_id,
            "event_name": event_name,
            "turn_id": turn_id,
            "reason": reason,
            "transcript_path": str(transcript_path),
            "transcript_size": transcript_size,
            "transcript_mtime_ns": transcript_mtime_ns,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _required_hook_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def log_local_failure(paths: LedgerPaths, category: str, exc: Exception) -> None:
    """Append a content-free local error marker without exposing exception text."""

    try:
        paths.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = paths.logs / f"{category}-errors.log"
        timestamp = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        line = f"{timestamp} {type(exc).__name__}\n"
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        return
