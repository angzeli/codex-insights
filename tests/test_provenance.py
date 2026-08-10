from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

import codex_insights.indexer as indexer_module
from codex_insights.adapters import CodexLocalAdapter
from codex_insights.cli import app
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source
from codex_insights.models import (
    EventFamily,
    EventProvenanceStatus,
    NormalizedEventObservation,
)
from codex_insights.provenance import (
    EventProvenanceAssessment,
    assess_event_provenance,
    mirrored_user_observations,
)

runner = CliRunner()


def _event(
    name: str,
    ordinal: int,
    family: EventFamily = EventFamily.USER_MESSAGE,
    *,
    stable_id: str | None = None,
    payload_type: str | None = None,
) -> NormalizedEventObservation:
    return NormalizedEventObservation(
        source_ordinal=ordinal,
        family_ordinal=ordinal,
        family=family,
        fingerprint=f"fingerprint-{name}",
        source_record_type="response_item",
        source_payload_type=payload_type or family.value,
        stable_id_digest=stable_id,
        fingerprint_version="synthetic-v1",
    )


def _statuses(result: EventProvenanceAssessment) -> tuple[EventProvenanceStatus, ...]:
    return tuple(item.status for item in result.decisions)


def test_independent_child_events_are_not_deduplicated() -> None:
    parent = (
        _event("prompt-a", 0),
        _event("command-a", 1, EventFamily.SHELL_COMMAND),
        _event("test-a", 2, EventFamily.VALIDATION_COMMAND),
        _event("complete-a", 3, EventFamily.TASK_LIFECYCLE),
    )
    child = (
        _event("prompt-b", 0),
        _event("command-b", 1, EventFamily.SHELL_COMMAND),
    )

    result = assess_event_provenance(parent, child)

    assert _statuses(result) == (
        EventProvenanceStatus.ORIGIN,
        EventProvenanceStatus.ORIGIN,
    )


def test_exact_replayed_parent_prefix_is_inherited() -> None:
    parent = (
        _event("prompt", 0),
        _event("command", 1, EventFamily.SHELL_COMMAND),
        _event("test", 2, EventFamily.VALIDATION_COMMAND),
        _event("complete", 3, EventFamily.TASK_LIFECYCLE),
    )
    child = (*parent, _event("child-prompt", 4), _event("child-command", 5))

    result = assess_event_provenance(parent, child)

    assert result.global_prefix_length == 4
    assert _statuses(result) == (
        EventProvenanceStatus.INHERITED_PREFIX,
        EventProvenanceStatus.INHERITED_PREFIX,
        EventProvenanceStatus.INHERITED_PREFIX,
        EventProvenanceStatus.INHERITED_PREFIX,
        EventProvenanceStatus.ORIGIN,
        EventProvenanceStatus.ORIGIN,
    )


def test_selective_user_message_replay_is_assessed_per_family() -> None:
    parent = (
        _event("prompt-1", 0),
        _event("command", 1, EventFamily.SHELL_COMMAND),
        _event("prompt-2", 2),
    )
    child = (_event("prompt-1", 0), _event("prompt-2", 1), _event("child", 2))

    result = assess_event_provenance(parent, child)

    assert _statuses(result) == (
        EventProvenanceStatus.INHERITED_PREFIX,
        EventProvenanceStatus.INHERITED_PREFIX,
        EventProvenanceStatus.ORIGIN,
    )


def test_partial_command_overlap_remains_ambiguous() -> None:
    parent = (_event("same-command", 0, EventFamily.SHELL_COMMAND),)
    child = (_event("same-command", 0, EventFamily.SHELL_COMMAND),)

    result = assess_event_provenance(parent, child)

    assert _statuses(result) == (EventProvenanceStatus.AMBIGUOUS,)


def test_same_command_with_distinct_stable_call_id_is_originated() -> None:
    parent = (
        _event(
            "same-command",
            0,
            EventFamily.SHELL_COMMAND,
            stable_id="parent-call",
        ),
    )
    child = (
        _event(
            "same-command",
            0,
            EventFamily.SHELL_COMMAND,
            stable_id="child-call",
        ),
    )

    result = assess_event_provenance(parent, child)

    assert _statuses(result) == (EventProvenanceStatus.ORIGIN,)
    assert result.decisions[0].evidence_type == "distinct_stable_source_id"


def test_exact_stable_source_identity_can_resolve_one_event() -> None:
    parent = (_event("prompt", 0, stable_id="stable"),)
    child = (_event("prompt", 0, stable_id="stable"),)

    result = assess_event_provenance(parent, child)

    assert _statuses(result) == (EventProvenanceStatus.INHERITED_EXACT,)


def test_siblings_keep_their_independent_suffixes() -> None:
    parent = (_event("prompt", 0), _event("command", 1, EventFamily.SHELL_COMMAND))
    child_a = (*parent, _event("child-a", 2))
    child_b = (*parent, _event("child-b", 2))

    result_a = assess_event_provenance(parent, child_a)
    result_b = assess_event_provenance(parent, child_b)

    assert _statuses(result_a)[-1] is EventProvenanceStatus.ORIGIN
    assert _statuses(result_b)[-1] is EventProvenanceStatus.ORIGIN


