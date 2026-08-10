"""Origin-aware prompt history and SQLite FTS queries."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codex_insights.db import open_index

from .prefixes import sqlite_like_prefix


class PromptNotFoundError(LookupError):
    """Raised when no logical prompt matches an identifier prefix."""


class AmbiguousPromptIdError(LookupError):
    """Raised when a logical prompt prefix is not unique."""


class PromptSearchQueryError(ValueError):
    """Raised when SQLite FTS5 rejects a query expression."""


@dataclass(frozen=True, slots=True)
class PromptFilters:
    since: datetime | None = None
    until: datetime | None = None
    repository: str | None = None
    model: str | None = None
    session: str | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class PromptListItem:
    prompt_id: str
    occurred_at: datetime | None
    repository: str
    model: str | None
    origin_session_id: str
    redaction_status: str
    observation_session_count: int
    replay_session_count: int
    snippet: str

    def to_dict(self) -> dict[str, object]:
        return {
            "prompt_id": self.prompt_id,
            "occurred_at": _json_datetime(self.occurred_at),
            "repository": self.repository,
            "model": self.model,
            "origin_session_id": self.origin_session_id,
            "redaction_status": self.redaction_status,
            "observation_session_count": self.observation_session_count,
            "replay_session_count": self.replay_session_count,
            "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class PromptDetail:
    prompt_id: str
    origin_session_id: str
    occurred_at: datetime | None
    repository: str
    model: str | None
    prompt_ordinal: int
    text: str
    redaction_status: str
    redaction_count: int
    original_character_count: int
    stored_character_count: int
    provenance_status: str
    provenance_confidence: str
    user_authorship_evidence: str
    observation_session_count: int
    replay_session_count: int

    def to_dict(self, *, include_text: bool = True) -> dict[str, object]:
        return {
            "prompt_id": self.prompt_id,
            "origin_session_id": self.origin_session_id,
            "occurred_at": _json_datetime(self.occurred_at),
            "repository": self.repository,
            "model": self.model,
            "prompt_ordinal": self.prompt_ordinal,
            "text": self.text if include_text else None,
            "redaction_status": self.redaction_status,
            "redaction_count": self.redaction_count,
            "original_character_count": self.original_character_count,
            "stored_character_count": self.stored_character_count,
            "provenance_status": self.provenance_status,
            "provenance_confidence": self.provenance_confidence,
            "user_authorship_evidence": self.user_authorship_evidence,
            "observation_session_count": self.observation_session_count,
            "replay_session_count": self.replay_session_count,
            "full_text_included": include_text,
        }


@dataclass(frozen=True, slots=True)
class PromptSearchResult(PromptListItem):
    rank: float

    def to_dict(self) -> dict[str, object]:
        return {**PromptListItem.to_dict(self), "rank": self.rank}


def list_prompts(
    database_path: Path,
    *,
    codex_home: Path,
    filters: PromptFilters | None = None,
) -> tuple[PromptListItem, ...]:
    selected = filters or PromptFilters()
    where, parameters = _filters(selected)
    query = _prompt_select("substr(replace(replace(p.text, char(10), ' '), char(13), ' '), 1, 180)")
    query += f" {where} ORDER BY event_time IS NULL, event_time DESC, p.prompt_id LIMIT ?"
    parameters.append(selected.limit)
    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        rows = connection.execute(query, parameters).fetchall()
    return tuple(_list_item(row) for row in rows)


def get_prompt(
    database_path: Path,
    *,
    codex_home: Path,
    prompt_prefix: str,
) -> PromptDetail:
    with closing(open_index(database_path, codex_home=codex_home)) as connection:
        prompt_id = _resolve_prompt_id(connection, prompt_prefix)
        row = connection.execute(
            _prompt_select("p.text") + " WHERE p.prompt_id = ?",
            (prompt_id,),
        ).fetchone()
    if row is None:
        raise PromptNotFoundError(prompt_prefix)
    return PromptDetail(
        prompt_id=str(row["prompt_id"]),
        origin_session_id=str(row["origin_session_id"]),
        occurred_at=_stored_datetime(row["event_time"]),
        repository=_repository_label(row["repository_name"], row["repository_root"]),
        model=_optional_str(row["model"]),
        prompt_ordinal=int(row["prompt_ordinal"]),
        text=str(row["snippet"]),
        redaction_status=str(row["redaction_status"]),
        redaction_count=int(row["redaction_count"]),
        original_character_count=int(row["original_character_count"]),
        stored_character_count=int(row["stored_character_count"]),
        provenance_status=str(row["provenance_status"]),
        provenance_confidence=str(row["provenance_confidence"]),
        user_authorship_evidence=str(row["user_authorship_evidence"]),
        observation_session_count=int(row["observation_session_count"]),
        replay_session_count=int(row["replay_session_count"]),
    )


def search_prompts(
    database_path: Path,
    *,
    codex_home: Path,
    query: str,
    filters: PromptFilters | None = None,
) -> tuple[PromptSearchResult, ...]:
    selected = filters or PromptFilters()
    if not query.strip():
        raise PromptSearchQueryError("Search query cannot be empty")
    where, parameters = _filters(selected, table_prefix="p")
    match_where = "prompts_fts MATCH ?"
    where = f"WHERE {match_where}" + (f" AND {where.removeprefix('WHERE ')}" if where else "")
    parameters.insert(0, query)
    sql = _prompt_select(
        "snippet(prompts_fts, 0, '[', ']', '…', 18)",
        fts=True,
    )
    sql += f" {where} ORDER BY rank, event_time IS NULL, event_time DESC, p.prompt_id LIMIT ?"
    parameters.append(selected.limit)
    try:
        with closing(open_index(database_path, codex_home=codex_home)) as connection:
            rows = connection.execute(sql, parameters).fetchall()
    except sqlite3.OperationalError as exc:
        raise PromptSearchQueryError(f"Invalid FTS5 query: {type(exc).__name__}") from exc
    results: list[PromptSearchResult] = []
    for row in rows:
        item = _list_item(row)
        results.append(
            PromptSearchResult(
                prompt_id=item.prompt_id,
                occurred_at=item.occurred_at,
                repository=item.repository,
                model=item.model,
                origin_session_id=item.origin_session_id,
                redaction_status=item.redaction_status,
                observation_session_count=item.observation_session_count,
                replay_session_count=item.replay_session_count,
                snippet=item.snippet,
                rank=float(row["rank"]),
            )
        )
    return tuple(results)


def _prompt_select(snippet_expression: str, *, fts: bool = False) -> str:
    fts_join = "JOIN prompts_fts ON prompts_fts.rowid = p.id" if fts else ""
    rank = ", bm25(prompts_fts) AS rank" if fts else ""
    return f"""
        SELECT p.prompt_id, p.prompt_ordinal, p.redaction_status, p.redaction_count,
               p.original_character_count, p.stored_character_count,
               p.provenance_status, p.provenance_confidence,
               p.user_authorship_evidence,
               s.source_session_id AS origin_session_id,
               s.repository_name, s.repository_root, s.model,
               COALESCE(p.occurred_at, s.started_at) AS event_time,
               {snippet_expression} AS snippet,
               (SELECT COUNT(DISTINCT observations.observed_session_id)
                FROM prompt_observations AS observations
                WHERE observations.prompt_id = p.id) AS observation_session_count,
               (SELECT COUNT(DISTINCT observations.observed_session_id)
                FROM prompt_observations AS observations
                WHERE observations.prompt_id = p.id
                  AND observations.observed_session_id != p.origin_session_id)
                   AS replay_session_count
               {rank}
        FROM prompts AS p
        JOIN source_sessions AS s ON s.id = p.origin_session_id
        {fts_join}
    """


def _filters(
    filters: PromptFilters,
    *,
    table_prefix: str = "p",
) -> tuple[str, list[object]]:
    if filters.limit < 1:
        raise ValueError("limit must be at least 1")
    conditions: list[str] = []
    parameters: list[object] = []
    event_time = f"COALESCE({table_prefix}.occurred_at, s.started_at)"
    if filters.since is not None:
        conditions.append(f"{event_time} >= ?")
        parameters.append(_database_datetime(filters.since))
    if filters.until is not None:
        conditions.append(f"{event_time} < ?")
        parameters.append(_database_datetime(filters.until))
    if filters.repository:
        conditions.append("(s.repository_name = ? COLLATE NOCASE OR s.repository_root = ?)")
        parameters.extend((filters.repository, filters.repository))
    if filters.model:
        conditions.append("s.model = ? COLLATE NOCASE")
        parameters.append(filters.model)
    if filters.session:
        conditions.append("s.source_session_id LIKE ? ESCAPE '\\'")
        parameters.append(sqlite_like_prefix(filters.session))
    return (f"WHERE {' AND '.join(conditions)}" if conditions else "", parameters)


def _resolve_prompt_id(connection: sqlite3.Connection, prefix: str) -> str:
    exact = connection.execute(
        "SELECT prompt_id FROM prompts WHERE prompt_id = ?",
        (prefix,),
    ).fetchone()
    if exact is not None:
        return str(exact["prompt_id"])
    rows = connection.execute(
        "SELECT prompt_id FROM prompts WHERE prompt_id LIKE ? ESCAPE '\\' "
        "ORDER BY prompt_id LIMIT 2",
        (sqlite_like_prefix(prefix),),
    ).fetchall()
    if not rows:
        raise PromptNotFoundError(prefix)
    if len(rows) > 1:
        raise AmbiguousPromptIdError(prefix)
    return str(rows[0]["prompt_id"])


def _list_item(row: sqlite3.Row) -> PromptListItem:
    return PromptListItem(
        prompt_id=str(row["prompt_id"]),
        occurred_at=_stored_datetime(row["event_time"]),
        repository=_repository_label(row["repository_name"], row["repository_root"]),
        model=_optional_str(row["model"]),
        origin_session_id=str(row["origin_session_id"]),
        redaction_status=str(row["redaction_status"]),
        observation_session_count=int(row["observation_session_count"]),
        replay_session_count=int(row["replay_session_count"]),
        snippet=str(row["snippet"]),
    )


def _repository_label(name: Any, root: Any) -> str:
    normalized_name = _optional_str(name)
    if normalized_name:
        return normalized_name
    normalized_root = _optional_str(root)
    return Path(normalized_root).name if normalized_root else "Outside Git repositories"


def _database_datetime(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _stored_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _json_datetime(value: datetime | None) -> str | None:
    return _database_datetime(value) if value is not None else None


def _as_utc(value: datetime) -> datetime:
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
