"""Deterministic, entirely synthetic Codex source corpus generation."""

from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SyntheticCorpusConfig:
    """Size and deterministic proportions for a generated source tree."""

    session_count: int = 240
    repository_count: int = 8
    model_count: int = 4
    seed: int = 20260810

    def validate(self) -> None:
        if self.session_count < 1:
            raise ValueError("session_count must be positive")
        if self.repository_count < 1:
            raise ValueError("repository_count must be positive")
        if self.model_count < 1:
            raise ValueError("model_count must be positive")


@dataclass(frozen=True, slots=True)
class SyntheticCorpus:
    """Paths and aggregate facts returned without private source content."""

    root: Path
    codex_home: Path
    repositories_root: Path
    state_database: Path
    session_count: int
    rollout_count: int
    missing_rollout_count: int
    child_count: int
    archived_count: int
    mutable_rollout: Path
    seed: int

    def to_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "codex_home": str(self.codex_home),
            "repositories_root": str(self.repositories_root),
            "state_database": str(self.state_database),
            "session_count": self.session_count,
            "rollout_count": self.rollout_count,
            "missing_rollout_count": self.missing_rollout_count,
            "child_count": self.child_count,
            "archived_count": self.archived_count,
            "mutable_rollout": str(self.mutable_rollout),
            "seed": self.seed,
        }


_PROMPTS = (
    "Implement the synthetic Python feature and add focused tests.",
    "Review this synthetic change for correctness and privacy safety.",
    "Diagnose the synthetic ORCA workflow without running calculations.",
    "Update the README and document the synthetic command behavior.",
    "Create a synthetic Git release plan; do not push.",
    "分析這個合成資料工作流，保留 UNKNOWN 並驗證測試。",
    "Refactor the synthetic parser while preserving its public interface.",
    "Investigate the synthetic data and report only aggregate results.",
)


def generate_synthetic_corpus(
    root: Path,
    *,
    config: SyntheticCorpusConfig | None = None,
) -> SyntheticCorpus:
    """Create a realistic source tree without copying any real Codex state."""

    selected = config or SyntheticCorpusConfig()
    selected.validate()
    destination = root.expanduser().resolve(strict=False)
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"Synthetic corpus destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    codex_home = destination / "codex-home"
    sessions_root = codex_home / "sessions"
    archived_root = codex_home / "archived_sessions"
    repositories_root = destination / "repositories"
    sessions_root.mkdir(parents=True)
    archived_root.mkdir(parents=True)
    repositories_root.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        "# Synthetic Codex configuration used only by tests.\n",
        encoding="utf-8",
    )

    repository_data = tuple(
        _create_repository(repositories_root / f"repo-{index:02d}", index=index)
        for index in range(selected.repository_count)
    )
    rng = random.Random(selected.seed)
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    thread_rows: list[tuple[object, ...]] = []
    edge_rows: list[tuple[str, str, str]] = []
    history_rows: list[dict[str, object]] = []
    token_sequences: dict[str, tuple[int, ...]] = {}
    prompt_records: dict[str, dict[str, object]] = {}
    rollout_paths: list[Path] = []
    missing_rollouts = 0
    archived_count = 0

    for index in range(selected.session_count):
        session_id = f"synthetic-{index:05d}"
        started = base_time + timedelta(minutes=index * 11)
        ended = started + timedelta(minutes=3 + index % 37)
        repo_index = index % selected.repository_count
        repository, commit_hash, starting_hash = repository_data[repo_index]
        model = f"synthetic-model-{index % selected.model_count}"
        archived = index % 7 == 0
        archived_count += int(archived)
        parent_index = _parent_index(index)
        parent_id = f"synthetic-{parent_index:05d}" if parent_index is not None else None
        if parent_id is not None:
            edge_rows.append((parent_id, session_id, "closed" if archived else "running"))

        missing = index > 0 and index % 211 == 0
        relative_path = (
            Path("archived_sessions") / f"rollout-{session_id}.jsonl"
            if archived
            else Path("sessions")
            / f"2026-01-{(index % 28) + 1:02d}"
            / f"rollout-{session_id}.jsonl"
        )
        rollout = codex_home / relative_path
        if missing:
            missing_rollouts += 1
        else:
            rollout.parent.mkdir(parents=True, exist_ok=True)
            prompt_text = _PROMPTS[index % len(_PROMPTS)]
            if index == 3:
                prompt_text += " API_KEY=synthetic-secret-value"
            prompt = _prompt_record(
                session_id,
                prompt_text,
                started + timedelta(seconds=5),
            )
            prompt_records[session_id] = prompt
            parent_tokens = token_sequences.get(parent_id or "", ())
            lineage_kind = _lineage_kind(index, parent_id)
            token_sequence = _token_sequence(
                index,
                lineage_kind=lineage_kind,
                parent_sequence=parent_tokens,
            )
            token_sequences[session_id] = token_sequence
            records = _rollout_records(
                session_id=session_id,
                started=started,
                cwd=repository,
                model=model,
                prompt=prompt,
                parent_prompt=prompt_records.get(parent_id or ""),
                token_sequence=token_sequence,
                commit_hash=commit_hash,
                index=index,
                rng=rng,
            )
            _write_rollout(
                rollout,
                records,
                malformed=index > 0 and index % 97 == 0,
                partial=index > 0 and index % 131 == 0,
            )
            rollout_paths.append(rollout)

        thread_rows.append(
            (
                session_id,
                relative_path.as_posix(),
                _timestamp(started),
                _timestamp(ended),
                _timestamp(ended) if archived else None,
                "cli" if index % 2 == 0 else "vscode",
                str(repository),
                model,
                "synthetic-provider",
                int(archived),
                "main",
                starting_hash if index == 1 else (None if index == 2 else commit_hash),
                f"https://example.invalid/synthetic/repo-{repo_index:02d}.git",
            )
        )
        if index % 5 == 0:
            history_rows.append(
                {
                    "session_id": session_id,
                    "ts": int(started.timestamp()),
                    "text": f"Synthetic history prompt {index}; never derived from user data.",
                }
            )

    state_database = codex_home / "state_9.sqlite"
    _write_state_database(state_database, thread_rows, edge_rows)
    _write_alternative_state_database(codex_home / "state_2.sqlite")
    (codex_home / "history.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in history_rows),
        encoding="utf-8",
    )
    mutable_rollout = next(
        path for path in rollout_paths if "archived_sessions" not in path.parts
    )
    return SyntheticCorpus(
        root=destination,
        codex_home=codex_home,
        repositories_root=repositories_root,
        state_database=state_database,
        session_count=selected.session_count,
        rollout_count=len(rollout_paths),
        missing_rollout_count=missing_rollouts,
        child_count=len(edge_rows),
        archived_count=archived_count,
        mutable_rollout=mutable_rollout,
        seed=selected.seed,
    )


