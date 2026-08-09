#!/usr/bin/env python3
"""Validate and narrowly repair Codex Insights editable-install machinery."""

from __future__ import annotations

import argparse
import os
import re
import site
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

EDITABLE_PTH = re.compile(r"__editable__\.codex_insights-[A-Za-z0-9_.+-]+\.pth\Z")


class EditableInstallError(RuntimeError):
    """Raised when an editable environment is unsafe or unusable."""


def validate_environment_destination(
    environment: Path,
    *,
    codex_home: Path | None = None,
) -> Path:
    """Reject a development environment inside the configured Codex home."""

    resolved = environment.expanduser().resolve(strict=False)
    source = (codex_home or _default_codex_home()).expanduser().resolve(strict=False)
    if resolved == source or source in resolved.parents:
        raise EditableInstallError(
            f"Development environment must be outside the Codex home: {source}"
        )
    return resolved


def active_environment(
    *,
    prefix: Path | None = None,
    base_prefix: Path | None = None,
    codex_home: Path | None = None,
) -> Path:
    """Return the active isolated environment or fail with a precise diagnostic."""

    environment = validate_environment_destination(
        prefix or Path(sys.prefix),
        codex_home=codex_home,
    )
    base = (base_prefix or Path(sys.base_prefix)).expanduser().resolve(strict=False)
    if environment == base:
        raise EditableInstallError(
            "No isolated virtual environment is active; run this helper with its Python."
        )
    return environment


