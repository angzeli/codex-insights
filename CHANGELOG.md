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

### Changed

- additive totals in `stats`, `repos`, `models`, and `usage` now subtract exact inherited/replayed
  baselines while retaining ambiguous child totals;
- per-session displays and token distributions remain observed rollout values.

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
