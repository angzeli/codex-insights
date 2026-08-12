from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.analytics.tasks import (
    TaskBreakdown,
    TaskFilters,
    get_task_report,
    get_task_reports_by_repository,
)
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home
from codex_insights.db import open_index
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


def test_grouped_repository_task_reports_match_individual_queries(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database

    grouped = get_task_reports_by_repository(database, codex_home=codex_home)

    assert set(grouped) == {"/repos/repo-one", "/repos/repo-two", "outside-git"}
    for repository, report in grouped.items():
        individual = get_task_report(
            database,
            codex_home=codex_home,
            breakdown=TaskBreakdown.TYPE,
            filters=TaskFilters(repository=repository),
        )
        assert report.to_dict() == individual.to_dict()


def test_task_evidence_coverage_separates_missing_intent_from_rule_opportunity(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database
    with open_index(database, codex_home=codex_home) as connection:
        identifiers = dict(
            connection.execute("SELECT source_session_id, id FROM source_sessions")
        )
        prompt_id = identifiers["session-alpha-one-1111"]
        activity_id = identifiers["session-alpha-two-2222"]
        fallback_id = identifiers["session-beta-3333"]
        no_evidence_id = identifiers["session-boundary-4444"]
        connection.execute(
            "UPDATE source_sessions SET repository_name = 'orca-analysis' WHERE id = ?",
            (fallback_id,),
        )
        connection.executemany(
            """
            INSERT INTO session_tasks(
                session_id, action, domain, facets_json, confidence,
                evidence_json, taxonomy_version, updated_at
            ) VALUES (?, ?, ?, '[]', 'low', ?, 'test-v1', '2026-08-12T00:00:00Z')
            """,
            (
                (prompt_id, "unknown", "unknown", '["insufficient_origin_intent"]'),
                (activity_id, "testing", "software_engineering", '["fallback_originated_testing"]'),
                (fallback_id, "unknown", "scientific_computing", '["fallback_repository_name"]'),
                (no_evidence_id, "unknown", "unknown", '["insufficient_origin_intent"]'),
            ),
        )
        connection.execute(
            """
            INSERT INTO prompts(
                prompt_id, origin_session_id, source_ordinal, prompt_ordinal, text,
                redaction_status, original_character_count, stored_character_count,
                provenance_status, provenance_confidence, user_authorship_evidence,
                fingerprint, content_schema_version, first_indexed_at, last_indexed_at
            ) VALUES ('coverage-prompt', ?, 0, 0, 'unmatched intent', 'none', 16, 16,
                      'origin', 'high', 'synthetic', 'prompt-fingerprint',
                      'prompt-content-v1', '2026-08-12T00:00:00Z', '2026-08-12T00:00:00Z')
            """,
            (prompt_id,),
        )
        event_id = int(
            connection.execute(
                """
                INSERT INTO event_observations(
                    observed_session_id, source_ordinal, family_ordinal, event_family,
                    source_record_type, source_payload_type, fingerprint,
                    provenance_status, origin_session_id, evidence_type, confidence,
                    fingerprint_version, provenance_algorithm_version, updated_at
                ) VALUES (?, 0, 0, 'tool_call', 'response_item', 'function_call',
                          'activity-fingerprint', 'origin', ?, 'synthetic', 'high',
                          'event-fingerprint-v1', 'event-provenance-v1',
                          '2026-08-12T00:00:00Z') RETURNING id
                """,
                (activity_id, activity_id),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO tool_activity(
                event_observation_id, observed_session_id, origin_session_id,
                source_ordinal, operation_ordinal, tool_family, tool_name,
                command_category, test_scope, result_status, provenance_status,
                redacted, truncated, extraction_version, classifier_version, updated_at
            ) VALUES (?, ?, ?, 0, 0, 'shell', 'exec_command', 'testing', 'subset',
                      'success', 'origin', 0, 0, 'tool-v1', 'command-v1',
                      '2026-08-12T00:00:00Z')
            """,
            (event_id, activity_id, activity_id),
        )
        connection.execute(
            """
            INSERT INTO thread_relationships(
                source_type, source_home, relationship_type,
                parent_source_session_id, child_source_session_id,
                parent_session_id, child_session_id, last_seen_at
            ) VALUES ('codex-local', ?, 'spawn', 'session-alpha-one-1111',
                      'session-boundary-4444', ?, ?, '2026-08-12T00:00:00Z')
            """,
            (str(codex_home), prompt_id, no_evidence_id),
        )
        connection.commit()

    report = get_task_report(database, codex_home=codex_home)
    coverage = report.metrics.evidence_coverage

    assert (
        coverage.prompt_backed
        + coverage.originated_activity_only
        + coverage.fallback_only
        + coverage.no_origin_evidence
    ) == report.metrics.session_count == 4
    assert coverage.prompt_backed == 1
    assert coverage.originated_activity_only == 1
    assert coverage.fallback_only == 1
    assert coverage.no_origin_evidence == 1
    assert coverage.subagent_sessions == 1
    assert coverage.both_unknown_without_prompt_intent == 1
    assert coverage.prompt_backed_unknown_dimensions == 1

    result = runner.invoke(
        app,
        ["tasks", "--codex-home", str(codex_home), "--db", str(database)],
    )
    assert result.exit_code == 0
    assert "Origin-intent evidence coverage" in result.stdout
    assert "Prompt-backed UNKNOWN dimensions" in result.stdout


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
