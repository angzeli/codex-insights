from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.analytics.prompts import (
    PromptFilters,
    PromptSearchQueryError,
    get_prompt,
    list_prompts,
    search_prompts,
)
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source
from codex_insights.privacy import MAX_PROMPT_CHARACTERS

runner = CliRunner()


@pytest.fixture
def prompt_index(tmp_path: Path) -> tuple[Path, Path]:
    codex_home = tmp_path / "synthetic-codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    repo_one = tmp_path / "repo-one"
    repo_two = tmp_path / "repo-two"
    (repo_one / ".git").mkdir(parents=True)
    (repo_two / ".git").mkdir(parents=True)

    shared = "Fix mixed Unicode 繁體中文 and the quoted phrase safely"
    root = _user_records(shared, "shared", "2026-08-01T00:00:00Z")
    child_instruction = _user_records(
        "synthetic child instruction", "child", "2026-08-01T00:10:00Z"
    )
    grand_instruction = _user_records(
        "synthetic grandchild instruction", "grand", "2026-08-01T00:20:00Z"
    )
    sibling_a = _user_records(
        "synthetic sibling A instruction", "sibling-a", "2026-08-01T00:30:00Z"
    )
    sibling_b = _user_records(
        "synthetic sibling B instruction", "sibling-b", "2026-08-01T00:40:00Z"
    )

    _write_rollout(sessions / "root.jsonl", root)
    _write_rollout(
        sessions / "independent.jsonl", _user_records(shared, "independent", "2026-08-02T00:00:00Z")
    )
    _write_rollout(sessions / "child.jsonl", (*root, *child_instruction))
    _write_rollout(sessions / "grandchild.jsonl", (*root, *child_instruction, *grand_instruction))
    _write_rollout(sessions / "sibling-a.jsonl", (*root, *sibling_a))
    _write_rollout(sessions / "sibling-b.jsonl", (*root, *sibling_b))

    ambiguous_record = _response_user_record(
        "ambiguous repeated prompt",
        None,
        "2026-08-03T00:00:00Z",
    )
    _write_rollout(sessions / "ambiguous-root.jsonl", (ambiguous_record,))
    _write_rollout(sessions / "ambiguous-child.jsonl", (ambiguous_record,))

    private_text = (
        "Use sk-ABCDEFGHIJKLMNOPQRSTUV123456 and Authorization: Bearer secretbearervalue "
        "with password=synthetic-password-value.\n"
        "-----BEGIN PRIVATE KEY-----\nsynthetic-private-material\n-----END PRIVATE KEY-----"
    )
    _write_rollout(
        sessions / "private.jsonl",
        _user_records(private_text, "private", "2026-08-04T00:00:00Z"),
    )
    long_text = "very-long-prompt " + "x" * (MAX_PROMPT_CHARACTERS + 500)
    _write_rollout(
        sessions / "long.jsonl",
        (*_user_records(long_text, "long", "2026-08-05T00:00:00Z"), {"malformed": object()}),
        malformed_tail=True,
    )
    _write_rollout(
        sessions / "missing-text.jsonl",
        (
            {
                "timestamp": "2026-08-06T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "synthetic://image"}],
                },
            },
        ),
    )

    session_rows = (
        ("root", "root.jsonl", "2026-08-01T00:00:00Z", "vscode", str(repo_one), "model-a"),
        (
            "independent",
            "independent.jsonl",
            "2026-08-02T00:00:00Z",
            "vscode",
            str(repo_two),
            "model-b",
        ),
        (
            "child",
            "child.jsonl",
            "2026-08-01T00:10:00Z",
            _subagent_source("root"),
            str(repo_one),
            "model-child",
        ),
        (
            "grandchild",
            "grandchild.jsonl",
            "2026-08-01T00:20:00Z",
            _subagent_source("child"),
            str(repo_one),
            "model-child",
        ),
        (
            "sibling-a",
            "sibling-a.jsonl",
            "2026-08-01T00:30:00Z",
            _subagent_source("root"),
            str(repo_one),
            "model-child",
        ),
        (
            "sibling-b",
            "sibling-b.jsonl",
            "2026-08-01T00:40:00Z",
            _subagent_source("root"),
            str(repo_one),
            "model-child",
        ),
        (
            "ambiguous-root",
            "ambiguous-root.jsonl",
            "2026-08-03T00:00:00Z",
            "vscode",
            str(repo_one),
            "model-a",
        ),
        (
            "ambiguous-child",
            "ambiguous-child.jsonl",
            "2026-08-03T00:10:00Z",
            _subagent_source("ambiguous-root"),
            str(repo_one),
            "model-child",
        ),
        ("private", "private.jsonl", "2026-08-04T00:00:00Z", "vscode", str(repo_one), "model-a"),
        ("long", "long.jsonl", "2026-08-05T00:00:00Z", "vscode", str(repo_one), "model-a"),
        (
            "missing-text",
            "missing-text.jsonl",
            "2026-08-06T00:00:00Z",
            "vscode",
            str(repo_one),
            "model-a",
        ),
    )
    with sqlite3.connect(codex_home / "state_9.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at TEXT,
                source TEXT, cwd TEXT, model TEXT
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL, child_thread_id TEXT PRIMARY KEY, status TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO threads VALUES (?, 'sessions/' || ?, ?, ?, ?, ?)",
            session_rows,
        )
        connection.executemany(
            "INSERT INTO thread_spawn_edges VALUES (?, ?, 'closed')",
            (
                ("root", "child"),
                ("child", "grandchild"),
                ("root", "sibling-a"),
                ("root", "sibling-b"),
                ("ambiguous-root", "ambiguous-child"),
            ),
        )
    database = tmp_path / "prompt-index.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(codex_home))
    first = index_source(adapter, database, codex_home=codex_home)
    assert first.failed == 0
    return database, codex_home


