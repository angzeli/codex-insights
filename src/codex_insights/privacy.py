"""Small, deterministic privacy filters for searchable prompt text."""

from __future__ import annotations

import re
from dataclasses import dataclass

PROMPT_CONTENT_SCHEMA_VERSION = "prompt-content-v1"
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
