from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.adapters.codex_events import extract_event
from codex_insights.analytics.tools import (
    ToolFilters,
    get_tool_activity_report,
    originated_command_counts,
)
from codex_insights.cli import app
from codex_insights.command_normalization import normalize_command
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source
from codex_insights.models import CommandCategory, ToolResultStatus
from codex_insights.models import TestScope as CommandTestScope

runner = CliRunner()


def test_command_privacy_and_test_scope_are_conservative() -> None:
    secret = normalize_command(
        "TOKEN=secret-value curl 'https://example.test/api?token=url-secret' "
        "-H 'Authorization: Bearer header-secret' --password cli-secret"
    )
    full = normalize_command("pytest")
    file_scope = normalize_command("pytest tests/test_tools.py")
    subset = normalize_command("python -m pytest tests/test_tools.py::test_one")
    heredoc = normalize_command("python - <<'PY'\n" + "private = 'x'\n" * 200 + "PY\n")

    assert "secret-value" not in secret.text
    assert "url-secret" not in secret.text
    assert "header-secret" not in secret.text
    assert "cli-secret" not in secret.text
    assert secret.redacted
    assert full.test_scope is CommandTestScope.FULL_SUITE
    assert file_scope.test_scope is CommandTestScope.FILE
    assert subset.test_scope is CommandTestScope.SUBSET
    assert heredoc.redacted
    assert "private" not in heredoc.text


@pytest.mark.parametrize(
    ("command", "executable", "category"),
    [
        ("git status", "git", CommandCategory.GIT_INSPECTION),
        ("git commit -m subject", "git", CommandCategory.GIT_MUTATION),
        (
            "/opt/homebrew/bin/python3 script.py",
            "python3",
            CommandCategory.PYTHON_EXECUTION,
        ),
        ("MPLBACKEND=Agg python script.py", "python", CommandCategory.PYTHON_EXECUTION),
        (
            "OMP_NUM_THREADS=8 MKL_NUM_THREADS=4 cp2k.psmp -i input.inp",
            "cp2k.psmp",
            CommandCategory.SCIENTIFIC_COMPUTATION,
        ),
        ("env FOO=bar python script.py", "python", CommandCategory.PYTHON_EXECUTION),
        (
            "/usr/bin/env -i PATH=/bin python -m pytest",
            "python",
            CommandCategory.TESTING,
        ),
        ("sudo -u runner pytest", "pytest", CommandCategory.TESTING),
        ("time -p pytest", "pytest", CommandCategory.TESTING),
        ("nohup pytest", "pytest", CommandCategory.TESTING),
        ("python -m pytest", "python", CommandCategory.TESTING),
        ("uv run python script.py", "uv", CommandCategory.PYTHON_EXECUTION),
        (
            "uv run --compile-bytecode python script.py",
            "uv",
            CommandCategory.PYTHON_EXECUTION,
        ),
        ("uv run pytest", "uv", CommandCategory.TESTING),
        ("uv run ruff check .", "uv", CommandCategory.LINTING),
        ("uv run mypy src", "uv", CommandCategory.TYPE_CHECKING),
        ("cd repo && pytest", "pytest", CommandCategory.TESTING),
        ("rg pattern . | head", "rg", CommandCategory.TEXT_SEARCH),
        ("git status && git diff", "git", CommandCategory.GIT_INSPECTION),
        ("git status || git diff", "git", CommandCategory.GIT_INSPECTION),
        ('"/Applications/My Tool/bin/tool" --flag', "tool", CommandCategory.OTHER),
        ("bash script.sh", "bash", CommandCategory.OTHER),
        ("orca input.inp", "orca", CommandCategory.SCIENTIFIC_COMPUTATION),
        ("vasp_std", "vasp_std", CommandCategory.SCIENTIFIC_COMPUTATION),
        ("multiwfn input.txt", "multiwfn", CommandCategory.SCIENTIFIC_COMPUTATION),
        ("python ase_workflow.py", "python", CommandCategory.SCIENTIFIC_COMPUTATION),
        ("pgrep -f orca", "pgrep", CommandCategory.PROCESS_STATUS_MONITORING),
        ("find . -name '*.py'", "find", CommandCategory.FILESYSTEM_INSPECTION),
    ],
)
def test_shell_head_resolution_is_conservative_and_preserves_categories(
    command: str,
    executable: str,
    category: CommandCategory,
) -> None:
    normalized = normalize_command(command)

    assert normalized.executable == executable
    assert normalized.category is category


