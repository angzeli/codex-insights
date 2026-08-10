from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from typer.testing import CliRunner

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home
from codex_insights.exporting import (
    EXPORT_SCHEMA,
    ExportBundle,
    ExportDataset,
    ExportFilters,
    ExportFormat,
    build_export,
    render_export,
)
from codex_insights.indexer import index_source
from codex_insights.privacy import ContentRetentionPolicy

runner = CliRunner()


def test_usage_json_export_uses_explicit_observed_and_reconciled_names(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database

    bundle = build_export(
        database,
        codex_home=codex_home,
        dataset=ExportDataset.USAGE,
        policy=ContentRetentionPolicy(),
    )
    payload = json.loads(render_export(bundle, ExportFormat.JSON))
    record = payload["records"][0]

    assert payload["schema"] == EXPORT_SCHEMA
    assert payload["metric_semantics"]["additive_tokens"] == (
        "reconciled_local_contribution"
    )
    assert "observed_rollout_total_tokens" in record
    assert "reconciled_local_total_tokens" in record
    assert "total_tokens" not in record


def test_content_exports_obey_active_retention_without_fabricating_text(
    privacy_source_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )
    disabled = ContentRetentionPolicy(store_prompts=False, store_command_text=False)

    prompts = build_export(
        database,
        codex_home=privacy_source_home,
        dataset=ExportDataset.PROMPTS,
        policy=disabled,
    )
    commands = build_export(
        database,
        codex_home=privacy_source_home,
        dataset=ExportDataset.COMMANDS,
        policy=disabled,
    )

    assert len(prompts.records) == 1
    assert prompts.records[0]["stored_redacted_prompt_text"] is None
    assert prompts.records[0]["text_included"] is False
    assert len(commands.records) == 1
    assert commands.records[0]["stored_redacted_bounded_command_text"] is None
    assert commands.records[0]["text_included"] is False
    assert commands.records[0]["command_fingerprint"]


def test_every_export_dataset_has_stable_json_and_tabular_csv(
    privacy_source_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )

    for dataset in ExportDataset:
        bundle = build_export(
            database,
            codex_home=privacy_source_home,
            dataset=dataset,
            policy=ContentRetentionPolicy(),
        )
        payload = json.loads(render_export(bundle, ExportFormat.JSON))
        csv_text = render_export(bundle, ExportFormat.CSV)
        assert payload["schema"] == EXPORT_SCHEMA
        assert payload["dataset"] == dataset.value
        assert csv_text.startswith("export_schema_version,dataset")


def test_csv_formula_injection_is_apostrophe_escaped() -> None:
    bundle = ExportBundle(
        dataset=ExportDataset.SESSIONS,
        generated_at="2026-08-10T00:00:00Z",
        filters=ExportFilters(),
        policy=ContentRetentionPolicy(),
        records=(
            {
                "export_schema_version": EXPORT_SCHEMA,
                "dataset": "sessions",
                "session_id": "synthetic",
                "repository_name": "=SUM(A1:A2)",
                "git_branch": "  @malicious",
            },
        ),
    )
    rows = list(csv.DictReader(io.StringIO(render_export(bundle, ExportFormat.CSV))))

    assert rows[0]["repository_name"] == "'=SUM(A1:A2)"
    assert rows[0]["git_branch"] == "'  @malicious"


def test_export_cli_is_atomic_explicit_and_rejects_source_destinations(
    analytics_database: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    database, codex_home = analytics_database
    config = tmp_path / "config.json"
    safe_output = tmp_path / "exports" / "sessions.json"
    common = [
        "--db",
        str(database),
        "--codex-home",
        str(codex_home),
        "--config",
        str(config),
    ]

    safe = runner.invoke(
        app,
        [
            "export",
            "--output",
            str(safe_output),
            "--create-parents",
            *common,
        ],
    )
    existing = runner.invoke(
        app,
        ["export", "--output", str(safe_output), *common],
    )
    source_target = runner.invoke(
        app,
        [
            "export",
            "--output",
            str(codex_home / "forbidden.json"),
            *common,
        ],
    )
    database_target = runner.invoke(
        app,
        ["export", "--output", str(database), "--overwrite", *common],
    )

    assert safe.exit_code == 0
    assert json.loads(safe_output.read_text(encoding="utf-8"))["schema"] == EXPORT_SCHEMA
    assert existing.exit_code == 2
    assert source_target.exit_code == 2
    assert database_target.exit_code == 2
    assert not (codex_home / "forbidden.json").exists()
