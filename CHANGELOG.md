# Changelog

All notable changes to Codex Insights are documented here.

## Unreleased

- normalized scalar and structured Codex client sources into stable client/subagent kinds without
  persisting raw structured encodings;
- separated turn lifecycle from task outcomes and added strongly evidenced outcome coverage;
- made incremental reconciliation dependency-aware while retaining explicit stage timings and
  unchanged-source idempotency;
- classified source-drift observations by risk/capability with bounded first/last/newly-seen
  diagnostics and no raw payload values;
- stabilized Git provenance with ancestry-qualified MEDIUM evidence, bounded LOW candidates,
  omitted-candidate coverage, and ref-state invalidation;
- added derived-schema migrations 17-21 for source kinds, outcome/lifecycle semantics, diagnostics,
  Git candidate state, effective tool timestamps, and measured analytics indexes;
- aggregated tool analytics in SQL and removed per-repository task-query loops from reports and the
  static dashboard.
- decomposed task-taxonomy coverage into prompt-backed, originated-activity-only,
  repository-fallback-only, and no-origin-evidence populations without broadening classifier rules;
- surfaced source/index lag, latest successful indexing, missing rollout, compatibility, token
  lineage, and event-provenance summaries in human `doctor --deep` output;
- added visible keyboard focus, 44-pixel controls, WCAG AA light-theme contrast, and programmatic
  table names/scoped headers to static dashboards and HTML reports;
- added focused Python 3.11/3.14 Windows wheel-install workflow coverage and made benchmark memory
  reporting explicitly unavailable when the POSIX `resource` module is absent.

## 1.1.0 - 2026-08-12

### Correctness and safety hardening

- made derived-index reset fail closed if the validated database changes filesystem identity before
  deletion, including identity-safe handling of SQLite sidecars;
- hardened shell executable normalization so shell keywords, leading options, and ambiguous
  compound syntax are not reported as executables;
- preserved conservative unknown/ambiguous command identity rather than guessing, with deterministic
  regression coverage for both failure classes.

## 1.0.0 - 2026-08-10

### Normalized analytics

- added explicit thread topology and content-free token-lineage evidence;
- reconciled additive usage by subtracting only exact inherited/replayed ancestor baselines while
  retaining observed per-rollout values and explicit ambiguity;
- added privacy-safe observed-versus-originated event provenance and replay diagnostics;
- added origin-aware redacted prompt indexing, replay observations, descriptive prompt features,
  and SQLite FTS5 search;
- added originated tool/command analytics, bounded command metadata, result coverage, and
  conservative repeated-invocation reporting;
- added stable repository identities and HIGH/MEDIUM/LOW provenance-aware Git associations;
- added deterministic originated-evidence outcome classification and explainable action/domain task
  taxonomy with UNKNOWN preserved;
- added weekly/monthly Markdown, stable JSON, and self-contained offline HTML reports;
- added a self-contained static dashboard with generation-time filters, shared metrics, explicit
  data quality, escaped labels, and no remote assets or private text.
- attributed daily and weekly token activity from nonnegative token-event increments instead of
  assigning a resumed rollout's full cumulative lifetime total to its start date;
- retained content-free token-event timestamps/vectors in derived schema 16 with explicit temporal
  fallback and unattributed coverage;
- added Dashboard Daily/Weekly/Overall overview controls, synchronized activity sorting by date,
  sessions, or tokens, and fixed bar-track geometry for long numeric labels.

### Compatibility and recovery

- added structural source database selection instead of numeric filename assumptions;
- added per-session capability states, bounded unknown-source metadata, parser/schema provenance,
  semantic-drift warnings, and `doctor --deep` diagnostics;
- added safe live-rollout mutation detection, one conservative retry, stale-state reporting, and
  previous-good transactional recovery for partial or failed reparses;
- added forward-compatibility fixtures for renamed catalogues, unknown records, removed fields,
  changed tool encodings, partial files, multiple state databases, and token semantic drift;
- preserved token lineage, event provenance, prompt search, Phase-II analytics, and retention state
  across representative historical derived-index migrations;
- verified failed derived-schema migrations roll back both schema objects and version metadata.

### Privacy and derived-data control

- added persistent future-retention controls for redacted prompt and bounded command text;
- added content-free privacy inspection and confirmed prompt/FTS or command-text purge;
- added stable `codex-insights-export-v1` JSON and formula-injection-safe CSV for normalized
  sessions, usage, prompts, commands, commits, outcomes, tasks, repositories, and models;
- added consistent SQLite derived-index backups and a guarded reset with an optional explicit backup;
- hardened all write destinations against source-home descendants, traversal, symlink aliases,
  hard-link/source-inode aliases, and protected derived files;
- retained the permanent exclusion of raw tool output, patches, hidden reasoning, environment dumps,
  and raw rollout records.

### Quality and release hardening

- added a deterministic realistic synthetic corpus and full source-mutation-invariant workflow;
- added Python 3.11-3.14 CI, Ruff/mypy jobs, wheel/sdist installation smoke tests, and macOS normal
  plus editable-install coverage including the hidden-`.pth` guard;
- added a manual deterministic benchmark workflow and a 10,000-session performance profile;
- bounded incremental event-provenance reconciliation to changed topology branches and indexed
  event foreign keys so updating one live rollout does not rescan or cascade through all events;
- made literal/exact identifier-prefix resolution consistent across session, prompt, provenance,
  tool, and Git analytics;
- froze observed, originated, reconciled, confidence, coverage, ambiguous, and UNKNOWN metric
  definitions for v1.

## 0.1.0 - 2026-08-09

### Added

- bounded, metadata-only discovery and source schema auditing;
- a read-only Codex-local adapter and incremental normalized SQLite index stored outside Codex home;
- session list/detail, repository/model summaries, and overview statistics;
- token usage by repository, model, day, and week with filters, top-N selection, timezone handling,
  distributions, sessions/day, JSON output, and field-level coverage;
- focused synthetic fixtures and safety tests that never access a user's real Codex history.

### Safety

- treated Codex-owned files and databases as strictly read-only;
- rejected analyzer databases under the selected Codex home;
- excluded prompts, transcript bodies, hidden reasoning, command arguments, stdout/stderr, patches,
  and raw rollout records from the initial index.
