"""Detached daily-ledger worker entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

from codex_insights.daily_ledger.config import default_ledger_paths, load_ledger_config
from codex_insights.daily_ledger.queue import log_local_failure
from codex_insights.daily_ledger.service import flush_queue


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path)
    arguments = parser.parse_args()
    paths = default_ledger_paths()
    try:
        config = load_ledger_config(arguments.config)
        paths = config.paths
        flush_queue(config, no_push=not config.push_enabled)
    except Exception as exc:
        log_local_failure(paths, "worker", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
