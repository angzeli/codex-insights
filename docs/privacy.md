# Privacy, retention, export, backup, and reset

Codex Insights treats the configured Codex home as immutable source data. Privacy controls manage
only the separate derived Insights database, its local configuration, and exports explicitly
requested by the user. They never delete or rewrite Codex history.

## Threat model

The derived database can expose sensitive context even though it is local:

- user prompts can contain unpublished ideas, credentials, filenames, or source excerpts;
- command text can expose paths, arguments, repository operations, or secret-like values;
- repository names, branches, working directories, Git hashes, and task labels reveal project
  context even without transcript text;
- reports, dashboards, exports, and backups are additional files with their own access controls;
- redaction recognizes selected high-risk patterns, not every possible secret or personal datum.

Raw tool output is excluded because stdout/stderr, patches, environment dumps, and file contents can
contain substantially more sensitive material than is needed for aggregate analytics. Hidden
reasoning and raw rollout records are also excluded. Codex Insights does not provide encryption or
key management; normal device, filesystem, and backup security still applies.

## What the derived index stores

Metadata retained independently of text settings includes normalized sessions, source paths,
repository and model attribution, token observations and reconciled contributions, event
fingerprints/provenance, prompt identity and replay observations when a prompt is stored, command
fingerprints/categories/executables, Git correlations, outcomes, task taxonomy, compatibility
diagnostics, and ingestion state.

Optional text is limited to:

- origin-aware user prompt text after `prompt-content-v1` redaction and a 100,000-character cap;
- command text after `command-privacy-v1` redaction, whitespace normalization, heredoc removal, and
  a 512-character cap.

The original secret or unbounded text is not retained alongside the filtered value. Command counts
use a normalized fingerprint rather than the presence of command text, so disabling command-text
retention does not turn known commands into zero.

Inspect the active policy and category counts without printing prompt or command values:

```bash
codex-insights privacy
codex-insights privacy inspect
codex-insights privacy inspect --json
```

`privacy inspect` reports the derived DB and config paths, text-policy flags, prompt/command body
counts, redacted/truncated item counts, replay observations, and derived analytics counts. Raw tool
output, hidden reasoning, and raw rollout storage are always reported as false.

## Future-retention policy

The defaults preserve the Phase-II behavior: redacted prompt text and bounded redacted command text
are stored. Change future indexing with:

```bash
codex-insights privacy config --store-prompts off
codex-insights privacy config --store-command-text off
codex-insights privacy config --store-prompts on --store-command-text on
```

The persistent JSON configuration schema is `codex-insights-config-v1`. Its default path is:

- macOS: `~/Library/Application Support/Codex Insights/config.json`
- Windows: `%LOCALAPPDATA%\Codex Insights\config.json`
- other platforms: `${XDG_CONFIG_HOME:-~/.config}/codex-insights/config.json`

`--config PATH` overrides it. Configuration paths resolving inside the Codex home, including
symlink aliases, are rejected.

These switches control future indexing. Turning a setting off does not silently delete already
stored text. Newly observed command metadata remains usable with `command_text = null`. Re-enabling
a setting triggers a controlled content reconciliation so unchanged source sessions that were
indexed while storage was disabled can be backfilled. No missing text is fabricated.

## Purging existing text

To remove existing content, combine the persistent policy change with an explicit purge:

```bash
codex-insights privacy config --store-prompts off --store-command-text off
codex-insights privacy purge prompts
codex-insights privacy purge command-text
```

Purge requires confirmation unless `--yes` is supplied. Prompt purge deletes logical prompt rows,
replay links, descriptive prompt features, and their FTS5 entries. SQLite secure deletion is enabled
for the operation and the external-content FTS index is rebuilt from the remaining prompt table.
Command-text purge sets only `command_text` to null; fingerprints, categories, executable names,
result metadata, originated counts, and the non-sensitive `git_commit` operation marker remain.

Purge does not run `VACUUM`, does not modify the retention setting, and never touches the Codex
source database. If storage remains enabled, later changed source sessions may store filtered text
again. Use `privacy config ... off` for a persistent no-new-text policy.

## Safe export

`export` writes one selected normalized dataset as JSON or CSV:

```bash
codex-insights export --dataset sessions --format json --output sessions.json
codex-insights export --dataset usage --format csv --output usage.csv
codex-insights export --dataset prompts --format json --output prompts.json
codex-insights export --dataset commands --format csv --output commands.csv
codex-insights export --dataset repositories --since 30d --output repos.json
```

Supported datasets are `sessions`, `usage`, `prompts`, `commands`, `commits`, `outcomes`, `tasks`,
`repositories`, and `models`. Filters include `--since`, `--until`, `--repo`, and `--model`.

JSON uses schema `codex-insights-export-v1`. Additive usage fields are named
`reconciled_local_*`; per-rollout values are named `observed_rollout_*`; inherited baselines are
named explicitly. Prompt exports contain logical origin-aware rows. Command exports contain only
originated command activity. Commit confidence and evidence type remain explicit.

Exports never reconstruct pre-redaction text, include raw tool output, or serialize hidden
reasoning/source payloads. When the active policy disables prompt or command text, the corresponding
export rows retain permitted metadata but serialize text as `null`/an empty CSV cell.

CSV applies a spreadsheet formula-injection defense: a textual cell whose first non-whitespace
character is `=`, `+`, `-`, or `@` is prefixed with an apostrophe. Consumers that need the literal
original derived value can remove that single export-layer apostrophe after treating the field as
text.

Exports do not overwrite an existing file without `--overwrite`. A missing parent directory is
created only with `--create-parents`. Writes use a same-directory temporary file and atomic replace.
Destinations resolving inside Codex home, aliasing a Codex source file, or overwriting the Insights
database/config are rejected.

## Derived-index backup

Create an explicit consistent SQLite backup with:

```bash
codex-insights backup-index /safe/path/codex-insights-backup.sqlite3
```

The command uses SQLite's backup API instead of copying an active database file. A
`backup_metadata` table records creation time, Insights schema version, and application version.
The command reports how many stored prompt and command bodies the backup contains so sensitive text
is not copied silently. It never includes the Codex home automatically, refuses source-home
destinations, does not overwrite without `--overwrite`, and requires `--create-parents` for a
missing parent directory.

## Resetting the derived index

`reset-index` deletes only a verified Codex Insights database and its SQLite `-wal`/`-shm` sidecars:

```bash
codex-insights reset-index
codex-insights reset-index --backup /safe/path/before-reset.sqlite3
codex-insights reset-index --yes
```

Before confirmation, the command resolves and displays the real database path, rejects roots/home
directories and source aliases, opens the target read-only, and verifies the expected
`schema_migrations`, `source_sessions`, and `index_runs` tables. Backup is never implicit because it
may duplicate retained text. A subsequent `codex-insights index` rebuilds the derived database from
the still-untouched source history under the then-active retention policy.

## Path-safety model

All configuration, export, backup, report, derived-database, purge, and reset paths are normalized
with real-path resolution before mutation. Exact/descendant Codex-home paths, parent traversal,
symlinks into source, and existing hard-link/same-inode aliases of known source files are rejected.
Known-source inode checks inspect only immediate Codex metadata and the `sessions` and
`archived_sessions` trees by filename/stat metadata; file contents are never dumped.
