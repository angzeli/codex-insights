from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from codex_insights.analytics import SessionFilters, get_usage_report, resolve_timezone
from codex_insights.analytics.reports import ReportKind, build_analytics_report
from codex_insights.cli import app
from codex_insights.db import open_index
from codex_insights.reporting import ReportFormat, render_report

runner = CliRunner()


def test_weekly_report_uses_shared_metrics_and_reconciles_breakdowns(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database
    zone = resolve_timezone("UTC")
    report = build_analytics_report(
        database,
        codex_home=codex_home,
        kind=ReportKind.WEEKLY,
        timezone=zone,
        report_date=datetime(2026, 8, 8, tzinfo=UTC).date(),
        now=datetime(2026, 8, 9, 12, tzinfo=UTC),
    )
    shared = get_usage_report(
        database,
        codex_home=codex_home,
        filters=SessionFilters(
            since=datetime(2026, 8, 3, tzinfo=UTC),
            until=datetime(2026, 8, 10, tzinfo=UTC),
            limit=1,
        ),
        timezone=zone,
    )
    payload = report.to_dict()

    assert payload["schema_version"] == "codex-insights-report-v1"
    assert payload["application_version"] == "1.1.0"
    assert payload["period"]["start"] == "2026-08-03"
    assert payload["period"]["end"] == "2026-08-09"
    assert payload["overview"]["sessions"] == shared.metrics.session_count == 3
    assert payload["overview"]["active_days"] == 3
    assert payload["overview"]["reconciled_tokens"] == shared.metrics.total_tokens == 50
    assert sum(
        group["metrics"]["total_tokens"] or 0 for group in payload["repositories"]
    ) == payload["overview"]["reconciled_tokens"]
    assert sum(
        group["metrics"]["total_tokens"] or 0 for group in payload["models"]
    ) == payload["overview"]["reconciled_tokens"]
    assert sum(
        group["metrics"]["reconciled_tokens"] or 0
        for group in payload["tasks"]["actions"]["groups"]
    ) == payload["overview"]["reconciled_tokens"]
    assert sum(
        group["count"] for group in payload["tools"]["command_categories"]
    ) == payload["tools"]["originated_commands"]
    assert payload["data_quality"]["unknown_outcomes"] == 3
    assert payload["previous_period"]["coverage_comparable"] is False


def test_report_formats_are_parseable_offline_and_escape_unsafe_names(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database
    with open_index(database, codex_home=codex_home) as connection:
        connection.execute(
            """
            INSERT INTO source_sessions(
                source_session_id, source_type, source_home, client_source,
                started_at, repository_root, repository_name, archived,
                first_ingested_at, last_ingested_at
            ) VALUES (?, 'codex-local', ?, 'cli', ?, ?, ?, 0, ?, ?)
            """,
            (
                "unsafe-unicode-session",
                str(codex_home),
                "2026-08-08T02:00:00Z",
                "/repos/unsafe-unicode",
                "<script>alert('x')</script> 中文專案 " + "長" * 90,
                "2026-08-08T02:00:00Z",
                "2026-08-08T02:00:00Z",
            ),
        )
        session_id = int(
            connection.execute(
                "SELECT id FROM source_sessions WHERE source_session_id = ?",
                ("unsafe-unicode-session",),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO usage(
                source_session_id, usage_semantics, token_update_count, updated_at
            ) VALUES (?, 'unavailable', 0, '2026-08-08T02:00:00Z')
            """,
            (session_id,),
        )
        connection.commit()
    report = build_analytics_report(
        database,
        codex_home=codex_home,
        kind=ReportKind.WEEKLY,
        timezone=resolve_timezone("UTC"),
        report_date=datetime(2026, 8, 8, tzinfo=UTC).date(),
        now=datetime(2026, 8, 9, 12, tzinfo=UTC),
    )

    markdown = render_report(report, ReportFormat.MARKDOWN)
    json_text = render_report(report, ReportFormat.JSON)
    html_text = render_report(report, ReportFormat.HTML)

    assert "## Data quality" in markdown
    assert "Session/token activity days" in markdown
    assert "| Active days |" not in markdown
    json_payload = json.loads(json_text)
    assert json_payload["overview"]["sessions"] == 4
    assert json_payload["overview"]["active_days"] == 3
    assert html_text.startswith("<!doctype html>")
    assert '<body class="ci-report">' in html_text
    assert "--ci-canvas: #11181d" in html_text
    assert "<link" not in html_text.casefold()
    assert "Session/token activity days" in html_text
    assert ">Active days<" not in html_text
    assert "<script" not in html_text.casefold()
    assert "&lt;script&gt;" in html_text
    assert "https://" not in html_text
    assert "http://" not in html_text
    assert "<html" in html_text and "</html>" in html_text
    assert html_text.count("<table>") == html_text.count('<caption class="sr-only">')
    assert html_text.count("<table>") > 0
    assert "<th>" not in html_text
    assert '<th scope="col">' in html_text
    assert '<caption class="sr-only">Repository activity</caption>' in html_text
    assert '<caption class="sr-only">Task outcomes</caption>' in html_text


def test_empty_and_sparse_monthly_reports_remain_explicit(tmp_path: Path) -> None:
    codex_home = tmp_path / "synthetic-codex-home"
    database = tmp_path / "empty.sqlite3"
    with open_index(database, codex_home=codex_home):
        pass

    report = build_analytics_report(
        database,
        codex_home=codex_home,
        kind=ReportKind.MONTHLY,
        timezone=resolve_timezone("Asia/Shanghai"),
        report_date=datetime(2026, 8, 1, tzinfo=UTC).date(),
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    payload = json.loads(render_report(report, ReportFormat.JSON))

    assert payload["overview"]["sessions"] == 0
    assert payload["overview"]["reconciled_tokens"] is None
    assert payload["data_quality"]["unknown_outcomes"] == 0
    assert payload["activity"] == []
    assert payload["weekly_activity"] == []


def test_report_cli_stdout_output_file_and_codex_home_write_guard(
    analytics_database: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    database, codex_home = analytics_database
    common = [
        "--date",
        "2026-08-08",
        "--timezone",
        "UTC",
        "--db",
        str(database),
        "--codex-home",
        str(codex_home),
    ]
    stdout = runner.invoke(app, ["report", "weekly", *common, "--format", "json"])
    output = tmp_path / "reports" / "month.html"
    written = runner.invoke(
        app,
        [
            "report",
            "monthly",
            *common,
            "--format",
            "html",
            "--output",
            str(output),
        ],
    )
    guarded = runner.invoke(
        app,
        [
            "report",
            "weekly",
            *common,
            "--output",
            str(codex_home / "forbidden.md"),
        ],
    )
    database_guard = runner.invoke(
        app,
        ["report", "weekly", *common, "--output", str(database)],
    )

    assert stdout.exit_code == 0
    assert json.loads(stdout.stdout)["report_kind"] == "weekly"
    assert written.exit_code == 0
    assert output.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert guarded.exit_code == 2
    assert "cannot be inside Codex home" in guarded.stderr
    assert not (codex_home / "forbidden.md").exists()
    assert database_guard.exit_code == 2
    assert "cannot overwrite" in database_guard.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