@pytest.mark.parametrize(
    "command",
    [
        "cd repo",
        "for file in *.py; do python \"$file\"; done",
        "while pgrep -f orca; do sleep 5; done",
        "if pytest; then echo ok; fi",
        "(cd repo && ruff check .)",
        "-f format pytest",
        "-v pytest",
        "command -v python",
        "env -S 'python -m pytest'",
        "python 'unterminated",
    ],
)
def test_ambiguous_or_non_executable_shell_heads_remain_unknown(command: str) -> None:
    normalized = normalize_command(command)

    assert normalized.executable is None
    assert normalized.category is CommandCategory.UNKNOWN


def test_heredoc_body_is_not_parsed_as_an_executable() -> None:
    normalized = normalize_command("python - <<'PY'\nfor = 'private'\nPY\n")

    assert normalized.executable == "python"
    assert normalized.category is CommandCategory.PYTHON_EXECUTION
    assert "private" not in normalized.text


def test_known_non_executable_heads_are_never_emitted() -> None:
    invalid = {"-f", "-v", "for", "while", "if", "then", "do", "done"}
    commands = (
        "-f value pytest",
        "-v pytest",
        "for item in one; do echo $item; done",
        "while true; do sleep 1; done",
        "if true; then pytest; fi",
    )

    assert {normalize_command(command).executable for command in commands}.isdisjoint(invalid)


def test_repeated_command_fingerprint_does_not_collapse_by_executable() -> None:
    status = normalize_command("git status")
    diff = normalize_command("git diff")

    assert status.executable == diff.executable == "git"
    assert status.fingerprint != diff.fingerprint


def test_new_exec_wrapper_and_structured_result_are_normalized_without_output() -> None:
    call = extract_event(
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call-1",
                "input": (
                    "const r = await tools.exec_command("
                    '{"cmd":"pytest tests/test_tools.py::test_one","workdir":"/tmp"}'
                    "); text(r.output);"
                ),
            },
        },
        source_ordinal=4,
        family_ordinal=0,
        occurred_at=None,
    )
    result = extract_event(
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "exit_code": 1,
                                "wall_time_seconds": 0.25,
                                "output": "raw output is intentionally discarded",
                            }
                        ),
                    }
                ],
            },
        },
        source_ordinal=5,
        family_ordinal=0,
        occurred_at=None,
    )

    assert call is not None
    assert len(call.tool_calls) == 1
    tool = call.tool_calls[0]
    assert tool.tool_name == "exec_command"
    assert tool.command_category is CommandCategory.TESTING
    assert tool.test_scope is CommandTestScope.SUBSET
    assert result is not None and result.tool_result is not None
    assert result.tool_result.status is ToolResultStatus.FAILURE
    assert result.tool_result.exit_code == 1
    assert result.tool_result.duration_seconds == 0.25


