from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from codex_insights.analytics.dashboard import build_dashboard_data
from codex_insights.analytics.queries import SessionFilters
from codex_insights.analytics.reports import ReportKind, build_analytics_report
from codex_insights.analytics.tools import ToolFilters, get_tool_activity_report
from codex_insights.analytics.usage import (
    UsageBreakdown,
    get_usage_report,
    resolve_timezone,
)
from codex_insights.cli import app
from codex_insights.exporting import ExportDataset, build_export
from codex_insights.privacy import ContentRetentionPolicy
from scripts.synthetic_corpus import SyntheticCorpusConfig, generate_synthetic_corpus

runner = CliRunner()


def test_generated_corpus_exercises_end_to_end_workflow_without_source_mutation(
    tmp_path: Path,
) -> None:
    corpus = generate_synthetic_corpus(
        tmp_path / "corpus",
        config=SyntheticCorpusConfig(
            session_count=120,
            repository_count=6,
            model_count=4,
            seed=314159,
        ),
    )
    database = tmp_path / "derived" / "index.sqlite3"
    config = tmp_path / "derived" / "config.json"
    report_markdown = tmp_path / "outputs" / "weekly.md"
    report_json = tmp_path / "outputs" / "weekly.json"
    report_html = tmp_path / "outputs" / "weekly.html"
    dashboard = tmp_path / "outputs" / "dashboard.html"
    export_json = tmp_path / "outputs" / "usage.json"
    backup = tmp_path / "outputs" / "before-reset.sqlite3"
    source_before = _source_snapshot(corpus.codex_home)
    common = ["--codex-home", str(corpus.codex_home), "--db", str(database)]

    _invoke_ok(["doctor", *common, "--json"])
    _invoke_ok(["doctor", *common, "--deep", "--json"])
    audit = _invoke_ok(
        ["audit-source", "--codex-home", str(corpus.codex_home), "--sample-size", "4", "--json"]
    )
    assert json.loads(audit.stdout)["rollouts"]["discovered_file_count"] == (
        corpus.rollout_count
    )

    _invoke_ok(["index", *common, "--config", str(config)])
    _invoke_ok(["index", *common, "--config", str(config)])
    with sqlite3.connect(database) as connection:
        last_run = connection.execute(
            """
            SELECT discovered_count, new_count, updated_count, unchanged_count,
                   skipped_count, failed_count
            FROM index_runs ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    assert last_run == (120, 0, 0, 120, 0, 0)

    _invoke_ok(["stats", *common, "--json"])
    _invoke_ok(["usage", *common, "--json", "--reconciliation"])
    _invoke_ok(["prompts", *common, "--limit", "10", "--json"])
    _invoke_ok(["search", "synthetic", *common, "--limit", "10", "--json"])
    _invoke_ok(["tools", *common, "--json"])
    _invoke_ok(["commands", *common, "--repeated", "--json"])
    commits = json.loads(_invoke_ok(["commits", *common, "--json"]).stdout)
    _invoke_ok(["outcomes", *common, "--json"])
    _invoke_ok(["tasks", *common, "--by", "type", "--json"])
    assert commits["high"] > 0
    assert commits["medium"] > 0
    assert commits["low"] > 0

    for report_format, output in (
        ("markdown", report_markdown),
        ("json", report_json),
        ("html", report_html),
    ):
        _invoke_ok(
            [
                "report",
                "weekly",
                *common,
                "--date",
                "2026-01-01",
                "--timezone",
                "UTC",
                "--format",
                report_format,
                "--output",
                str(output),
            ]
        )
    _invoke_ok(
        [
            "dashboard",
            *common,
            "--timezone",
            "UTC",
            "--config",
            str(config),
            "--output",
            str(dashboard),
        ]
    )
    privacy = json.loads(
        _invoke_ok(
            ["privacy", "inspect", *common, "--config", str(config), "--json"]
        ).stdout
    )
    assert privacy["raw_tool_outputs_stored"] is False
    _invoke_ok(
        [
            "export",
            *common,
            "--config",
            str(config),
            "--dataset",
            "usage",
            "--format",
            "json",
            "--output",
            str(export_json),
        ]
    )

    _assert_reconciliation_invariants(database, corpus.codex_home, config)
    assert _source_snapshot(corpus.codex_home) == source_before
    _invoke_ok(["backup-index", str(backup), *common])
    _invoke_ok(["reset-index", *common, "--yes", "--json"])
    assert not database.exists()
    assert _source_snapshot(corpus.codex_home) == source_before
    _invoke_ok(["index", *common, "--config", str(config)])
    assert _source_snapshot(corpus.codex_home) == source_before


def _assert_reconciliation_invariants(
    database: Path,
    codex_home: Path,
    config: Path,
) -> None:
    zone = resolve_timezone("UTC")
    filters = SessionFilters(limit=1)
    global_usage = get_usage_report(
        database,
        codex_home=codex_home,
        filters=filters,
        timezone=zone,
    )
    by_repo = get_usage_report(
        database,
        codex_home=codex_home,
        filters=filters,
        breakdown=UsageBreakdown.REPOSITORY,
        timezone=zone,
    )
    by_model = get_usage_report(
        database,
        codex_home=codex_home,
        filters=filters,
        breakdown=UsageBreakdown.MODEL,
        timezone=zone,
    )
    tools = get_tool_activity_report(
        database,
        codex_home=codex_home,
        filters=ToolFilters(limit=1_000),
    )
    report = build_analytics_report(
        database,
        codex_home=codex_home,
        kind=ReportKind.WEEKLY,
        timezone=zone,
        report_date=date(2026, 1, 1),
    )
    dashboard = build_dashboard_data(
        database,
        codex_home=codex_home,
        timezone=zone,
        config_path=config,
    )
    usage_export = build_export(
        database,
        codex_home=codex_home,
        dataset=ExportDataset.USAGE,
        policy=ContentRetentionPolicy(),
    )

    assert sum(group.metrics.total_tokens or 0 for group in by_repo.groups) == (
        global_usage.metrics.total_tokens
    )
    assert sum(group.metrics.total_tokens or 0 for group in by_model.groups) == (
        global_usage.metrics.total_tokens
    )
    assert sum(group.count for group in tools.categories) == tools.originated_commands
    assert report.overview["reconciled_tokens"] == global_usage.metrics.total_tokens
    assert dashboard.overview["reconciled_tokens"] == global_usage.metrics.total_tokens
    assert sum(
        int(record["reconciled_local_total_tokens"])
        for record in usage_export.records
        if record["reconciled_local_total_tokens"] is not None
    ) == global_usage.metrics.total_tokens

    with sqlite3.connect(database) as connection:
        logical_prompts, physical_prompts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM prompts), "
            "(SELECT COUNT(*) FROM prompt_observations)"
        ).fetchone()
        inherited_high = connection.execute(
            """
            SELECT COUNT(*)
            FROM session_commit_associations AS associations
            JOIN thread_relationships AS relationships
              ON relationships.child_session_id = associations.session_id
            WHERE associations.confidence = 'high'
              AND associations.evidence_origin_session_id != associations.session_id
            """
        ).fetchone()[0]
        inherited_outcomes = connection.execute(
            """
            SELECT COUNT(*)
            FROM session_outcomes AS outcomes
            JOIN thread_relationships AS relationships
              ON relationships.child_session_id = outcomes.session_id
            WHERE outcomes.evidence_json LIKE '%inherited%'
              AND outcomes.outcome != 'unknown'
            """
        ).fetchone()[0]
    assert physical_prompts >= logical_prompts
    assert physical_prompts > logical_prompts
    assert inherited_high == 0
    assert inherited_outcomes == 0


def _invoke_ok(arguments: list[str]) -> object:
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    return result


def _source_snapshot(root: Path) -> dict[str, tuple[int, int, int, str]]:
    snapshot: dict[str, tuple[int, int, int, str]] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        for name in sorted(files):
            path = Path(current) / name
            stat = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[str(path.relative_to(root))] = (
                stat.st_mode,
                stat.st_size,
                stat.st_mtime_ns,
                digest,
            )
    return snapshot
