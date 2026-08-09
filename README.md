# Codex Insights

Codex Insights is a local-first, read-only analytics and observability workbench for Codex
sessions. Its goal is to make patterns such as project activity, token use, model selection,
tool frequency, Git correlation, outcomes, and efficiency trends understandable without
uploading a user's private working history.

## Project status

This repository is at the foundation stage. The CLI can report its version and run a bounded
environment check. It does **not** parse or index Codex sessions yet, and it has no dashboard.

```text
codex-insights --help
codex-insights version
codex-insights doctor
codex-insights doctor --codex-home /path/to/codex-home
```

Codex home resolution uses this precedence:

1. `--codex-home`
2. `CODEX_HOME`
3. `~/.codex`

## Local-first and read-only

Codex-owned files are source material, never application state. Codex Insights must not write,
rename, move, delete, vacuum, migrate, or otherwise modify anything in the selected Codex home.
Any future SQLite inspection must use explicit read-only connections. The analyzer's own SQLite
index belongs in a separate application-data directory.

Raw rollout histories can contain prompts, source excerpts, commands, file paths, tool output,
and environment details. Feeding those histories wholesale back into an agent can unnecessarily
expose sensitive data and create a second copy in model context. Codex Insights therefore uses
bounded discovery, source adapters, normalized metadata, and aggregate analytics. It must never
recursively dump a Codex home into an agent context.

See [docs/data-safety.md](docs/data-safety.md) for the enforceable policy.

## Development setup

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

pytest
ruff check .
mypy
codex-insights --help
codex-insights version
codex-insights doctor --codex-home tests/fixtures/codex_home
```

All tests use committed synthetic fixtures or temporary directories. They must never inspect the
developer's real Codex history.

## Planned architecture

Codex storage is treated as an unstable external format behind an adapter boundary:

```text
Codex source files
  -> source adapters
  -> normalized internal models
  -> separate Codex Insights SQLite index
  -> analytics
  -> CLI / reports / future dashboard
```

The `src/codex_insights/adapters/` package isolates source-format changes. Normalized models avoid
retaining raw tool stdout or stderr, while `analytics/` remains independent of the source format.
The initial database module enforces separation between Codex home and the analyzer index.

More detail is in [docs/architecture.md](docs/architecture.md).

## License

MIT
