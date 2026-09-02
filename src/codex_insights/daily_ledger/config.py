"""Configuration and XDG-compatible paths for the daily ledger."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

LEDGER_SCHEMA_VERSION = "1.0"
CONFIG_ENVIRONMENT_VARIABLE = "CODEX_INSIGHTS_DAILY_LEDGER_CONFIG"


class LedgerConfigurationError(ValueError):
    """Raised when the daily-ledger configuration is missing or unsafe."""


@dataclass(frozen=True, slots=True)
class LedgerPaths:
    """Resolved local-only paths used by capture and queue processing."""

    config: Path
    state: Path
    helpers: Path

    @property
    def pending(self) -> Path:
        return self.state / "pending"

    @property
    def processing(self) -> Path:
        return self.state / "processing"

    @property
    def processed(self) -> Path:
        return self.state / "processed"

    @property
    def failed(self) -> Path:
        return self.state / "failed"

    @property
    def cache(self) -> Path:
        return self.state / "cache"

    @property
    def locks(self) -> Path:
        return self.state / "locks"

    @property
    def logs(self) -> Path:
        return self.state / "logs"

    def queue_directories(self) -> tuple[Path, ...]:
        return (
            self.pending,
            self.processing,
            self.processed,
            self.failed,
            self.cache,
            self.locks,
            self.logs,
        )


@dataclass(frozen=True, slots=True)
class LedgerConfig:
    """Validated V1 configuration without credentials."""

    schema_version: str
    device_id: str
    ledger_checkout: Path
    remote: str
    branch: str
    push_enabled: bool
    paths: LedgerPaths

def default_ledger_paths(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> LedgerPaths:
    """Return the documented XDG-compatible configuration and state paths."""

    environment = os.environ if environ is None else environ
    user_home = Path.home() if home is None else home
    config_root = Path(environment.get("XDG_CONFIG_HOME", user_home / ".config"))
    state_root = Path(environment.get("XDG_STATE_HOME", user_home / ".local" / "state"))
    data_root = Path(environment.get("XDG_DATA_HOME", user_home / ".local" / "share"))
    explicit_config = environment.get(CONFIG_ENVIRONMENT_VARIABLE)
    config = (
        Path(explicit_config)
        if explicit_config
        else config_root / "codex-insights" / "daily-ledger.toml"
    )
    return LedgerPaths(
        config=_resolved(config),
        state=_resolved(state_root / "codex-insights" / "daily-ledger"),
        helpers=_resolved(data_root / "codex-insights" / "daily-ledger"),
    )


def load_ledger_config(
    explicit: Path | None = None,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> LedgerConfig:
    """Load and validate the V1 TOML configuration."""

    defaults = default_ledger_paths(home=home, environ=environ)
    path = _resolved(explicit) if explicit is not None else defaults.config
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LedgerConfigurationError(f"Daily-ledger config not found: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LedgerConfigurationError(
            f"Cannot read daily-ledger config: {type(exc).__name__}"
        ) from exc
    if not isinstance(raw, dict):
        raise LedgerConfigurationError("Daily-ledger config must be a TOML table")
    allowed = {
        "schema_version",
        "timezone",
        "device_id",
        "ledger_checkout",
        "remote",
        "branch",
        "push_enabled",
        "state_dir",
        "helpers_dir",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise LedgerConfigurationError(
            "Unsupported daily-ledger config keys: " + ", ".join(unknown)
        )
    schema_version = _required_string(raw, "schema_version")
    if schema_version != LEDGER_SCHEMA_VERSION:
        raise LedgerConfigurationError(
            f"Unsupported daily-ledger schema {schema_version!r}; "
            f"expected {LEDGER_SCHEMA_VERSION!r}"
        )
    device_id = _safe_identifier(_required_string(raw, "device_id"), "device_id")
    remote = _safe_git_name(_required_string(raw, "remote"), "remote")
    branch = _safe_git_name(_required_string(raw, "branch"), "branch")
    push_enabled = raw.get("push_enabled")
    if not isinstance(push_enabled, bool):
        raise LedgerConfigurationError("push_enabled must be true or false")
    checkout = _resolved(Path(_required_string(raw, "ledger_checkout")))
    state = _resolved(Path(str(raw.get("state_dir", defaults.state))))
    helpers = _resolved(Path(str(raw.get("helpers_dir", defaults.helpers))))
    paths = LedgerPaths(config=path, state=state, helpers=helpers)
    _reject_overlaps(checkout, paths)
    return LedgerConfig(
        schema_version=schema_version,
        device_id=device_id,
        ledger_checkout=checkout,
        remote=remote,
        branch=branch,
        push_enabled=push_enabled,
        paths=paths,
    )


def ensure_state_directories(paths: LedgerPaths) -> None:
    """Create only the documented local state directories."""

    for directory in paths.queue_directories():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)


def validate_checkout_separation(
    config: LedgerConfig,
    *,
    codex_home: Path | None = None,
    audited_working_directories: tuple[Path, ...] = (),
) -> None:
    """Reject a ledger checkout that overlaps Codex or an audited repository."""

    checkout = _resolved(config.ledger_checkout)
    protected = tuple(
        _resolved(path)
        for path in ((codex_home,) if codex_home is not None else ())
        + audited_working_directories
    )
    for path in protected:
        if _overlaps(checkout, path):
            raise LedgerConfigurationError(
                f"Ledger checkout must not overlap protected path: {path}"
            )


def _required_string(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LedgerConfigurationError(f"{key} must be a non-empty string")
    return value.strip()


def _safe_identifier(value: str, label: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if len(value) > 128 or any(character not in allowed for character in value):
        raise LedgerConfigurationError(f"{label} contains unsupported characters")
    return value


def _safe_git_name(value: str, label: str) -> str:
    if len(value) > 128 or value.startswith("-") or any(
        character.isspace() or character in "~^:?*[\\" for character in value
    ):
        raise LedgerConfigurationError(f"{label} is not a safe Git name")
    return value


def _reject_overlaps(checkout: Path, paths: LedgerPaths) -> None:
    for local in (paths.config, paths.state, paths.helpers):
        if _overlaps(checkout, local):
            raise LedgerConfigurationError(
                "Ledger checkout must be separate from config, state, and helper paths"
            )


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)
