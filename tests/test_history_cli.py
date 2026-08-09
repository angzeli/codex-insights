from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from codex_insights.cli import app

runner = CliRunner()


def test_sessions_cli_json_supports_all_filter_shapes(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database
    common = ["--db", str(database), "--codex-home", str(codex_home), "--json"]

    source = runner.invoke(app, ["sessions", "--source", "editor", *common])
    archived = runner.invoke(app, ["sessions", "--archived", *common])
    active = runner.invoke(app, ["sessions", "--active", "--limit", "2", *common])
    date_range = runner.invoke(
        app,
        ["sessions", "--since", "2026-08-07", "--until", "2026-08-08", *common],
    )

    assert source.exit_code == 0
    assert [item["session_id"] for item in json.loads(source.stdout)] == ["session-alpha-two-2222"]
    assert [item["session_id"] for item in json.loads(archived.stdout)] == [
        "session-alpha-two-2222"
    ]
    assert [item["session_id"] for item in json.loads(active.stdout)] == [
        "session-boundary-4444",
        "session-beta-3333",
    ]
    assert [item["session_id"] for item in json.loads(date_range.stdout)] == [
        "session-beta-3333",
        "session-alpha-two-2222",
    ]


def test_sessions_terminal_output_is_compact_and_hides_paths(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database

    result = runner.invoke(
        app,
        [
            "sessions",
            "--db",
            str(database),
            "--codex-home",
            str(codex_home),
            "--limit",
            "2",
        ],
        env={"COLUMNS": "70"},
    )

    assert result.exit_code == 0
    assert "Repository" in result.stdout
    assert "Tokens" in result.stdout
    assert "repo-two" in result.stdout
    assert "/repos/" not in result.stdout
    assert "/work/" not in result.stdout


def test_session_cli_prefix_json_and_lookup_errors(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database
    common = ["--db", str(database), "--codex-home", str(codex_home)]

    detail = runner.invoke(app, ["session", "session-beta", *common, "--json"])
    ambiguous = runner.invoke(app, ["session", "session-alpha", *common])
    missing = runner.invoke(app, ["session", "missing", *common])

    assert detail.exit_code == 0
    payload = json.loads(detail.stdout)
    assert payload["session_id"] == "session-beta-3333"
    assert payload["cwd"] == "/work/outside-git"
    assert payload["usage"]["total_tokens"] == 50
    assert "transcript" not in payload
    assert ambiguous.exit_code == 2
    assert "Ambiguous session prefix" in ambiguous.stderr
    assert missing.exit_code == 2
    assert "No session matches" in missing.stderr


def test_session_cli_renders_missing_tokens_as_unknown(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database

    result = runner.invoke(
        app,
        [
            "session",
            "session-alpha-two",
            "--db",
            str(database),
            "--codex-home",
            str(codex_home),
        ],
    )

    assert result.exit_code == 0
    assert "unavailable" in result.stdout
    assert "unknown" in result.stdout
    assert "malformed_lines=1" in result.stdout


def test_aggregate_commands_emit_machine_readable_json(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database
    common = ["--db", str(database), "--codex-home", str(codex_home), "--json"]

    repos = runner.invoke(app, ["repos", *common])
    models = runner.invoke(app, ["models", *common])
    stats = runner.invoke(app, ["stats", *common])

    assert repos.exit_code == 0
    assert any(
        item["repository"] == "Outside Git repositories" and item["in_git_repository"] is False
        for item in json.loads(repos.stdout)
    )
    assert models.exit_code == 0
    model_rows = json.loads(models.stdout)
    assert model_rows[0]["model"] == "model-a"
    assert any(
        item["model"] == "Unknown model" and item["total_known_tokens"] is None
        for item in model_rows
    )
    assert stats.exit_code == 0
    stats_payload = json.loads(stats.stdout)
    assert stats_payload["indexed_sessions"] == 4
    assert stats_payload["total_known_tokens"] == 150
    assert stats_payload["token_data_fraction"] == 0.5
