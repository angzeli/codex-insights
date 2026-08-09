from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source
from codex_insights.lineage import analyze_thread_topology, assess_token_lineage
from codex_insights.models import (
    DeduplicationStatus,
    DeltaConsistency,
    NormalizedThreadRelationship,
    NormalizedTokenSnapshot,
    UsageVector,
)


def _snapshot(total: int, *, last: int | None = None) -> NormalizedTokenSnapshot:
    return NormalizedTokenSnapshot(
        cumulative=UsageVector(total_tokens=total),
        last_turn=UsageVector(total_tokens=last) if last is not None else None,
    )


def _relationship(parent: str, child: str) -> NormalizedThreadRelationship:
    return NormalizedThreadRelationship(
        parent_source_session_id=parent,
        child_source_session_id=child,
        source_type="synthetic",
        source_home=Path("/synthetic-codex-home"),
    )


def test_independent_child_retains_its_full_observed_contribution() -> None:
    result = assess_token_lineage(
        (_snapshot(100),),
        (_snapshot(0), _snapshot(20, last=20)),
    )

    assert result.status is DeduplicationStatus.INDEPENDENT
    assert result.incremental_usage == UsageVector(total_tokens=20)
    assert 100 + (result.incremental_usage.total_tokens or 0) == 120


def test_exact_inherited_baseline_contributes_only_child_increment() -> None:
    result = assess_token_lineage(
        (_snapshot(100),),
        (_snapshot(100), _snapshot(120, last=20)),
    )

    assert result.status is DeduplicationStatus.INHERITED_EXACT
    assert result.inherited_baseline == UsageVector(total_tokens=100)
    assert result.incremental_usage == UsageVector(total_tokens=20)
    assert result.delta_consistency is DeltaConsistency.EXACT


def test_replayed_parent_prefix_is_removed_once() -> None:
    parent = tuple(_snapshot(total) for total in (10, 40, 100))
    child = (*parent, _snapshot(110, last=10), _snapshot(120, last=10))

    result = assess_token_lineage(parent, child)

    assert result.status is DeduplicationStatus.INHERITED_PREFIX
    assert result.matched_snapshot_count == 3
    assert result.inherited_baseline == UsageVector(total_tokens=100)
    assert result.incremental_usage == UsageVector(total_tokens=20)
    assert result.delta_consistency is DeltaConsistency.EXACT


def test_partial_ancestor_baseline_without_exact_sequence_evidence_is_ambiguous() -> None:
    result = assess_token_lineage(
        (_snapshot(100),),
        (_snapshot(80), _snapshot(120)),
    )

    assert result.status is DeduplicationStatus.AMBIGUOUS
    assert result.inherited_baseline is None
    assert result.incremental_usage is None


def test_matching_total_alone_does_not_prove_inheritance() -> None:
    parent = (
        NormalizedTokenSnapshot(
            cumulative=UsageVector(input_tokens=80, output_tokens=20, total_tokens=100)
        ),
    )
    child = (
        NormalizedTokenSnapshot(
            cumulative=UsageVector(input_tokens=70, output_tokens=30, total_tokens=100)
        ),
        NormalizedTokenSnapshot(
            cumulative=UsageVector(input_tokens=90, output_tokens=30, total_tokens=120)
        ),
    )

    result = assess_token_lineage(parent, child)

    assert result.status is DeduplicationStatus.AMBIGUOUS


def test_nested_inherited_threads_add_only_each_generation_increment() -> None:
    child = assess_token_lineage(
        (_snapshot(100),),
        (_snapshot(100), _snapshot(130)),
    )
    grandchild = assess_token_lineage(
        (_snapshot(100), _snapshot(130)),
        (_snapshot(130), _snapshot(150)),
    )

    assert child.status is DeduplicationStatus.INHERITED_EXACT
    assert grandchild.status is DeduplicationStatus.INHERITED_EXACT
    assert 100 + (child.incremental_usage.total_tokens or 0) + (
        grandchild.incremental_usage.total_tokens or 0
    ) == 150


def test_sibling_increments_remain_additive() -> None:
    parent = (_snapshot(100),)
    child_a = assess_token_lineage(parent, (_snapshot(100), _snapshot(120)))
    child_b = assess_token_lineage(parent, (_snapshot(100), _snapshot(130)))

    assert child_a.status is DeduplicationStatus.INHERITED_EXACT
    assert child_b.status is DeduplicationStatus.INHERITED_EXACT
    assert 100 + (child_a.incremental_usage.total_tokens or 0) + (
        child_b.incremental_usage.total_tokens or 0
    ) == 150


