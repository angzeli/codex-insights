from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home

runner = CliRunner()

_RAW_FIXTURE_VALUES = (
    "SYNTHETIC PRIVATE TITLE",
    "SYNTHETIC SECRET ARCHIVED MESSAGE",
    "SYNTHETIC SECRET COMMAND",
    "SYNTHETIC SECRET HISTORY PROMPT",
    "SYNTHETIC SECRET SESSION TITLE",
)


def test_source_audit_discovers_synthetic_layout_read_only(
    synthetic_audit_home: Path,
) -> None:
    database = synthetic_audit_home / "state_7.sqlite"
    database_before = database.read_bytes()
    files_before = _relative_files(synthetic_audit_home)

    result = CodexLocalAdapter(resolve_codex_home(synthetic_audit_home)).audit(
        sample_size=10,
        verbose=True,
    )

    assert result.history.exists is True
    assert result.history.malformed_line_count == 1
    assert result.history.records_with_session_id == 2
    assert result.rollouts.discovered_file_count == 3
    assert result.rollouts.sampled_file_count == 3
    assert result.rollouts.malformed_line_count == 1
    assert "payload.info.total_token_usage.input_tokens" in result.rollouts.token_fields
    assert any(item.name == "unknown_future_event" for item in result.rollouts.record_types)
    assert any(item.name == "exec_command" for item in result.rollouts.tool_names)

    assert len(result.state_databases) == 1
    state = result.state_databases[0]
    assert state.relative_path == "state_7.sqlite"
    assert state.likely_session_tables == ("threads",)
    assert state.rollout_references_checked == 4
    assert state.missing_rollout_references == 1
    threads = next(table for table in state.tables if table.name == "threads")
    assert threads.row_count == 4
    assert any(column.likely_role == "git_metadata" for column in threads.columns)
    assert threads.sampled_metadata_fields
    backfill = next(table for table in state.tables if table.name == "backfill_state")
    assert backfill.likely_session_table is False

    serialized = json.dumps(result.to_dict())
    assert all(raw_value not in serialized for raw_value in _RAW_FIXTURE_VALUES)
    assert database.read_bytes() == database_before
    assert _relative_files(synthetic_audit_home) == files_before


def test_source_audit_sample_size_limits_rollout_reads(synthetic_audit_home: Path) -> None:
    result = CodexLocalAdapter(resolve_codex_home(synthetic_audit_home)).audit(sample_size=1)

    assert result.rollouts.discovered_file_count == 3
    assert result.rollouts.sampled_file_count == 1


def test_source_audit_handles_missing_home(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    result = CodexLocalAdapter(resolve_codex_home(missing)).audit()

    assert result.codex_home_exists is False
    assert result.rollouts.discovered_file_count == 0
    assert result.warnings


def test_audit_source_json_cli_never_falls_back_to_real_home(
    synthetic_audit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called() -> Path:
        raise AssertionError("Path.home() must not be used with explicit --codex-home")

    monkeypatch.setattr(Path, "home", fail_if_called)
    result = runner.invoke(
        app,
        [
            "audit-source",
            "--codex-home",
            str(synthetic_audit_home),
            "--sample-size",
            "10",
            "--json",
            "--verbose",
        ],
        env={"CODEX_HOME": "/must/not/be/used"},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["codex_home"] == str(synthetic_audit_home)
    assert payload["rollouts"]["discovered_file_count"] == 3
    assert all(raw_value not in result.stdout for raw_value in _RAW_FIXTURE_VALUES)


def test_audit_source_human_output_is_aggregate_only(synthetic_audit_home: Path) -> None:
    result = runner.invoke(
        app,
        ["audit-source", "--codex-home", str(synthetic_audit_home), "--sample-size", "10"],
        env={"CODEX_HOME": "/must/not/be/used"},
    )

    assert result.exit_code == 0
    assert "Codex source audit" in result.stdout
    assert "unknown_future_event" in result.stdout
    assert "input_tokens" in result.stdout
    assert all(raw_value not in result.stdout for raw_value in _RAW_FIXTURE_VALUES)


def _relative_files(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    )
