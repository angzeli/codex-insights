# Codex source compatibility and recovery

Codex local storage is an undocumented input format, not a stable API. Codex Insights treats each
recognized format as a capability-bearing source and preserves the last trustworthy normalized
state when a newer source cannot be parsed safely. All source access remains bounded and read-only;
recovery changes only the separate Codex Insights database.

## Independent versions

The derived SQLite schema version and source interpretation versions are deliberately separate:

- the **Codex Insights schema version** controls migrations of the derived database only;
- the **source parser version** identifies the source-to-normalized mapping and invalidates stale
  incremental parse results when that mapping changes;
- the **source-schema fingerprint version** hashes table/column structure and observed JSONL field
  names, record types, and payload types, never transcript content;
- the **event-fingerprint/provenance algorithm version** identifies event-origin matching rules;
- the **token-lineage algorithm version** identifies inherited-baseline accounting rules;
- outcome and task classifier versions identify their existing deterministic rules.

Per-session compatibility rows retain the source type, source database and rollout reference,
successful parser version, content-free source-schema hints/fingerprint, capability set, parse
status, indexing time, last successful parse time, and staleness. A failed newer attempt is recorded
in `ingestion_state` without overwriting the successful parser provenance or normalized analytics.

## Schema drift and capability detection

The adapter recognizes catalogue columns by semantic aliases rather than one table or database
name. Each parsed session records explicit states for:

- session catalogue and rollout metadata;
- token usage and token-lineage evidence;
- prompt content, event provenance, tool calls, and command extraction;
- Git metadata, repository and model attribution;
- task lifecycle, archive metadata, and duration timestamps.

`available`, `degraded`, `not_observed`, and `unknown` are distinct. In particular,
`not_observed` does not mean zero activity, and `unknown` is never rendered as complete coverage.
Global coverage snapshots aggregate these per-session states after each successful index run.

## State database selection

All top-level `state_*.sqlite` candidates plus legacy `state.sqlite` are opened with SQLite
`mode=ro` and `PRAGMA query_only = ON`. Candidates are scored using:

- SQLite readability;
- a structurally compatible session catalogue;
- timestamp and explicit relationship support;
- catalogue row count;
- bounded rollout-reference validity within the configured Codex home;
- modification time only as a late deterministic tie-breaker.

Codex Insights does not select solely by filename or the highest numeric suffix. If more than one
candidate is viable, exactly one is selected deterministically, the alternatives and scores are
reported, and their rows are not combined.

## Unknown records and semantic drift

Unknown record types, payload types, field names, and tool encodings do not abort a parse. The
derived database stores only a bounded name/category, count, parser/schema provenance, cheap
first/last timestamps, affected-session count, newly-seen status, and capability impact. Arbitrary
raw payloads are discarded. Categories distinguish `field_passthrough`, `recognized_ignored`,
`semantic_gap`, `tool_result_gap`, `lifecycle_gap`, and `unclassified` so harmless structural volume
does not obscure actionable semantic drift.

Structural compatibility is not enough. The parser warns when recognized cumulative token vectors
decrease or violate known component/total relationships. The indexer also records unresolved spawn
edges and cycles. Coverage snapshots compare prompt, tool, provenance, repository, model, token,
and other capability coverage with the previous successful run. A warning requires at least ten
baseline sessions, an absolute drop of 35 percentage points, and a fall to at most half the prior
ratio. These are drift warnings; they do not silently change accounting or classification rules.

## Live files and previous-good recovery

Rollouts are checked by device/inode identity, byte size, and nanosecond mtime before and after a
streaming parse. A detected mutation is retried once from the current file identity. If the file
changes again, the session is deferred as `pending_source_change`.

An incomplete final JSONL line is treated as a live partial write, not general corruption. For a
new session, the preceding valid records may be stored as an explicitly stale `indexed_partial`
result. For an already indexed session, the previous good usage, event observations, prompts,
tools, provenance, and lineage are retained and the source is marked `pending_partial_write`.
Once a complete stable source is available, an ordinary index run replaces the stale status
transactionally.

Every session replacement is one derived-database transaction. Parse or persistence failure leaves
the previous normalized rows intact. Safe error metadata contains the source reference, parser
version, exception type, status, and timestamp—never the failed line or payload.

## Diagnostics

`codex-insights doctor --deep` performs bounded, read-only diagnostics. It reports derived DB
integrity/schema, component versions, candidate and selected state databases, missing rollout
references, source/index counts, stale or failed sessions, bounded categorized source-format
diagnostics, capability coverage/regressions, and token-lineage/event-provenance status. `--json`
returns the same structured aggregate result. It does not trigger a re-index or migrate either
source or derived databases.

Local telemetry and compatibility diagnostics describe observed local files. They do not reproduce
or claim equivalence to OpenAI billing, quota, or private server-side accounting.
