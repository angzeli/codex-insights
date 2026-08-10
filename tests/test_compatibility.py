from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from codex_insights.adapters import CodexLocalAdapter
from codex_insights.adapters.base import SourceChangedDuringParseError
from codex_insights.adapters.codex_index import select_state_database
from codex_insights.config import resolve_codex_home
from codex_insights.indexer import index_source
from codex_insights.models import CapabilityStatus, SourceCapability

FIXTURES = Path(__file__).parent / "fixtures" / "compatibility"


def _home_from_version(tmp_path: Path, version: str) -> Path:
    home = tmp_path / f"codex-{version}"
    home.mkdir()
    rollout_source = FIXTURES / version / "rollout.jsonl"
    shutil.copyfile(rollout_source, home / "rollout.jsonl")
    state_fixture = FIXTURES / version / "state.sql"
    sql = (
        state_fixture.read_text(encoding="utf-8")
        if state_fixture.exists()
        else (
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, "
            "created_at TEXT, updated_at TEXT, archived INTEGER DEFAULT 0);"
            f"INSERT INTO threads VALUES ('{version}', 'rollout.jsonl', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', 0);"
        )
    )
    with sqlite3.connect(home / "state_12.sqlite") as connection:
        connection.executescript(sql)
    return home


def _capabilities(parsed: object) -> dict[SourceCapability, CapabilityStatus]:
    return {item.capability: item.status for item in parsed.capabilities}  # type: ignore[attr-defined]


def test_current_and_renamed_catalogue_schemas_are_detected(tmp_path: Path) -> None:
    current = _home_from_version(tmp_path, "version_a")
    renamed = _home_from_version(tmp_path, "version_b")

    current_candidates, current_warnings = CodexLocalAdapter(
        resolve_codex_home(current)
    ).discover_sessions()
    renamed_candidates, renamed_warnings = CodexLocalAdapter(
        resolve_codex_home(renamed)
    ).discover_sessions()

    assert current_candidates[0].session.source_session_id == "version-a"
    assert renamed_candidates[0].session.source_session_id == "version-b"
    assert current_candidates[0].source_schema_fingerprint
    assert renamed_candidates[0].source_schema_fingerprint
    assert current_candidates[0].source_schema_fingerprint != (
        renamed_candidates[0].source_schema_fingerprint
    )
    assert current_warnings == ()
    assert renamed_warnings == ()


def test_unknown_records_are_counted_without_raw_payload_persistence(tmp_path: Path) -> None:
    home = _home_from_version(tmp_path, "version_c")
    database = tmp_path / "index.sqlite3"

    report = index_source(
        CodexLocalAdapter(resolve_codex_home(home)),
        database,
        codex_home=home,
    )

    assert report.failed == 0
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            """
            SELECT unknown_kind, unknown_name, record_count
            FROM unknown_source_records ORDER BY unknown_kind, unknown_name
            """
        ).fetchall()
    assert ("record_type", "future_record_v2", 1) in rows
    assert ("payload_type", "future_payload_v2", 1) in rows
    database_bytes = database.read_bytes()
    assert b'"future_field":true' not in database_bytes
    assert b'"future_payload_field":1' not in database_bytes


def test_removed_optional_fields_degrade_to_explicit_not_observed(tmp_path: Path) -> None:
    home = _home_from_version(tmp_path, "version_d")
    adapter = CodexLocalAdapter(resolve_codex_home(home))
    candidate = adapter.discover_sessions()[0][0]

    parsed = adapter.parse_session(candidate)
    capabilities = _capabilities(parsed)

    assert capabilities[SourceCapability.TOKEN_USAGE] is CapabilityStatus.NOT_OBSERVED
    assert capabilities[SourceCapability.DURATION_TIMESTAMPS] is CapabilityStatus.NOT_OBSERVED
    assert parsed.session.model is None


def test_changed_tool_encoding_is_degraded_and_reported(tmp_path: Path) -> None:
    home = _home_from_version(tmp_path, "version_e")
    adapter = CodexLocalAdapter(resolve_codex_home(home))
    parsed = adapter.parse_session(adapter.discover_sessions()[0][0])
    capabilities = _capabilities(parsed)

    assert capabilities[SourceCapability.TOOL_ACTIVITY] is CapabilityStatus.DEGRADED
    assert capabilities[SourceCapability.COMMAND_EXTRACTION] is CapabilityStatus.DEGRADED
    assert any(
        item.kind == "tool_encoding" and item.name == "tool_invocation_v2"
        for item in parsed.unknown_source_records
    )


