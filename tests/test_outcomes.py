from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.analytics.outcomes import get_outcome_report
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source
from codex_insights.models import SessionOutcome
from codex_insights.outcomes import (
    LifecycleStatus,
    OutcomeConfidence,
    OutcomeEvidence,
    OutcomeEvidenceKind,
    classify_outcome,
)

runner = CliRunner()


@pytest.mark.parametrize(
    ("kinds", "expected", "confidence"),
    (
        (
            (OutcomeEvidenceKind.VALIDATION_PASS, OutcomeEvidenceKind.HIGH_COMMIT),
            SessionOutcome.SUCCESS,
            OutcomeConfidence.MEDIUM,
        ),
        (
            (OutcomeEvidenceKind.VALIDATION_PASS,),
            SessionOutcome.SUCCESS,
            OutcomeConfidence.MEDIUM,
        ),
        (
            (OutcomeEvidenceKind.HIGH_COMMIT,),
            SessionOutcome.SUCCESS,
            OutcomeConfidence.MEDIUM,
        ),
        (
            (OutcomeEvidenceKind.VALIDATION_FAIL, OutcomeEvidenceKind.VALIDATION_PASS),
            SessionOutcome.SUCCESS_WITH_WARNINGS,
            OutcomeConfidence.MEDIUM,
        ),
        (
            (OutcomeEvidenceKind.EDIT, OutcomeEvidenceKind.VALIDATION_FAIL),
            SessionOutcome.FAILED,
            OutcomeConfidence.HIGH,
        ),
        (
            (OutcomeEvidenceKind.ABORT,),
            SessionOutcome.ABANDONED,
            OutcomeConfidence.HIGH,
        ),
        (
            (OutcomeEvidenceKind.EDIT,),
            SessionOutcome.PARTIAL,
            OutcomeConfidence.LOW,
        ),
        ((), SessionOutcome.UNKNOWN, OutcomeConfidence.LOW),
    ),
)
def test_pure_outcome_scenarios(
    kinds: tuple[OutcomeEvidenceKind, ...],
    expected: SessionOutcome,
    confidence: OutcomeConfidence,
) -> None:
    evidence = tuple(
        OutcomeEvidence(sequence=index, kind=kind) for index, kind in enumerate(kinds)
    )

    result = classify_outcome(evidence)

    assert result.outcome is expected
    assert result.confidence is confidence


def test_expected_probe_failure_is_not_itself_failure_evidence() -> None:
    result = classify_outcome(())

    assert result.outcome is SessionOutcome.UNKNOWN
    assert result.evidence == ("no_originated_evidence",)


def test_turn_completion_is_lifecycle_not_task_success() -> None:
    result = classify_outcome(
        (
            OutcomeEvidence(sequence=1, kind=OutcomeEvidenceKind.EDIT),
            OutcomeEvidence(sequence=2, kind=OutcomeEvidenceKind.TASK_COMPLETE),
        )
    )

    assert result.outcome is SessionOutcome.PARTIAL
    assert result.confidence is OutcomeConfidence.LOW
    assert result.lifecycle_status is LifecycleStatus.TURN_COMPLETED
    assert result.strongly_evidenced is False


def test_completion_without_task_evidence_remains_unknown() -> None:
    result = classify_outcome(
        (OutcomeEvidence(sequence=1, kind=OutcomeEvidenceKind.TASK_COMPLETE),)
    )

    assert result.outcome is SessionOutcome.UNKNOWN
    assert result.lifecycle_status is LifecycleStatus.TURN_COMPLETED
    assert result.evidence == ("turn_completed_without_task_outcome_evidence",)


def test_inherited_validation_does_not_classify_child_and_index_is_idempotent(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    parent_records = _validation_records("parent-call", exit_code=0)
    _write_records(sessions / "parent.jsonl", parent_records)
    _write_records(sessions / "child.jsonl", parent_records)
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
                'child', 'sessions/child.jsonl', '2026-08-01T00:01:00Z',
                '{"subagent":{"thread_spawn":{"parent_thread_id":"parent"}}}'
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
            SELECT sessions.source_session_id, outcomes.outcome, outcomes.confidence,
                   outcomes.evidence_json, outcomes.updated_at
            FROM session_outcomes AS outcomes
            JOIN source_sessions AS sessions ON sessions.id = outcomes.session_id
            ORDER BY sessions.source_session_id
            """
        ).fetchall()
    second = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            """
            SELECT sessions.source_session_id, outcomes.outcome, outcomes.confidence,
                   outcomes.evidence_json, outcomes.updated_at
            FROM session_outcomes AS outcomes
            JOIN source_sessions AS sessions ON sessions.id = outcomes.session_id
            ORDER BY sessions.source_session_id
            """
        ).fetchall()

    assert first.new == 2
    assert second.unchanged == 2
    assert before == after
    assert before[0][0:2] == ("child", "unknown")
    assert before[1][0:2] == ("parent", "success")

    report = get_outcome_report(database, codex_home=codex_home)
    assert report.session_count == 2
    assert report.classifiable_count == 1
    assert report.strongly_evidenced_count == 1
    assert report.unknown_count == 1
    cli = runner.invoke(
        app,
        [
            "outcomes",
            "--codex-home",
            str(codex_home),
            "--db",
            str(database),
            "--json",
        ],
    )
    assert cli.exit_code == 0
    payload = json.loads(cli.stdout)
    assert payload["unknown_count"] == 1
    assert payload["strongly_evidenced_count"] == 1
    assert payload["lifecycle_semantics"] == "originated_turn_lifecycle_evidence"


def _validation_records(
    call_id: str,
    *,
    exit_code: int,
) -> tuple[dict[str, object], ...]:
    return (
        {
            "timestamp": "2026-08-01T00:02:00Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "pytest tests/test_synthetic.py"}),
                "call_id": call_id,
                "id": call_id + "-item",
            },
        },
        {
            "timestamp": "2026-08-01T00:02:01Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(
                    {"exit_code": exit_code, "wall_time_seconds": 0.1, "output": "synthetic"}
                ),
            },
        },
    )


def _write_records(path: Path, records: tuple[dict[str, object], ...]) -> None:
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