def append_changed_session_event(rollout: Path) -> None:
    """Append one deterministic unknown event for incremental-update benchmarks."""

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-12-31T23:59:59Z",
                    "type": "event_msg",
                    "payload": {"type": "synthetic_changed_benchmark_event"},
                },
                sort_keys=True,
            )
            + "\n"
        )
    stat = rollout.stat()
    os.utime(rollout, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def _create_repository(path: Path, *, index: int) -> tuple[Path, str, str]:
    path.mkdir(parents=True)
    (path / "README.md").write_text(
        f"# Synthetic repository {index}\n\nNo real user content.\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synthetic Fixture",
        "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic Fixture",
        "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        "GIT_AUTHOR_DATE": _timestamp(
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index * 11)
        ),
        "GIT_COMMITTER_DATE": _timestamp(
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index * 11)
        ),
    }
    _git(path, "init", "--quiet", "--initial-branch=main", env=environment)
    _git(path, "add", "README.md", env=environment)
    _git(path, "commit", "--quiet", "-m", f"synthetic commit {index}", env=environment)
    starting_hash = _git(path, "rev-parse", "HEAD", env=environment).strip()
    (path / "fixture.txt").write_text("Synthetic descendant commit.\n", encoding="utf-8")
    descendant_time = _timestamp(
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index * 11 + 2)
    )
    descendant_environment = {
        **environment,
        "GIT_AUTHOR_DATE": descendant_time,
        "GIT_COMMITTER_DATE": descendant_time,
    }
    _git(path, "add", "fixture.txt", env=descendant_environment)
    _git(
        path,
        "commit",
        "--quiet",
        "-m",
        f"synthetic descendant {index}",
        env=descendant_environment,
    )
    commit_hash = _git(path, "rev-parse", "HEAD", env=environment).strip()
    return path.resolve(), commit_hash, starting_hash


