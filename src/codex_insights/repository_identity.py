"""Stable, privacy-conscious repository identities for historical attribution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REPOSITORY_IDENTITY_VERSION = "repository-identity-v1"


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """One deterministic repository identity with its supporting evidence."""

    key: str
    display_name: str
    method: str
    normalized_remote: str | None
    canonical_root: Path | None
    common_git_dir: Path | None
    path_exists: bool


def resolve_repository_identity(
    repository_root: Path | None,
    repository_name: str | None,
    git_origin_url: str | None,
) -> RepositoryIdentity | None:
    """Prefer normalized remote identity, then common Git dir, then exact path."""

    normalized_remote = normalize_remote_url(git_origin_url)
    root = repository_root.expanduser().resolve(strict=False) if repository_root else None
    common_git_dir = resolve_common_git_dir(root) if root else None
    display_name = repository_name or (root.name if root else None) or _remote_name(
        normalized_remote
    )
    if normalized_remote is not None:
        evidence = f"remote\0{normalized_remote}"
        method = "normalized_remote"
    elif common_git_dir is not None:
        evidence = f"gitdir\0{common_git_dir}"
        method = "common_git_dir"
    elif root is not None:
        evidence = f"path\0{root}"
        method = "repository_path"
    else:
        return None
    digest = hashlib.sha256(evidence.encode("utf-8", errors="replace")).hexdigest()
    return RepositoryIdentity(
        key=f"repo_{digest}",
        display_name=display_name or "Unknown repository",
        method=method,
        normalized_remote=normalized_remote,
        canonical_root=root,
        common_git_dir=common_git_dir,
        path_exists=bool(root and root.is_dir()),
    )


def normalize_remote_url(value: str | None) -> str | None:
    """Normalize common HTTPS/SSH/scp remotes without retaining credentials."""

    if value is None or not value.strip():
        return None
    remote = value.strip()
    if "://" not in remote and ":" in remote:
        host_part, path_part = remote.split(":", 1)
        host = host_part.rsplit("@", 1)[-1].casefold()
        path = _normalize_remote_path(path_part)
        return f"{host}/{path}" if host and path else None
    parsed = urlsplit(remote)
    host = (parsed.hostname or "").casefold()
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    path = _normalize_remote_path(parsed.path)
    if parsed.scheme == "file" and path:
        return f"file/{path}"
    return f"{host}/{path}" if host and path else None


def resolve_common_git_dir(repository_root: Path) -> Path | None:
    """Resolve the shared Git directory for a main checkout or linked worktree."""

    marker = repository_root / ".git"
    if marker.is_dir():
        return marker.resolve(strict=False)
    if not marker.is_file():
        return None
    try:
        content = marker.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    first_line = content.splitlines()[0] if content.splitlines() else ""
    if not first_line.casefold().startswith("gitdir:"):
        return None
    raw_target = first_line.split(":", 1)[1].strip()
    target = Path(raw_target)
    git_dir = (target if target.is_absolute() else repository_root / target).resolve(
        strict=False
    )
    common_marker = git_dir / "commondir"
    if common_marker.is_file():
        try:
            raw_common = common_marker.read_text(
                encoding="utf-8", errors="replace"
            )[:4096].strip()
        except OSError:
            raw_common = ""
        if raw_common:
            common = Path(raw_common)
            return (common if common.is_absolute() else git_dir / common).resolve(
                strict=False
            )
    if git_dir.parent.name == "worktrees":
        return git_dir.parent.parent.resolve(strict=False)
    return git_dir


def _normalize_remote_path(value: str) -> str:
    path = value.strip().strip("/")
    return path[:-4] if path.casefold().endswith(".git") else path


def _remote_name(value: str | None) -> str | None:
    return value.rsplit("/", 1)[-1] if value else None
