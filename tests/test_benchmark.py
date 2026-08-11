from __future__ import annotations

from pathlib import Path

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
    assert artifacts["database_bytes"] > 0
    assert artifacts["dashboard_html_bytes"] < 500_000
    assert "fresh index" in render_summary(result)
