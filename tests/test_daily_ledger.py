from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

import codex_insights.daily_ledger.git_sync as git_sync_module
import codex_insights.daily_ledger.service as service_module
from codex_insights.adapters.base import SourceChangedDuringParseError
from codex_insights.cli import app
from codex_insights.daily_ledger.config import (
    LEDGER_SCHEMA_VERSION,
    LedgerConfig,
    LedgerConfigurationError,
    LedgerPaths,
    default_ledger_paths,
    load_ledger_config,
)
from codex_insights.daily_ledger.doctor import run_doctor
from codex_insights.daily_ledger.exporter import export_day
from codex_insights.daily_ledger.git_sync import (
    GitSyncError,
    NonAllowlistedDirtyError,
    is_allowlisted_path,
)
from codex_insights.daily_ledger.privacy import (
    MAX_JSONL_RECORD_BYTES,
    sanitize_remote_text,
    scan_remote_file,
    scan_remote_value,
)
from codex_insights.daily_ledger.queue import (
    STOP_STDOUT,
    capture_hook_payload,
    run_capture_hook,
)
from codex_insights.daily_ledger.report_policy import (
    DEFAULT_REPORT_POLICY_TEXT,
    ReportPolicyError,
    parse_reporting_policy,
    report_date_for_timestamp,
    reporting_window_for_date,
)
from codex_insights.daily_ledger.schema_validation import validate_generated_document
from codex_insights.daily_ledger.service import backfill_range, flush_queue

runner = CliRunner()


@pytest.mark.parametrize("tail,category", [
    (b'{"safe":true}\n', None),
    (b'{"value":"password=synthetic"}\n', "credential_like"),
    (b'{"value":"/Users/example/private"}\n', "absolute_path"),
    (b'{"prompt":"forbidden"}\n', "forbidden_field"),
    (b'{invalid}\n', "malformed_jsonl_record"),
    (b'{"value":NaN}\n', "malformed_jsonl_record"),
    (b'{"value":"\xff"}\n', "malformed_jsonl_record"),
    (b'x' * (MAX_JSONL_RECORD_BYTES + 1), "oversized_jsonl_record"),
])
def test_large_jsonl_streams_and_checks_late_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail: bytes, category: str | None,
) -> None:
    path = tmp_path / "events.jsonl"
    safe = b'{"value":"' + b'safe event ' * 100 + b'"}\n'
    with path.open("wb") as stream:
        for _ in range(2200):
            stream.write(safe)
        stream.write(tail)
        stream.write(b'{"safe":true}\n')
    assert path.stat().st_size > 2 * 1024 * 1024
    original_open = Path.open

    class BoundedReader:
        def __enter__(self):
            self.stream = original_open(path, "rb")
            return self

        def __exit__(self, *args):
            self.stream.close()

        def readline(self, size):
            assert 0 < size <= MAX_JSONL_RECORD_BYTES + 1
            return self.stream.readline(size)

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: BoundedReader())
    findings = scan_remote_file(path, location="events.jsonl")
    if category is None:
        assert not findings
    else:
        assert findings[0].category == category
        assert findings[0].location.startswith("events.jsonl:2201")


def test_non_jsonl_retains_whole_file_limit(tmp_path: Path) -> None:
    path = tmp_path / "large.json"
    path.write_bytes(b' ' * (2 * 1024 * 1024 + 1))
    assert scan_remote_file(path, location="large.json")[0].category == "oversized_file"


def test_session_shards_are_independent_and_migrate_legacy(tmp_path: Path) -> None:
    config = _direct_config(tmp_path)
    for identity in ("first", "second"):
        source = _write_rollout(tmp_path / identity / "session.jsonl", _validation_records())
        payload = _hook_payload(source, tmp_path / "project", event="Stop")
        payload["session_id"] = identity
        capture_hook_payload(payload, config=config, launch_worker=False)
    flush_queue(config, no_push=True)
    shards = sorted(config.ledger_checkout.rglob("events/*.jsonl"))
    assert len(shards) == 2
    assert not list(config.ledger_checkout.rglob("events.jsonl"))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in shards}
    day = shards[0].parents[1]
    manifest = json.loads((day / "manifest.json").read_text())
    for shard in shards:
        relative = str(shard.relative_to(day))
        assert manifest["generated_file_hashes"][relative] == hashlib.sha256(
            shard.read_bytes()).hexdigest()
        assert all(e["session_key"] == shard.stem for e in _read_jsonl(shard))
        assert is_allowlisted_path(str(shard.relative_to(config.ledger_checkout)))
    ids = [e["event_id"] for shard in shards for e in _read_jsonl(shard)]
    assert len(ids) == len(set(ids))
    export_day(config, date(2026, 9, 1))
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in shards}
    cache_path = config.paths.cache / "sessions" / f"{shards[0].stem}.json"
    cache = json.loads(cache_path.read_text())
    cache["slices"]["2026-09-01"]["record"]["objective"] = "Updated objective."
    new_event = dict(cache["slices"]["2026-09-01"]["events"][0])
    new_event["event_id"] = "late-event"
    cache["slices"]["2026-09-01"]["events"].append(new_event)
    cache_path.write_text(json.dumps(cache))
    export_day(config, date(2026, 9, 1))
    assert before[shards[0]][0] != shards[0].read_bytes()
    assert before[shards[1]] == (shards[1].read_bytes(), shards[1].stat().st_mtime_ns)
    # Legacy file can exceed the old limit; migration uses cached evidence.
    legacy = day / "events.jsonl"
    legacy.write_bytes(b'{"safe":true}\n' * 170000)
    migrated = export_day(config, date(2026, 9, 1))
    assert migrated.changed and not legacy.exists()
    assert "events.jsonl" not in json.loads((day / "manifest.json").read_text())[
        "generated_file_hashes"]
    assert not export_day(config, date(2026, 9, 1)).changed