def test_indexed_tool_activity_is_lineage_aware_and_reconciles(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    repository = tmp_path / "repo-one"
    (repository / ".git").mkdir(parents=True)
    parent_call = _call("git status", "parent-call", "parent-item", "2026-08-01T00:01:00Z")
    parent_output = _output("parent-call", 0, "2026-08-01T00:01:01Z")
    child_call = _call("git status", "child-call", "child-item", "2026-08-01T00:02:00Z")
    child_output = _output("child-call", 1, "2026-08-01T00:02:01Z")
    orphan_output = _output("missing-call", 0, "2026-08-01T00:02:02Z")
    _write_records(sessions / "parent.jsonl", (parent_call, parent_output))
    _write_records(
        sessions / "child.jsonl",
        (parent_call, parent_output, child_call, child_output, orphan_output),
    )
    with sqlite3.connect(codex_home / "state_9.sqlite") as connection:
        connection.executescript(
            f"""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at TEXT,
                source TEXT, cwd TEXT, model TEXT
            );
            INSERT INTO threads VALUES (
                'parent', 'sessions/parent.jsonl', '2026-08-01T00:00:00Z',
                'vscode', {json.dumps(str(repository))}, 'model-a'
            );
            INSERT INTO threads VALUES (
                'child', 'sessions/child.jsonl', '2026-08-01T00:00:30Z',
                '{{"subagent":{{"thread_spawn":{{"parent_thread_id":"parent"}}}}}}',
                {json.dumps(str(repository))}, 'model-b'
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL, child_thread_id TEXT PRIMARY KEY, status TEXT
            );
            INSERT INTO thread_spawn_edges VALUES ('parent', 'child', 'closed');
            """
        )
    database = tmp_path / "analytics.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(codex_home))

    first = index_source(adapter, database, codex_home=codex_home)
    second = index_source(adapter, database, codex_home=codex_home)
    report = get_tool_activity_report(
        database,
        codex_home=codex_home,
        filters=ToolFilters(limit=20),
        commands_only=True,
        repeated=True,
    )

    assert first.new == 2
    assert second.unchanged == 2
    assert second.updated == 0
    assert report.provenance.observed == 3
    assert report.provenance.originated == 2
    assert report.provenance.inherited == 1
    assert report.originated_commands == 2
    assert report.known_results == 2
    assert report.failed_results == 1
    assert report.repeated_commands[0].invocation_count == 2
    assert report.repeated_commands[0].session_count == 2

    by_repo = originated_command_counts(
        database, codex_home=codex_home, dimension="repo"
    )
    by_model = originated_command_counts(
        database, codex_home=codex_home, dimension="model"
    )
    by_category = originated_command_counts(
        database, codex_home=codex_home, dimension="category"
    )
    assert sum(by_repo.values()) == report.originated_commands
    assert sum(by_model.values()) == report.originated_commands
    assert sum(by_category.values()) == report.originated_commands

    with sqlite3.connect(database) as connection:
        child_rows = connection.execute(
            """
            SELECT activity.provenance_status, activity.result_status
            FROM tool_activity AS activity
            JOIN source_sessions AS sessions ON sessions.id = activity.observed_session_id
            WHERE sessions.source_session_id = 'child'
            ORDER BY activity.source_ordinal
            """
        ).fetchall()
        stored_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_activity)")
        }
    assert child_rows == [("inherited_prefix", "success"), ("origin", "failure")]
    assert "output" not in stored_columns

    cli = runner.invoke(
        app,
        [
            "commands",
            "--codex-home",
            str(codex_home),
            "--db",
            str(database),
            "--repeated",
            "--json",
        ],
    )
    assert cli.exit_code == 0
    assert json.loads(cli.stdout)["originated_commands"] == 2


def test_parser_version_reclassifies_persisted_executables_without_reset(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    repository = tmp_path / "repo-one"
    (repository / ".git").mkdir(parents=True)
    _write_records(
        sessions / "session.jsonl",
        (
            _call(
                "for file in *.py; do python \"$file\"; done",
                "loop-call",
                "loop-item",
                "2026-08-01T00:01:00Z",
            ),
            _call(
                "cd repo && pytest",
                "test-call",
                "test-item",
                "2026-08-01T00:02:00Z",
            ),
        ),
    )
    with sqlite3.connect(codex_home / "state_9.sqlite") as connection:
        connection.executescript(
            f"""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at TEXT,
                source TEXT, cwd TEXT, model TEXT
            );
            INSERT INTO threads VALUES (
                'session', 'sessions/session.jsonl', '2026-08-01T00:00:00Z',
                'vscode', {json.dumps(str(repository))}, 'model-a'
            );
            """
        )
    database = tmp_path / "analytics.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(codex_home))

    first = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE ingestion_state SET parser_version = 'codex-source-parser-v8'"
        )
        connection.execute(
            """
            UPDATE tool_activity
            SET executable = CASE source_ordinal WHEN 0 THEN 'for' ELSE 'cd' END,
                command_category = 'other',
                classifier_version = 'command-classifier-v1'
            """
        )
        connection.commit()

    upgraded = index_source(adapter, database, codex_home=codex_home)
    unchanged = index_source(adapter, database, codex_home=codex_home)

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            """
            SELECT source_ordinal, executable, command_category, classifier_version
            FROM tool_activity
            ORDER BY source_ordinal
            """
        ).fetchall()
    assert first.new == 1
    assert upgraded.updated == 1
    assert upgraded.new == 0
    assert rows == [
        (0, None, "unknown", "command-classifier-v2"),
        (1, "pytest", "testing", "command-classifier-v2"),
    ]
    assert unchanged.unchanged == 1
    assert unchanged.updated == 0


def _call(command: str, call_id: str, item_id: str, timestamp: str) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": command}),
            "call_id": call_id,
            "id": item_id,
        },
    }


def _output(call_id: str, exit_code: int, timestamp: str) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(
                {
                    "exit_code": exit_code,
                    "wall_time_seconds": 0.1,
                    "output": "synthetic raw output that must not be persisted",
                }
            ),
        },
    }


def _write_records(path: Path, records: tuple[dict[str, object], ...]) -> None:
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
