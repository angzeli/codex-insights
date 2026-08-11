"""Deterministic indexing and analytics benchmark over synthetic Codex state."""

from __future__ import annotations

import argparse
import json
import platform
import resource
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypeVar

from codex_insights import __version__
from codex_insights.adapters import CodexLocalAdapter
from codex_insights.analytics.dashboard import build_dashboard_data
from codex_insights.analytics.prompts import PromptFilters, search_prompts
from codex_insights.analytics.queries import SessionFilters, get_stats
from codex_insights.analytics.reports import ReportKind, build_analytics_report
from codex_insights.analytics.tasks import TaskBreakdown, get_task_report
from codex_insights.analytics.tools import ToolFilters, get_tool_activity_report
from codex_insights.analytics.usage import (
    UsageBreakdown,
    get_usage_report,
    resolve_timezone,
)
from codex_insights.config import resolve_codex_home
from codex_insights.dashboard_rendering import render_dashboard
from codex_insights.db import SCHEMA_VERSION
from codex_insights.indexer import IndexReport, index_source
from codex_insights.reporting import ReportFormat, render_report

if __package__:
    from scripts.synthetic_corpus import (
        SyntheticCorpusConfig,
        append_changed_session_event,
        generate_synthetic_corpus,
    )
else:
    from synthetic_corpus import (  # type: ignore[import-not-found]
        SyntheticCorpusConfig,
        append_changed_session_event,
        generate_synthetic_corpus,
    )

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    session_count: int
    repository_count: int
    model_count: int
    seed: int

    def to_dict(self) -> dict[str, int]:
        return {
            "session_count": self.session_count,
            "repository_count": self.repository_count,
            "model_count": self.model_count,
            "seed": self.seed,
        }


def run_benchmark(config: BenchmarkConfig, *, workspace: Path) -> dict[str, object]:
    """Run one fresh/unchanged/changed index cycle and shared analytics probes."""

    generated_seconds, corpus = _measure(
        lambda: generate_synthetic_corpus(
            workspace / "corpus",
            config=SyntheticCorpusConfig(
                session_count=config.session_count,
                repository_count=config.repository_count,
                model_count=config.model_count,
                seed=config.seed,
            ),
        )
    )
    database = workspace / "derived" / "index.sqlite3"
    configuration = workspace / "derived" / "config.json"
    adapter = CodexLocalAdapter(resolve_codex_home(corpus.codex_home))

    fresh_seconds, fresh = _measure(
        lambda: index_source(adapter, database, codex_home=corpus.codex_home)
    )
    unchanged_seconds, unchanged = _measure(
        lambda: index_source(adapter, database, codex_home=corpus.codex_home)
    )
    append_changed_session_event(corpus.mutable_rollout)
    changed_seconds, changed = _measure(
        lambda: index_source(adapter, database, codex_home=corpus.codex_home)
    )

    zone = resolve_timezone("UTC")
    query_seconds: dict[str, float] = {}
    query_seconds["stats"] = _measure(
        lambda: get_stats(database, codex_home=corpus.codex_home)
    )[0]
    query_seconds["usage_summary"] = _measure(
        lambda: get_usage_report(
            database,
            codex_home=corpus.codex_home,
            filters=SessionFilters(limit=1),
            timezone=zone,
        )
    )[0]
    query_seconds["usage_by_repo"] = _measure(
        lambda: get_usage_report(
            database,
            codex_home=corpus.codex_home,
            filters=SessionFilters(limit=1),
            breakdown=UsageBreakdown.REPOSITORY,
            timezone=zone,
        )
    )[0]
    query_seconds["task_breakdown"] = _measure(
        lambda: get_task_report(
            database,
            codex_home=corpus.codex_home,
            breakdown=TaskBreakdown.TYPE,
        )
    )[0]
    query_seconds["tool_summary"] = _measure(
        lambda: get_tool_activity_report(
            database,
            codex_home=corpus.codex_home,
            filters=ToolFilters(limit=25),
        )
    )[0]
    query_seconds["prompt_search"] = _measure(
        lambda: search_prompts(
            database,
            codex_home=corpus.codex_home,
            query="synthetic",
            filters=PromptFilters(limit=25),
        )
    )[0]

    report_seconds, report_text = _measure(
        lambda: render_report(
            build_analytics_report(
                database,
                codex_home=corpus.codex_home,
                kind=ReportKind.WEEKLY,
                timezone=zone,
                report_date=date(2026, 1, 1),
                now=datetime(2026, 1, 8, tzinfo=UTC),
            ),
            ReportFormat.HTML,
        )
    )
    dashboard_seconds, dashboard_text = _measure(
        lambda: render_dashboard(
            build_dashboard_data(
                database,
                codex_home=corpus.codex_home,
                timezone=zone,
                config_path=configuration,
                now=datetime(2026, 1, 8, tzinfo=UTC),
            )
        )
    )
    result: dict[str, object] = {
        "schema": "codex-insights-benchmark-v1",
        "generated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "application_version": __version__,
        "database_schema_version": SCHEMA_VERSION,
        "python_version": platform.python_version(),
        "platform": platform.system(),
        "dataset": {
            **config.to_dict(),
            "rollout_count": corpus.rollout_count,
            "child_count": corpus.child_count,
            "archived_count": corpus.archived_count,
            "missing_rollout_count": corpus.missing_rollout_count,
        },
        "timings_seconds": {
            "corpus_generation": generated_seconds,
            "fresh_index": fresh_seconds,
            "unchanged_index": unchanged_seconds,
            "changed_session_index": changed_seconds,
            "report_generation": report_seconds,
            "dashboard_generation": dashboard_seconds,
            "queries": query_seconds,
        },
        "index_results": {
            "fresh": _index_counts(fresh),
            "unchanged": _index_counts(unchanged),
            "changed_session": _index_counts(changed),
        },
        "artifacts": {
            "database_bytes": database.stat().st_size,
            "report_html_bytes": len(report_text.encode("utf-8")),
            "dashboard_html_bytes": len(dashboard_text.encode("utf-8")),
        },
        "peak_memory_mib": _peak_memory_mib(),
        "ratios": {
            "fresh_to_unchanged_speedup": (
                fresh_seconds / unchanged_seconds if unchanged_seconds else None
            ),
            "fresh_to_changed_speedup": (
                fresh_seconds / changed_seconds if changed_seconds else None
            ),
        },
    }
    return result