@pytest.mark.parametrize("failures", [1, 3, 10])
def test_transcript_retry_budget_and_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: int,
) -> None:
    config, _ = _git_config(tmp_path, push_enabled=True)
    _queue_records(config, tmp_path, _validation_records())
    first = next(config.paths.pending.glob("*.json"))
    raw = json.loads(first.read_text())
    raw["event_id"] = "zz-unrelated"
    other = config.paths.pending / "zz-unrelated.json"
    other.write_text(json.dumps(raw))
    real_build = service_module.build_session_cache
    calls = []
    clock = [0.0]
    sleeps = []

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    def build(job, *args, **kwargs):
        calls.append(job.event_id)
        if job.event_id != "zz-unrelated" and calls.count(job.event_id) <= failures:
            raise SourceChangedDuringParseError("synthetic mutation")
        return real_build(job, *args, **kwargs)

    monkeypatch.setattr(service_module, "build_session_cache", build)
    monkeypatch.setattr(service_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(service_module.time, "sleep", sleep)
    result = flush_queue(config)
    assert calls[1] == "zz-unrelated"
    assert result.retry_attempts == min(failures, 3)
    assert sleeps == list(service_module.TRANSCRIPT_RETRY_DELAYS[:min(failures, 3)])
    assert result.failed_jobs == 0 and result.git.pushed
    assert result.retry_exhausted_jobs == int(failures > 3)
    assert result.retained_jobs == int(failures > 3)
    assert first.exists() == (failures > 3)
    assert not list(config.paths.processing.glob("*.json"))
    events = [e for p in config.ledger_checkout.rglob("events/*.jsonl") for e in _read_jsonl(p)]
    assert len(events) == len({e["event_id"] for e in events})
    summary = json.loads(next(config.ledger_checkout.rglob("summary.json")).read_text())
    assert summary["totals"]["validation_events"] == 1


def test_missing_source_isolated_from_valid_push(tmp_path: Path) -> None:
    config, _ = _git_config(tmp_path, push_enabled=True)
    _queue_records(config, tmp_path, _validation_records())
    raw = json.loads(next(config.paths.pending.glob("*.json")).read_text())
    raw["event_id"] = "missing"
    raw["transcript_path"] = str(tmp_path / "absent.jsonl")
    (config.paths.pending / "missing.json").write_text(json.dumps(raw))
    result = flush_queue(config)
    assert result.failed_jobs == 1 and result.processed_jobs == 1
    assert result.git.pushed
    error = json.loads((config.paths.failed / "missing.error.json").read_text())
    assert error["error_type"] == "FileNotFoundError"
    assert (config.paths.failed / "missing.json").exists()


def test_xdg_defaults_and_v1_config_loading(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = default_ledger_paths(home=home, environ={})
    assert paths.config == home / ".config/codex-insights/daily-ledger.toml"
    assert paths.state == home / ".local/state/codex-insights/daily-ledger"
    assert paths.helpers == home / ".local/share/codex-insights/daily-ledger"
    config_path = _write_config(tmp_path, push_enabled=False)

    config = load_ledger_config(config_path)

    assert config.schema_version == LEDGER_SCHEMA_VERSION
    assert config.device_id == "synthetic-device"


def test_config_rejects_credentials_and_unknown_keys(tmp_path: Path) -> None:
    path = _write_config(tmp_path, push_enabled=False)
    path.write_text(path.read_text() + 'api_token = "not-allowed"\n', encoding="utf-8")

    with pytest.raises(LedgerConfigurationError, match="Unsupported"):
        load_ledger_config(path)


def test_legacy_local_timezone_is_ignored_as_a_reporting_source(tmp_path: Path) -> None:
    path = _write_config(tmp_path, push_enabled=False)
    path.write_text(path.read_text() + 'timezone = "Europe/London"\n', encoding="utf-8")

    config = load_ledger_config(path)

    assert not hasattr(config, "timezone")


def test_asia_singapore_reporting_window_ends_at_2300() -> None:
    policy = parse_reporting_policy(DEFAULT_REPORT_POLICY_TEXT)

    window = reporting_window_for_date(policy, date(2026, 9, 2))

    assert window.identity() == {
        "report_date": "2026-09-02",
        "timezone": "Asia/Singapore",
        "day_boundary_local": "23:00",
        "window_start": "2026-09-01T23:00:00+08:00",
        "window_end": "2026-09-02T23:00:00+08:00",
    }


def test_event_at_225959_belongs_to_report_ending_that_date() -> None:
    policy = parse_reporting_policy(DEFAULT_REPORT_POLICY_TEXT)
    event = datetime(2026, 9, 2, 22, 59, 59, tzinfo=ZoneInfo("Asia/Singapore"))

    assert report_date_for_timestamp(policy, event) == date(2026, 9, 2)


def test_event_exactly_at_230000_belongs_to_next_report() -> None:
    policy = parse_reporting_policy(DEFAULT_REPORT_POLICY_TEXT)
    event = datetime(2026, 9, 2, 23, 0, 0, tzinfo=ZoneInfo("Asia/Singapore"))

    assert report_date_for_timestamp(policy, event) == date(2026, 9, 3)


def test_historical_report_uses_period_effective_for_its_report_date() -> None:
    policy = parse_reporting_policy(_transition_policy())

    singapore = reporting_window_for_date(policy, date(2026, 9, 30))
    london = reporting_window_for_date(policy, date(2026, 10, 1))

    assert singapore.timezone == "Asia/Singapore"
    assert london.timezone == "Europe/London"


def test_singapore_to_london_transition_has_no_gap_or_overlap() -> None:
    policy = parse_reporting_policy(_transition_policy())
    previous = reporting_window_for_date(policy, date(2026, 9, 30))
    transition = reporting_window_for_date(policy, date(2026, 10, 1))

    assert transition.window_start == previous.window_end


def test_timezone_transition_windows_may_be_longer_or_shorter_than_24_hours() -> None:
    policy = parse_reporting_policy(
        _policy_text(
            ("2026-09-02", "Asia/Singapore"),
            ("2026-10-01", "Europe/London"),
            ("2026-11-01", "Asia/Singapore"),
        )
    )

    longer = reporting_window_for_date(policy, date(2026, 10, 1))
    shorter = reporting_window_for_date(policy, date(2026, 11, 1))

    assert longer.window_end.astimezone(UTC) - longer.window_start.astimezone(UTC) > timedelta(
        hours=24
    )
    assert shorter.window_end.astimezone(UTC) - shorter.window_start.astimezone(UTC) < timedelta(
        hours=24
    )


def test_europe_london_dst_uses_the_correct_local_offsets() -> None:
    policy = parse_reporting_policy(
        _policy_text(("2026-01-01", "Europe/London"))
    )

    window = reporting_window_for_date(policy, date(2026, 3, 29))

    assert window.identity()["window_start"] == "2026-03-28T23:00:00+00:00"
    assert window.identity()["window_end"] == "2026-03-29T23:00:00+01:00"
    assert window.window_end.astimezone(UTC) - window.window_start.astimezone(UTC) == timedelta(
        hours=23
    )


def test_overlapping_effective_periods_are_rejected() -> None:
    text = _policy_text(
        ("2026-09-02", "Asia/Singapore"),
        ("2026-09-02", "Europe/London"),
    )

    with pytest.raises(ReportPolicyError, match="strictly ordered"):
        parse_reporting_policy(text)


def test_fixed_utc_offset_is_rejected_as_an_iana_timezone() -> None:
    text = _policy_text(("2026-09-02", "UTC+8"))

    with pytest.raises(ReportPolicyError, match="Invalid IANA timezone"):
        parse_reporting_policy(text)


def test_stop_capture_is_atomic_and_stdout_is_json(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, push_enabled=False)
    config = load_ledger_config(config_path)
    transcript = _write_rollout(tmp_path / "codex" / "session.jsonl", _planning_records())
    payload = _hook_payload(transcript, tmp_path / "project", event="Stop")

    destination = capture_hook_payload(
        payload,
        config=config,
        now=datetime(2026, 9, 1, 2, tzinfo=UTC),
        launch_worker=False,
    )
    exit_code, stdout = run_capture_hook(
        json.dumps(payload), config_path=config_path, launch_worker=False
    )

    assert exit_code == 0
    assert json.loads(stdout) == {"continue": True, "suppressOutput": True}
    assert stdout == STOP_STDOUT
    assert destination is not None and destination.is_file()
    assert not tuple(config.paths.pending.glob("*.tmp"))


def test_malformed_optional_fields_fail_open(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, push_enabled=False)
    transcript = _write_rollout(tmp_path / "codex" / "session.jsonl", _planning_records())
    payload = _hook_payload(transcript, tmp_path / "project", event="Stop")
    payload.update(
        {"turn_id": 42, "last_assistant_message": {"unexpected": True}, "reason": []}
    )

    exit_code, stdout = run_capture_hook(
        json.dumps(payload), config_path=config_path, launch_worker=False
    )

    assert exit_code == 0
    assert stdout == STOP_STDOUT
    job = json.loads(next(load_ledger_config(config_path).paths.pending.glob("*.json")).read_text())
    assert job["turn_id"] is None
    assert job["assistant_outcome_excerpt"] is None


def test_session_end_uses_lightweight_path_and_no_stdout(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, push_enabled=False)
    transcript = _write_rollout(tmp_path / "codex" / "session.jsonl", _planning_records())
    payload = _hook_payload(transcript, tmp_path / "project", event="SessionEnd")

    started = time.monotonic()
    exit_code, stdout = run_capture_hook(
        json.dumps(payload), config_path=config_path, launch_worker=False
    )

    assert time.monotonic() - started < 1
    assert exit_code == 0
    assert stdout == ""


def test_sample_helper_fails_open_when_package_import_is_unavailable(
    tmp_path: Path,
) -> None:
    helper = Path(__file__).parents[1] / "examples" / "daily-ledger" / "capture_hook.py"
    environment = {"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
    result = subprocess.run(
        [sys.executable, "-S", str(helper)],
        input=json.dumps({"hook_event_name": "Stop"}),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0
    assert result.stdout == STOP_STDOUT
    assert result.stderr == ""
    assert (tmp_path / "state/codex-insights/daily-ledger/logs/hook-errors.log").is_file()


def test_same_hook_event_is_idempotent(tmp_path: Path) -> None:
    config = _direct_config(tmp_path)
    transcript = _write_rollout(tmp_path / "codex" / "session.jsonl", _planning_records())
    payload = _hook_payload(transcript, tmp_path / "project", event="Stop")

    capture_hook_payload(payload, config=config, launch_worker=False)
    capture_hook_payload(payload, config=config, launch_worker=False)

    assert len(tuple(config.paths.pending.glob("*.json"))) == 1
    result = flush_queue(config, no_push=True)
    assert result.processed_jobs == 1
    events = _read_jsonl(next(config.ledger_checkout.rglob("events/*.jsonl")))
    identifiers = [str(item["event_id"]) for item in events]
    assert len(identifiers) == len(set(identifiers))


def test_multiple_stop_events_update_one_session_record(tmp_path: Path) -> None:
    config = _direct_config(tmp_path)
    transcript = _write_rollout(tmp_path / "codex" / "session.jsonl", _planning_records())
    payload = _hook_payload(transcript, tmp_path / "project", event="Stop")
    capture_hook_payload(payload, config=config, launch_worker=False)
    flush_queue(config, no_push=True)
    _write_rollout(transcript, _validation_records(), append=True)
    payload["turn_id"] = "turn-2"
    capture_hook_payload(payload, config=config, launch_worker=False)

    result = flush_queue(config, no_push=True)

    assert result.processed_jobs == 2
    session_files = tuple(config.ledger_checkout.rglob("sessions/*.json"))
    assert len(session_files) == 1


def test_two_concurrent_workers_do_not_corrupt_queue(tmp_path: Path) -> None:
    config = _direct_config(tmp_path)
    transcript = _write_rollout(tmp_path / "codex" / "session.jsonl", _validation_records())
    capture_hook_payload(
        _hook_payload(transcript, tmp_path / "project", event="Stop"),
        config=config,
        launch_worker=False,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: flush_queue(config, no_push=True), range(2)))

    assert sum(item.processed_jobs for item in results) == 1
    assert len(tuple(config.paths.cache.glob("sessions/*.json"))) == 1
    assert json.loads(next(config.paths.cache.glob("sessions/*.json")).read_text())


def test_interrupted_processing_job_is_recovered_under_worker_lock(tmp_path: Path) -> None:
    config = _direct_config(tmp_path)
    _queue_records(config, tmp_path, _validation_records())
    ensure_pending = next(config.paths.pending.glob("*.json"))
    config.paths.processing.mkdir(parents=True, exist_ok=True)
    interrupted = config.paths.processing / ensure_pending.name
    ensure_pending.replace(interrupted)

    result = flush_queue(config, no_push=True)

    assert result.processed_jobs == 1
    assert not tuple(config.paths.processing.glob("*.json"))
    assert tuple(config.paths.pending.glob("*.json"))


def test_planning_only_is_not_completed(tmp_path: Path) -> None:
    record = _process_records(tmp_path, _planning_records())

    assert record["activity_state"] == "planning_only"
    assert record["underlying_outcome"]["outcome"] == "unknown"


def test_assistant_completion_claim_does_not_upgrade_planning(tmp_path: Path) -> None:
    record = _process_records(
        tmp_path,
        _planning_records(),
        assistant_message="Everything is complete and all tests passed.",
    )

    assert record["activity_state"] == "planning_only"
    assert record["evidence"]["assistant_claim_used_for_completion"] is False


def test_validation_pass_maps_to_completed_locally(tmp_path: Path) -> None:
    record = _process_records(tmp_path, _validation_records())

    assert record["activity_state"] == "completed_locally"
    assert record["git"]["pushed"] is None
    assert record["validation"][0]["result"] == "success"


def test_unrelated_tracked_head_does_not_upgrade_activity_to_completed(
    tmp_path: Path,
) -> None:
    config = _direct_config(tmp_path)
    remote = tmp_path / "project-remote.git"
    project = tmp_path / "project"
    _run("git", "init", "--bare", str(remote))
    _run("git", "init", "-b", "main", str(project))
    _run("git", "-C", str(project), "config", "user.name", "Synthetic User")
    _run(
        "git", "-C", str(project), "config", "user.email", "synthetic@example.invalid"
    )
    _run("git", "-C", str(project), "remote", "add", "origin", str(remote))
    (project / "README.md").write_text("synthetic\n", encoding="utf-8")
    _run("git", "-C", str(project), "add", "README.md")
    _run("git", "-C", str(project), "commit", "-m", "Synthetic baseline")
    _run("git", "-C", str(project), "push", "-u", "origin", "main")
    transcript = _write_rollout(tmp_path / "codex" / "session.jsonl", _validation_records())
    capture_hook_payload(
        _hook_payload(transcript, project, event="Stop"),
        config=config,
        launch_worker=False,
    )

    flush_queue(config, no_push=True)
    record = json.loads(next(config.ledger_checkout.rglob("sessions/*.json")).read_text())

    assert record["git"]["pushed"] is True
    assert record["commits"] == []
    assert record["activity_state"] == "completed_locally"


def test_missing_event_timestamp_preserves_capture_fallback(tmp_path: Path) -> None:
    records = list(_planning_records())
    records[-1] = dict(records[-1])
    records[-1].pop("timestamp")

    record = _process_records(tmp_path, tuple(records))

    assert record["timestamp_precision"] == "capture_fallback"


def test_cross_reporting_boundary_splits_without_double_counting(tmp_path: Path) -> None:
    records = (
        *_planning_records(timestamp="2026-09-01T14:59:50Z"),
        *_validation_records(timestamp="2026-09-01T15:00:10Z", call_id="validate-2"),
    )
    config = _direct_config(tmp_path)
    transcript = _write_rollout(tmp_path / "codex" / "session.jsonl", records)
    capture_hook_payload(
        _hook_payload(transcript, tmp_path / "project", event="Stop"),
        config=config,
        now=datetime(2026, 9, 1, 16, 1, tzinfo=UTC),
        launch_worker=False,
    )

    flush_queue(config, no_push=True)

    session_files = sorted(config.ledger_checkout.rglob("sessions/*.json"))
    assert [path.parents[1].name for path in session_files] == ["01", "02"]
    summaries = [
        json.loads(path.read_text())
        for path in sorted(config.ledger_checkout.rglob("summary.json"))
    ]
    assert sum(int(item["totals"]["validation_events"]) for item in summaries) == 1
    shards = sorted(config.ledger_checkout.rglob("events/*.jsonl"))
    assert len(shards) == 2
    event_ids = [e["event_id"] for p in shards for e in _read_jsonl(p)]
    assert len(event_ids) == len(set(event_ids))


def test_commits_and_validations_are_deduplicated(tmp_path: Path) -> None:
    commit_hash = "a" * 40
    records = (
        *_validation_records(),
        *_commit_records(commit_hash, call_id="commit-1"),
        *_commit_records(commit_hash, call_id="commit-2", timestamp="2026-09-01T02:00:00Z"),
    )
    record = _process_records(tmp_path, records)
    summary = json.loads(next((tmp_path / "ledger").rglob("summary.json")).read_text())

    assert len(record["commits"]) == 1
    assert summary["totals"]["commits"] == 1
    assert summary["totals"]["validation_events"] == 1


@pytest.mark.parametrize(
    "unsafe",
    (
        "/Users/example/private/file.txt",
        "sk-abcdefghijklmnopqrstuvwxyz123456",
        "person@example.com",
        "OPENAI_API_KEY=something-private",
        "password=hunter123",
    ),
)
def test_central_redaction_omits_unsafe_text(unsafe: str) -> None:
    assert sanitize_remote_text(unsafe) is None


def test_forbidden_fields_and_raw_transcript_content_do_not_enter_ledger(tmp_path: Path) -> None:
    records = _planning_records(
        prompt=(
            "Prepare a bounded daily record. "
            "PRIVATE_SECOND_SENTENCE complete raw prompt text must not be copied."
        )
    )
    _process_records(tmp_path, records)
    ledger_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "ledger").rglob("*")
        if path.is_file()
    )

    assert "PRIVATE_SECOND_SENTENCE" not in ledger_text
    assert '"session_id"' not in ledger_text
    assert '"transcript_path"' not in ledger_text
    assert str(tmp_path) not in ledger_text


def test_late_arrival_increments_revision_and_names_changed_session(tmp_path: Path) -> None:
    config = _direct_config(tmp_path)
    transcript = _write_rollout(tmp_path / "codex" / "session.jsonl", _planning_records())
    payload = _hook_payload(transcript, tmp_path / "project", event="Stop")
    capture_hook_payload(payload, config=config, launch_worker=False)
    flush_queue(config, no_push=True)
    first_manifest = json.loads(next(config.ledger_checkout.rglob("manifest.json")).read_text())
    _write_rollout(transcript, _validation_records(), append=True)
    payload["turn_id"] = "late-turn"
    capture_hook_payload(payload, config=config, launch_worker=False)

    flush_queue(config, no_push=True)
    second_manifest = json.loads(next(config.ledger_checkout.rglob("manifest.json")).read_text())

    assert first_manifest["revision"] == 1
    assert second_manifest["revision"] == 2
    assert second_manifest["late_arrival"]["previous_revision"] == 1
    assert len(second_manifest["late_arrival"]["changed_session_keys"]) == 1


def test_representative_outputs_validate_against_packaged_schemas(tmp_path: Path) -> None:
    _process_records(tmp_path, _validation_records())
    session = json.loads(next((tmp_path / "ledger").rglob("sessions/*.json")).read_text())
    summary = json.loads(next((tmp_path / "ledger").rglob("summary.json")).read_text())
    manifest = json.loads(next((tmp_path / "ledger").rglob("manifest.json")).read_text())

    validate_generated_document(session, "codex-session-v1.schema.json")
    validate_generated_document(summary, "codex-daily-summary-v1.schema.json")
    validate_generated_document(manifest, "codex-manifest-v1.schema.json")


def test_checked_in_examples_validate_against_packaged_schemas() -> None:
    example_directory = Path(__file__).parents[1] / "examples" / "daily-ledger"
    sample_directory = example_directory / "sample"
    policy = parse_reporting_policy(
        (example_directory / "report-policy.yaml").read_text(encoding="utf-8")
    )
    local_config = (example_directory / "daily-ledger.toml").read_text(encoding="utf-8")

    assert policy.periods[0].timezone == "Asia/Singapore"
    assert policy.periods[0].boundary_text == "23:00"
    assert "timezone" not in local_config
    for filename, schema in (
        ("session.json", "codex-session-v1.schema.json"),
        ("summary.json", "codex-daily-summary-v1.schema.json"),
        ("manifest.json", "codex-manifest-v1.schema.json"),
    ):
        document = json.loads((sample_directory / filename).read_text(encoding="utf-8"))
        validate_generated_document(document, schema)
    manifest = json.loads((sample_directory / "manifest.json").read_text(encoding="utf-8"))
    expected_hashes = manifest["generated_file_hashes"]
    for relative in (
        "events.jsonl",
        "sessions/0123456789abcdef0123456789abcdef.json",
        "summary.json",
    ):
        source = (
            sample_directory / "session.json"
            if relative.startswith("sessions/")
            else sample_directory / relative
        )
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected_hashes[relative]
    for line in (sample_directory / "events.jsonl").read_text(encoding="utf-8").splitlines():
        assert not scan_remote_value(json.loads(line))


def test_hook_manager_preserves_unrelated_groups_on_install_and_disable(
    tmp_path: Path,
) -> None:
    target = tmp_path / "hooks.json"
    custom_group = {
        "matcher": "custom",
        "hooks": [{"type": "command", "command": "custom-safe-hook"}],
    }
    target.write_text(
        json.dumps({"custom_setting": True, "hooks": {"Stop": [custom_group]}}),
        encoding="utf-8",
    )
    manager = Path(__file__).parents[1] / "examples" / "daily-ledger" / "manage_hooks.py"

    _run(sys.executable, str(manager), "install", "--target", str(target))
    installed = json.loads(target.read_text(encoding="utf-8"))
    assert installed["custom_setting"] is True
    assert custom_group in installed["hooks"]["Stop"]
    assert len(installed["hooks"]["Stop"]) == 2
    assert len(installed["hooks"]["SessionEnd"]) == 1

    _run(sys.executable, str(manager), "disable", "--target", str(target))
    disabled = json.loads(target.read_text(encoding="utf-8"))
    assert disabled == {"custom_setting": True, "hooks": {"Stop": [custom_group]}}


def test_no_op_export_does_not_increment_revision(tmp_path: Path) -> None:
    config = _direct_config(tmp_path)
    _queue_records(config, tmp_path, _validation_records())
    flush_queue(config, no_push=True)
    manifest_path = next(config.ledger_checkout.rglob("manifest.json"))
    before = json.loads(manifest_path.read_text())

    result = export_day(config, date.fromisoformat(str(before["date"])))
    after = json.loads(manifest_path.read_text())

    assert result.changed is False
    assert after["revision"] == before["revision"]


def test_historical_manifest_keeps_timezone_after_future_policy_change(
    tmp_path: Path,
) -> None:
    config = _direct_config(tmp_path)
    _queue_records(config, tmp_path, _validation_records())
    flush_queue(config, no_push=True)
    manifest_path = next(config.ledger_checkout.rglob("manifest.json"))
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    _write_report_policy(config, _transition_policy())

    result = export_day(config, date(2026, 9, 1))
    after = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert result.changed is False
    assert after == before
    assert after["timezone"] == "Asia/Singapore"
    assert after["window_end"] == "2026-09-01T23:00:00+08:00"


def test_export_initializes_exact_static_contract_without_reports(tmp_path: Path) -> None:
    config = _direct_config(tmp_path)
    _queue_records(config, tmp_path, _validation_records())

    flush_queue(config, no_push=True)

    for relative in (
        "README.md",
        "SCHEMA.md",
        "schema/codex-session-v1.schema.json",
        "schema/codex-daily-summary-v1.schema.json",
        "schema/codex-manifest-v1.schema.json",
        "config/project-aliases.yaml",
        "config/report-policy.yaml",
        "status/latest.json",
    ):
        assert (config.ledger_checkout / relative).is_file()
    assert not (config.ledger_checkout / "reports").exists()


def test_dirty_checkout_outside_allowlist_fails_closed(tmp_path: Path) -> None:
    config, _ = _git_config(tmp_path, push_enabled=True)
    _queue_records(config, tmp_path, _validation_records())
    (config.ledger_checkout / "unrelated-notes.txt").write_text("preserve me", encoding="utf-8")

    with pytest.raises(NonAllowlistedDirtyError):
        flush_queue(config)

    assert len(tuple(config.paths.pending.glob("*.json"))) == 1
    assert (config.ledger_checkout / "unrelated-notes.txt").read_text() == "preserve me"


def test_allowlist_is_exact_and_rejects_arbitrary_children() -> None:
    assert is_allowlisted_path("README.md")
    assert is_allowlisted_path(
        "ledger/codex/2026/09/01/sessions/0123456789abcdef0123456789abcdef.json"
    )
    assert not is_allowlisted_path("ledger/private-transcript.txt")
    assert not is_allowlisted_path("config/credentials.env")


def test_secret_in_allowlisted_config_fails_closed_and_retains_queue(tmp_path: Path) -> None:
    config, _ = _git_config(tmp_path, push_enabled=True)
    _queue_records(config, tmp_path, _validation_records())
    policy = config.ledger_checkout / "config" / "report-policy.yaml"
    policy.parent.mkdir(parents=True)
    policy.write_text("API_TOKEN=do-not-publish\n", encoding="utf-8")

    with pytest.raises(GitSyncError, match="Privacy check failed"):
        flush_queue(config)

    assert len(tuple(config.paths.pending.glob("*.json"))) == 1


def test_push_failure_retains_then_later_flush_succeeds(tmp_path: Path) -> None:
    config, remote = _git_config(tmp_path, push_enabled=True)
    _queue_records(config, tmp_path, _validation_records())
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    with pytest.raises(GitSyncError):
        flush_queue(config)
    assert len(tuple(config.paths.pending.glob("*.json"))) == 1
    hook.unlink()

    result = flush_queue(config)

    assert result.git is not None and result.git.pushed is True
    assert not tuple(config.paths.pending.glob("*.json"))
    assert len(tuple(config.paths.processed.glob("*.json"))) == 1


def test_non_fast_forward_retry_is_bounded_and_never_forces(tmp_path: Path) -> None:
    config, remote = _git_config(tmp_path, push_enabled=True)
    _queue_records(config, tmp_path, _validation_records())
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    original = git_sync_module._git
    calls: list[tuple[str, ...]] = []

    def recording_git(checkout: Path, *arguments: str, timeout: int = 10):  # type: ignore[no-untyped-def]
        calls.append(arguments)
        return original(checkout, *arguments, timeout=timeout)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(git_sync_module, "_git", recording_git)
        with pytest.raises(GitSyncError):
            flush_queue(config)

    pushes = [call for call in calls if call and call[0] == "push"]
    assert len(pushes) == 2
    assert all("--force" not in call and "-f" not in call for call in calls)


def test_successful_flush_then_noop_sync_creates_no_commit(tmp_path: Path) -> None:
    config, _ = _git_config(tmp_path, push_enabled=True)
    _queue_records(config, tmp_path, _validation_records())
    flush_queue(config)
    before = _git_output(config.ledger_checkout, "rev-parse", "HEAD")
    manifest = json.loads(next(config.ledger_checkout.rglob("manifest.json")).read_text())

    from codex_insights.daily_ledger.service import export_cached_day

    export_cached_day(config, date.fromisoformat(str(manifest["date"])), no_push=False)
    after = _git_output(config.ledger_checkout, "rev-parse", "HEAD")

    assert after == before


def test_doctor_detects_injected_absolute_path_leakage(tmp_path: Path) -> None:
    config = _direct_config(tmp_path)
    bad = config.ledger_checkout / "status" / "bad.json"
    bad.parent.mkdir(parents=True)
    bad.write_text(json.dumps({"value": "/Users/example/private"}), encoding="utf-8")

    report = run_doctor(config, codex_home=tmp_path / "synthetic-codex-home")

    assert any(item.category == "absolute_path" for item in report.leakage_findings)


def test_doctor_reports_active_window_and_invalid_period_definitions(
    tmp_path: Path,
) -> None:
    config = _direct_config(tmp_path)
    _write_report_policy(config, _transition_policy())

    report = run_doctor(
        config,
        codex_home=tmp_path / "synthetic-codex-home",
        now=datetime(2026, 9, 30, 16, 0, tzinfo=UTC),
    )

    assert report.timezone == "Europe/London"
    assert report.day_boundary_local == "23:00"
    assert report.current_report_date == "2026-10-01"
    assert report.current_window_start == "2026-09-30T23:00:00+08:00"
    assert report.current_window_end == "2026-10-01T23:00:00+01:00"
    assert report.next_reporting_boundary == report.current_window_end
    assert report.timezone_valid is True
    assert report.reporting_periods_valid is True

    _write_report_policy(
        config,
        _policy_text(
            ("2026-09-02", "Asia/Singapore"),
            ("2026-09-02", "Europe/London"),
        ),
    )
    invalid = run_doctor(
        config,
        codex_home=tmp_path / "synthetic-codex-home",
        now=datetime(2026, 9, 30, 16, 0, tzinfo=UTC),
    )
    assert invalid.timezone_valid is False
    assert invalid.reporting_periods_valid is False
    assert invalid.reporting_policy_error is not None


def test_backfill_uses_synthetic_inventory_and_retains_no_push_jobs(tmp_path: Path) -> None:
    config = _direct_config(tmp_path)
    codex_home = tmp_path / "codex-home"
    transcript = _write_rollout(codex_home / "sessions" / "backfill.jsonl", _validation_records())
    with sqlite3.connect(codex_home / "state_1.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT, created_at TEXT,
                updated_at TEXT, source TEXT, cwd TEXT, archived INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                "backfill-session",
                str(transcript.relative_to(codex_home)),
                "2026-09-01T00:00:00Z",
                "2026-09-01T03:00:00Z",
                "cli",
                str(tmp_path / "project"),
            ),
        )

    result = backfill_range(
        config,
        since=date(2026, 9, 1),
        until=date(2026, 9, 2),
        codex_home=codex_home,
        no_push=True,
    )

    assert result.processed_jobs == 1
    assert result.retained_jobs == 1
    assert tuple(config.ledger_checkout.rglob("sessions/*.json"))