def _git(path: Path, *arguments: str, env: dict[str, str]) -> str:
    completed = subprocess.run(
        ("git", "-C", str(path), *arguments),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout


def _parent_index(index: int) -> int | None:
    position = index % 50
    if position in {6, 7}:
        return index - position
    if position == 28:
        return index - 22
    return None


def _lineage_kind(index: int, parent_id: str | None) -> str:
    if parent_id is None:
        return "root"
    if index % 20 == 7:
        return "ambiguous"
    if index % 50 == 28:
        return "inherited"
    return "inherited" if index % 2 == 0 else "independent"


def _token_sequence(
    index: int,
    *,
    lineage_kind: str,
    parent_sequence: tuple[int, ...],
) -> tuple[int, ...]:
    if index % 19 == 0:
        return ()
    own = 1000 + (index % 23) * 37
    if lineage_kind == "root" or not parent_sequence:
        return (own // 3, own)
    parent_final = parent_sequence[-1]
    if lineage_kind == "inherited":
        return (*parent_sequence, parent_final + own)
    if lineage_kind == "ambiguous":
        return (max(1, parent_final * 4 // 5), parent_final + own)
    return (0, own)


def _prompt_record(session_id: str, text: str, timestamp: datetime) -> dict[str, object]:
    return {
        "timestamp": _timestamp(timestamp),
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "id": f"prompt-{session_id}",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _rollout_records(
    *,
    session_id: str,
    started: datetime,
    cwd: Path,
    model: str,
    prompt: dict[str, object],
    parent_prompt: dict[str, object] | None,
    token_sequence: tuple[int, ...],
    commit_hash: str,
    index: int,
    rng: random.Random,
) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = [
        {
            "timestamp": _timestamp(started),
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": str(cwd),
                "source": "synthetic",
                "model": model,
                "model_provider": "synthetic-provider",
                "cli_version": "synthetic-1.0",
            },
        }
    ]
    if parent_prompt is not None:
        records.append(parent_prompt)
    records.append(prompt)
    command = _command_for(index)
    call_id = f"call-{session_id}"
    records.extend(
        (
            {
                "timestamp": _timestamp(started + timedelta(seconds=15)),
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": call_id,
                    "arguments": json.dumps({"cmd": command}, sort_keys=True),
                },
            },
            {
                "timestamp": _timestamp(started + timedelta(seconds=20)),
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(
                        {
                            "exit_code": 1 if index % 29 == 0 else 0,
                            "output": "synthetic tool output is never persisted",
                        },
                        sort_keys=True,
                    ),
                },
            },
        )
    )
    if index % 8 == 0:
        git_call = f"git-{session_id}"
        records.extend(
            (
                {
                    "timestamp": _timestamp(started + timedelta(seconds=25)),
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": git_call,
                        "arguments": json.dumps(
                            {"cmd": "git commit -m 'synthetic fixture commit'"}
                        ),
                    },
                },
                {
                    "timestamp": _timestamp(started + timedelta(seconds=26)),
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": git_call,
                        "output": json.dumps(
                            {
                                "exit_code": 0,
                                "output": (
                                    f"[main {commit_hash[:12]}] synthetic fixture commit\n"
                                ),
                            }
                        ),
                    },
                },
            )
        )
    elif index in {1, 2}:
        git_call = f"git-candidate-{session_id}"
        records.extend(
            (
                {
                    "timestamp": _timestamp(started + timedelta(seconds=25)),
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": git_call,
                        "arguments": json.dumps(
                            {"cmd": "git commit -m 'synthetic candidate commit'"}
                        ),
                    },
                },
                {
                    "timestamp": _timestamp(started + timedelta(seconds=26)),
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": git_call,
                        "output": json.dumps(
                            {
                                "exit_code": 0 if index == 1 else 1,
                                "output": "synthetic commit result without a hash",
                            }
                        ),
                    },
                },
            )
        )
    for position, total in enumerate(token_sequence):
        input_tokens = max(0, total * 4 // 5)
        output_tokens = total - input_tokens
        records.append(
            {
                "timestamp": _timestamp(started + timedelta(seconds=30 + position)),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "total_tokens": total,
                        },
                        "total_token_usage": {
                            "input_tokens": input_tokens,
                            "cached_input_tokens": input_tokens // 5,
                            "output_tokens": output_tokens,
                            "reasoning_output_tokens": output_tokens // 4,
                            "total_tokens": total,
                        },
                    },
                },
            }
        )
    if index % 17 == 0:
        records.append(
            {
                "timestamp": _timestamp(started + timedelta(seconds=50)),
                "type": "synthetic_unknown_record",
                "payload": {"shape": rng.choice(("alpha", "beta", "gamma"))},
            }
        )
    records.append(
        {
            "timestamp": _timestamp(started + timedelta(minutes=2)),
            "type": "event_msg",
            "payload": {"type": "task_complete" if index % 13 else "synthetic_lifecycle"},
        }
    )
    return tuple(records)


def _command_for(index: int) -> str:
    commands = (
        "python -m pytest tests/test_synthetic.py",
        "git status --short",
        "python synthetic_analysis.py --dry-run",
        "ruff check synthetic.py",
        "mypy synthetic.py",
        "python -m build",
        "orca synthetic.inp --dry-run",
        "rg synthetic src",
    )
    return commands[index % len(commands)]


def _write_rollout(
    path: Path,
    records: tuple[dict[str, object], ...],
    *,
    malformed: bool,
    partial: bool,
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        if malformed:
            handle.write("{synthetic-malformed-json}\n")
        if partial:
            handle.write('{"type":"event_msg","payload":')


def _write_state_database(
    path: Path,
    threads: list[tuple[object, ...]],
    edges: list[tuple[str, str, str]],
) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT,
                archived_at TEXT,
                source TEXT,
                cwd TEXT,
                model TEXT,
                model_provider TEXT,
                archived INTEGER,
                git_branch TEXT,
                git_sha TEXT,
                git_origin_url TEXT
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT PRIMARY KEY,
                status TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            threads,
        )
        connection.executemany(
            "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
            edges,
        )


def _write_alternative_state_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO metadata VALUES ('kind', 'synthetic-incompatible')")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sessions", type=int, default=240)
    parser.add_argument("--repositories", type=int, default=8)
    parser.add_argument("--models", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20260810)
    arguments = parser.parse_args()
    corpus = generate_synthetic_corpus(
        arguments.output,
        config=SyntheticCorpusConfig(
            session_count=arguments.sessions,
            repository_count=arguments.repositories,
            model_count=arguments.models,
            seed=arguments.seed,
        ),
    )
    print(json.dumps(corpus.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
