"""Developer-only aggregate smoke test for a real, strictly read-only Codex source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.analytics.queries import get_stats
from codex_insights.analytics.usage import get_usage_report, resolve_timezone
from codex_insights.config import resolve_codex_home, resolve_index_path
from codex_insights.diagnostics import run_deep_diagnostics
from codex_insights.indexer import IndexReport, index_source
from codex_insights.privacy import load_retention_policy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--confirm-read-only-source",
        action="store_true",
        help="Acknowledge that only source reads and separate derived-index writes will occur.",
    )
    arguments = parser.parse_args()
    if not arguments.confirm_read_only_source:
        raise SystemExit("Pass --confirm-read-only-source to run the developer-only smoke test.")
    resolution = resolve_codex_home(arguments.codex_home)
    database = resolve_index_path(arguments.db)
    adapter = CodexLocalAdapter(resolution)
    probe = adapter.probe()
    audit = adapter.audit(sample_size=3, verbose=False)
    diagnostics = run_deep_diagnostics(resolution.path, database)
    policy = load_retention_policy(arguments.config, codex_home=resolution.path)
    first = index_source(
        adapter,
        database,
        codex_home=resolution.path,
        retention_policy=policy,
    )
    second = index_source(
        adapter,
        database,
        codex_home=resolution.path,
        retention_policy=policy,
    )
    stats = get_stats(database, codex_home=resolution.path)
    usage = get_usage_report(
        database,
        codex_home=resolution.path,
        timezone=resolve_timezone("local"),
        include_reconciliation=True,
    )
    print(
        json.dumps(
            {
                "source_access": "read_only",
                "codex_home_exists": probe.codex_home_exists,
                "audit": {
                    "state_databases": len(audit.state_databases),
                    "rollout_files": audit.rollouts.discovered_file_count,
                    "sampled_rollouts": audit.rollouts.sampled_file_count,
                    "warnings": len(audit.warnings),
                },
                "deep_doctor": {
                    "source_session_count": diagnostics.source_session_count,
                    "derived_session_count": diagnostics.indexed_session_count,
                    "stale_sessions": diagnostics.stale_session_count,
                    "parse_failures": diagnostics.parse_failure_count,
                },
                "first_index": _index_counts(first),
                "second_index": _index_counts(second),
                "stats": stats.to_dict(),
                "usage": usage.to_dict(),
            },
            indent=2,
            sort_keys=True,
        )
    )


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


if __name__ == "__main__":
    main()
