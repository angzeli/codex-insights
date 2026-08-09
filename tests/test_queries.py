from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codex_insights.analytics import (
    AmbiguousSessionIdError,
    SessionFilters,
    SessionNotFoundError,
    TimeExpressionError,
    get_session,
    get_stats,
    list_models,
    list_repositories,
    list_sessions,
    parse_time_range,
)


def test_session_filters_and_deterministic_ordering(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database
    all_rows = list_sessions(database, codex_home=codex_home)

    assert [row.session_id for row in all_rows] == [
        "session-boundary-4444",
        "session-beta-3333",
        "session-alpha-two-2222",
        "session-alpha-one-1111",
    ]
    assert [
        row.session_id
        for row in list_sessions(
            database,
            codex_home=codex_home,
            filters=SessionFilters(repository="repo-one"),
        )
    ] == ["session-alpha-two-2222", "session-alpha-one-1111"]
    assert [
        row.session_id
        for row in list_sessions(
            database,
            codex_home=codex_home,
            filters=SessionFilters(model="model-a"),
        )
    ] == ["session-beta-3333", "session-alpha-one-1111"]
    assert [
        row.session_id
        for row in list_sessions(
            database,
            codex_home=codex_home,
            filters=SessionFilters(source="editor"),
        )
    ] == ["session-alpha-two-2222"]
    assert [
        row.session_id
        for row in list_sessions(
            database,
            codex_home=codex_home,
            filters=SessionFilters(archived=True),
        )
    ] == ["session-alpha-two-2222"]
    assert [
        row.session_id
        for row in list_sessions(
            database,
            codex_home=codex_home,
            filters=SessionFilters(archived=False, limit=2),
        )
    ] == ["session-boundary-4444", "session-beta-3333"]
    assert [
        row.session_id
        for row in list_sessions(
            database,
            codex_home=codex_home,
            filters=SessionFilters(repository="outside-git"),
        )
    ] == ["session-beta-3333"]

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO source_sessions(
                source_session_id, source_type, source_home, client_source, started_at,
                archived, first_ingested_at, last_ingested_at
            ) VALUES (?, 'codex-local', ?, 'cli', '2026-08-09T00:00:00Z', 0, ?, ?)
            """,
            (
                "session-alpha-tie-0000",
                str(codex_home),
                "2026-08-09T01:00:00Z",
                "2026-08-09T01:00:00Z",
            ),
        )
    tied = list_sessions(
        database,
        codex_home=codex_home,
        filters=SessionFilters(limit=2),
    )
    assert [row.session_id for row in tied] == [
        "session-alpha-tie-0000",
        "session-boundary-4444",
    ]


def test_date_boundaries_are_inclusive_since_and_exclusive_until(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database
    since, until = parse_time_range("2026-08-07", "2026-08-08")
    date_rows = list_sessions(
        database,
        codex_home=codex_home,
        filters=SessionFilters(since=since, until=until),
    )
    assert [row.session_id for row in date_rows] == [
        "session-beta-3333",
        "session-alpha-two-2222",
    ]

    _, exact_until = parse_time_range(None, "2026-08-09T00:00:00Z")
    exact_rows = list_sessions(
        database,
        codex_home=codex_home,
        filters=SessionFilters(until=exact_until),
    )
    assert [row.session_id for row in exact_rows] == [
        "session-beta-3333",
        "session-alpha-two-2222",
        "session-alpha-one-1111",
    ]


def test_relative_durations_and_invalid_ranges() -> None:
    now = datetime(2026, 8, 9, 12, tzinfo=UTC)

    since, until = parse_time_range("7d", "24h", now=now)

    assert since == datetime(2026, 8, 2, 12, tzinfo=UTC)
    assert until == datetime(2026, 8, 8, 12, tzinfo=UTC)
    with pytest.raises(TimeExpressionError):
        parse_time_range("2026-08-09", "2026-08-01", now=now)
    with pytest.raises(TimeExpressionError):
        parse_time_range("recently", None, now=now)


def test_session_prefix_resolution_and_missing_token_data(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database

    detail = get_session(database, "session-beta", codex_home=codex_home)
    missing_usage = get_session(database, "session-alpha-two-2222", codex_home=codex_home)

    assert detail.session_id == "session-beta-3333"
    assert detail.duration_seconds == 600
    assert detail.event_counts == (("shell_command", 2),)
    assert missing_usage.usage.known is False
    assert missing_usage.usage.total_tokens is None
    assert missing_usage.source_coverage.status == "indexed_with_warnings"
    assert missing_usage.warnings == ("malformed_lines=1;oversized_lines=0",)

    with pytest.raises(AmbiguousSessionIdError) as ambiguous:
        get_session(database, "session-alpha", codex_home=codex_home)
    assert ambiguous.value.matches == (
        "session-alpha-one-1111",
        "session-alpha-two-2222",
    )
    with pytest.raises(SessionNotFoundError):
        get_session(database, "does-not-exist", codex_home=codex_home)


def test_repository_and_model_aggregates_keep_unknown_categories(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database

    repositories = list_repositories(database, codex_home=codex_home)
    models = list_models(database, codex_home=codex_home)

    assert [row.repository for row in repositories] == [
        "repo-one",
        "repo-two",
        "Outside Git repositories",
    ]
    assert repositories[0].session_count == 2
    assert repositories[0].total_known_tokens == 100
    assert repositories[0].sessions_with_token_data == 1
    assert repositories[1].total_known_tokens is None
    outside = repositories[2]
    assert outside.in_git_repository is False
    assert outside.session_count == 1
    assert outside.total_known_tokens == 50

    assert [(row.model, row.session_count) for row in models] == [
        ("model-a", 2),
        ("Unknown model", 1),
        ("model-b", 1),
    ]
    assert models[0].total_known_tokens == 150
    assert models[1].total_known_tokens is None


def test_stats_preserve_token_coverage(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database

    stats = get_stats(
        database,
        codex_home=codex_home,
        now=datetime(2026, 8, 9, 12, tzinfo=UTC),
    )

    assert stats.indexed_sessions == 4
    assert stats.active_days == 4
    assert stats.repositories == 2
    assert stats.sessions_today == 1
    assert stats.sessions_last_7_days == 3
    assert stats.sessions_last_30_days == 4
    assert stats.total_known_tokens == 150
    assert stats.sessions_with_token_data == 2
    assert stats.token_data_fraction == 0.5
