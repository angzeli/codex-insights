from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.analytics.tasks import get_task_report
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source
from codex_insights.prompt_features import extract_prompt_features


def test_extracts_descriptive_features_without_quality_score() -> None:
    text = """# Goal
Implement src/example.py.

## Acceptance Criteria
- Add the command.
- Run pytest tests/test_example.py.
- Create two focused commits.

## Non-goals:
- Do not add a dashboard.

Treat the source as read-only.
"""

    features = extract_prompt_features(text)

    assert features.character_length == len(text)
    assert features.line_count == text.count("\n") + 1
    assert features.structured_heading_count == 3
    assert features.has_acceptance_criteria
    assert features.requests_validation
    assert features.path_reference_count == 2
    assert features.requests_commit
    assert features.requests_multiple_commits
    assert features.has_explicit_non_goals
    assert features.has_read_only_constraint
    assert features.approximate_requirement_count >= 4
    assert not hasattr(features, "quality_score")


def test_features_support_chinese_and_avoid_negated_commit_false_positive() -> None:
    features = extract_prompt_features(
        """## 驗收標準：
- 執行 pytest 驗證。
- 請提交這些修改。
- 不要修改只讀來源。
"""
    )
    negated = extract_prompt_features("Do not commit this. The commit history is context only.")

    assert features.has_acceptance_criteria
    assert features.requests_validation
    assert features.requests_commit
    assert features.has_explicit_non_goals
    assert features.has_read_only_constraint
    assert not negated.requests_commit


def test_prompt_feature_reconciliation_and_minimum_sample_correlations(
    tmp_path: Path,
) -> None:
    codex_home, database, adapter = _feature_fixture(
        tmp_path,
        total=10,
        validation_count=5,
    )

    first = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "SELECT prompt_id, requests_validation, feature_version, updated_at "
            "FROM prompt_features ORDER BY prompt_id"
        ).fetchall()
    second = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT prompt_id, requests_validation, feature_version, updated_at "
            "FROM prompt_features ORDER BY prompt_id"
        ).fetchall()

    assert first.new == 10
    assert second.unchanged == 10
    assert before == after
    assert len(before) == 10
    assert sum(row[1] for row in before) == 5

    report = get_task_report(database, codex_home=codex_home)
    validation = next(
        item
        for item in report.prompt_feature_correlations
        if item.feature == "validation_request"
    )
    assert report.metrics.sessions_with_prompt_features == 10
    assert report.metrics.prompts_with_features == 10
    assert validation.eligible
    assert validation.with_feature.sample_size == 5
    assert validation.without_feature.sample_size == 5
    assert validation.with_feature.outcomes == (("unknown", 5),)


def test_small_prompt_feature_samples_do_not_emit_outcome_claims(tmp_path: Path) -> None:
    codex_home, database, adapter = _feature_fixture(
        tmp_path,
        total=2,
        validation_count=1,
    )
    index_source(adapter, database, codex_home=codex_home)

    report = get_task_report(database, codex_home=codex_home)
    validation = next(
        item
        for item in report.prompt_feature_correlations
        if item.feature == "validation_request"
    )

    assert not validation.eligible
    assert validation.with_feature.sample_size == 1
    assert validation.without_feature.sample_size == 1
    assert validation.with_feature.outcomes == ()
    assert validation.without_feature.outcomes == ()


def _feature_fixture(
    tmp_path: Path,
    *,
    total: int,
    validation_count: int,
) -> tuple[Path, Path, CodexLocalAdapter]:
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    thread_rows: list[tuple[str, str, str, str]] = []
    for index in range(total):
        session_id = f"session-{index}"
        path = sessions / f"{session_id}.jsonl"
        validation = " Run pytest." if index < validation_count else ""
        prompt = f"Implement Python feature {index}.{validation}"
        path.write_text(
            json.dumps(
                {
                    "timestamp": f"2026-08-01T00:{index:02d}:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": prompt},
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        thread_rows.append(
            (
                session_id,
                f"sessions/{path.name}",
                f"2026-08-01T00:{index:02d}:00Z",
                "vscode",
            )
        )
    with sqlite3.connect(codex_home / "state_12.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT, created_at TEXT, source TEXT)"
        )
        connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?)", thread_rows)
    database = tmp_path / "analytics.sqlite3"
    return codex_home, database, CodexLocalAdapter(resolve_codex_home(codex_home))
