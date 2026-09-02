#!/usr/bin/env python3
"""Merge or remove only the sample daily-ledger hook entries."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MARKER = "codex-insights/daily-ledger/capture_hook.py"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "disable"))
    parser.add_argument("--target", type=Path, default=Path.home() / ".codex" / "hooks.json")
    arguments = parser.parse_args()
    target = arguments.target.expanduser().resolve(strict=False)
    current = _load(target)
    updated = _install(current) if arguments.action == "install" else _disable(current)
    if updated == current:
        print(f"No change needed: {target}")
        return 0
    if target.exists():
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = target.with_name(f"{target.name}.backup-{timestamp}")
        backup.write_bytes(target.read_bytes())
        os.chmod(backup, 0o600)
        print(f"Backup: {backup}")
    _atomic_json(target, updated)
    print(f"Updated: {target}")
    return 0


def _load(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"hooks": {}}
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("hooks.json must contain one JSON object")
    hooks = raw.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("hooks must be a JSON object")
    return raw


def _install(current: dict[str, object]) -> dict[str, object]:
    updated = json.loads(json.dumps(current))
    hooks = updated.setdefault("hooks", {})
    assert isinstance(hooks, dict)
    for event, timeout in (("Stop", 5), ("SessionEnd", 3)):
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{event} must be a JSON array")
        if any(MARKER in json.dumps(group) for group in groups):
            continue
        groups.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            '/usr/bin/env python3 "$HOME/.local/share/'
                            'codex-insights/daily-ledger/capture_hook.py"'
                        ),
                        "timeout": timeout,
                    }
                ]
            }
        )
    return updated


def _disable(current: dict[str, object]) -> dict[str, object]:
    updated = json.loads(json.dumps(current))
    hooks = updated.get("hooks")
    if not isinstance(hooks, dict):
        return updated
    for event in ("Stop", "SessionEnd"):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        retained = [group for group in groups if MARKER not in json.dumps(group)]
        if retained:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    return updated


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