def test_origin_aware_prompt_count_and_replay_observations(
    prompt_index: tuple[Path, Path],
) -> None:
    database, codex_home = prompt_index

    rows = list_prompts(database, codex_home=codex_home, filters=PromptFilters(limit=100))

    assert len(rows) == 5
    shared = [row for row in rows if "quoted phrase" in row.snippet]
    assert len(shared) == 2
    root = next(row for row in shared if row.origin_session_id == "root")
    independent = next(row for row in shared if row.origin_session_id == "independent")
    assert root.prompt_id != independent.prompt_id
    assert root.observation_session_count == 5
    assert root.replay_session_count == 4
    assert not any("child instruction" in row.snippet for row in rows)


def test_prompt_ids_and_rows_are_stable_across_unchanged_index(
    prompt_index: tuple[Path, Path],
) -> None:
    database, codex_home = prompt_index
    adapter = CodexLocalAdapter(resolve_codex_home(codex_home))
    with sqlite3.connect(database) as connection:
        before = tuple(connection.execute("SELECT * FROM prompts ORDER BY prompt_id"))
        observations_before = tuple(
            connection.execute(
                "SELECT * FROM prompt_observations ORDER BY prompt_id, event_observation_id"
            )
        )

    second = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        after = tuple(connection.execute("SELECT * FROM prompts ORDER BY prompt_id"))
        observations_after = tuple(
            connection.execute(
                "SELECT * FROM prompt_observations ORDER BY prompt_id, event_observation_id"
            )
        )

    assert second.updated == 0
    assert second.unchanged == 11
    assert before == after
    assert observations_before == observations_after


def test_fts_unicode_phrase_and_filters(prompt_index: tuple[Path, Path]) -> None:
    database, codex_home = prompt_index
    chinese = search_prompts(database, codex_home=codex_home, query="繁體中文")
    phrase = search_prompts(database, codex_home=codex_home, query='"quoted phrase"')
    filtered = search_prompts(
        database,
        codex_home=codex_home,
        query="quoted",
        filters=PromptFilters(
            since=datetime(2026, 8, 1, tzinfo=UTC),
            until=datetime(2026, 8, 2, tzinfo=UTC),
            repository="repo-one",
            model="model-a",
            session="root",
        ),
    )

    assert {row.origin_session_id for row in chinese} == {"root", "independent"}
    assert {row.origin_session_id for row in phrase} == {"root", "independent"}
    assert [row.origin_session_id for row in filtered] == ["root"]
    with pytest.raises(PromptSearchQueryError):
        search_prompts(database, codex_home=codex_home, query='"')


def test_redaction_private_key_and_long_prompt_policy(
    prompt_index: tuple[Path, Path],
) -> None:
    database, codex_home = prompt_index
    rows = list_prompts(database, codex_home=codex_home, filters=PromptFilters(limit=100))
    private_row = next(row for row in rows if row.redaction_status == "redacted")
    long_row = next(row for row in rows if row.redaction_status == "truncated")
    private = get_prompt(database, codex_home=codex_home, prompt_prefix=private_row.prompt_id[:20])
    long = get_prompt(database, codex_home=codex_home, prompt_prefix=long_row.prompt_id[:20])

    assert "synthetic-password-value" not in private.text
    assert "secretbearervalue" not in private.text
    assert "synthetic-private-material" not in private.text
    assert "[REDACTED" in private.text
    assert long.stored_character_count == MAX_PROMPT_CHARACTERS
    assert long.text.endswith("[TRUNCATED BY CODEX INSIGHTS]")
    database_bytes = database.read_bytes()
    assert b"synthetic-password-value" not in database_bytes
    assert b"synthetic-private-material" not in database_bytes


def test_prompt_cli_smoke_is_origin_aware_and_redacted(
    prompt_index: tuple[Path, Path],
) -> None:
    database, codex_home = prompt_index
    listing = runner.invoke(
        app,
        ["prompts", "--codex-home", str(codex_home), "--db", str(database), "--json"],
    )
    search = runner.invoke(
        app,
        [
            "search",
            '"quoted phrase"',
            "--codex-home",
            str(codex_home),
            "--db",
            str(database),
            "--json",
        ],
    )
    payload = json.loads(listing.stdout)
    detail = runner.invoke(
        app,
        [
            "prompt",
            payload[0]["prompt_id"][:20],
            "--codex-home",
            str(codex_home),
            "--db",
            str(database),
            "--json",
        ],
    )

    assert listing.exit_code == 0
    assert search.exit_code == 0
    assert detail.exit_code == 0
    assert len(payload) == 5
    assert "synthetic-password-value" not in listing.stdout + search.stdout + detail.stdout


def _user_records(text: str, label: str, timestamp: str) -> tuple[dict[str, object], ...]:
    return (
        _response_user_record(text, f"id-{label}", timestamp),
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "client_id": f"id-{label}",
                "message": text,
            },
        },
    )


def _response_user_record(
    text: str,
    identifier: str | None,
    timestamp: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }
    if identifier is not None:
        payload["id"] = identifier
    return {"timestamp": timestamp, "type": "response_item", "payload": payload}


def _write_rollout(
    path: Path,
    records: tuple[dict[str, object], ...],
    *,
    malformed_tail: bool = False,
) -> None:
    serializable = [record for record in records if record.get("malformed") is None]
    text = "".join(json.dumps(record) + "\n" for record in serializable)
    if malformed_tail:
        text += "{malformed synthetic json\n"
    path.write_text(text, encoding="utf-8")


def _subagent_source(parent: str) -> str:
    return json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": parent}}})
