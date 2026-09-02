#!/usr/bin/env python3
"""Installed Stop/SessionEnd helper; keep stdout reserved for the hook contract."""

from __future__ import annotations

import json
import os
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path


def _enter_managed_environment() -> None:
    if sys.prefix != sys.base_prefix:
        return
    interpreter = (
        Path.home() / ".local" / "share" / "codex-insights" / "venv" / "bin" / "python"
    )
    if interpreter.is_file():
        os.execv(str(interpreter), [str(interpreter), __file__])


def main() -> int:
    with suppress(OSError):
        _enter_managed_environment()
    stdin_text = sys.stdin.read()
    try:
        from codex_insights.daily_ledger.queue import run_capture_hook

        exit_code, output = run_capture_hook(stdin_text)
    except Exception as exc:
        _log_bootstrap_failure(exc)
        exit_code, output = 0, _fail_open_output(stdin_text)
    if output:
        sys.stdout.write(output)
        sys.stdout.flush()
    return exit_code


def _fail_open_output(stdin_text: str) -> str:
    try:
        payload = json.loads(stdin_text)
    except json.JSONDecodeError:
        return ""
    if isinstance(payload, dict) and payload.get("hook_event_name") == "Stop":
        return '{"continue":true,"suppressOutput":true}'
    return ""


def _log_bootstrap_failure(exc: Exception) -> None:
    try:
        state_root = Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        )
        logs = state_root / "codex-insights" / "daily-ledger" / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = logs / "hook-errors.log"
        timestamp = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {type(exc).__name__}\n")
    except OSError:
        return


if __name__ == "__main__":
    raise SystemExit(main())
