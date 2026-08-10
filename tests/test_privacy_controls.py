from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_insights import db as db_module
from codex_insights.adapters import CodexLocalAdapter
from codex_insights.analytics.prompts import search_prompts
from codex_insights.analytics.tools import get_tool_activity_report
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home
from codex_insights.db import SCHEMA_VERSION
from codex_insights.indexer import index_source
from codex_insights.path_safety import UnsafeDestinationError
from codex_insights.privacy import (
    ContentRetentionPolicy,
    PurgeTarget,
    inspect_privacy,
    load_retention_policy,
    purge_derived_content,
    save_retention_policy,
)

runner = CliRunner()


def test_persistent_policy_is_outside_source_and_round_trips(
    privacy_source_home: Path,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config" / "privacy.json"
    policy = ContentRetentionPolicy(store_prompts=False, store_command_text=False)

    saved = save_retention_policy(policy, config, codex_home=privacy_source_home)

    assert saved == config
    assert load_retention_policy(config, codex_home=privacy_source_home) == policy
    assert json.loads(config.read_text(encoding="utf-8"))["schema"] == (
        "codex-insights-config-v1"
    )
    with pytest.raises(UnsafeDestinationError):
        save_retention_policy(
            policy,
            privacy_source_home / "config.json",
            codex_home=privacy_source_home,
        )


def test_retention_policy_preserves_metadata_and_reenable_backfills(
    privacy_source_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(privacy_source_home))
    disabled = ContentRetentionPolicy(store_prompts=False, store_command_text=False)

    first = index_source(
        adapter,
        database,
        codex_home=privacy_source_home,
        retention_policy=disabled,
    )
    with sqlite3.connect(database) as connection:
        prompts = connection.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
        command = connection.execute(
            "SELECT command_text, command_fingerprint, command_operation FROM tool_activity"
        ).fetchone()
        state = connection.execute(
            "SELECT store_prompts, store_command_text FROM content_retention_state"
        ).fetchone()

    assert first.new == 1
    assert prompts == 0
    assert command[0] is None
    assert command[1]
    assert command[2] == "git_commit"
    assert state == (0, 0)
    assert get_tool_activity_report(
        database, codex_home=privacy_source_home, commands_only=True
    ).originated_commands == 1

    enabled = ContentRetentionPolicy()
    second = index_source(
        adapter,
        database,
        codex_home=privacy_source_home,
        retention_policy=enabled,
    )
    third = index_source(
        adapter,
        database,
        codex_home=privacy_source_home,
        retention_policy=enabled,
    )
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT (SELECT COUNT(*) FROM prompts), "
            "(SELECT COUNT(*) FROM tool_activity WHERE command_text IS NOT NULL)"
        ).fetchone()

    assert second.updated == 1
    assert stored == (1, 1)
    assert third.unchanged == 1
    assert third.updated == 0

    off_again = index_source(
        adapter,
        database,
        codex_home=privacy_source_home,
        retention_policy=disabled,
    )
    with sqlite3.connect(database) as connection:
        retained = connection.execute(
            "SELECT (SELECT COUNT(*) FROM prompts), "
            "(SELECT COUNT(*) FROM tool_activity WHERE command_text IS NOT NULL)"
        ).fetchone()
    assert off_again.unchanged == 1
    assert retained == (1, 1)


def test_schema_migration_completes_while_text_retention_is_disabled(
    privacy_source_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "schema-13.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 14):
            connection.executescript(db_module._MIGRATIONS[version])
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, '2026-08-10T00:00:00Z')",
                (version,),
            )
        connection.commit()

    report = index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
        retention_policy=ContentRetentionPolicy(
            store_prompts=False,
            store_command_text=False,
        ),
    )
    with sqlite3.connect(database) as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        retained = connection.execute(
            "SELECT (SELECT COUNT(*) FROM prompts), "
            "(SELECT COUNT(*) FROM tool_activity WHERE command_text IS NOT NULL)"
        ).fetchone()
        state = connection.execute(
            "SELECT store_prompts, store_command_text FROM content_retention_state"
        ).fetchone()

    assert report.failed == 0
    assert version == SCHEMA_VERSION
    assert retained == (0, 0)
    assert state == (0, 0)


def test_purge_removes_prompt_fts_and_command_text_but_keeps_analytics(
    privacy_source_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    source_database = privacy_source_home / "state_9.sqlite"
    source_before = source_database.read_bytes()
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )
    before = inspect_privacy(database, codex_home=privacy_source_home)

    prompts = purge_derived_content(
        database,
        codex_home=privacy_source_home,
        target=PurgeTarget.PROMPTS,
    )
    commands = purge_derived_content(
        database,
        codex_home=privacy_source_home,
        target=PurgeTarget.COMMAND_TEXT,
    )
    after = inspect_privacy(database, codex_home=privacy_source_home)

    assert before.counts["stored_prompt_bodies"] == 1
    assert before.counts["stored_command_text"] == 1
    assert prompts.affected_items == 1
    assert commands.affected_items == 1
    assert after.counts["stored_prompt_bodies"] == 0
    assert after.counts["logical_prompts"] == 0
    assert after.counts["stored_command_text"] == 0
    assert after.counts["command_metadata"] == 1
    assert search_prompts(database, codex_home=privacy_source_home, query="SUM") == ()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM prompts_fts").fetchone()[0] == 0
        command = connection.execute(
            "SELECT command_text, command_fingerprint, command_operation FROM tool_activity"
        ).fetchone()
    assert command[0] is None
    assert command[1]
    assert command[2] == "git_commit"
    assert get_tool_activity_report(
        database, codex_home=privacy_source_home, commands_only=True
    ).originated_commands == 1
    assert b"not persisted" not in database.read_bytes()
    assert source_database.read_bytes() == source_before


def test_privacy_cli_config_inspect_and_purge_are_content_free(
    privacy_source_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    config = tmp_path / "config.json"
    index_source(
        CodexLocalAdapter(resolve_codex_home(privacy_source_home)),
        database,
        codex_home=privacy_source_home,
    )

    configured = runner.invoke(
        app,
        [
            "privacy",
            "config",
            "--store-prompts",
            "off",
            "--store-command-text",
            "off",
            "--config",
            str(config),
            "--codex-home",
            str(privacy_source_home),
            "--json",
        ],
    )
    inspected = runner.invoke(
        app,
        [
            "privacy",
            "inspect",
            "--db",
            str(database),
            "--config",
            str(config),
            "--codex-home",
            str(privacy_source_home),
            "--json",
        ],
    )
    purged = runner.invoke(
        app,
        [
            "privacy",
            "purge",
            "prompts",
            "--db",
            str(database),
            "--codex-home",
            str(privacy_source_home),
            "--yes",
            "--json",
        ],
    )

    assert configured.exit_code == 0
    assert inspected.exit_code == 0
    assert purged.exit_code == 0
    inspection = json.loads(inspected.stdout)
    assert inspection["policy"]["store_prompts"] is False
    assert inspection["raw_tool_outputs_stored"] is False
    combined = configured.stdout + inspected.stdout + purged.stdout
    assert "synthetic-secret" not in combined
    assert "SUM(A1:A2)" not in combined
