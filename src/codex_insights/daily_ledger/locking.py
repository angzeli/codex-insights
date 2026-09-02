"""Small process lock used by detached queue workers."""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


class LockUnavailableError(RuntimeError):
    """Raised when another daily-ledger worker owns the process lock."""


class ProcessLock:
    """Cross-platform advisory lock with a non-blocking acquisition."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> ProcessLock:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.path.open("a+b")
        try:
            _lock(handle)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise LockUnavailableError(str(self.path)) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._handle is None:
            return
        _unlock(self._handle)
        self._handle.close()
        self._handle = None


def _lock(handle: BinaryIO) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - exercised on Windows
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - exercised on Windows
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