def test_backfill_and_retained_flush_export_only_requested_report_dates(
    tmp_path: Path,
) -> None:
    config = _direct_config(tmp_path)
    codex_home = tmp_path / "codex-home"
    records = (
        *_planning_records(timestamp="2026-08-31T14:00:00Z"),
        *_validation_records(timestamp="2026-09-01T01:00:00Z"),
    )
    transcript = _write_rollout(
        codex_home / "sessions" / "cross-range-backfill.jsonl", records
    )
    with sqlite3.connect(codex_home / "state_1.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT, created_at TEXT,
                updated_at TEXT, source TEXT, cwd TEXT, archived INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                "cross-range-backfill-session",
                str(transcript.relative_to(codex_home)),
                "2026-08-31T14:00:00Z",
                "2026-09-01T03:00:00Z",
                "cli",
                str(tmp_path / "project"),
            ),
        )

    first = backfill_range(
        config,
        since=date(2026, 9, 1),
        until=date(2026, 9, 1),
        codex_home=codex_home,
        no_push=True,
    )
    second = flush_queue(config, no_push=True)
    report_dates = sorted(
        path.parent.name for path in config.ledger_checkout.rglob("manifest.json")
    )

    assert first.exported_dates == ("2026-09-01",)
    assert second.exported_dates == ("2026-09-01",)
    assert report_dates == ["01"]


