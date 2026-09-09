"""Central remote-ledger redaction and leakage checks."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_JSONL_RECORD_BYTES = 256 * 1024
MAX_REMOTE_FILE_BYTES = 2 * 1024 * 1024

_ABSOLUTE_PATH = re.compile(
    r"(?:/Users/[^\s]+|/home/[^\s]+|[A-Za-z]:\\(?:Users|Documents and Settings)\\[^\s]+)"
)
_TOKEN = re.compile(
    r"(?i)(?:\bsk-[A-Za-z0-9_-]{12,}|\bghp_[A-Za-z0-9_-]{12,}|"
    r"\bgithub_pat_[A-Za-z0-9_-]{12,}|\bxox[baprs]-[A-Za-z0-9_-]{12,}|"
    r"\bAKIA[0-9A-Z]{16}\b|authorization\s*:\s*(?:bearer|basic)\s+\S+)"
)
_CREDENTIAL = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|token|secret|password|passwd|pwd)"
    r"\s*[:=]\s*\S+"
)
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_ENVIRONMENT_ASSIGNMENT = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\s*=\s*[^\s]+")
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-\n]*PRIVATE KEY-----")
_HOME_PATH = re.compile(r"(?:^|\s)~/(?:\S+)")
_TRANSCRIPT_MARKER = re.compile(r'(?i)"(?:payload|prompt|tool_output|raw_hook_payload)"\s*:')


@dataclass(frozen=True, slots=True)
class LeakageFinding:
    """Content-free description of a remote-ledger privacy violation."""

    category: str
    location: str


def sanitize_remote_text(value: object, *, maximum_characters: int = 240) -> str | None:
    """Return compact safe text, omitting the whole value when it looks sensitive."""

    if not isinstance(value, str):
        return None
    compact = " ".join(value.replace("\x00", "").split())
    if not compact:
        return None
    if any(
        pattern.search(compact)
        for pattern in (
            _ABSOLUTE_PATH,
            _TOKEN,
            _CREDENTIAL,
            _EMAIL,
            _ENVIRONMENT_ASSIGNMENT,
            _PRIVATE_KEY,
            _HOME_PATH,
        )
    ):
        return None
    if len(compact) > maximum_characters:
        compact = compact[: maximum_characters - 1].rstrip() + "…"
    return compact


def scan_remote_value(value: object, *, location: str = "$") -> tuple[LeakageFinding, ...]:
    """Recursively scan JSON-compatible data without returning the sensitive value."""

    findings: list[LeakageFinding] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child = f"{location}.{key_text}"
            if key_text.casefold() in {
                "raw_hook_payload",
                "raw_transcript",
                "prompt",
                "tool_output",
                "session_id",
                "transcript_path",
                "cwd",
            }:
                findings.append(LeakageFinding("forbidden_field", child))
            findings.extend(scan_remote_value(item, location=child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            findings.extend(scan_remote_value(item, location=f"{location}[{index}]"))
    elif isinstance(value, str):
        for category, pattern in (
            ("absolute_path", _ABSOLUTE_PATH),
            ("token_like", _TOKEN),
            ("credential_like", _CREDENTIAL),
            ("email_like", _EMAIL),
            ("environment_assignment", _ENVIRONMENT_ASSIGNMENT),
            ("private_key", _PRIVATE_KEY),
            ("home_path", _HOME_PATH),
            ("raw_transcript_shape", _TRANSCRIPT_MARKER),
        ):
            if pattern.search(value):
                findings.append(LeakageFinding(category, location))
    return tuple(findings)


def scan_remote_file(path: Path, *, location: str) -> tuple[LeakageFinding, ...]:
    """Scan one bounded contract file without returning any sensitive content."""

    try:
        if path.is_symlink():
            return (LeakageFinding("symlink", location),)
        if path.suffix == ".jsonl":
            return _scan_jsonl(path, location=location)
        if path.stat().st_size > MAX_REMOTE_FILE_BYTES:
            return (LeakageFinding("oversized_file", location),)
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            return scan_remote_value(json.loads(text), location=location)
        return scan_remote_value(text, location=location)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return (LeakageFinding("malformed_or_unreadable_file", location),)


def _scan_jsonl(path: Path, *, location: str) -> tuple[LeakageFinding, ...]:
    # Bound the read itself, including a line with no newline. Stop on the first
    # violation so diagnostic accumulation cannot grow with the file either.
    with path.open("rb") as stream:
        line_number = 0
        while line := stream.readline(MAX_JSONL_RECORD_BYTES + 1):
            line_number += 1
            record_location = f"{location}:{line_number}"
            if len(line) > MAX_JSONL_RECORD_BYTES:
                return (LeakageFinding("oversized_jsonl_record", record_location),)
            if not line.strip():
                continue
            try:
                record = json.loads(line.decode("utf-8"), parse_constant=_reject_json_constant)
                findings = scan_remote_value(record, location=record_location)
            except (UnicodeError, ValueError, RecursionError):
                return (LeakageFinding("malformed_jsonl_record", record_location),)
            if findings:
                return findings
    return ()


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON constant")


def assert_remote_safe(value: object) -> None:
    """Fail closed when a candidate remote JSON value violates the privacy contract."""

    findings = scan_remote_value(value)
    if findings:
        summary = ", ".join(f"{item.category}@{item.location}" for item in findings[:5])
        raise ValueError(f"Remote-ledger privacy validation failed: {summary}")
    json.dumps(value, ensure_ascii=False, allow_nan=False)


def safe_repository_name(value: object) -> str | None:
    """Return a basename-like repository label without accepting a local path."""

    safe = sanitize_remote_text(value, maximum_characters=128)
    if safe is None or "/" in safe or "\\" in safe:
        return None
    return safe
