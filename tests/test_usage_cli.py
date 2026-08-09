from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codex_insights.cli import app

runner = CliRunner()


def test_usage_cli_summary_and_json_breakdowns(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database
    common = [
        "--db",
        str(database),
        "--codex-home",
        str(codex_home),
        "--timezone",
        "UTC",
    ]

    summary = runner.invoke(app, ["usage", *common])
    repos = runner.invoke(app, ["usage", "--by", "repo", "--top", "2", "--json", *common])
    days = runner.invoke(
        app,
        [
            "usage",
            "--by",
            "day",
            "--since",
            "2026-08-08",
            "--until",
            "2026-08-09",
            "--json",
            *common,
        ],
    )
    reconciliation = runner.invoke(
        app,
        ["usage", "--reconciliation", "--json", *common],
    )

    assert summary.exit_code == 0
    assert "150 reconciled tokens across 2/4 sessions" in summary.stdout
    assert "Reasoning output" in summary.stdout
    assert "unknown" in summary.stdout
    assert repos.exit_code == 0
    repo_payload = json.loads(repos.stdout)
    assert repo_payload["breakdown"] == "repo"
    assert [group["label"] for group in repo_payload["groups"]] == [
        "repo-one",
        "Outside Git repositories",
    ]
    assert repo_payload["metrics"]["coverage"]["total_tokens"] == 2
    assert days.exit_code == 0
    day_payload = json.loads(days.stdout)
    assert [group["label"] for group in day_payload["groups"]] == [
        "2026-08-08",
        "2026-08-09",
    ]
    assert reconciliation.exit_code == 0
    reconciliation_payload = json.loads(reconciliation.stdout)["reconciliation"]
    assert reconciliation_payload["observed_rollout_tokens"] == 150
    assert reconciliation_payload["inherited_replayed_tokens"] == 0
    assert reconciliation_payload["reconciled_tokens"] == 150
    assert reconciliation_payload["root_threads"] == 4


def test_usage_cli_filters_and_validation(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database
    common = ["--db", str(database), "--codex-home", str(codex_home)]

    filtered = runner.invoke(
        app,
        ["usage", "--repo", "repo-one", "--model", "model-a", "--json", *common],
    )
    invalid_top = runner.invoke(app, ["usage", "--by", "day", "--top", "2", *common])
    invalid_timezone = runner.invoke(
        app,
        ["usage", "--timezone", "Mars/Olympus_Mons", *common],
    )

    assert filtered.exit_code == 0
    assert json.loads(filtered.stdout)["metrics"]["session_count"] == 1
    assert invalid_top.exit_code == 2
    assert "repository or model" in invalid_top.stderr
    assert invalid_timezone.exit_code == 2
    assert "Unknown timezone" in invalid_timezone.stderr