def test_partial_final_line_preserves_valid_prefix(tmp_path: Path) -> None:
    home = _home_from_version(tmp_path, "version_f")
    rollout = home / "rollout.jsonl"
    rollout.write_bytes(rollout.read_bytes().rstrip(b"\n"))
    adapter = CodexLocalAdapter(resolve_codex_home(home))
    candidate = adapter.discover_sessions()[0][0]

    parsed = adapter.parse_session(candidate)

    assert parsed.valid_record_count == 1
    assert parsed.partial_final_line is True
    assert parsed.malformed_line_count == 0


def test_semantic_token_regression_produces_warning(tmp_path: Path) -> None:
    home = _home_from_version(tmp_path, "version_h")
    adapter = CodexLocalAdapter(resolve_codex_home(home))
    parsed = adapter.parse_session(adapter.discover_sessions()[0][0])

    assert [warning.code for warning in parsed.semantic_warnings] == [
        "cumulative_token_decrease"
    ]
    assert parsed.semantic_warnings[0].count == 3


def test_state_database_selection_prefers_consistent_source_not_version_name(
    tmp_path: Path,
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    shutil.copyfile(FIXTURES / "version_g" / "rollout.jsonl", home / "rollout.jsonl")
    for name, fixture in (
        ("state_1.sqlite", "preferred.sql"),
        ("state_99.sqlite", "alternative.sql"),
    ):
        with sqlite3.connect(home / name) as connection:
            connection.executescript(
                (FIXTURES / "version_g" / fixture).read_text(encoding="utf-8")
            )

    selection = select_state_database(home)
    candidates, warnings = CodexLocalAdapter(resolve_codex_home(home)).discover_sessions()

    assert selection.selected is not None
    assert selection.selected.path.name == "state_1.sqlite"
    assert candidates[0].session.source_session_id == "preferred"
    assert any("not combined" in warning for warning in warnings)


def test_previous_good_state_survives_partial_write_and_recovers(
    synthetic_audit_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(synthetic_audit_home))
    index_source(adapter, database, codex_home=synthetic_audit_home)
    rollout = (
        synthetic_audit_home
        / "sessions"
        / "2026"
        / "08"
        / "09"
        / "rollout-modern.jsonl"
    )
    good_content = rollout.read_bytes()
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            """
            SELECT usage.total_tokens, session_compatibility.last_successful_parse_at
            FROM usage
            JOIN source_sessions ON source_sessions.id = usage.source_session_id
            JOIN session_compatibility
              ON session_compatibility.source_session_id = source_sessions.id
            WHERE source_sessions.source_session_id = 'synthetic-thread-modern'
            """
        ).fetchone()

    rollout.write_bytes(
        good_content + b'{"type":"event_msg","payload":{"type":"token_count"'
    )
    partial = index_source(adapter, database, codex_home=synthetic_audit_home)

    with sqlite3.connect(database) as connection:
        during = connection.execute(
            """
            SELECT usage.total_tokens, session_compatibility.last_successful_parse_at,
                   session_compatibility.parse_status, session_compatibility.stale
            FROM usage
            JOIN source_sessions ON source_sessions.id = usage.source_session_id
            JOIN session_compatibility
              ON session_compatibility.source_session_id = source_sessions.id
            WHERE source_sessions.source_session_id = 'synthetic-thread-modern'
            """
        ).fetchone()
    assert partial.skipped == 2
    assert tuple(during[:2]) == tuple(before)
    assert tuple(during[2:]) == ("pending_partial_write", 1)

    rollout.write_bytes(
        good_content
        + (
            b'{"timestamp":"2026-08-09T00:06:00Z","type":"event_msg",'
            b'"payload":{"type":"token_count","info":{"total_token_usage":'
            b'{"input_tokens":250,"output_tokens":50,"total_tokens":300}}}}\n'
        )
    )
    recovered = index_source(adapter, database, codex_home=synthetic_audit_home)

    with sqlite3.connect(database) as connection:
        after = connection.execute(
            """
            SELECT usage.total_tokens, session_compatibility.parse_status,
                   session_compatibility.stale
            FROM usage
            JOIN source_sessions ON source_sessions.id = usage.source_session_id
            JOIN session_compatibility
              ON session_compatibility.source_session_id = source_sessions.id
            WHERE source_sessions.source_session_id = 'synthetic-thread-modern'
            """
        ).fetchone()
    assert recovered.updated == 1
    assert tuple(after) == (300, "indexed_with_warnings", 0)