def editable_site_packages(
    environment: Path,
    candidates: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    """Return only site-packages directories contained by the active environment."""

    discovered = candidates or tuple(Path(item) for item in site.getsitepackages())
    contained = tuple(
        resolved
        for candidate in discovered
        if _is_within((resolved := candidate.expanduser().resolve(strict=False)), environment)
    )
    if not contained:
        raise EditableInstallError(
            f"No site-packages directory belongs to the active environment: {environment}"
        )
    return contained


def find_editable_pth(environment: Path, site_packages: Sequence[Path]) -> Path:
    """Find exactly one verified Codex Insights editable ``.pth`` artifact."""

    matches: list[Path] = []
    for directory in site_packages:
        if not _is_within(directory, environment):
            raise EditableInstallError(
                f"site-packages is outside the active environment: {directory}"
            )
        if not directory.is_dir():
            continue
        matches.extend(
            candidate.resolve(strict=False)
            for candidate in directory.glob("__editable__.codex_insights-*.pth")
            if candidate.is_file() and EDITABLE_PTH.fullmatch(candidate.name)
        )
    if not matches:
        raise EditableInstallError(
            "No Codex Insights editable .pth was found inside the active environment. "
            "Run: python -m pip install -e ."
        )
    if len(matches) != 1:
        raise EditableInstallError(
            f"Expected one Codex Insights editable .pth, found {len(matches)}."
        )
    target = matches[0]
    validate_editable_target(target, environment, site_packages)
    return target


def validate_editable_target(
    target: Path,
    environment: Path,
    site_packages: Sequence[Path],
) -> None:
    """Fail closed unless ``target`` is the expected artifact in this environment."""

    resolved = target.expanduser().resolve(strict=False)
    if not EDITABLE_PTH.fullmatch(resolved.name):
        raise EditableInstallError(f"Refusing unrelated .pth file: {resolved.name}")
    if not _is_within(resolved, environment):
        raise EditableInstallError(f"Editable .pth is outside the active environment: {resolved}")
    if not any(resolved.parent == directory.resolve(strict=False) for directory in site_packages):
        raise EditableInstallError(f"Editable .pth is outside active site-packages: {resolved}")


def has_hidden_flag(flags: int, *, hidden_flag: int | None = None) -> bool:
    """Test the macOS user-hidden bit while remaining portable to other platforms."""

    marker = getattr(stat, "UF_HIDDEN", 0) if hidden_flag is None else hidden_flag
    return bool(marker and flags & marker)


def repair_hidden_editable_pth(
    target: Path,
    environment: Path,
    site_packages: Sequence[Path],
    *,
    platform_name: str = sys.platform,
    read_flags: Callable[[Path], int] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Clear ``UF_HIDDEN`` only from the verified derived editable artifact."""

    validate_editable_target(target, environment, site_packages)
    flags = (read_flags or _file_flags)(target)
    if platform_name != "darwin" or not has_hidden_flag(flags):
        return False
    run(
        ["chflags", "nohidden", str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    if has_hidden_flag((read_flags or _file_flags)(target)):
        raise EditableInstallError(f"UF_HIDDEN remains set on editable .pth: {target}")
    sleep(0.75)
    if has_hidden_flag((read_flags or _file_flags)(target)):
        raise EditableInstallError(
            "macOS reapplied UF_HIDDEN to the editable .pth. Recreate the environment with "
            "a non-dot name, for example: ./scripts/setup-dev.sh"
        )
    return True


def verify_install(
    environment: Path,
    target: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str, str]:
    """Verify fresh Python startup and the environment-local CLI entry point."""

    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command_name = "Scripts/codex-insights.exe" if os.name == "nt" else "bin/codex-insights"
    command = environment / command_name
    if not python.is_file():
        raise EditableInstallError(f"Active environment Python is missing: {python}")
    if not command.is_file() or not _is_within(command.resolve(strict=False), environment):
        raise EditableInstallError(
            f"Codex Insights CLI is missing from the active environment: {command}"
        )

    imported = run(
        [
            str(python),
            "-v",
            "-c",
            "import codex_insights; print(codex_insights.__file__)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if "Skipping hidden .pth file" in imported.stderr:
        raise EditableInstallError(
            f"Python skipped the hidden editable .pth: {target}. "
            "Run this guard again to repair only that generated file."
        )
    if imported.returncode != 0:
        detail = _last_nonempty_line(imported.stderr) or "unknown import error"
        raise EditableInstallError(f"Codex Insights import failed: {detail}")

    version = run(
        [str(command), "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if version.returncode != 0:
        detail = _last_nonempty_line(version.stderr) or "unknown CLI error"
        raise EditableInstallError(f"Codex Insights CLI failed: {detail}")
    return imported.stdout.strip(), version.stdout.strip()


def run_guard() -> None:
    """Validate the active editable environment and print bounded diagnostics."""

    environment = active_environment()
    directories = editable_site_packages(environment)
    target = find_editable_pth(environment, directories)
    before = _file_flags(target)
    repaired = repair_hidden_editable_pth(target, environment, directories)
    module_path, version = verify_install(environment, target)
    print(f"Environment: {environment}")
    print(f"Editable .pth: {target}")
    print(f"macOS flags before guard: {before}")
    print(f"UF_HIDDEN repaired: {'yes' if repaired else 'no'}")
    print(f"Import: {module_path}")
    print(f"CLI: {version}")


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def _file_flags(path: Path) -> int:
    return int(getattr(os.lstat(path), "st_flags", 0))


def _is_within(path: Path, parent: Path) -> bool:
    resolved_path = path.resolve(strict=False)
    resolved_parent = parent.resolve(strict=False)
    return resolved_path == resolved_parent or resolved_parent in resolved_path.parents


def _last_nonempty_line(value: str) -> str | None:
    return next((line.strip() for line in reversed(value.splitlines()) if line.strip()), None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate-environment",
        type=Path,
        help="Validate a proposed environment path without creating or changing it.",
    )
    arguments = parser.parse_args()
    try:
        if arguments.validate_environment is not None:
            destination = validate_environment_destination(arguments.validate_environment)
            print(f"Environment path is safe: {destination}")
            return
        run_guard()
    except EditableInstallError as exc:
        parser.exit(1, f"editable-install guard: {exc}\n")


if __name__ == "__main__":
    main()