def test_backfill_uses_policy_effective_for_historical_report_date(
    tmp_path: Path,
) -> None:
    config = _direct_config(tmp_path)
    _write_report_policy(config, _transition_policy())
    codex_home = tmp_path / "codex-home"
    transcript = _write_rollout(
        codex_home / "sessions" / "london-backfill.jsonl",
        _validation_records(timestamp="2026-10-01T21:30:00Z"),
    )
    with sqlite3.connect(codex_home / "state_1.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT, created_at TEXT,
                updated_at TEXT, source TEXT, cwd TEXT, archived INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                "london-backfill-session",
                str(transcript.relative_to(codex_home)),
                "2026-10-01T21:00:00Z",
                "2026-10-01T21:40:00Z",
                "cli",
                str(tmp_path / "project"),
            ),
        )

    result = backfill_range(
        config,
        since=date(2026, 10, 1),
        until=date(2026, 10, 1),
        codex_home=codex_home,
        no_push=True,
    )
    manifest = json.loads(
        (
            config.ledger_checkout
            / "ledger/codex/2026/10/01/manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert result.processed_jobs == 1
    assert manifest["report_date"] == "2026-10-01"
    assert manifest["timezone"] == "Europe/London"
    assert manifest["window_start"] == "2026-09-30T23:00:00+08:00"


def test_daily_ledger_cli_help_is_registered() -> None:
    for command in ("doctor", "capture-hook", "flush", "export", "backfill"):
        result = runner.invoke(app, ["daily-ledger", command, "--help"])
        assert result.exit_code == 0
        assert "Usage:" in result.stdout


def test_privacy_scanner_flags_forbidden_remote_fields() -> None:
    findings = scan_remote_value({"session_id": "raw", "safe": "value"})
    assert [(item.category, item.location) for item in findings] == [
        ("forbidden_field", "$.session_id")
    ]


def _transition_policy() -> str:
    return _policy_text(
        ("2026-09-02", "Asia/Singapore"),
        ("2026-10-01", "Europe/London"),
    )


def _policy_text(*periods: tuple[str, str]) -> str:
    lines = [
        'schema_version: "1.0"',
        'report_date_label: "window_end_local_date"',
        "",
        "periods:",
    ]
    for effective, timezone in periods:
        lines.extend(
            (
                f'  - effective_from_report_date: "{effective}"',
                f'    timezone: "{timezone}"',
                '    day_boundary_local: "23:00"',
            )
        )
    lines.extend(
        (
            "",
            "reports_directory: null",
            "assistant_claims_are_verified_evidence: false",
            "unknown_outcomes_remain_unknown: true",
            "",
        )
    )
    return "\n".join(lines)


def _write_report_policy(config: LedgerConfig, text: str) -> Path:
    path = config.ledger_checkout / "config" / "report-policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _direct_config(tmp_path: Path, *, push_enabled: bool = False) -> LedgerConfig:
    paths = LedgerPaths(
        config=tmp_path / "config" / "daily-ledger.toml",
        state=tmp_path / "state",
        helpers=tmp_path / "helpers",
    )
    return LedgerConfig(
        schema_version="1.0",
        device_id="synthetic-device",
        ledger_checkout=tmp_path / "ledger",
        remote="origin",
        branch="main",
        push_enabled=push_enabled,
        paths=paths,
    )


def _write_config(tmp_path: Path, *, push_enabled: bool) -> Path:
    config = _direct_config(tmp_path, push_enabled=push_enabled)
    config.paths.config.parent.mkdir(parents=True, exist_ok=True)
    config.paths.config.write_text(
        "\n".join(
            (
                'schema_version = "1.0"',
                'device_id = "synthetic-device"',
                f'ledger_checkout = "{config.ledger_checkout}"',
                'remote = "origin"',
                'branch = "main"',
                f"push_enabled = {str(push_enabled).lower()}",
                f'state_dir = "{config.paths.state}"',
                f'helpers_dir = "{config.paths.helpers}"',
                "",
            )
        ),
        encoding="utf-8",
    )
    return config.paths.config


def _planning_records(
    *,
    timestamp: str = "2026-09-01T01:00:00Z",
    prompt: str = "Plan a safe daily ledger.",
) -> tuple[dict[str, object], ...]:
    return (
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        },
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "task_complete"},
        },
    )


