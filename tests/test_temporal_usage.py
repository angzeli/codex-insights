from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from codex_insights.analytics import (
    SessionFilters,
    UsageBreakdown,
    get_usage_report,
    resolve_timezone,
)
from codex_insights.analytics.dashboard import DashboardFilters, build_dashboard_data
from codex_insights.analytics.temporal_usage import (
    StoredTokenEvent,
    attribute_session_usage,
)
from codex_insights.models import UsageVector


def _event(
    ordinal: int,
    when: datetime | None,
    total: int,
    *,
    delta: bool = False,
) -> StoredTokenEvent:
    vector = UsageVector(total_tokens=total)
    return StoredTokenEvent(
        source_ordinal=ordinal,
        occurred_at=when,
        cumulative=None if delta else vector,
        delta=vector if delta else None,
    )


def test_cumulative_increments_span_days_without_changing_all_time_total() -> None:
    first = datetime(2026, 5, 10, 1, tzinfo=UTC)
    resumed = datetime(2026, 8, 1, 2, tzinfo=UTC)

    result = attribute_session_usage(
        semantics="cumulative_total",
        target=UsageVector(total_tokens=120),
        inherited_baseline=None,
        events=(_event(1, first, 100), _event(2, resumed, 120)),
    )

    assert result.status == "complete"
    assert [item.usage.total_tokens for item in result.contributions] == [100, 20]
    assert result.attributed_usage.total_tokens == 120
    assert result.unattributed_usage.total_tokens is None


def test_exact_lineage_baseline_is_removed_but_ambiguous_lineage_is_not_guessed() -> None:
    first = datetime(2026, 8, 1, 1, tzinfo=UTC)
    second = datetime(2026, 8, 1, 2, tzinfo=UTC)
    events = (_event(1, first, 100), _event(2, second, 120))

    exact = attribute_session_usage(
        semantics="cumulative_total",
        target=UsageVector(total_tokens=20),
        inherited_baseline=UsageVector(total_tokens=100),
        events=events,
    )
    ambiguous = attribute_session_usage(
        semantics="cumulative_total",
        target=UsageVector(total_tokens=120),
        inherited_baseline=None,
        events=events,
    )

    assert [item.usage.total_tokens for item in exact.contributions] == [20]
    assert exact.contributions[0].occurred_at == second
    assert [item.usage.total_tokens for item in ambiguous.contributions] == [100, 20]


def test_nonmonotonic_and_missing_timestamp_evidence_remain_explicit() -> None:
    when = datetime(2026, 8, 1, tzinfo=UTC)
    nonmonotonic = attribute_session_usage(
        semantics="cumulative_total",
        target=UsageVector(total_tokens=90),
        inherited_baseline=None,
        events=(_event(1, when, 100), _event(2, when, 90)),
    )
    partial = attribute_session_usage(
        semantics="cumulative_total",
        target=UsageVector(total_tokens=20),
        inherited_baseline=None,
        events=(_event(1, None, 10), _event(2, when, 20)),
    )

    assert nonmonotonic.status == "fallback"
    assert nonmonotonic.reason == "nonmonotonic_cumulative_usage"
    assert nonmonotonic.unattributed_usage.total_tokens == 90
    assert partial.status == "partial"
    assert partial.attributed_usage.total_tokens == 10
    assert partial.unattributed_usage.total_tokens == 10


def test_event_time_is_shared_by_usage_breakdowns_and_dashboard(
    analytics_database: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    database, codex_home = analytics_database
    with sqlite3.connect(database) as connection:
        session_id = int(
            connection.execute(
                "SELECT id FROM source_sessions WHERE source_session_id = ?",
                ("session-alpha-one-1111",),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE source_sessions SET started_at = '2026-05-10T00:00:00Z' WHERE id = ?",
            (session_id,),
        )
        connection.execute("DELETE FROM token_events WHERE source_session_id = ?", (session_id,))
        connection.executemany(
            """
            INSERT INTO token_events(
                source_session_id, event_ordinal, source_ordinal, occurred_at,
                event_kind, cumulative_input_tokens,
                cumulative_cached_input_tokens, cumulative_output_tokens,
                cumulative_total_tokens
            ) VALUES (?, ?, ?, ?, 'cumulative_snapshot', ?, ?, ?, ?)
            """,
            (
                (session_id, 0, 1, "2026-05-10T01:00:00Z", 20, 0, 5, 25),
                (session_id, 1, 2, "2026-08-01T01:00:00Z", 80, 10, 20, 100),
            ),
        )
        connection.commit()

    zone = resolve_timezone("UTC")
    all_time = get_usage_report(database, codex_home=codex_home, timezone=zone)
    daily = get_usage_report(
        database,
        codex_home=codex_home,
        timezone=zone,
        breakdown=UsageBreakdown.DAY,
    )
    august = SessionFilters(
        since=datetime(2026, 8, 1, tzinfo=UTC),
        until=datetime(2026, 8, 2, tzinfo=UTC),
        limit=1,
    )
    august_summary = get_usage_report(
        database,
        codex_home=codex_home,
        timezone=zone,
        filters=august,
    )
    by_repo = get_usage_report(
        database,
        codex_home=codex_home,
        timezone=zone,
        filters=august,
        breakdown=UsageBreakdown.REPOSITORY,
    )
    by_model = get_usage_report(
        database,
        codex_home=codex_home,
        timezone=zone,
        filters=august,
        breakdown=UsageBreakdown.MODEL,
    )
    dashboard = build_dashboard_data(
        database,
        codex_home=codex_home,
        timezone=zone,
        filters=DashboardFilters(since=august.since, until=august.until),
        config_path=tmp_path / "config.json",
        now=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    by_day = {group.label: group.metrics.total_tokens for group in daily.groups}
    assert all_time.metrics.total_tokens == 150
    assert by_day["2026-05-10"] == 25
    assert by_day["2026-08-01"] == 75
    assert august_summary.metrics.total_tokens == 75
    assert sum(group.metrics.total_tokens or 0 for group in by_repo.groups) == 75
    assert sum(group.metrics.total_tokens or 0 for group in by_model.groups) == 75
    assert dashboard.overview["reconciled_tokens"] == 75
    assert {
        row["label"]: row["metrics"]["total_tokens"]  # type: ignore[index]
        for row in dashboard.activity
    }["2026-08-01"] == 75
