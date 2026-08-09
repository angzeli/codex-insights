from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.analytics.tasks import TaskBreakdown, get_task_report
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source
from codex_insights.taxonomy import (
    TaskAction,
    TaskDomain,
    TaskEvidence,
    classify_task,
)

runner = CliRunner()


@pytest.mark.parametrize(
    ("prompt", "action", "domain"),
    (
        (
            "Implement a local Python command and add tests.",
            TaskAction.IMPLEMENTATION,
            TaskDomain.SOFTWARE_ENGINEERING,
        ),
        (
            "Fix the ORCA workflow convergence failure.",
            TaskAction.BUG_FIX,
            TaskDomain.SCIENTIFIC_COMPUTING,
        ),
        (
            "Review this implementation for correctness.",
            TaskAction.CODE_REVIEW,
            TaskDomain.SOFTWARE_ENGINEERING,
        ),
        (
            "Diagnose the CP2K calculation status.",
            TaskAction.SCIENTIFIC_STATUS_OR_DIAGNOSIS,
            TaskDomain.SCIENTIFIC_COMPUTING,
        ),
        (
            "Update the README documentation.",
            TaskAction.DOCUMENTATION,
            TaskDomain.DOCUMENTATION,
        ),
        (
            "Create a release commit and tag.",
            TaskAction.GIT_OR_RELEASE,
            TaskDomain.GIT_RELEASE,
        ),
        (
            "Research and compare approaches for this API.",
            TaskAction.RESEARCH_OR_EXPLORATION,
            TaskDomain.SOFTWARE_ENGINEERING,
        ),
        (
            "修復 ORCA 工作流的報錯，並驗證 Python 測試。",
            TaskAction.BUG_FIX,
            TaskDomain.SCIENTIFIC_COMPUTING,
        ),
        (
            "Please review 這個 Python implementation 的 correctness。",
            TaskAction.CODE_REVIEW,
            TaskDomain.SOFTWARE_ENGINEERING,
        ),
    ),
)
def test_explainable_action_and_domain_rules(
    prompt: str,
    action: TaskAction,
    domain: TaskDomain,
) -> None:
    result = classify_task(TaskEvidence(prompts=(prompt,)))

    assert result.action is action
    assert result.domain is domain
    assert result.evidence


def test_no_origin_prompt_is_unknown_and_non_goal_is_not_positive_intent() -> None:
    empty = classify_task(TaskEvidence())
    non_goal = classify_task(TaskEvidence(prompts=("Do not implement a dashboard.",)))

    assert empty.action is TaskAction.UNKNOWN
    assert empty.domain is TaskDomain.UNKNOWN
    assert non_goal.action is TaskAction.UNKNOWN


def test_identical_prompts_in_independent_roots_classify_independently() -> None:
    prompt = "Implement a Python database migration."

    first = classify_task(TaskEvidence(prompts=(prompt,)))
    second = classify_task(TaskEvidence(prompts=(prompt,)))

    assert first == second
    assert first.action is TaskAction.IMPLEMENTATION


def test_inherited_parent_intent_does_not_override_child_origin_intent(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    parent_prompt = _prompt("Implement the ORCA workflow.", "2026-08-01T00:01:00Z")
    child_prompt = _prompt(
        "Review this implementation for correctness.",
        "2026-08-01T00:02:00Z",
    )
    _write_records(sessions / "parent.jsonl", (parent_prompt,))
    _write_records(sessions / "child.jsonl", (parent_prompt, child_prompt))
    with sqlite3.connect(codex_home / "state_9.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT, created_at TEXT, source TEXT
            );
            INSERT INTO threads VALUES (
                'parent', 'sessions/parent.jsonl', '2026-08-01T00:00:00Z', 'vscode'
            );
            INSERT INTO threads VALUES (
                'child', 'sessions/child.jsonl', '2026-08-01T00:00:30Z', 'vscode'
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY, status TEXT
            );
            INSERT INTO thread_spawn_edges VALUES ('parent', 'child', 'closed');
            """
        )
    database = tmp_path / "analytics.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(codex_home))

    first = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            """
            SELECT sessions.source_session_id, tasks.action, tasks.domain,
                   tasks.evidence_json, tasks.updated_at
            FROM session_tasks AS tasks
            JOIN source_sessions AS sessions ON sessions.id = tasks.session_id
            ORDER BY sessions.source_session_id
            """
        ).fetchall()
    second = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            """
            SELECT sessions.source_session_id, tasks.action, tasks.domain,
                   tasks.evidence_json, tasks.updated_at
            FROM session_tasks AS tasks
            JOIN source_sessions AS sessions ON sessions.id = tasks.session_id
            ORDER BY sessions.source_session_id
            """
        ).fetchall()
        prompt_counts = dict(
            connection.execute(
                """
                SELECT sessions.source_session_id, COUNT(prompts.id)
                FROM source_sessions AS sessions
                LEFT JOIN prompts ON prompts.origin_session_id = sessions.id
                GROUP BY sessions.source_session_id
                """
            )
        )

    assert first.new == 2
    assert second.unchanged == 2
    assert before == after
    assert before[0][0:2] == ("child", "code_review")
    assert before[1][0:2] == ("parent", "implementation")
    assert prompt_counts == {"child": 1, "parent": 1}

    report = get_task_report(
        database,
        codex_home=codex_home,
        breakdown=TaskBreakdown.TYPE,
    )
    assert report.metrics.session_count == 2
    assert {group.key for group in report.groups} == {"code_review", "implementation"}
    cli = runner.invoke(
        app,
        [
            "tasks",
            "--by",
            "type",
            "--codex-home",
            str(codex_home),
            "--db",
            str(database),
            "--json",
        ],
    )
    assert cli.exit_code == 0
    assert json.loads(cli.stdout)["metrics"]["session_count"] == 2


def _prompt(text: str, timestamp: str) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    }


def _write_records(path: Path, records: tuple[dict[str, object], ...]) -> None:
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
