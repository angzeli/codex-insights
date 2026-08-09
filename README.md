# Codex Insights

Codex Insights is a local-first, read-only analytics and observability workbench for Codex
sessions. Its goal is to make patterns such as project activity, token use, model selection,
tool frequency, Git correlation, outcomes, and efficiency trends understandable without
uploading a user's private working history.

## Project status: v0.1.0 MVP

The local-first command-line MVP is ready for daily use. It can audit an installed Codex layout,
incrementally index normalized session metadata, explore individual sessions, and report token
usage by repository, model, day, or week with explicit data coverage.

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
codex-insights usage --since 7d
codex-insights usage --since 7d --by repo
```

Codex home resolution uses this precedence:

1. `--codex-home`
2. `CODEX_HOME`
3. `~/.codex`

## First-run flow

Python 3.11 or newer is required. From a local checkout:

1. Install Codex Insights.

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install .
   ```

2. Confirm the runtime and detected Codex home without reading session histories.

   ```bash
   codex-insights doctor
   ```

3. Run the bounded, metadata-only source audit.

   ```bash
   codex-insights audit-source
   ```

4. Build or incrementally refresh the separate analytics index.

   ```bash
   codex-insights index
   ```

5. Check indexed coverage and recent activity.

   ```bash
   codex-insights stats
   ```

6. Review the last seven days by normalized repository.

   ```bash
   codex-insights usage --since 7d --by repo
   ```

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

## Analyze token usage

`usage` reports reconciled additive token totals and always shows how many matching sessions
contain token records. Exact inherited or replayed parent baselines are counted once; ambiguous
child relationships retain their observed total and are reported explicitly. Missing per-session
or per-field values stay `unknown` in tables and `null` in JSON.

```bash
codex-insights usage
codex-insights usage --since 7d --by repo --top 10
codex-insights usage --since 30d --by model --top 5
codex-insights usage --by day --timezone Asia/Shanghai
codex-insights usage --by week --timezone local
codex-insights usage --repo my-project --model model-name --json
codex-insights usage --reconciliation
```

Repository groups come only from the normalized repository root/name recorded during indexing;
arbitrary working-directory substrings are not treated as repository identities. Day and week
buckets use `--timezone`, whose default is the machine's local timezone. Weeks start on Monday.
Mean, median, and nearest-rank p90 tokens/session describe observed per-rollout totals and use only
sessions with known total-token data. Additive totals use reconciled per-session contributions.
Sessions/day uses the selected calendar window, or the inclusive first-to-latest activity span
when no time filter is supplied.

`usage --reconciliation` shows the observed rollout sum, confidently identified inherited/replayed
usage, reconciled aggregate, child-thread coverage, and ambiguous contribution. Child-exclusive
usage is attributed to the child's own start date, repository, and model. Local telemetry is not a
guarantee of billing or quota semantics and is not tuned to match a Codex UI value.

This abridged example is generated from the repository's synthetic four-session fixture—not real
history:

```text
$ codex-insights usage --by repo --timezone UTC
150 reconciled tokens across 2/4 sessions with token records (50.0%)
Timezone: UTC

Repository                Sessions  Reconciled tokens  Token data  Mean/session    P90  Sessions/day
repo-one                         2           100         1/2         100.0  100.0          0.22
Outside Git repositories        1            50         1/1          50.0   50.0          0.11
repo-two                         1       unknown         0/1       unknown unknown          0.11
```

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
# Creates the non-hidden venv-acceptance environment, installs editable,
# and verifies both a fresh import and the CLI entry point.
./scripts/setup-dev.sh
source venv-acceptance/bin/activate

pytest
ruff check .
mypy
codex-insights --help
codex-insights version
codex-insights doctor --codex-home tests/fixtures/codex_home
```

All tests use committed synthetic fixtures or temporary directories. They must never inspect the
developer's real Codex history.

### macOS editable-install caveat

A regular `python -m pip install .` installs package files normally and does not depend on a
`.pth` file. A setuptools editable install uses a generated file such as
`__editable__.codex_insights-0.1.0.pth` to add this repository's `src/` directory at Python
startup.

Python 3.14 on macOS skips a `.pth` whose filesystem flags include `UF_HIDDEN`. This can produce a
misleading `ModuleNotFoundError` even when the editable file contains the correct source path. It
is filesystem metadata on the development environment, not a Codex source-discovery or history
ingestion failure.

The supported setup helper prevents the observed condition by defaulting to the non-dot-prefixed
`venv-acceptance` directory. It then verifies that the editable artifact is inside that active
environment, checks its macOS flags, and starts a fresh Python process to confirm the file was not
skipped. If `UF_HIDDEN` is present, the helper runs targeted `chflags nohidden` only on the verified
Codex Insights editable `.pth`; it never recursively changes an environment or touches unrelated
`.pth` files. The guard waits briefly and fails with instructions to recreate the environment if
macOS reapplies the flag, as occurred in the affected dot-prefixed acceptance environment.

To diagnose an existing editable environment without reinstalling, run the guard with that
environment's Python:

```bash
path/to/environment/bin/python scripts/editable_install_guard.py
```

Avoid dot-prefixed development environments in macOS user-visible folders such as Desktop. The
failure was not reproduced by a fresh dot-prefixed environment under `/tmp`, so the directory name
alone is not a universal trigger; the supported non-dot name avoids the observed Desktop metadata
interaction and the guard detects any recurrence.

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

## Intentionally not implemented in v0.1.0

- prompt text search;
- Git commit correlation;
- session outcome classification;
- a dashboard or web server;
- LLM-based transcript analysis.

These features require separate privacy, evidence, and product-design work. The MVP does not store
the raw content they would need.

## License

MIT
