"""Bounded Git synchronization for a dedicated private ledger checkout."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from codex_insights.daily_ledger.config import LedgerConfig
from codex_insights.daily_ledger.privacy import scan_remote_file
from codex_insights.daily_ledger.report_policy import (
    load_reporting_policy,
    reporting_window_for_timestamp,
)
from codex_insights.path_safety import atomic_write_text

_ALLOWED_STATIC_FILES = frozenset(
    {
        "README.md",
        "SCHEMA.md",
        "config/project-aliases.yaml",
        "config/report-policy.yaml",
        "schema/codex-session-v1.schema.json",
        "schema/codex-daily-summary-v1.schema.json",
        "schema/codex-manifest-v1.schema.json",
        "status/latest.json",
    }
)


class GitSyncError(RuntimeError):
    """Raised when safe dedicated-checkout synchronization cannot complete."""


class NonAllowlistedDirtyError(GitSyncError):
    """Raised when the dedicated checkout contains unrelated changes."""


@dataclass(frozen=True, slots=True)
class CheckoutInspection:
    """Read-only checkout facts used by doctor and synchronization."""

    valid_checkout: bool
    top_level_matches: bool
    remote_exists: bool
    remote_url_present: bool
    branch: str | None
    dirty_allowlisted: tuple[str, ...]
    dirty_non_allowlisted: tuple[str, ...]
    git_identity_ready: bool


@dataclass(frozen=True, slots=True)
class GitSyncResult:
    """Result of one bounded commit-and-push operation."""

    committed: bool
    pushed: bool
    commit_sha: str | None
    push_attempts: int


def inspect_checkout(config: LedgerConfig) -> CheckoutInspection:
    """Inspect only the configured checkout and remote without network access."""

    checkout = config.ledger_checkout
    top = _git(checkout, "rev-parse", "--show-toplevel")
    valid = top.returncode == 0
    top_level_matches = valid and Path(top.stdout.strip()).resolve(strict=False) == checkout
    remote = _git(checkout, "remote", "get-url", config.remote) if valid else _failed()
    branch = _git(checkout, "branch", "--show-current") if valid else _failed()
    allowed, disallowed = _dirty_paths(checkout) if valid else ((), ())
    user_name = _git(checkout, "config", "user.name") if valid else _failed()
    user_email = _git(checkout, "config", "user.email") if valid else _failed()
    return CheckoutInspection(
        valid_checkout=valid,
        top_level_matches=top_level_matches,
        remote_exists=remote.returncode == 0,
        remote_url_present=remote.returncode == 0 and bool(remote.stdout.strip()),
        branch=branch.stdout.strip() if branch.returncode == 0 and branch.stdout.strip() else None,
        dirty_allowlisted=allowed,
        dirty_non_allowlisted=disallowed,
        git_identity_ready=(
            user_name.returncode == 0
            and bool(user_name.stdout.strip())
            and user_email.returncode == 0
            and bool(user_email.stdout.strip())
        ),
    )


def sync_checkout(
    config: LedgerConfig,
    rebuild: Callable[[], object],
    *,
    now: datetime | None = None,
) -> GitSyncResult:
    """Rebuild, stage allowlisted paths, commit, rebase, and push with one retry."""

    inspection = inspect_checkout(config)
    if not inspection.valid_checkout or not inspection.top_level_matches:
        raise GitSyncError("Configured ledger checkout is not its Git top level")
    if not inspection.remote_exists or not inspection.remote_url_present:
        raise GitSyncError(f"Configured Git remote is unavailable: {config.remote}")
    if inspection.dirty_non_allowlisted:
        raise NonAllowlistedDirtyError(
            "Dedicated checkout has non-allowlisted changes: "
            + ", ".join(inspection.dirty_non_allowlisted)
        )
    if not inspection.git_identity_ready:
        raise GitSyncError("Git user.name and user.email must be configured")
    if not inspection.dirty_allowlisted:
        _fetch_and_rebase(config)
    rebuild()
    _assert_only_allowlisted(config.ledger_checkout)
    committed = _commit_allowlisted(config, now=now)
    attempts = 1
    push = _git(
        config.ledger_checkout,
        "push",
        config.remote,
        f"HEAD:{config.branch}",
        timeout=30,
    )
    if push.returncode != 0:
        attempts = 2
        _fetch_and_rebase(config)
        rebuild()
        _assert_only_allowlisted(config.ledger_checkout)
        committed = _commit_allowlisted(config, now=now) or committed
        push = _git(
            config.ledger_checkout,
            "push",
            config.remote,
            f"HEAD:{config.branch}",
            timeout=30,
        )
    if push.returncode != 0:
        raise GitSyncError("Git push failed after one bounded retry")
    commit_sha = _git(config.ledger_checkout, "rev-parse", "HEAD")
    sha = commit_sha.stdout.strip() if commit_sha.returncode == 0 else None
    _record_success(config, sha)
    return GitSyncResult(
        committed=committed,
        pushed=True,
        commit_sha=sha,
        push_attempts=attempts,
    )


def is_allowlisted_path(path: str) -> bool:
    """Return whether one repository-relative path belongs to the ledger contract."""

    normalized = PurePosixPath(path.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        return False
    text = normalized.as_posix()
    if text in _ALLOWED_STATIC_FILES:
        return True
    parts = normalized.parts
    if len(parts) == 6 and parts[:2] == ("ledger", "codex"):
        year, month, day, filename = parts[2:]
        return _valid_day_path(year, month, day) and filename in {
            "manifest.json",
            "summary.json",
            "events.jsonl",
        }
    if len(parts) == 7 and parts[:2] == ("ledger", "codex"):
        year, month, day, directory, filename = parts[2:]
        return (
            _valid_day_path(year, month, day)
            and (directory, PurePosixPath(filename).suffix)
            in {("sessions", ".json"), ("events", ".jsonl")}
            and len(PurePosixPath(filename).stem) == 32
            and all(character in "0123456789abcdef"
                    for character in PurePosixPath(filename).stem)
        )
    return False


def _fetch_and_rebase(config: LedgerConfig) -> None:
    fetch = _git(
        config.ledger_checkout,
        "fetch",
        config.remote,
        config.branch,
        timeout=30,
    )
    missing_remote_branch = "couldn't find remote ref" in fetch.stderr.casefold()
    if fetch.returncode != 0 and not missing_remote_branch:
        raise GitSyncError("Git fetch failed; queue retained")
    tracking = _git(
        config.ledger_checkout,
        "rev-parse",
        "--verify",
        f"refs/remotes/{config.remote}/{config.branch}",
    )
    if tracking.returncode != 0:
        return
    pull = _git(
        config.ledger_checkout,
        "pull",
        "--rebase",
        config.remote,
        config.branch,
        timeout=30,
    )
    if pull.returncode != 0:
        raise GitSyncError("Git pull --rebase failed; queue retained")


def _commit_allowlisted(config: LedgerConfig, *, now: datetime | None) -> bool:
    allowed, disallowed = _dirty_paths(config.ledger_checkout)
    if disallowed:
        raise NonAllowlistedDirtyError(
            "Dedicated checkout has non-allowlisted changes: " + ", ".join(disallowed)
        )
    if not allowed:
        return False
    assert_allowlisted_content_safe(config.ledger_checkout, allowed)
    stage = _git(config.ledger_checkout, "add", "--", *allowed)
    if stage.returncode != 0:
        raise GitSyncError("Could not stage allowlisted ledger paths")
    staged = _git(config.ledger_checkout, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        return False
    if staged.returncode != 1:
        raise GitSyncError("Could not inspect staged ledger changes")
    instant = now or datetime.now(tz=UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    reporting_window = reporting_window_for_timestamp(
        load_reporting_policy(config), instant
    )
    timestamp = instant.astimezone(reporting_window.window_end.tzinfo)
    message = f"ledger(codex): sync {timestamp:%Y-%m-%d %H:%M %Z}"
    commit = _git(config.ledger_checkout, "commit", "-m", message, timeout=30)
    if commit.returncode != 0:
        raise GitSyncError("Could not commit allowlisted ledger changes")
    return True


def _assert_only_allowlisted(checkout: Path) -> None:
    _, disallowed = _dirty_paths(checkout)
    if disallowed:
        raise NonAllowlistedDirtyError(
            "Dedicated checkout has non-allowlisted changes: " + ", ".join(disallowed)
        )


def assert_allowlisted_content_safe(checkout: Path, paths: tuple[str, ...]) -> None:
    """Reject unsafe contents before an allowlisted file is interpreted or staged."""

    for relative in paths:
        path = checkout / relative
        if not path.exists():
            continue
        findings = scan_remote_file(path, location=relative)
        if findings:
            first = findings[0]
            raise GitSyncError(
                f"Privacy check failed for allowlisted content: {first.category}@{first.location}"
            )


def _valid_day_path(year: str, month: str, day: str) -> bool:
    try:
        datetime.strptime(f"{year}-{month}-{day}", "%Y-%m-%d")
    except ValueError:
        return False
    return len(year) == 4 and len(month) == 2 and len(day) == 2


def _dirty_paths(checkout: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    result = _git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if result.returncode != 0:
        return (), ()
    paths: set[str] = set()
    entries = result.stdout.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:] if len(entry) > 3 else ""
        if status.startswith(("R", "C")) and index < len(entries):
            path = entries[index]
            index += 1
        if path:
            paths.add(path)
    allowed = tuple(sorted(path for path in paths if is_allowlisted_path(path)))
    disallowed = tuple(sorted(path for path in paths if not is_allowlisted_path(path)))
    return allowed, disallowed


def _record_success(config: LedgerConfig, commit_sha: str | None) -> None:
    payload = {
        "schema": "daily-ledger-last-push-v1",
        "pushed_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "commit_sha": commit_sha,
        "remote": config.remote,
        "branch": config.branch,
    }
    path = config.paths.cache / "last-successful-push.json"
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        overwrite=True,
        create_parents=True,
    )


def _git(
    checkout: Path,
    *arguments: str,
    timeout: int = 10,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            args=["git", *arguments],
            returncode=127,
            stdout="",
            stderr=type(exc).__name__,
        )


def _failed() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
