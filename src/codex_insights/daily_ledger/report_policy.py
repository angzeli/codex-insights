"""Committed reporting policy and timezone-aware half-open report windows."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from codex_insights.daily_ledger.config import LedgerConfig
from codex_insights.path_safety import atomic_write_text

REPORT_POLICY_SCHEMA_VERSION = "1.0"
REPORT_DATE_LABEL = "window_end_local_date"
DEFAULT_REPORT_POLICY_TEXT = """schema_version: "1.0"
report_date_label: "window_end_local_date"

periods:
  - effective_from_report_date: "2026-09-02"
    timezone: "Asia/Singapore"
    day_boundary_local: "23:00"

reports_directory: null
assistant_claims_are_verified_evidence: false
unknown_outcomes_remain_unknown: true
"""
_LEGACY_REPORT_POLICY_TEXT = """schema_version: "1.0"
reports_directory: null
assistant_claims_are_verified_evidence: false
unknown_outcomes_remain_unknown: true
"""
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ReportPolicyError(ValueError):
    """Raised when the committed report policy is missing or invalid."""


@dataclass(frozen=True, slots=True)
class ReportingPeriod:
    """One reporting policy effective from a report-date label onward."""

    effective_from_report_date: date
    timezone: str
    day_boundary_local: time

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def boundary_text(self) -> str:
        return self.day_boundary_local.strftime("%H:%M")


@dataclass(frozen=True, slots=True)
class ReportingPolicy:
    """Validated reporting periods from the committed ledger policy."""

    schema_version: str
    report_date_label: str
    periods: tuple[ReportingPeriod, ...]


@dataclass(frozen=True, slots=True)
class ReportingWindow:
    """One authoritative half-open reporting window."""

    report_date: date
    timezone: str
    day_boundary_local: str
    window_start: datetime
    window_end: datetime

    def identity(self) -> dict[str, str]:
        """Return the stable manifest/summary identity for this window."""

        return {
            "report_date": self.report_date.isoformat(),
            "timezone": self.timezone,
            "day_boundary_local": self.day_boundary_local,
            "window_start": _format_boundary(self.window_start),
            "window_end": _format_boundary(self.window_end),
        }


def report_policy_path(config: LedgerConfig) -> Path:
    """Return the committed source-of-truth policy path."""

    return config.ledger_checkout / "config" / "report-policy.yaml"


def ensure_report_policy(config: LedgerConfig) -> Path:
    """Create the default policy, or migrate only the exact prior generated default."""

    path = report_policy_path(config)
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        atomic_write_text(
            path,
            DEFAULT_REPORT_POLICY_TEXT,
            overwrite=False,
            create_parents=True,
        )
        return path
    except OSError as exc:
        raise ReportPolicyError("Cannot read committed report policy") from exc
    if current == _LEGACY_REPORT_POLICY_TEXT:
        atomic_write_text(
            path,
            DEFAULT_REPORT_POLICY_TEXT,
            overwrite=True,
            create_parents=True,
        )
    return path


def load_reporting_policy(config: LedgerConfig) -> ReportingPolicy:
    """Load the committed policy without consulting the local TOML configuration."""

    path = report_policy_path(config)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ReportPolicyError(f"Committed report policy not found: {path}") from exc
    except OSError as exc:
        raise ReportPolicyError("Cannot read committed report policy") from exc
    return parse_reporting_policy(text)


def parse_reporting_policy(text: str) -> ReportingPolicy:
    """Parse the deliberately small YAML subset used by report-policy.yaml."""

    top, raw_periods = _parse_policy_yaml(text)
    allowed_top = {
        "schema_version",
        "report_date_label",
        "periods",
        "reports_directory",
        "assistant_claims_are_verified_evidence",
        "unknown_outcomes_remain_unknown",
    }
    unknown_top = sorted(set(top) - allowed_top)
    if unknown_top:
        raise ReportPolicyError("Unsupported report-policy keys: " + ", ".join(unknown_top))
    schema_version = top.get("schema_version")
    if schema_version != REPORT_POLICY_SCHEMA_VERSION:
        raise ReportPolicyError(
            f"Unsupported report-policy schema {schema_version!r}; "
            f"expected {REPORT_POLICY_SCHEMA_VERSION!r}"
        )
    report_date_label = top.get("report_date_label")
    if report_date_label != REPORT_DATE_LABEL:
        raise ReportPolicyError(
            f"report_date_label must be {REPORT_DATE_LABEL!r}"
        )
    if not raw_periods:
        raise ReportPolicyError("report-policy periods must contain at least one period")
    periods: list[ReportingPeriod] = []
    for index, raw in enumerate(raw_periods):
        allowed_period = {
            "effective_from_report_date",
            "timezone",
            "day_boundary_local",
        }
        unknown_period = sorted(set(raw) - allowed_period)
        if unknown_period:
            raise ReportPolicyError(
                f"Unsupported keys in report-policy period {index + 1}: "
                + ", ".join(unknown_period)
            )
        try:
            effective_text = _required_policy_string(
                raw, "effective_from_report_date"
            )
            effective = date.fromisoformat(effective_text)
        except ValueError as exc:
            raise ReportPolicyError(
                f"Invalid effective_from_report_date in period {index + 1}"
            ) from exc
        timezone = _required_policy_string(raw, "timezone")
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ReportPolicyError(
                f"Invalid IANA timezone in period {index + 1}: {timezone}"
            ) from exc
        boundary_text = _required_policy_string(raw, "day_boundary_local")
        if _TIME_PATTERN.fullmatch(boundary_text) is None:
            raise ReportPolicyError(
                f"Invalid day_boundary_local in period {index + 1}: {boundary_text}"
            )
        hour, minute = (int(part) for part in boundary_text.split(":"))
        periods.append(
            ReportingPeriod(
                effective_from_report_date=effective,
                timezone=timezone,
                day_boundary_local=time(hour=hour, minute=minute),
            )
        )
    effective_dates = [period.effective_from_report_date for period in periods]
    adjacent_dates = zip(effective_dates, effective_dates[1:], strict=False)
    if any(current <= previous for previous, current in adjacent_dates):
        raise ReportPolicyError(
            "Reporting periods must be strictly ordered without overlapping effective dates"
        )
    policy = ReportingPolicy(
        schema_version=str(schema_version),
        report_date_label=str(report_date_label),
        periods=tuple(periods),
    )
    _validate_transition_windows(policy)
    return policy


def reporting_window_for_date(
    policy: ReportingPolicy,
    report_date: date,
) -> ReportingWindow:
    """Return the authoritative half-open window ending on one report date."""

    active = _period_for_report_date(policy, report_date)
    previous = _period_for_report_date(policy, report_date - timedelta(days=1))
    window_start = _local_boundary(report_date - timedelta(days=1), previous)
    window_end = _local_boundary(report_date, active)
    if window_end.astimezone(UTC) <= window_start.astimezone(UTC):
        raise ReportPolicyError(
            f"Reporting window for {report_date.isoformat()} is not positive"
        )
    return ReportingWindow(
        report_date=report_date,
        timezone=active.timezone,
        day_boundary_local=active.boundary_text,
        window_start=window_start,
        window_end=window_end,
    )


def report_date_for_timestamp(
    policy: ReportingPolicy,
    timestamp: datetime,
) -> date:
    """Assign one aware timestamp to exactly one half-open reporting window."""

    if timestamp.tzinfo is None:
        raise ReportPolicyError("Reporting timestamps must be timezone-aware")
    instant = timestamp.astimezone(UTC)
    candidates: set[date] = set()
    for period in policy.periods:
        local_date = instant.astimezone(period.zone).date()
        candidates.update(
            local_date + timedelta(days=offset) for offset in range(-3, 4)
        )
        effective = period.effective_from_report_date
        candidates.update(effective + timedelta(days=offset) for offset in range(-2, 3))
    matches = [
        candidate
        for candidate in sorted(candidates)
        if _contains(reporting_window_for_date(policy, candidate), instant)
    ]
    if len(matches) != 1:
        raise ReportPolicyError(
            "Reporting policy does not assign the timestamp to exactly one window"
        )
    return matches[0]


def reporting_window_for_timestamp(
    policy: ReportingPolicy,
    timestamp: datetime,
) -> ReportingWindow:
    """Return the reporting window containing one aware timestamp."""

    return reporting_window_for_date(policy, report_date_for_timestamp(policy, timestamp))


def _period_for_report_date(
    policy: ReportingPolicy,
    report_date: date,
) -> ReportingPeriod:
    selected = policy.periods[0]
    for period in policy.periods:
        if period.effective_from_report_date > report_date:
            break
        selected = period
    return selected


def _local_boundary(report_date: date, period: ReportingPeriod) -> datetime:
    boundary = datetime.combine(report_date, period.day_boundary_local, tzinfo=period.zone)
    round_trip = boundary.astimezone(UTC).astimezone(period.zone)
    if round_trip.replace(fold=boundary.fold) != boundary:
        raise ReportPolicyError(
            f"Nonexistent local reporting boundary for {report_date} in {period.timezone}"
        )
    return boundary


def _contains(window: ReportingWindow, instant: datetime) -> bool:
    value = instant.astimezone(UTC)
    return (
        window.window_start.astimezone(UTC)
        <= value
        < window.window_end.astimezone(UTC)
    )


def _validate_transition_windows(policy: ReportingPolicy) -> None:
    for period in policy.periods:
        transition = period.effective_from_report_date
        previous = reporting_window_for_date(policy, transition - timedelta(days=1))
        current = reporting_window_for_date(policy, transition)
        if previous.window_end.astimezone(UTC) != current.window_start.astimezone(UTC):
            raise ReportPolicyError(
                f"Reporting periods leave a gap or overlap at {transition.isoformat()}"
            )


def _parse_policy_yaml(
    text: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    top: dict[str, object] = {}
    periods: list[dict[str, object]] = []
    in_periods = False
    current: dict[str, object] | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0:
            key, value = _yaml_pair(stripped, line_number)
            if key == "periods":
                if value:
                    raise ReportPolicyError("periods must be a YAML sequence")
                in_periods = True
                top["periods"] = periods
                current = None
            else:
                if in_periods:
                    in_periods = False
                    current = None
                top[key] = _yaml_scalar(value, line_number)
            continue
        if in_periods and indent == 2 and stripped.startswith("- "):
            current = {}
            periods.append(current)
            key, value = _yaml_pair(stripped[2:], line_number)
            current[key] = _yaml_scalar(value, line_number)
            continue
        if in_periods and indent == 4 and current is not None:
            key, value = _yaml_pair(stripped, line_number)
            current[key] = _yaml_scalar(value, line_number)
            continue
        raise ReportPolicyError(f"Unsupported YAML structure on line {line_number}")
    return top, periods


def _yaml_pair(value: str, line_number: int) -> tuple[str, str]:
    if ":" not in value:
        raise ReportPolicyError(f"Expected key/value pair on line {line_number}")
    key, raw = value.split(":", 1)
    if not key.strip():
        raise ReportPolicyError(f"Missing key on line {line_number}")
    return key.strip(), raw.strip()


def _yaml_scalar(value: str, line_number: int) -> object:
    if not value:
        return ""
    if value in {"null", "~"}:
        return None
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith(('"', "'")):
        if value.startswith("'") and value.endswith("'"):
            return value[1:-1].replace("''", "'")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ReportPolicyError(f"Invalid quoted value on line {line_number}") from exc
        if not isinstance(parsed, str):
            raise ReportPolicyError(f"Expected string on line {line_number}")
        return parsed
    return value


def _required_policy_string(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ReportPolicyError(f"Missing report-policy value: {key}")
    return value


def _format_boundary(value: datetime) -> str:
    return value.isoformat(timespec="seconds")