def test_previous_good_state_survives_parse_failure(
    synthetic_audit_home: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite3"
    base = CodexLocalAdapter(resolve_codex_home(synthetic_audit_home))
    index_source(base, database, codex_home=synthetic_audit_home)
    rollout = (
        synthetic_audit_home
        / "sessions"
        / "2026"
        / "08"
        / "09"
        / "rollout-modern.jsonl"
    )
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            """
            SELECT usage.total_tokens, COUNT(events.id)
            FROM source_sessions AS sessions
            JOIN usage ON usage.source_session_id = sessions.id
            LEFT JOIN event_observations AS events ON events.observed_session_id = sessions.id
            WHERE sessions.source_session_id = 'synthetic-thread-modern'
            GROUP BY sessions.id
            """
        ).fetchone()
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"future_record"}\n')

    class FailingAdapter:
        name = base.name
        parser_version = base.parser_version

        def discover_sessions(self):  # type: ignore[no-untyped-def]
            return base.discover_sessions()

        def parse_session(self, candidate):  # type: ignore[no-untyped-def]
            if candidate.session.source_session_id == "synthetic-thread-modern":
                raise ValueError("synthetic failure without payload")
            return base.parse_session(candidate)

    report = index_source(FailingAdapter(), database, codex_home=synthetic_audit_home)
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            """
            SELECT usage.total_tokens, COUNT(events.id), compatibility.parse_status,
                   compatibility.stale
            FROM source_sessions AS sessions
            JOIN usage ON usage.source_session_id = sessions.id
            JOIN session_compatibility AS compatibility
              ON compatibility.source_session_id = sessions.id
            LEFT JOIN event_observations AS events ON events.observed_session_id = sessions.id
            WHERE sessions.source_session_id = 'synthetic-thread-modern'
            GROUP BY sessions.id
            """
        ).fetchone()
    assert report.failed == 1
    assert tuple(after[:2]) == tuple(before)
    assert tuple(after[2:]) == ("failed", 1)


def test_source_change_signal_retries_once(tmp_path: Path) -> None:
    home = _home_from_version(tmp_path, "version_a")
    database = tmp_path / "index.sqlite3"
    base = CodexLocalAdapter(resolve_codex_home(home))

    class OneMutationAdapter:
        name = base.name
        parser_version = base.parser_version
        attempts = 0

        def discover_sessions(self):  # type: ignore[no-untyped-def]
            return base.discover_sessions()

        def parse_session(self, candidate):  # type: ignore[no-untyped-def]
            self.attempts += 1
            if self.attempts == 1:
                raise SourceChangedDuringParseError("synthetic_live_append")
            return base.parse_session(candidate)

    adapter = OneMutationAdapter()
    report = index_source(adapter, database, codex_home=home)

    assert report.new == 1
    assert report.failed == 0
    assert adapter.attempts == 2


def test_major_prompt_coverage_drop_is_warned_without_guessing(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    with sqlite3.connect(home / "state_1.sqlite") as connection:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
                created_at TEXT, updated_at TEXT, source TEXT
            )
            """
        )
        for index in range(10):
            rollout = home / f"rollout-{index}.jsonl"
            rollout.write_text(
                '{"timestamp":"2026-02-01T00:00:00Z","type":"event_msg",'
                '"payload":{"type":"user_message","message":"synthetic prompt"}}\n',
                encoding="utf-8",
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, 'cli')",
                (
                    f"session-{index}",
                    rollout.name,
                    "2026-02-01T00:00:00Z",
                    "2026-02-01T00:01:00Z",
                ),
            )
    database = tmp_path / "index.sqlite3"
    adapter = CodexLocalAdapter(resolve_codex_home(home))
    first = index_source(adapter, database, codex_home=home)
    assert first.new == 10

    for index in range(10):
        (home / f"rollout-{index}.jsonl").write_text(
            '{"timestamp":"2026-02-01T00:02:00Z","type":"event_msg",'
            '"payload":{"type":"agent_message"}}\n',
            encoding="utf-8",
        )
    second = index_source(adapter, database, codex_home=home)

    assert second.updated == 10
    assert any("prompt_content coverage fell" in warning for warning in second.warnings)
