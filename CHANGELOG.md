# Changelog

All notable changes to Codex Insights are documented here.

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
