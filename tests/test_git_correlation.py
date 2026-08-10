from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.analytics.git import GitFilters, get_commit_report, list_session_commits
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source

runner = CliRunner()


def test_exact_originated_commit_hash_is_high_and_inherited_child_is_excluded(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    initial_hash = _initialize_repository(repository)
    commit_hash = _create_commit(
        repository,
        name="change.txt",
        content="change\n",
        message="synthetic change",
        timestamp="2026-08-01T00:10:00+00:00",
    )
    parent_records = _commit_records(commit_hash, "2026-08-01T00:09:00Z")
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    _write_records(sessions / "parent.jsonl", parent_records)
    _write_records(sessions / "child.jsonl", parent_records)
    _write_state(
        codex_home,
        repository=repository,
        initial_hash=initial_hash,
        child=True,
    )
    database = tmp_path / "analytics.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(codex_home))

    first = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "SELECT confidence, evidence_type, evidence_origin_session_id "
            "FROM session_commit_associations"
        ).fetchall()
    second = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO source_sessions(
                source_session_id, source_type, source_home, archived,
                first_ingested_at, last_ingested_at
            ) VALUES ('parent-longer', 'codex-local', ?, 0,
                      '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z')
            """,
            (str(codex_home),),
        )
        after = connection.execute(
            "SELECT confidence, evidence_type, evidence_origin_session_id "
            "FROM session_commit_associations"
        ).fetchall()
        connection.commit()
        linked_sessions = connection.execute(
            """
            SELECT sessions.source_session_id
            FROM session_commit_associations AS associations
            JOIN source_sessions AS sessions ON sessions.id = associations.session_id
            """
        ).fetchall()

    report = get_commit_report(database, codex_home=codex_home)
    assert first.new == 2
    assert second.unchanged == 2
    assert before == after
    assert report.high == 1
    assert report.medium == 0
    assert report.low == 0
    assert linked_sessions == [("parent",)]
    assert report.associations[0].commit_hash == commit_hash
    assert report.associations[0].evidence_type == "originated_commit_result_hash"
    assert len(list_session_commits(database, "parent", codex_home=codex_home)) == 1
    assert list_session_commits(database, "child", codex_home=codex_home) == ()

    cli = runner.invoke(
        app,
        [
            "commits",
            "--codex-home",
            str(codex_home),
            "--db",
            str(database),
            "--json",
        ],
    )
    assert cli.exit_code == 0
    assert json.loads(cli.stdout)["high"] == 1
    session_cli = runner.invoke(
        app,
        [
            "session",
            "parent",
            "--commits",
            "--codex-home",
            str(codex_home),
            "--db",
            str(database),
            "--json",
        ],
    )
    assert session_cli.exit_code == 0
    assert len(json.loads(session_cli.stdout)["commits"]) == 1


def test_two_concurrent_sessions_keep_timing_only_commit_candidates_low(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    initial_hash = _initialize_repository(repository)
    _create_commit(
        repository,
        name="ambiguous.txt",
        content="ambiguous\n",
        message="ambiguous timing",
        timestamp="2026-08-01T00:10:00+00:00",
    )
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    records = _commit_records(None, "2026-08-01T00:09:00Z")
    _write_records(sessions / "first.jsonl", records)
    _write_records(sessions / "second.jsonl", records)
    _write_state(
        codex_home,
        repository=repository,
        initial_hash=initial_hash,
        child=False,
        identifiers=("first", "second"),
    )
    database = tmp_path / "analytics.sqlite3"

    index_source(
        CodexLocalAdapter(resolve_codex_home(codex_home)),
        database,
        codex_home=codex_home,
    )
    report = get_commit_report(database, codex_home=codex_home)
    limited = get_commit_report(
        database,
        codex_home=codex_home,
        filters=GitFilters(limit=1),
    )

    assert report.high == 0
    assert report.medium == 0
    assert report.low == 2
    assert report.ambiguous == 2
    assert limited.low == 2
    assert limited.ambiguous == 2
    assert len(limited.associations) == 1


def _initialize_repository(repository: Path) -> str:
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Synthetic User")
    _git(repository, "config", "user.email", "synthetic@example.test")
    return _create_commit(
        repository,
        name="initial.txt",
        content="initial\n",
        message="initial",
        timestamp="2026-08-01T00:00:00+00:00",
    )


def _create_commit(
    repository: Path,
    *,
    name: str,
    content: str,
    message: str,
    timestamp: str,
) -> str:
    (repository / name).write_text(content, encoding="utf-8")
    _git(repository, "add", name)
    environment = dict(os.environ)
    environment["GIT_AUTHOR_DATE"] = timestamp
    environment["GIT_COMMITTER_DATE"] = timestamp
    _git(repository, "commit", "-m", message, environment=environment)
    return _git(repository, "rev-parse", "HEAD").stdout.strip()


def _git(
    repository: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )


def _commit_records(
    commit_hash: str | None,
    timestamp: str,
) -> tuple[dict[str, object], ...]:
    output_text = (
        f"[main {commit_hash[:8]}] synthetic change\n" if commit_hash is not None else "done\n"
    )
    return (
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "git commit -m synthetic"}),
                "call_id": "commit-call",
                "id": "commit-item",
            },
        },
        {
            "timestamp": "2026-08-01T00:10:01Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "commit-call",
                "output": json.dumps(
                    {"exit_code": 0, "wall_time_seconds": 0.1, "output": output_text}
                ),
            },
        },
    )


def _write_state(
    codex_home: Path,
    *,
    repository: Path,
    initial_hash: str,
    child: bool,
    identifiers: tuple[str, str] = ("parent", "child"),
) -> None:
    first, second = identifiers
    source = (
        '{"subagent":{"thread_spawn":{"parent_thread_id":"parent"}}}'
        if child
        else "vscode"
    )
    with sqlite3.connect(codex_home / "state_9.sqlite") as connection:
        connection.executescript(
            f"""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT, created_at TEXT, updated_at TEXT,
                archived_at TEXT, source TEXT, cwd TEXT, git_branch TEXT, git_sha TEXT
            );
            INSERT INTO threads VALUES (
                '{first}', 'sessions/{first}.jsonl', '2026-08-01T00:05:00Z',
                '2026-08-01T00:20:00Z', '2026-08-01T00:20:00Z', 'vscode',
                '{repository}', 'main', '{initial_hash}'
            );
            INSERT INTO threads VALUES (
                '{second}', 'sessions/{second}.jsonl', '2026-08-01T00:05:00Z',
                '2026-08-01T00:20:00Z', '2026-08-01T00:20:00Z', '{source}',
                '{repository}', 'main', '{initial_hash}'
            );
            """
        )
        if child:
            connection.executescript(
                """
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY, status TEXT
                );
                INSERT INTO thread_spawn_edges VALUES ('parent', 'child', 'closed');
                """
            )


def _write_records(path: Path, records: tuple[dict[str, object], ...]) -> None:
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