def _validation_records(
    *,
    timestamp: str = "2026-09-01T01:10:00Z",
    call_id: str = "validate-1",
    exit_code: int = 0,
) -> tuple[dict[str, object], ...]:
    return (
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "python -m pytest tests/test_safe.py"}),
                "call_id": call_id,
            },
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"exit_code": exit_code, "output": ""}),
            },
        },
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "task_complete"},
        },
    )


def _commit_records(
    commit_hash: str,
    *,
    call_id: str,
    timestamp: str = "2026-09-01T01:30:00Z",
) -> tuple[dict[str, object], ...]:
    return (
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "git commit -m safe-ledger-change"}),
                "call_id": call_id,
            },
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"exit_code": 0, "output": commit_hash}),
            },
        },
    )


def _write_rollout(
    path: Path,
    records: tuple[dict[str, object], ...],
    *,
    append: bool = False,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _hook_payload(transcript: Path, cwd: Path, *, event: str) -> dict[str, object]:
    cwd.mkdir(parents=True, exist_ok=True)
    return {
        "session_id": "raw-session-id-must-remain-local",
        "transcript_path": str(transcript),
        "cwd": str(cwd),
        "hook_event_name": event,
        "turn_id": "turn-1",
        "last_assistant_message": "Bounded outcome excerpt.",
    }


def _queue_records(
    config: LedgerConfig,
    tmp_path: Path,
    records: tuple[dict[str, object], ...],
    *,
    assistant_message: str | None = None,
) -> None:
    transcript = _write_rollout(tmp_path / "codex" / "session.jsonl", records)
    payload = _hook_payload(transcript, tmp_path / "project", event="Stop")
    if assistant_message is not None:
        payload["last_assistant_message"] = assistant_message
    capture_hook_payload(
        payload,
        config=config,
        now=datetime(2026, 9, 1, 2, tzinfo=UTC),
        launch_worker=False,
    )


def _process_records(
    tmp_path: Path,
    records: tuple[dict[str, object], ...],
    *,
    assistant_message: str | None = None,
) -> dict[str, object]:
    config = _direct_config(tmp_path)
    _queue_records(config, tmp_path, records, assistant_message=assistant_message)
    result = flush_queue(config, no_push=True)
    assert result.failed_jobs == 0
    return json.loads(next(config.ledger_checkout.rglob("sessions/*.json")).read_text())


def _git_config(tmp_path: Path, *, push_enabled: bool) -> tuple[LedgerConfig, Path]:
    config = _direct_config(tmp_path, push_enabled=push_enabled)
    remote = tmp_path / "ledger-remote.git"
    _run("git", "init", "--bare", str(remote))
    _run("git", "init", "-b", "main", str(config.ledger_checkout))
    _run("git", "-C", str(config.ledger_checkout), "config", "user.name", "Synthetic User")
    _run(
        "git",
        "-C",
        str(config.ledger_checkout),
        "config",
        "user.email",
        "synthetic@example.invalid",
    )
    _run(
        "git",
        "-C",
        str(config.ledger_checkout),
        "remote",
        "add",
        "origin",
        str(remote),
    )
    return config, remote


def _run(*arguments: str) -> None:
    subprocess.run(arguments, check=True, capture_output=True, text=True)


def _git_output(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]
