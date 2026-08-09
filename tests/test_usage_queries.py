from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from codex_insights.analytics import (
    SessionFilters,
    TimezoneError,
    UsageBreakdown,
    get_usage_report,
    parse_time_range,
    resolve_timezone,
)


def test_usage_summary_preserves_metric_coverage_and_percentiles(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database

    report = get_usage_report(
        database,
        codex_home=codex_home,
        timezone=resolve_timezone("UTC"),
        now=datetime(2026, 8, 9, 12, tzinfo=UTC),
    )

    assert report.breakdown is UsageBreakdown.SUMMARY
    assert report.timezone == "UTC"
    assert report.groups == ()
    assert report.metrics.session_count == 4
    assert report.metrics.total_tokens == 150
    assert report.metrics.input_tokens == 120
    assert report.metrics.cached_input_tokens == 15
    assert report.metrics.output_tokens == 30
    assert report.metrics.reasoning_output_tokens is None
    assert report.metrics.coverage.total_tokens == 2
    assert report.metrics.coverage.reasoning_output_tokens == 0
    assert report.metrics.mean_tokens_per_session == 75.0
    assert report.metrics.median_tokens_per_session == 75.0
    assert report.metrics.p90_tokens_per_session == 100.0
    assert report.metrics.sessions_per_day == pytest.approx(4 / 9)


def test_usage_filters_and_normalized_repository_grouping(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database
    zone = resolve_timezone("UTC")

    repositories = get_usage_report(
        database,
        codex_home=codex_home,
        breakdown=UsageBreakdown.REPOSITORY,
        timezone=zone,
    )
    filtered = get_usage_report(
        database,
        codex_home=codex_home,
        breakdown=UsageBreakdown.MODEL,
        filters=SessionFilters(repository="repo-one", model="model-a", limit=1),
        timezone=zone,
    )
    outside = get_usage_report(
        database,
        codex_home=codex_home,
        filters=SessionFilters(repository="outside-git", limit=1),
        timezone=zone,
    )

    assert [group.label for group in repositories.groups] == [
        "repo-one",
        "Outside Git repositories",
        "repo-two",
    ]
    assert repositories.groups[0].repository_root == Path("/repos/repo-one")
    assert repositories.groups[0].metrics.total_tokens == 100
    assert repositories.groups[0].metrics.coverage.total_tokens == 1
    assert repositories.groups[2].metrics.total_tokens is None
    assert filtered.metrics.session_count == 1
    assert filtered.metrics.total_tokens == 100
    assert [group.label for group in filtered.groups] == ["model-a"]
    assert outside.metrics.session_count == 1
    assert outside.metrics.total_tokens == 50


def test_usage_top_n_is_deterministic_and_limited_to_repo_or_model(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database

    report = get_usage_report(
        database,
        codex_home=codex_home,
        breakdown=UsageBreakdown.MODEL,
        timezone=resolve_timezone("UTC"),
        top=2,
    )

    assert [group.label for group in report.groups] == ["model-a", "model-b"]
    with pytest.raises(ValueError, match="repository or model"):
        get_usage_report(
            database,
            codex_home=codex_home,
            breakdown=UsageBreakdown.DAY,
            timezone=resolve_timezone("UTC"),
            top=1,
        )


def test_day_and_week_groups_use_requested_timezone(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database

    utc_days = get_usage_report(
        database,
        codex_home=codex_home,
        breakdown=UsageBreakdown.DAY,
        timezone=resolve_timezone("UTC"),
    )
    pacific_days = get_usage_report(
        database,
        codex_home=codex_home,
        breakdown=UsageBreakdown.DAY,
        timezone=resolve_timezone("America/Los_Angeles"),
    )
    weeks = get_usage_report(
        database,
        codex_home=codex_home,
        breakdown=UsageBreakdown.WEEK,
        timezone=resolve_timezone("UTC"),
    )

    assert [group.label for group in utc_days.groups] == [
        "2026-08-01",
        "2026-08-07",
        "2026-08-08",
        "2026-08-09",
    ]
    assert [group.label for group in pacific_days.groups] == [
        "2026-07-31",
        "2026-08-07",
        "2026-08-08",
    ]
    assert pacific_days.groups[-1].metrics.session_count == 2
    assert [group.label for group in weeks.groups] == ["2026-07-27", "2026-08-03"]
    assert weeks.groups[-1].metrics.session_count == 3


def test_usage_date_filters_respect_timezone_calendar_boundaries(
    analytics_database: tuple[Path, Path],
) -> None:
    database, codex_home = analytics_database
    zone = resolve_timezone("America/Los_Angeles")
    since, until = parse_time_range(
        "2026-08-08",
        "2026-08-08",
        timezone=zone.timezone,
    )

    report = get_usage_report(
        database,
        codex_home=codex_home,
        filters=SessionFilters(since=since, until=until, limit=1),
        breakdown=UsageBreakdown.DAY,
        timezone=zone,
    )

    assert since == datetime(2026, 8, 8, 7, tzinfo=UTC)
    assert until == datetime(2026, 8, 9, 7, tzinfo=UTC)
    assert report.metrics.session_count == 2
    assert [group.label for group in report.groups] == ["2026-08-08"]


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(TimezoneError):
        resolve_timezone("Mars/Olympus_Mons")