def test_nested_lineage_matches_each_immediate_parent_without_recursion() -> None:
    root = (_event("root-1", 0), _event("root-2", 1))
    child = (*root, _event("child", 2))
    grandchild = (*child, _event("grandchild", 3))

    child_result = assess_event_provenance(root, child)
    grandchild_result = assess_event_provenance(child, grandchild)

    assert child_result.global_prefix_length == 2
    assert grandchild_result.global_prefix_length == 3
    assert _statuses(grandchild_result)[-1] is EventProvenanceStatus.ORIGIN


def test_ambiguous_and_cyclic_evidence_is_explicit() -> None:
    parent = (_event("overlap", 0),)
    child = (_event("overlap", 0), _event("new", 1))

    ambiguous = assess_event_provenance(parent, child)
    cyclic = assess_event_provenance(parent, child, cyclic=True)

    assert _statuses(ambiguous) == (
        EventProvenanceStatus.AMBIGUOUS,
        EventProvenanceStatus.ORIGIN,
    )
    assert set(_statuses(cyclic)) == {EventProvenanceStatus.AMBIGUOUS}


def test_adjacent_user_wrapper_records_are_mirrored_observations() -> None:
    events = (
        _event("same", 10, payload_type="message"),
        _event("same", 11, payload_type="user_message"),
        _event("later", 12, payload_type="user_message"),
    )

    assert mirrored_user_observations(events) == {0: 1}


def test_index_persists_event_provenance_and_unchanged_run_is_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home = tmp_path / "synthetic-codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    root_records = _semantic_records("parent")
    child_records = (*root_records, *_semantic_records("child"))
    _write_records(sessions / "root.jsonl", root_records)
    _write_records(sessions / "child.jsonl", child_records)
    with sqlite3.connect(codex_home / "state_9.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at TEXT, source TEXT
            );
            INSERT INTO threads VALUES (
                'root', 'sessions/root.jsonl', '2026-08-01T00:00:00Z', 'vscode'
            );
            INSERT INTO threads VALUES (
                'child', 'sessions/child.jsonl', '2026-08-01T00:10:00Z',
                '{"subagent":{"thread_spawn":{"parent_thread_id":"root"}}}'
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL, child_thread_id TEXT PRIMARY KEY, status TEXT
            );
            INSERT INTO thread_spawn_edges VALUES ('root', 'child', 'closed');
            """
        )
    database = tmp_path / "analytics.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(codex_home))

    first = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        before = tuple(
            connection.execute(
                "SELECT * FROM event_observations ORDER BY observed_session_id, source_ordinal"
            )
        )
        replay = connection.execute(
            """
            SELECT SUM(inherited_events), SUM(ambiguous_events)
            FROM event_replay_summary
            """
        ).fetchone()
        child_statuses = dict(
            connection.execute(
                """
                SELECT provenance_status, COUNT(*)
                FROM event_observations AS events
                JOIN source_sessions AS sessions ON sessions.id = events.observed_session_id
                WHERE sessions.source_session_id = 'child'
                GROUP BY provenance_status
                """
            )
        )

    event_row_reads = 0
    original_event_rows = indexer_module._event_rows

    def counted_event_rows(
        connection: sqlite3.Connection, session_id: int
    ) -> tuple[sqlite3.Row, ...]:
        nonlocal event_row_reads
        event_row_reads += 1
        return original_event_rows(connection, session_id)

    monkeypatch.setattr(indexer_module, "_event_rows", counted_event_rows)
    second = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        after = tuple(
            connection.execute(
                "SELECT * FROM event_observations ORDER BY observed_session_id, source_ordinal"
            )
        )

    assert first.new == 2
    assert replay == (3, 0)
    assert child_statuses == {
        "inherited_prefix": 2,
        "observed_duplicate": 2,
        "origin": 2,
    }
    cli = runner.invoke(
        app,
        [
            "provenance",
            "--codex-home",
            str(codex_home),
            "--db",
            str(database),
            "--family",
            "user_message",
            "--json",
        ],
    )
    assert cli.exit_code == 0
    cli_payload = json.loads(cli.stdout)
    assert cli_payload["inherited_events"] == 1
    assert "synthetic parent prompt" not in cli.stdout
    assert second.updated == 0
    assert second.unchanged == 2
    assert event_row_reads == 0
    assert before == after


def _semantic_records(label: str) -> tuple[dict[str, object], ...]:
    return (
        {
            "timestamp": "2026-08-01T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": f"message-{label}",
                "content": [{"type": "input_text", "text": f"synthetic {label} prompt"}],
            },
        },
        {
            "timestamp": "2026-08-01T00:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "client_id": f"message-{label}",
                "message": f"synthetic {label} prompt",
            },
        },
        {
            "timestamp": "2026-08-01T00:01:00Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": f"call-{label}",
                "arguments": json.dumps({"cmd": f"synthetic-{label}-command"}),
            },
        },
    )


def _write_records(path: Path, records: tuple[dict[str, object], ...]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
