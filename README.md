# Codex Insights

Codex Insights is a local-first, read-only analytics and observability workbench for Codex
sessions. Its goal is to make patterns such as project activity, token use, model selection,
tool frequency, Git correlation, outcomes, and efficiency trends understandable without
uploading a user's private working history.

## Project status

The local indexing and core history-exploration foundation is implemented. The CLI can run a
bounded source audit, incrementally index normalized session metadata and aggregates, list and
inspect sessions, and summarize repository/model activity. Git commit correlation, outcome
heuristics, content search, and a dashboard remain future work.

```text
codex-insights --help
codex-insights version
codex-insights doctor
codex-insights doctor --codex-home /path/to/codex-home
codex-insights audit-source --codex-home /path/to/codex-home
codex-insights index --codex-home /path/to/codex-home --db /path/to/index.sqlite3
codex-insights db-info --db /path/to/codex-insights.sqlite3
codex-insights sessions --since 7d
codex-insights session SESSION_ID
codex-insights repos
codex-insights models
codex-insights stats
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

`audit-source` is the first bounded source-inspection command. It discovers versioned state
databases, counts rollout files with metadata calls, opens SQLite in read-only/query-only mode, and
streams only a small rollout sample. It reports schemas, record types, token field names, and
redacted text-field lengths—not prompt bodies, command arguments, or tool output.

```bash
# Human-readable aggregate audit; samples at most five rollout files by default.
codex-insights audit-source

# Machine-readable schema observations.
codex-insights audit-source --json --sample-size 5

# Per-file and redacted field-shape detail. Values remain redacted.
codex-insights audit-source --verbose
```

`index` loads structural catalogue metadata from a discovered state database, then streams only
the referenced rollout files that are new or changed. It stores normalized session metadata,
cumulative token totals (or explicitly labelled summed deltas), and aggregate event counts. It
does not retain prompts, message bodies, hidden reasoning, command arguments, patches, tool output,
stdout/stderr, environment dumps, or raw JSONL records.

```bash
# Uses the platform-aware database default documented below.
codex-insights index

# Keep a project-specific derived database at an explicit safe path.
codex-insights index --codex-home /path/to/codex-home --db /path/to/index.sqlite3

# Show schema version, session count, latest run, and source coverage.
codex-insights db-info --db /path/to/index.sqlite3
```

## Explore indexed history

History commands query only the normalized Codex Insights database. They never reopen rollout
files or print transcript content.

```bash
# Newest sessions first; paths are hidden in this compact view.
codex-insights sessions --since 7d --active --limit 25

# Filters can be combined. Relative durations include values such as 24h, 7d, and 30d.
codex-insights sessions --repo my-project --model model-name --source cli

# A full ID or an unambiguous prefix is accepted.
codex-insights session 019fe51f

codex-insights repos
codex-insights models
codex-insights stats

# Machine-readable forms are available on every history command.
codex-insights sessions --since 30d --json
codex-insights session 019fe51f --json
```

`--since` is inclusive. An ISO timestamp supplied to `--until` is exclusive; a date-only value
includes that complete calendar day. Stored timestamps are UTC, while human-readable tables render
times in the local timezone. Missing token data is shown as `unknown` and serialized as `null`, not
treated as zero. Sessions without a resolved Git root appear under `Outside Git repositories`.

See [docs/data-safety.md](docs/data-safety.md) for the enforceable policy.
Observed source concepts and unstable assumptions are tracked in
[docs/source-format.md](docs/source-format.md).

The default derived database is `~/Library/Application Support/Codex Insights/index.sqlite3` on
macOS, `%LOCALAPPDATA%\Codex Insights\index.sqlite3` on Windows, and
`${XDG_DATA_HOME:-~/.local/share}/codex-insights/index.sqlite3` on other platforms. `--db PATH`
overrides it. Codex Insights rejects a database path equal to or nested beneath the selected Codex
home.

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
The database module enforces separation between Codex home and the analyzer index. The core
indexer consumes only the normalized adapter contract and therefore does not depend on a particular
state database version, table name, rollout directory depth, or undocumented event name.
Reusable history queries live under `analytics/`; terminal and JSON formatting remain in the CLI
layer and never query Codex-owned storage.

More detail is in [docs/architecture.md](docs/architecture.md).
The normalized tables and migration policy are documented in
[docs/database-schema.md](docs/database-schema.md).

## License

MIT
