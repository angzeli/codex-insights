"""Privacy filtering, persistent retention policy, inspection, and content purge."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from codex_insights.config import resolve_config_path
from codex_insights.db import open_index
from codex_insights.path_safety import atomic_write_text, validate_write_target

PROMPT_CONTENT_SCHEMA_VERSION = "prompt-content-v1"
PRIVACY_CONFIG_SCHEMA = "codex-insights-config-v1"
MAX_PROMPT_CHARACTERS = 100_000
_TRUNCATION_MARKER = "\n[TRUNCATED BY CODEX INSIGHTS]"

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
    re.DOTALL,
)
_AUTHORIZATION = re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+")
_COMMON_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9_-]{16,}|"
    r"github_pat_[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9_-]{16,}|"
    r"AKIA[0-9A-Z]{16})\b"
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password)"
    r"(\s*[:=]\s*)[^\s,;]{6,}"
)


@dataclass(frozen=True, slots=True)
class RedactedPrompt:
    """Stored-safe prompt content plus redaction/truncation metadata."""

    text: str
    status: str
    original_character_count: int
    stored_character_count: int
    redaction_count: int


class PurgeTarget(StrEnum):
    """Derived text categories that can be removed without touching source data."""

    PROMPTS = "prompts"
    COMMAND_TEXT = "command-text"


@dataclass(frozen=True, slots=True)
class ContentRetentionPolicy:
    """Persistent policy controlling text stored by future indexing runs."""

    store_prompts: bool = True
    store_command_text: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": PRIVACY_CONFIG_SCHEMA,
            "store_prompts": self.store_prompts,
            "store_command_text": self.store_command_text,
            "store_raw_tool_output": False,
        }


@dataclass(frozen=True, slots=True)
class PrivacyInspection:
    """Content-free counts describing what the derived index retains."""

    database_path: Path
    config_path: Path
    config_exists: bool
    policy: ContentRetentionPolicy
    counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "database_path": str(self.database_path),
            "config_path": str(self.config_path),
            "config_exists": self.config_exists,
            "policy": self.policy.to_dict(),
            "counts": dict(sorted(self.counts.items())),
            "raw_tool_outputs_stored": False,
            "hidden_reasoning_stored": False,
            "raw_rollout_records_stored": False,
        }


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """Aggregate result of deleting one category of derived text."""

    target: PurgeTarget
    affected_items: int
    database_path: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target.value,
            "affected_items": self.affected_items,
            "database_path": str(self.database_path),
        }


def redact_prompt(text: str, *, maximum_characters: int = MAX_PROMPT_CHARACTERS) -> RedactedPrompt:
    """Redact a small set of high-risk secret shapes before bounding storage."""

    if maximum_characters < len(_TRUNCATION_MARKER) + 1:
        raise ValueError("maximum_characters is too small for the truncation marker")
    redacted = text
    redactions = 0

    redacted, count = _PRIVATE_KEY.subn("[REDACTED PRIVATE KEY]", redacted)
    redactions += count
    redacted, count = _AUTHORIZATION.subn(r"\1[REDACTED]", redacted)
    redactions += count
    redacted, count = _COMMON_TOKEN.subn("[REDACTED TOKEN]", redacted)
    redactions += count

    def assignment_replacement(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}[REDACTED]"

    redacted, count = _CREDENTIAL_ASSIGNMENT.subn(assignment_replacement, redacted)
    redactions += count

    truncated = len(redacted) > maximum_characters
    if truncated:
        redacted = redacted[: maximum_characters - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
    if redactions and truncated:
        status = "redacted_and_truncated"
    elif redactions:
        status = "redacted"
    elif truncated:
        status = "truncated"
    else:
        status = "none"
    return RedactedPrompt(
        text=redacted,
        status=status,
        original_character_count=len(text),
        stored_character_count=len(redacted),
        redaction_count=redactions,
    )


def load_retention_policy(
    config_path: Path | None,
    *,
    codex_home: Path,
) -> ContentRetentionPolicy:
    """Load the local policy or return the Phase-II-compatible defaults."""

    path = validate_write_target(
        resolve_config_path(config_path),
        codex_home=codex_home,
        operation="Privacy configuration",
    )
    if not path.exists():
        return ContentRetentionPolicy()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read privacy configuration: {type(exc).__name__}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != PRIVACY_CONFIG_SCHEMA:
        raise ValueError(f"Unsupported privacy configuration schema in {path}")
    prompts = raw.get("store_prompts")
    commands = raw.get("store_command_text")
    if not isinstance(prompts, bool) or not isinstance(commands, bool):
        raise ValueError("Privacy configuration flags must be true or false")
    return ContentRetentionPolicy(
        store_prompts=prompts,
        store_command_text=commands,
    )


def save_retention_policy(
    policy: ContentRetentionPolicy,
    config_path: Path | None,
    *,
    codex_home: Path,
) -> Path:
    """Atomically persist the privacy policy outside the Codex source home."""

    path = validate_write_target(
        resolve_config_path(config_path),
        codex_home=codex_home,
        operation="Privacy configuration",
    )
    payload = json.dumps(policy.to_dict(), indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, payload, overwrite=True, create_parents=True)
    return path


def inspect_privacy(
    database_path: Path,
    *,
    codex_home: Path,
    config_path: Path | None = None,
) -> PrivacyInspection:
    """Count retained categories without returning prompt or command values."""

    policy_path = validate_write_target(
        resolve_config_path(config_path),
        codex_home=codex_home,
        operation="Privacy configuration",
    )
    policy = load_retention_policy(policy_path, codex_home=codex_home)
    resolved_database = database_path.expanduser().resolve(strict=False)
    with closing(open_index(resolved_database, codex_home=codex_home)) as connection:
        counts = {
            "session_metadata": _count(connection, "SELECT COUNT(*) FROM source_sessions"),
            "token_metadata": _count(connection, "SELECT COUNT(*) FROM usage"),
            "event_provenance_metadata": _count(
                connection, "SELECT COUNT(*) FROM event_observations"
            ),
            "logical_prompts": _count(connection, "SELECT COUNT(*) FROM prompts"),
            "stored_prompt_bodies": _count(
                connection, "SELECT COUNT(*) FROM prompts WHERE length(text) > 0"
            ),
            "prompt_replay_observations": _count(
                connection,
                """
                SELECT COUNT(*)
                FROM prompt_observations AS observations
                JOIN prompts ON prompts.id = observations.prompt_id
                WHERE observations.observed_session_id != prompts.origin_session_id
                """,
            ),
            "prompt_items_redacted": _count(
                connection,
                "SELECT COUNT(*) FROM prompts WHERE redaction_status IN "
                "('redacted', 'redacted_and_truncated')",
            ),
            "prompt_items_truncated": _count(
                connection,
                "SELECT COUNT(*) FROM prompts WHERE redaction_status IN "
                "('truncated', 'redacted_and_truncated')",
            ),
            "command_metadata": _count(
                connection,
                "SELECT COUNT(*) FROM tool_activity WHERE command_fingerprint IS NOT NULL",
            ),
            "stored_command_text": _count(
                connection,
                "SELECT COUNT(*) FROM tool_activity WHERE command_text IS NOT NULL",
            ),
            "command_items_redacted": _count(
                connection,
                "SELECT COUNT(*) FROM tool_activity "
                "WHERE command_fingerprint IS NOT NULL AND redacted = 1",
            ),
            "command_items_truncated": _count(
                connection,
                "SELECT COUNT(*) FROM tool_activity "
                "WHERE command_fingerprint IS NOT NULL AND truncated = 1",
            ),
            "git_correlations": _count(
                connection, "SELECT COUNT(*) FROM session_commit_associations"
            ),
            "outcomes": _count(connection, "SELECT COUNT(*) FROM session_outcomes"),
            "taxonomy": _count(connection, "SELECT COUNT(*) FROM session_tasks"),
            "managed_report_metadata": 0,
        }
    return PrivacyInspection(
        database_path=resolved_database,
        config_path=policy_path,
        config_exists=policy_path.is_file(),
        policy=policy,
        counts=counts,
    )


def purge_derived_content(
    database_path: Path,
    *,
    codex_home: Path,
    target: PurgeTarget,
) -> PurgeResult:
    """Remove only selected text from the derived database with secure deletion enabled."""

    resolved_database = database_path.expanduser().resolve(strict=False)
    with closing(open_index(resolved_database, codex_home=codex_home)) as connection:
        connection.execute("PRAGMA secure_delete = ON")
        if target is PurgeTarget.PROMPTS:
            affected = _count(connection, "SELECT COUNT(*) FROM prompts")
            with connection:
                connection.execute("DELETE FROM prompts")
                connection.execute("INSERT INTO prompts_fts(prompts_fts) VALUES ('rebuild')")
        else:
            rows = connection.execute(
                "SELECT id, command_text, command_operation FROM tool_activity "
                "WHERE command_text IS NOT NULL"
            ).fetchall()
            affected = len(rows)
            from codex_insights.git_correlation import is_git_commit_command

            with connection:
                for row in rows:
                    if row["command_operation"] is None and is_git_commit_command(
                        str(row["command_text"])
                    ):
                        connection.execute(
                            "UPDATE tool_activity SET command_operation = 'git_commit' "
                            "WHERE id = ?",
                            (int(row["id"]),),
                        )
                connection.execute(
                    "UPDATE tool_activity SET command_text = NULL WHERE command_text IS NOT NULL"
                )
        _checkpoint_if_possible(connection)
    return PurgeResult(
        target=target,
        affected_items=affected,
        database_path=resolved_database,
    )


def _count(connection: sqlite3.Connection, query: str) -> int:
    return int(connection.execute(query).fetchone()[0])


def _checkpoint_if_possible(connection: sqlite3.Connection) -> None:
    with suppress(sqlite3.OperationalError):
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def utc_now() -> str:
    """Return one stable UTC timestamp for exports and backup metadata."""

    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
