"""Read-only daily-ledger diagnostics."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from codex_insights.daily_ledger.config import LedgerConfig
from codex_insights.daily_ledger.git_sync import inspect_checkout
from codex_insights.daily_ledger.privacy import LeakageFinding, scan_remote_file
from codex_insights.daily_ledger.report_policy import (
    ReportPolicyError,
    load_reporting_policy,
    reporting_window_for_timestamp,
)


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Bounded configuration, queue, checkout, and privacy diagnostics."""

    schema_version: str
    config_path: str
    config_exists: bool
    timezone: str | None
    timezone_valid: bool
    day_boundary_local: str | None
    current_report_date: str | None
    current_window_start: str | None
    current_window_end: str | None
    next_reporting_boundary: str | None
    reporting_periods_valid: bool
    reporting_policy_error: str | None
    device_id: str
    checkout: str
    checkout_exists: bool
    checkout_valid: bool
    checkout_is_git_top_level: bool
    remote: str
    remote_exists: bool
    auth_readiness: str
    helper_path: str
    helper_installed: bool
    hook_config_path: str
    hook_config_present: bool
    pending_jobs: int
    processing_jobs: int
    failed_jobs: int
    last_successful_push: str | None
    dirty_allowlisted: tuple[str, ...]
    dirty_non_allowlisted: tuple[str, ...]
    leakage_findings: tuple[LeakageFinding, ...]
    scanned_generated_files: int

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["leakage_findings"] = [asdict(item) for item in self.leakage_findings]
        return payload


def run_doctor(
    config: LedgerConfig,
    *,
    codex_home: Path,
    now: datetime | None = None,
) -> DoctorReport:
    """Inspect the configured environment without network access or mutation."""

    checkout = inspect_checkout(config)
    helper = config.paths.helpers / "capture_hook.py"
    hook_config = codex_home.expanduser().resolve(strict=False) / "hooks.json"
    findings, scanned = _scan_generated_files(config.ledger_checkout)
    last_push = _load_last_push(config)
    timezone: str | None = None
    day_boundary: str | None = None
    report_date: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    policy_error: str | None = None
    try:
        policy = load_reporting_policy(config)
        instant = now or datetime.now(tz=UTC)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        window = reporting_window_for_timestamp(policy, instant)
        identity = window.identity()
        timezone = window.timezone
        day_boundary = window.day_boundary_local
        report_date = window.report_date.isoformat()
        window_start = identity["window_start"]
        window_end = identity["window_end"]
    except ReportPolicyError as exc:
        policy_error = str(exc)
    return DoctorReport(
        schema_version=config.schema_version,
        config_path=str(config.paths.config),
        config_exists=config.paths.config.is_file(),
        timezone=timezone,
        timezone_valid=policy_error is None,
        day_boundary_local=day_boundary,
        current_report_date=report_date,
        current_window_start=window_start,
        current_window_end=window_end,
        next_reporting_boundary=window_end,
        reporting_periods_valid=policy_error is None,
        reporting_policy_error=policy_error,
        device_id=config.device_id,
        checkout=str(config.ledger_checkout),
        checkout_exists=config.ledger_checkout.is_dir(),
        checkout_valid=checkout.valid_checkout,
        checkout_is_git_top_level=checkout.top_level_matches,
        remote=config.remote,
        remote_exists=checkout.remote_exists,
        auth_readiness=(
            "local_git_identity_and_remote_ready_network_not_tested"
            if checkout.git_identity_ready and checkout.remote_url_present
            else "local_git_configuration_incomplete"
        ),
        helper_path=str(helper),
        helper_installed=helper.is_file(),
        hook_config_path=str(hook_config),
        hook_config_present=_hook_config_mentions_helper(hook_config),
        pending_jobs=_job_count(config.paths.pending),
        processing_jobs=_job_count(config.paths.processing),
        failed_jobs=_job_count(config.paths.failed),
        last_successful_push=last_push,
        dirty_allowlisted=checkout.dirty_allowlisted,
        dirty_non_allowlisted=checkout.dirty_non_allowlisted,
        leakage_findings=findings,
        scanned_generated_files=scanned,
    )


def _hook_config_mentions_helper(path: Path) -> bool:
    try:
        if path.stat().st_size > 1024 * 1024:
            return False
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    encoded = json.dumps(raw, sort_keys=True)
    return "codex-insights/daily-ledger/capture_hook.py" in encoded


def _job_count(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for path in directory.glob("*.json") if not path.name.endswith(".error.json"))


def _load_last_push(config: LedgerConfig) -> str | None:
    path = config.paths.cache / "last-successful-push.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    value = raw.get("pushed_at") if isinstance(raw, dict) else None
    return value if isinstance(value, str) else None


def _scan_generated_files(checkout: Path) -> tuple[tuple[LeakageFinding, ...], int]:
    roots = (
        checkout / "ledger" / "codex",
        checkout / "status",
        checkout / "schema",
        checkout / "config",
    )
    findings: list[LeakageFinding] = []
    scanned = 0
    candidates = [checkout / "README.md", checkout / "SCHEMA.md"]
    for root in roots:
        if not root.is_dir():
            continue
        candidates.extend(path for path in sorted(root.rglob("*")) if path.is_file())
    for path in candidates:
        if scanned >= 10_000 or not path.is_file():
            continue
        scanned += 1
        findings.extend(scan_remote_file(path, location=str(path.relative_to(checkout))))
    return tuple(findings), scanned
