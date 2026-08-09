from __future__ import annotations

import sqlite3
from pathlib import Path

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source
from codex_insights.repository_identity import (
    normalize_remote_url,
    resolve_repository_identity,
)


def test_remote_identity_removes_credentials_and_normalizes_common_forms() -> None:
    https = normalize_remote_url(
        "https://token-user:secret@example.com/owner/project.git?token=ignored"
    )
    ssh = normalize_remote_url("git@example.com:owner/project.git")

    assert https == "example.com/owner/project"
    assert ssh == "example.com/owner/project"
    assert "secret" not in (https or "")


def test_repository_identity_keeps_same_basename_paths_distinct(tmp_path: Path) -> None:
    first = tmp_path / "one" / "project"
    second = tmp_path / "two" / "project"
    (first / ".git").mkdir(parents=True)
    (second / ".git").mkdir(parents=True)

    first_identity = resolve_repository_identity(first, "project", None)
    second_identity = resolve_repository_identity(second, "project", None)

    assert first_identity is not None and second_identity is not None
    assert first_identity.key != second_identity.key


def test_main_checkout_and_linked_worktree_share_common_git_identity(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    common = main / ".git"
    worktree_admin = common / "worktrees" / "linked"
    worktree_admin.mkdir(parents=True)
    linked.mkdir()
    (linked / ".git").write_text(
        f"gitdir: {worktree_admin}\n",
        encoding="utf-8",
    )
    (worktree_admin / "commondir").write_text("../..\n", encoding="utf-8")

    main_identity = resolve_repository_identity(main, "main", None)
    linked_identity = resolve_repository_identity(linked, "main", None)

    assert main_identity is not None and linked_identity is not None
    assert main_identity.key == linked_identity.key
    assert linked_identity.method == "common_git_dir"


def test_index_assigns_moved_roots_with_same_remote_to_one_repository(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    first = tmp_path / "old" / "project"
    second = tmp_path / "new" / "project"
    (first / ".git").mkdir(parents=True)
    (second / ".git").mkdir(parents=True)
    with sqlite3.connect(codex_home / "state_9.sqlite") as connection:
        connection.executescript(
            f"""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT, created_at TEXT, cwd TEXT,
                git_origin_url TEXT
            );
            INSERT INTO threads VALUES (
                'old-session', 'sessions/missing-old.jsonl', '2026-08-01T00:00:00Z',
                '{first}', 'https://example.com/owner/project.git'
            );
            INSERT INTO threads VALUES (
                'new-session', 'sessions/missing-new.jsonl', '2026-08-02T00:00:00Z',
                '{second}', 'git@example.com:owner/project.git'
            );
            """
        )
    database = tmp_path / "analytics.sqlite3"

    report = index_source(
        CodexLocalAdapter(resolve_codex_home(codex_home)),
        database,
        codex_home=codex_home,
    )

    with sqlite3.connect(database) as connection:
        repository_count = connection.execute("SELECT COUNT(*) FROM repositories").fetchone()[0]
        assignments = connection.execute(
            "SELECT COUNT(DISTINCT repository_id) FROM source_sessions"
        ).fetchone()[0]
    assert report.discovered == 2
    assert repository_count == 1
    assert assignments == 1
