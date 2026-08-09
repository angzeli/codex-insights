"""Versioned, descriptive prompt features without quality scoring."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

PROMPT_FEATURE_VERSION = "prompt-features-v1"
REQUIREMENT_HEURISTIC_VERSION = "approx-requirements-v1"

_HEADING = re.compile(
    r"(?im)^\s*(?:#{1,6}\s+|(?:goal|requirements?|constraints?|validation|tests?|"
    r"acceptance criteria|scope|non-goals?)\s*:|(?:目標|目标|要求|約束|约束|驗收標準|"
    r"验收标准|測試|测试|範圍|范围|非目標|非目标)\s*[：:])"
)
_ACCEPTANCE = re.compile(r"(?i)\bacceptance criteria\b|驗收標準|验收标准")
_VALIDATION = re.compile(
    r"(?i)\b(pytest|test(?:s|ing)?|validate|validation|verify|ruff|mypy)\b|"
    r"測試|测试|驗證|验证|校驗|校验"
)
_PATH = re.compile(
    r"(?<![\w.])(?:\.{0,2}/|~/|/)[^\s'\"`<>]+|"
    r"(?<![\w.])[\w.-]+\.(?:py|md|toml|json|jsonl|yaml|yml|sql|sh|tsx?|jsx?|cpp|c|h)\b",
    re.IGNORECASE,
)
_COMMIT = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:please\s+)?(?:git\s+)?commit\b|"
    r"\b(?:create|make|produce)\s+(?:\w+\s+){0,3}commits?\b|"
    r"\bcommits?\s+(?:this|it|the|these|changes?)\b|"
    r"(?:請|请)?提交(?:這些|这些|更改|修改|代碼|代码)"
)
_MULTIPLE_COMMITS = re.compile(
    r"(?i)\b(?:two|three|multiple|separate|focused|atomic|a few)\s+commits?\b|"
    r"\bcommits?\s+(?:separately|individually)\b|"
    r"(?:兩個|两个|三個|三个|多個|多个|分開|分别|各自|幾個|几个).{0,8}提交"
)
_NON_GOAL = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:do not|don't|never|non-goals?\s*:|"
    r"不要|不得|切勿|非目標\s*[：:]|非目标\s*[：:])"
)
_READ_ONLY = re.compile(
    r"(?i)\bread[- ]only\b|\bdo not (?:write|modify|change|delete)\b|"
    r"只讀|只读|不得(?:寫入|写入|修改|刪除|删除)|不要(?:寫入|写入|修改|刪除|删除)"
)
_REQUIREMENT_LINE = re.compile(
    r"(?im)^\s*(?:[-*+]\s+|\d+[.)]\s+|(?:must|should|support|ensure|add|"
    r"implement|create|do not|never)\b|(?:必須|必须|應|应|需要|支持|實現|实现|"
    r"添加|不要|不得)\b)"
)


@dataclass(frozen=True, slots=True)
class PromptFeatures:
    character_length: int
    stored_character_length: int
    line_count: int
    structured_heading_count: int
    has_acceptance_criteria: bool
    requests_validation: bool
    path_reference_count: int
    requests_commit: bool
    requests_multiple_commits: bool
    has_explicit_non_goals: bool
    has_read_only_constraint: bool
    approximate_requirement_count: int
    source_truncated: bool
    feature_version: str = PROMPT_FEATURE_VERSION
    requirement_heuristic_version: str = REQUIREMENT_HEURISTIC_VERSION


def extract_prompt_features(
    text: str,
    *,
    original_character_count: int | None = None,
    source_truncated: bool = False,
) -> PromptFeatures:
    """Return privacy-safe descriptive features over already-redacted prompt text."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    positive_intent = "\n".join(
        line for line in normalized.splitlines() if not _NON_GOAL.search(line)
    )
    multiple_commits = bool(_MULTIPLE_COMMITS.search(positive_intent))
    requirement_lines = sum(1 for _ in _REQUIREMENT_LINE.finditer(normalized))
    approximate_requirements = requirement_lines
    if normalized.strip() and approximate_requirements == 0:
        approximate_requirements = 1
    return PromptFeatures(
        character_length=(
            max(original_character_count, 0)
            if original_character_count is not None
            else len(text)
        ),
        stored_character_length=len(text),
        line_count=normalized.count("\n") + 1 if normalized else 0,
        structured_heading_count=sum(1 for _ in _HEADING.finditer(normalized)),
        has_acceptance_criteria=bool(_ACCEPTANCE.search(normalized)),
        requests_validation=bool(_VALIDATION.search(positive_intent)),
        path_reference_count=sum(1 for _ in _PATH.finditer(normalized)),
        requests_commit=bool(_COMMIT.search(positive_intent)) or multiple_commits,
        requests_multiple_commits=multiple_commits,
        has_explicit_non_goals=bool(_NON_GOAL.search(normalized)),
        has_read_only_constraint=bool(_READ_ONLY.search(normalized)),
        approximate_requirement_count=approximate_requirements,
        source_truncated=source_truncated,
    )


