from __future__ import annotations

import os
from pathlib import Path

import pytest

from codex_insights.path_safety import UnsafeDestinationError, validate_write_target


def test_write_targets_reject_codex_home_children_traversal_and_symlinks(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    source_database = codex_home / "state_9.sqlite"
    source_database.write_bytes(b"synthetic source")
    outside = tmp_path / "outside"
    outside.mkdir()
    directory_alias = outside / "codex-alias"
    directory_alias.symlink_to(codex_home, target_is_directory=True)
    file_alias = outside / "state-alias.sqlite"
    file_alias.symlink_to(source_database)
    hardlink_alias = outside / "state-hardlink.sqlite"
    os.link(source_database, hardlink_alias)

    unsafe = (
        codex_home,
        codex_home / "export.json",
        outside / ".." / "codex-home" / "backup.sqlite",
        directory_alias / "export.json",
        file_alias,
        hardlink_alias,
    )
    for target in unsafe:
        with pytest.raises(UnsafeDestinationError):
            validate_write_target(
                target,
                codex_home=codex_home,
                operation="Synthetic write",
            )


def test_write_targets_allow_safe_temporary_and_application_support_paths(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    temporary = tmp_path / "exports" / "sessions.json"
    application_support = (
        tmp_path / "Library" / "Application Support" / "Codex Insights" / "index.sqlite3"
    )

    assert validate_write_target(
        temporary,
        codex_home=codex_home,
        operation="Export",
    ) == temporary
    assert validate_write_target(
        application_support,
        codex_home=codex_home,
        operation="Derived database",
    ) == application_support
