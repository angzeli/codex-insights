# Changelog

All notable changes to Codex Insights are documented here.

## Unreleased

### Added

- normalized explicit thread-spawn relationships and content-free token-lineage evidence;
- `usage --reconciliation` diagnostics for observed, inherited/replayed, reconciled, ambiguous,
  and unavailable token accounting.
- privacy-safe, versioned semantic event fingerprints and explicit observed-versus-originated
  provenance across parent/child threads;
- `provenance` aggregate diagnostics with session, event-family, and JSON filters.
- lineage-aware logical prompt indexing with stable IDs, replay observations, bounded secret
  redaction, and long-content truncation;
- `prompts`, `prompt`, and SQLite FTS5-backed `search` commands with repository, model, session,
  and time filters.
- lineage-aware `tools` and `commands` analytics with privacy-filtered command metadata, normalized
  categories, result coverage, and repeated-command diagnostics;
- stable repository identities plus confidence-tiered, provenance-aware Git commit correlation;
- deterministic session outcome classification from originated validation, edit, error, commit, and
  lifecycle evidence;
- explainable action/domain task taxonomy, scientific facets, and versioned descriptive prompt
  features without an opaque quality score;
- weekly and monthly Markdown, versioned JSON, and self-contained offline HTML reports built from
  shared analytics functions.
- schema-13 compatibility provenance, per-session capability states, bounded unknown-record
  metadata, and coverage-regression snapshots;
- `doctor --deep` human/JSON diagnostics for state-database selection, parser versions, stale
  sessions, recovery failures, source/index reconciliation, and lineage/provenance health;
- safe live-rollout mutation detection, conservative retry/defer behavior, and previous-good
  transactional recovery for partial or failed reparses.
- persistent future-retention controls for redacted logical prompts and bounded command text,
  content-free `privacy inspect`, and confirmed prompt/command-text purge;
- stable `codex-insights-export-v1` JSON and formula-injection-safe CSV exports for normalized
  sessions, usage, prompts, commands, commits, outcomes, tasks, repositories, and models;
- consistent SQLite derived-index backup metadata and guarded `reset-index` with an optional
  explicit backup.

### Changed

- additive totals in `stats`, `repos`, `models`, and `usage` now subtract exact inherited/replayed
  baselines while retaining ambiguous child totals;
- per-session displays and token distributions remain observed rollout values.
- aggregate tool/command metrics exclude exact inherited or replayed observations while preserving
  per-session observations and explicit ambiguity;
- periodic comparisons suppress percentages when token coverage changes materially.
- state databases are selected by compatible structure and rollout-reference consistency rather
  than numeric filename order; alternative candidates are reported and never silently combined.

### Safety

- tool stdout/stderr, patches, hidden reasoning, environment dumps, and raw rollout records remain
  excluded from the derived index;
- report output is opt-in and cannot be written inside a Codex home or over the analytics database;
- Git inspection and correlation use read-only repository operations.
- configuration, export, backup, report, database, and reset paths resolve symlinks and reject
  source-home descendants or existing same-inode aliases of known Codex source files;
- export never reconstructs redacted content, CSV escapes formula-like text, and reset verifies the
  expected Insights schema before deleting only derived database files.

## 0.1.0 - 2026-08-09

### Added

- bounded, metadata-only discovery and schema auditing for local Codex storage;
- a read-only source adapter and incremental normalized SQLite index;
- session listing, filtering, prefix lookup, repository/model summaries, and overview statistics;
- token usage reports by repository, model, day, and week with filters, top-N selection, timezone
  handling, percentiles, sessions/day, JSON output, and explicit field-level coverage;
- synthetic fixtures and safety tests that never access a user's real Codex history.

### Safety

- Codex-owned files and databases remain strictly read-only;
- the analyzer database must be stored outside the selected Codex home;
- prompts, transcript bodies, hidden reasoning, command arguments, stdout/stderr, patches, and raw
  rollout records are not retained in the index.

### Not included

- prompt text search;
- Git commit correlation;
- outcome classification;
- a dashboard;
- LLM-based transcript analysis.
