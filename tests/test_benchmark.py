from __future__ import annotations

from pathlib import Path

from scripts import benchmark as benchmark_module
from scripts.benchmark import BenchmarkConfig, render_summary, run_benchmark


def test_small_benchmark_exercises_fresh_unchanged_and_changed_paths(
    tmp_path: Path,
) -> None:
    result = run_benchmark(
        BenchmarkConfig(
            session_count=60,
            repository_count=5,
            model_count=3,
            seed=271828,
        ),
        workspace=tmp_path / "benchmark",
    )
    index_results = result["index_results"]
    timings = result["timings_seconds"]
    artifacts = result["artifacts"]
    stage_timings = result["index_stage_timings_seconds"]

    assert result["schema"] == "codex-insights-benchmark-v1"
    assert index_results["fresh"]["new"] == 60
    assert index_results["unchanged"]["unchanged"] == 60
    assert index_results["unchanged"]["updated"] == 0
    assert index_results["changed_session"]["updated"] == 1
    assert timings["fresh_index"] > 0
    assert timings["unchanged_index"] > 0
    assert timings["changed_session_index"] > 0
    assert timings["report_generation"] > 0
    assert timings["dashboard_generation"] > 0
    assert timings["queries"]["prompt_search"] > 0
    assert stage_timings["fresh"]["coverage_snapshots"] > 0
    assert stage_timings["unchanged"]["coverage_snapshots"] > 0
    assert stage_timings["changed_session"]["outcome_reconciliation"] > 0
    assert artifacts["database_bytes"] > 0
    assert artifacts["dashboard_html_bytes"] < 500_000
    assert "fresh index" in render_summary(result)


def test_benchmark_memory_is_explicitly_unavailable_without_posix_resource() -> None:
    original = benchmark_module._RESOURCE_MODULE
    try:
        benchmark_module._RESOURCE_MODULE = None
        assert benchmark_module._peak_memory_mib() is None
    finally:
        benchmark_module._RESOURCE_MODULE = original

    summary = render_summary(
        {
            "dataset": {"session_count": 1, "rollout_count": 1},
            "timings_seconds": {
                "fresh_index": 1.0,
                "unchanged_index": 0.5,
                "changed_session_index": 0.6,
                "report_generation": 0.1,
                "dashboard_generation": 0.1,
                "queries": {"stats": 0.01},
            },
            "artifacts": {"database_bytes": 1, "dashboard_html_bytes": 1},
            "ratios": {"fresh_to_unchanged_speedup": 2.0},
            "peak_memory_mib": None,
        }
    )
    assert "peak memory:       unavailable" in summary
