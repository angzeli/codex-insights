"""Literal SQLite prefix matching shared by analytics commands."""

from __future__ import annotations


def sqlite_like_prefix(value: str) -> str:
    """Return a LIKE pattern that treats user-provided wildcard characters literally."""

    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"
