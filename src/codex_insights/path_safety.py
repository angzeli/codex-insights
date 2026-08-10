"""Real-path and inode safeguards for writes near immutable Codex source data."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path


class UnsafeDestinationError(ValueError):
    """Raised when a write/delete target overlaps protected source or derived data."""


def resolved_path(path: Path) -> Path:
    """Resolve user paths and existing symlink aliases without requiring existence."""

    return path.expanduser().resolve(strict=False)


def validate_write_target(
    target: Path,
    *,
    codex_home: Path,
    operation: str,
    protected_paths: Iterable[Path] = (),
) -> Path:
    """Return a real target path after excluding Codex source and protected files."""

    destination = resolved_path(target)
    source_home = resolved_path(codex_home)
    if destination == source_home or source_home in destination.parents:
        raise UnsafeDestinationError(
            f"{operation} target cannot be inside Codex home: {source_home}"
        )

    for protected in protected_paths:
        protected_path = resolved_path(protected)
        if destination == protected_path or _same_existing_file(destination, protected_path):
            raise UnsafeDestinationError(
                f"{operation} target cannot overwrite protected path: {protected_path}"
            )

    if destination.exists():
        for source_path in _known_source_files(source_home):
            if _same_existing_file(destination, source_path):
                raise UnsafeDestinationError(
                    f"{operation} target aliases a Codex source file: {source_path}"
                )
    return destination


def prepare_output_parent(destination: Path, *, create_parents: bool) -> None:
    """Require an existing parent unless explicit parent creation was requested."""

    parent = destination.parent
    if parent.exists():
        if not parent.is_dir():
            raise ValueError(f"Output parent is not a directory: {parent}")
        return
    if not create_parents:
        raise FileNotFoundError(
            f"Output parent does not exist: {parent}; pass --create-parents to create it"
        )
    parent.mkdir(parents=True, exist_ok=True)


def atomic_write_text(
    destination: Path,
    text: str,
    *,
    overwrite: bool,
    create_parents: bool,
) -> None:
    """Write UTF-8 text through a same-directory temporary file and atomic replace."""

    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"Output already exists: {destination}; pass --overwrite to replace it"
        )
    prepare_output_parent(destination, create_parents=create_parents)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if destination.exists() and not overwrite:
            raise FileExistsError(
                f"Output already exists: {destination}; pass --overwrite to replace it"
            )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _same_existing_file(first: Path, second: Path) -> bool:
    try:
        return first.exists() and second.exists() and first.samefile(second)
    except OSError:
        return False


def _known_source_files(codex_home: Path) -> Iterator[Path]:
    """Yield only immediate metadata and known rollout trees; never read file contents."""

    if not codex_home.is_dir():
        return
    try:
        for item in codex_home.iterdir():
            if item.is_file() or item.is_symlink():
                yield item
    except OSError:
        return
    for directory_name in ("sessions", "archived_sessions"):
        root = codex_home / directory_name
        if not root.is_dir():
            continue
        for current, _, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in files:
                yield current_path / name
