# Changelog

All notable changes to Codex Insights are documented here.

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