def render_summary(result: dict[str, object]) -> str:
    dataset = _dict(result["dataset"])
    timings = _dict(result["timings_seconds"])
    queries = _dict(timings["queries"])
    artifacts = _dict(result["artifacts"])
    ratios = _dict(result["ratios"])
    return "\n".join(
        (
            "Codex Insights synthetic benchmark",
            f"  sessions:          {int(dataset['session_count']):,}",
            f"  rollouts:          {int(dataset['rollout_count']):,}",
            f"  fresh index:       {float(timings['fresh_index']):.3f}s",
            f"  unchanged index:   {float(timings['unchanged_index']):.3f}s",
            f"  changed session:   {float(timings['changed_session_index']):.3f}s",
            f"  unchanged speedup: {_optional_decimal(ratios['fresh_to_unchanged_speedup'])}x",
            f"  query max:         {max(float(value) for value in queries.values()):.3f}s",
            f"  report:            {float(timings['report_generation']):.3f}s",
            f"  dashboard:         {float(timings['dashboard_generation']):.3f}s",
            f"  peak memory:       {float(result['peak_memory_mib']):.1f} MiB",
            f"  database:          {int(artifacts['database_bytes']) / 1_048_576:.2f} MiB",
            f"  dashboard HTML:    {int(artifacts['dashboard_html_bytes']) / 1024:.1f} KiB",
        )
    )


def _measure(function: Callable[[], T]) -> tuple[float, T]:
    started = time.perf_counter()
    value = function()
    return time.perf_counter() - started, value


def _peak_memory_mib() -> float:
    maximum = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return maximum / 1_048_576 if platform.system() == "Darwin" else maximum / 1024


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("benchmark result has an invalid shape")
    return value


def _optional_decimal(value: object) -> str:
    return f"{float(value):.1f}" if isinstance(value, (int, float)) else "unknown"


def _index_counts(report: IndexReport) -> dict[str, int]:
    return {
        "discovered": report.discovered,
        "new": report.new,
        "updated": report.updated,
        "unchanged": report.unchanged,
        "skipped": report.skipped,
        "failed": report.failed,
        "warning_count": len(report.warnings),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=1_000)
    parser.add_argument("--repositories", type=int, default=20)
    parser.add_argument("--models", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260810)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--workspace",
        type=Path,
        help="Empty directory to retain generated artifacts; defaults to a temporary directory.",
    )
    arguments = parser.parse_args()
    config = BenchmarkConfig(
        session_count=arguments.sessions,
        repository_count=arguments.repositories,
        model_count=arguments.models,
        seed=arguments.seed,
    )
    if arguments.workspace is not None:
        workspace = arguments.workspace.expanduser().resolve(strict=False)
        if workspace.exists() and any(workspace.iterdir()):
            raise SystemExit(f"Benchmark workspace is not empty: {workspace}")
        workspace.mkdir(parents=True, exist_ok=True)
        result = run_benchmark(config, workspace=workspace)
    else:
        with tempfile.TemporaryDirectory(prefix="codex-insights-benchmark-") as temporary:
            result = run_benchmark(config, workspace=Path(temporary))
    if arguments.output is not None:
        destination = arguments.output.expanduser().resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(render_summary(result))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
