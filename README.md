# Codex Insights

Codex Insights is a local-first, read-only analytics and observability workbench for Codex
sessions. Its goal is to make patterns such as project activity, token use, model selection,
tool frequency, Git correlation, outcomes, and efficiency trends understandable without
uploading a user's private working history.

## Project status: v0.1.0 MVP with Phase-II local analytics

The local-first command-line MVP is ready for daily use. It can audit an installed Codex layout,
incrementally index normalized session metadata, explore individual sessions, report token usage,
audit cross-thread replay, search redacted origin-aware user prompts, analyze originated commands,
correlate Git commits, classify outcomes/tasks, and generate offline weekly or monthly reports.

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
codex-insights provenance
codex-insights prompts --since 7d
codex-insights search '"quoted phrase"'
codex-insights prompt PROMPT_ID
codex-insights tools --since 7d
codex-insights commands --repeated
codex-insights commits --confidence high
codex-insights outcomes --since 30d
codex-insights tasks --by type
codex-insights report weekly --format markdown
codex-insights report monthly --format html --output ~/codex-month.html
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
cumulative token totals (or explicitly labelled summed deltas), aggregate event counts, compact
provenance fingerprints, redacted user-authored prompt text for local search, and bounded,
privacy-filtered command metadata. It does not retain assistant/hidden reasoning, raw command
results, patches, tool output, stdout/stderr, environment dumps, or raw JSONL records.

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

`provenance` applies the same observed-versus-originated distinction to selected non-token event
families. It stores only versioned fingerprints, order, approximate lengths, and evidence—not
message bodies, commands, patches, tool output, or reasoning. Exact ordered replay is attributed to
the known ancestor; weak overlap remains explicit and is not deduplicated. See
[docs/event-provenance.md](docs/event-provenance.md).

## Search user prompts

Prompt history is local, origin-aware, and privacy-filtered. Replay copies in descendant rollouts
are observations of the originating prompt rather than separate search hits. System/developer
instructions, subagent instructions, assistant content, reasoning, commands, and tool output are
excluded. Obvious credential forms are redacted before storage, and prompts are capped at 100,000
stored characters.

```bash
codex-insights prompts --since 7d --repo my-project
codex-insights search "migration" --model model-name
codex-insights search '"quoted phrase"' --session SESSION_PREFIX --json
codex-insights prompt prm_IDENTIFIER_PREFIX
```

Search uses SQLite FTS5 rather than a silent substring fallback. Prompt output is sensitive even
after redaction; see [docs/privacy.md](docs/privacy.md) for the exact content, lineage, redaction,
and long-prompt policy.

## Phase-II analytics and reports

Tool, Git, outcome, and task analytics use the same explicit observed-versus-originated provenance
model as prompts and tokens:

```bash
codex-insights tools --since 7d
codex-insights commands --since 7d --repeated
codex-insights commits --confidence high
codex-insights outcomes --since 30d
codex-insights tasks --by type
codex-insights tasks --by domain --repo codex-insights
```

Commands are bounded and privacy-filtered before storage; raw stdout/stderr is not retained. Commit
correlation uses read-only Git inspection and reports HIGH, MEDIUM, and LOW evidence separately.
Outcomes and tasks are deterministic, explainable classifications, with `unknown` preferred over
unsupported certainty. Prompt features are descriptive and versioned; no prompt-quality score or LLM
classifier is used.

Periodic reports reuse these analytics and write a file only when `--output` is explicit:

```bash
codex-insights report weekly --date 2026-08-09 --timezone UTC
codex-insights report monthly --format json --output ~/codex-month.json
codex-insights report weekly --format html --output ~/codex-week.html
```

This abridged report fragment also comes from synthetic fixtures:

```text
# Codex Insights Weekly Report

Period: 2026-08-03 to 2026-08-09 (UTC)
Sessions: 3
Reconciled known tokens: 50
Token coverage: 1/3 sessions
UNKNOWN outcomes: 3
```

See [docs/metrics.md](docs/metrics.md), [docs/git-correlation.md](docs/git-correlation.md),
[docs/outcomes.md](docs/outcomes.md), [docs/task-taxonomy.md](docs/task-taxonomy.md), and
[docs/reports.md](docs/reports.md) for the exact evidence, attribution, and coverage semantics.

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

## Intentionally not implemented

- a dashboard or web server;
- LLM or semantic transcript analysis;
- prompt-style quality scoring or causal efficiency claims;
- reproduction of OpenAI server-side billing or quota accounting.

These features require separate evidence and product-design work. Codex Insights does not retain the
raw assistant/tool transcript content that such analysis would require.

## License

MIT
