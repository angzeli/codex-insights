from __future__ import annotations

import sqlite3

from codex_insights.analytics.prompts import _resolve_prompt_id
from codex_insights.analytics.provenance import _resolve_session as resolve_provenance_session
from codex_insights.analytics.tools import _resolve_session as resolve_tool_session


def test_session_resolvers_prefer_exact_ids_and_escape_like_wildcards() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE source_sessions (id INTEGER PRIMARY KEY, source_session_id TEXT)"
    )
    connection.executemany(
        "INSERT INTO source_sessions VALUES (?, ?)",
        (
            (1, "exact"),
            (2, "exact-longer"),
            (3, "literal%-session"),
            (4, "literalX-session"),
        ),
    )

    assert resolve_tool_session(connection, "exact") == 1
    assert resolve_provenance_session(connection, "exact") == (1, "exact")
    assert resolve_tool_session(connection, "literal%") == 3
    assert resolve_provenance_session(connection, "literal%") == (
        3,
        "literal%-session",
    )


def test_prompt_resolver_prefers_exact_ids_and_escapes_like_wildcards() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE prompts (prompt_id TEXT PRIMARY KEY)")
    connection.executemany(
        "INSERT INTO prompts VALUES (?)",
        (("exact",), ("exact-longer",), ("literal%-prompt",), ("literalX-prompt",)),
    )

    assert _resolve_prompt_id(connection, "exact") == "exact"
    assert _resolve_prompt_id(connection, "literal%") == "literal%-prompt"