def reconcile_prompt_features(connection: sqlite3.Connection) -> None:
    """Refresh prompt features idempotently from safe logical prompt rows."""

    prompt_ids: set[int] = set()
    for row in connection.execute(
        "SELECT id, text, original_character_count, redaction_status FROM prompts ORDER BY id"
    ):
        prompt_id = int(row["id"])
        prompt_ids.add(prompt_id)
        features = extract_prompt_features(
            str(row["text"]),
            original_character_count=int(row["original_character_count"]),
            source_truncated="truncated" in str(row["redaction_status"]),
        )
        _upsert_features(connection, prompt_id, features)
    if prompt_ids:
        placeholders = ",".join("?" for _ in prompt_ids)
        with connection:
            connection.execute(
                f"DELETE FROM prompt_features WHERE prompt_id NOT IN ({placeholders})",
                tuple(sorted(prompt_ids)),
            )
    else:
        with connection:
            connection.execute("DELETE FROM prompt_features")


def _upsert_features(
    connection: sqlite3.Connection,
    prompt_id: int,
    features: PromptFeatures,
) -> None:
    values: tuple[object, ...] = (
        features.character_length,
        features.stored_character_length,
        features.line_count,
        features.structured_heading_count,
        int(features.has_acceptance_criteria),
        int(features.requests_validation),
        features.path_reference_count,
        int(features.requests_commit),
        int(features.requests_multiple_commits),
        int(features.has_explicit_non_goals),
        int(features.has_read_only_constraint),
        features.approximate_requirement_count,
        int(features.source_truncated),
        features.feature_version,
        features.requirement_heuristic_version,
    )
    existing = connection.execute(
        """
        SELECT character_length, stored_character_length, line_count,
               structured_heading_count, has_acceptance_criteria,
               requests_validation, path_reference_count, requests_commit,
               requests_multiple_commits, has_explicit_non_goals,
               has_read_only_constraint, approximate_requirement_count,
               source_truncated, feature_version, requirement_heuristic_version
        FROM prompt_features WHERE prompt_id = ?
        """,
        (prompt_id,),
    ).fetchone()
    if existing is not None and tuple(existing) == values:
        return
    now = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    with connection:
        connection.execute(
            """
            INSERT INTO prompt_features(
                prompt_id, character_length, stored_character_length, line_count,
                structured_heading_count, has_acceptance_criteria,
                requests_validation, path_reference_count, requests_commit,
                requests_multiple_commits, has_explicit_non_goals,
                has_read_only_constraint, approximate_requirement_count,
                source_truncated, feature_version, requirement_heuristic_version,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(prompt_id) DO UPDATE SET
                character_length = excluded.character_length,
                stored_character_length = excluded.stored_character_length,
                line_count = excluded.line_count,
                structured_heading_count = excluded.structured_heading_count,
                has_acceptance_criteria = excluded.has_acceptance_criteria,
                requests_validation = excluded.requests_validation,
                path_reference_count = excluded.path_reference_count,
                requests_commit = excluded.requests_commit,
                requests_multiple_commits = excluded.requests_multiple_commits,
                has_explicit_non_goals = excluded.has_explicit_non_goals,
                has_read_only_constraint = excluded.has_read_only_constraint,
                approximate_requirement_count = excluded.approximate_requirement_count,
                source_truncated = excluded.source_truncated,
                feature_version = excluded.feature_version,
                requirement_heuristic_version = excluded.requirement_heuristic_version,
                updated_at = excluded.updated_at
            """,
            (prompt_id, *values, now),
        )
