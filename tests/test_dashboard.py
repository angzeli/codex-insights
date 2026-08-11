from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from codex_insights.analytics import SessionFilters, get_usage_report, resolve_timezone
from codex_insights.analytics.dashboard import DashboardFilters, build_dashboard_data
from codex_insights.cli import app
from codex_insights.dashboard_rendering import render_dashboard
from codex_insights.db import open_index

runner = CliRunner()


def test_dashboard_uses_shared_metrics_and_reconciles_filtered_breakdowns(
    analytics_database: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    database, codex_home = analytics_database
    with open_index(database, codex_home=codex_home) as connection:
        session_ids = dict(
            connection.execute("SELECT source_session_id, id FROM source_sessions")
        )
        connection.executemany(
            """
            INSERT INTO session_tasks(
                session_id, action, domain, facets_json, confidence,
                evidence_json, taxonomy_version, updated_at
            ) VALUES (?, ?, ?, '[]', 'high', '[]', 'test-v1', '2026-08-10T00:00:00Z')
            """,
            (
                (session_ids["session-alpha-one-1111"], "implementation", "developer_tooling"),
                (session_ids["session-beta-3333"], "code_review", "software_engineering"),
            ),
        )
        connection.commit()

    filters = DashboardFilters(
        since=datetime(2026, 8, 3, tzinfo=UTC),
        until=datetime(2026, 8, 10, tzinfo=UTC),
        task_action="code_review",
    )
    dashboard = build_dashboard_data(
        database,
        codex_home=codex_home,
        timezone=resolve_timezone("UTC"),
        filters=filters,
        config_path=tmp_path / "config.json",
        now=datetime(2026, 8, 10, tzinfo=UTC),
    )
    shared = get_usage_report(
        database,
        codex_home=codex_home,
        timezone=resolve_timezone("UTC"),
        filters=SessionFilters(
            since=filters.since,
            until=filters.until,
            task_action="code_review",
            limit=1,
        ),
    )
    payload = dashboard.to_dict()

    assert payload["overview"]["sessions"] == shared.metrics.session_count == 1
    assert payload["overview"]["active_days"] == 2
    assert payload["overview"]["reconciled_tokens"] == shared.metrics.total_tokens == 50
    assert sum(
        group["metrics"]["total_tokens"] or 0 for group in payload["repositories"]
    ) == payload["overview"]["reconciled_tokens"]
    assert sum(
        group["metrics"]["total_tokens"] or 0 for group in payload["models"]
    ) == payload["overview"]["reconciled_tokens"]
    assert payload["filters"]["task_action"] == "code_review"


def test_dashboard_html_is_offline_content_free_and_escapes_hostile_metadata(
    analytics_database: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    database, codex_home = analytics_database
    hostile = '中文專案 <script>alert("x")</script><img src=x onerror=alert(1)> & "quoted"'
    with open_index(database, codex_home=codex_home) as connection:
        connection.execute(
            """
            UPDATE source_sessions
            SET repository_name = ?, model = ?, git_branch = ?
            WHERE source_session_id = 'session-alpha-one-1111'
            """,
            (hostile, "model<style>body{display:none}</style>", "' onmouseover='x"),
        )
        connection.commit()
    data = build_dashboard_data(
        database,
        codex_home=codex_home,
        timezone=resolve_timezone("UTC"),
        config_path=tmp_path / "config.json",
    )

    rendered = render_dashboard(data)

    assert rendered.startswith("<!doctype html>")
    assert '<body class="ci-dashboard">' in rendered
    assert "--ci-canvas: #11181d" in rendered
    assert "<link" not in rendered.casefold()
    assert "<script" not in rendered.casefold()
    assert "<img" not in rendered.casefold()
    assert "http://" not in rendered and "https://" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "中文專案" in rendered
    assert "synthetic-secret-value" not in rendered
    assert "command text" in rendered.casefold()  # methodology only, never a stored command
    assert all(label in rendered for label in ("Daily", "Weekly", "Overall"))
    assert all(label in rendered for label in ("Date", "Sessions", "Tokens"))
    assert rendered.count("Session/token activity days") == 3
    assert ">Active days<" not in rendered
    assert rendered.count("Sessions started per day") == 3
    assert rendered.count("Reconciled tokens by event day") == 3
    assert "grid-template-columns: minmax(8rem, 10rem) minmax(12rem, 1fr) 9rem" in rendered
    assert "width: 9rem" in rendered


def test_empty_dashboard_and_cli_write_guards(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    database = tmp_path / "index.sqlite3"
    with open_index(database, codex_home=codex_home):
        pass
    data = build_dashboard_data(
        database,
        codex_home=codex_home,
        timezone=resolve_timezone("Asia/Shanghai"),
        config_path=tmp_path / "config.json",
    )
    output = tmp_path / "reports" / "dashboard.html"
    common = ["--db", str(database), "--codex-home", str(codex_home)]

    written = runner.invoke(
        app,
        ["dashboard", *common, "--output", str(output), "--create-parents"],
    )
    guarded = runner.invoke(
        app,
        ["dashboard", *common, "--output", str(codex_home / "forbidden.html")],
    )
    database_guard = runner.invoke(
        app,
        ["dashboard", *common, "--output", str(database)],
    )

    assert data.overview["sessions"] == 0
    assert data.overview["reconciled_tokens"] is None
    assert "No matching activity" in render_dashboard(data)
    assert written.exit_code == 0
    assert output.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert guarded.exit_code == 2
    assert not (codex_home / "forbidden.html").exists()
    assert database_guard.exit_code == 2
    assert "cannot overwrite" in database_guard.stderr


def test_10k_dashboard_stays_aggregated_and_practical(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    database = tmp_path / "large.sqlite3"
    indexed_at = "2026-08-10T00:00:00Z"
    with open_index(database, codex_home=codex_home) as connection:
        connection.executemany(
            """
            INSERT INTO source_sessions(
                source_session_id, source_type, source_home, client_source,
                started_at, apparent_ended_at, repository_root, repository_name,
                model, archived, first_ingested_at, last_ingested_at
            ) VALUES (?, 'codex-local', ?, 'synthetic', ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                (
                    f"large-{index:05d}",
                    str(codex_home),
                    f"2026-08-{(index % 9) + 1:02d}T{index % 24:02d}:00:00Z",
                    f"2026-08-{(index % 9) + 1:02d}T{index % 24:02d}:10:00Z",
                    f"/synthetic/repo-{index % 20:02d}",
                    f"repo-{index % 20:02d}",
                    f"model-{index % 4}",
                    indexed_at,
                    indexed_at,
                )
                for index in range(10_000)
            ),
        )
        rows = connection.execute("SELECT id FROM source_sessions ORDER BY id").fetchall()
        connection.executemany(
            """
            INSERT INTO usage(
                source_session_id, usage_semantics, input_tokens, output_tokens,
                total_tokens, token_update_count, updated_at
            ) VALUES (?, 'cumulative_total', ?, ?, ?, 1, ?)
            """,
            (
                (int(row[0]), 80 + index, 20, 100 + index, indexed_at)
                for index, row in enumerate(rows)
            ),
        )
        connection.commit()

    started = time.perf_counter()
    data = build_dashboard_data(
        database,
        codex_home=codex_home,
        timezone=resolve_timezone("UTC"),
        config_path=tmp_path / "config.json",
    )
    rendered = render_dashboard(data)
    elapsed = time.perf_counter() - started

    assert data.overview["sessions"] == 10_000
    assert len(data.repositories) == 20
    assert len(data.models) == 4
    assert len(rendered.encode("utf-8")) < 500_000
    assert elapsed < 20  # gross guard only; normal timing is substantially lower