def test_missing_child_token_data_remains_unavailable() -> None:
    result = assess_token_lineage((_snapshot(100),), ())

    assert result.status is DeduplicationStatus.UNAVAILABLE
    assert result.incremental_usage is None


def test_malformed_cycle_is_reported_without_recursing_forever() -> None:
    relationships = (_relationship("a", "b"), _relationship("b", "a"))

    topology = analyze_thread_topology({"a", "b", "orphan-root"}, relationships)
    result = assess_token_lineage((_snapshot(100),), (_snapshot(100),), cyclic=True)

    assert topology.cycle_nodes == frozenset({"a", "b"})
    assert topology.root_threads == 1
    assert result.status is DeduplicationStatus.CYCLE


def test_index_persists_lineage_once_and_unchanged_reindex_is_idempotent(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "synthetic-codex-home"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    _write_rollout(sessions / "root.jsonl", (10, 40, 100))
    _write_rollout(sessions / "inherited.jsonl", (10, 40, 100, 120))
    _write_rollout(sessions / "independent.jsonl", (0, 30))
    _write_rollout(sessions / "ambiguous.jsonl", (80, 120))
    source_database = codex_home / "state_9.sqlite"
    with sqlite3.connect(source_database) as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT
            );
            INSERT INTO threads VALUES ('root', 'sessions/root.jsonl',
                '2026-08-01T00:00:00Z', '2026-08-01T01:00:00Z');
            INSERT INTO threads VALUES ('inherited', 'sessions/inherited.jsonl',
                '2026-08-01T01:01:00Z', '2026-08-01T02:00:00Z');
            INSERT INTO threads VALUES ('independent', 'sessions/independent.jsonl',
                '2026-08-01T01:02:00Z', '2026-08-01T02:00:00Z');
            INSERT INTO threads VALUES ('ambiguous', 'sessions/ambiguous.jsonl',
                '2026-08-01T01:03:00Z', '2026-08-01T02:00:00Z');
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            );
            INSERT INTO thread_spawn_edges VALUES ('root', 'inherited', 'closed');
            INSERT INTO thread_spawn_edges VALUES ('root', 'independent', 'closed');
            INSERT INTO thread_spawn_edges VALUES ('root', 'ambiguous', 'closed');
            """
        )
    source_before = source_database.read_bytes()
    database = tmp_path / "analytics" / "index.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(codex_home))

    first = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        lineage_before = tuple(
            connection.execute(
                "SELECT * FROM token_lineage ORDER BY child_session_id"
            ).fetchall()
        )
        relationships_before = tuple(
            connection.execute(
                "SELECT * FROM thread_relationships ORDER BY child_source_session_id"
            ).fetchall()
        )
        statuses = dict(
            connection.execute(
                """
                SELECT s.source_session_id, l.deduplication_status
                FROM token_lineage AS l
                JOIN source_sessions AS s ON s.id = l.child_session_id
                """
            )
        )
        observed, reconciled = connection.execute(
            "SELECT SUM(observed_total_tokens), SUM(aggregate_total_tokens) "
            "FROM accounted_usage"
        ).fetchone()

    second = index_source(adapter, database, codex_home=codex_home)
    with sqlite3.connect(database) as connection:
        lineage_after = tuple(
            connection.execute(
                "SELECT * FROM token_lineage ORDER BY child_session_id"
            ).fetchall()
        )
        relationships_after = tuple(
            connection.execute(
                "SELECT * FROM thread_relationships ORDER BY child_source_session_id"
            ).fetchall()
        )

    assert first.new == 4
    assert statuses == {
        "ambiguous": "ambiguous",
        "inherited": "inherited_prefix",
        "independent": "independent",
    }
    assert (observed, reconciled) == (370, 270)
    assert second.updated == 0
    assert second.unchanged == 4
    assert lineage_before == lineage_after
    assert relationships_before == relationships_after
    assert source_database.read_bytes() == source_before


def _write_rollout(path: Path, totals: tuple[int, ...]) -> None:
    records = [
        {
            "timestamp": f"2026-08-01T00:{index:02d}:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": total}},
            },
        }
        for index, total in enumerate(totals)
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
