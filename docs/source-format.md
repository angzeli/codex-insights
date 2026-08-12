# Codex local source format observations

Codex local storage is undocumented and may change between installations or versions. This
document records what Codex Insights can safely observe, what it merely assumes, and which stable
concepts a future indexer may normalize. It is not a specification for Codex itself.

## Empirically observed current layout

A bounded audit of the current local installation on 2026-08-09 observed the following structural
facts. They are evidence for the next adapter version, not universal Codex contracts:

- `history.jsonl` was absent; the useful sources were a versioned state database plus active and
  archived rollout trees.
- `state_5.sqlite` contained a primary `threads` table and related `thread_*` tables. The `threads`
  row count aligned with the number of discovered rollout files in that audit.
- Useful `threads` columns covered ID, rollout path, creation/update/recency timestamps, source,
  provider/model, working directory, redacted title/first-message fields, archive state, and Git SHA,
  branch, and origin metadata.
- Observed rollout record types included `session_meta`, `turn_context`, `event_msg`,
  `response_item`, `world_state`, compaction records, and inter-agent metadata.
- Observed payload types included token counts, messages, reasoning, function/custom tool calls and
  outputs, task lifecycle records, patch completion, thread settings, and abort/compaction events.
- Token usage appeared beneath both `payload.info.last_token_usage` and
  `payload.info.total_token_usage`, with input, output, cached input, cache-write input, reasoning
  output, and total-token fields. Time-to-first-token also appeared as a separate metric.
- One sampled rollout reached the 4 MiB scan limit, confirming that bounded scans must remain part
  of the default behavior.

No title, prompt, message, command argument, or tool output value was returned by the audit.

## Audit scope and limits

`codex-insights audit-source` performs shallow discovery at the selected Codex home and recursive
metadata-only discovery inside the known `sessions/` and `archived_sessions/` roots. It never
recursively scans unrelated directories. Versioned state databases are discovered with the
top-level `state_*.sqlite` pattern rather than a fixed version number.

The default audit:

- stats every discovered rollout JSONL file to obtain count, size, and modification time;
- streams a timeline-spread sample of at most five rollout files;
- reads at most 4 MiB or 20,000 lines from each sampled rollout;
- bounds individual JSONL lines at 1 MiB;
- samples the head and tail of a large `history.jsonl` rather than reading gigabytes;
- opens state SQLite databases with `mode=ro` and `PRAGMA query_only = ON`;
- uses a progress limit for table row counts and a bounded rollout-reference check;
- returns text field names, types, presence counts, and approximate lengths, never their values.

`--sample-size 0` disables rollout content sampling while preserving discovery totals. Values above
100 are rejected. `--verbose` adds per-file scan details and tiny redacted SQLite metadata samples;
it does not unredact titles, content, prompts, commands, or tool output.

## Observed and recognized structural fields

The audit recognizes these as structural clues when present. Presence is not guaranteed.

### Top-level layout

- `history.jsonl`
- `sessions/`
- `archived_sessions/`
- `state_*.sqlite` and legacy `state.sqlite`
- adjacent JSON or TOML files whose names suggest configuration, version, model, global, or metadata
  state; credential-like files are excluded

### SQLite

All non-internal table names and column declarations are observable schema metadata. Tables are
considered likely session/thread tables when their names mention sessions, threads, or
conversations, or when their columns combine an identifier with a working directory, rollout path,
or timestamp.

Likely column roles include:

- session/thread/conversation identity;
- title or summary, always value-redacted;
- current working directory or worktree;
- source or origin;
- model;
- creation, update, start, or end timestamps;
- rollout file path;
- archived state;
- Git branch, commit, SHA, or related metadata;
- free-text content, whose values are never returned.

Row counts may be omitted when the query reaches its progress limit. Rollout paths may be checked
for existence, but path values are not included in the human report.

### Rollout JSONL

The parser accepts unknown record shapes and defensively observes:

- top-level field names;
- structural values from `type`, `event`, or `kind` when they are short identifier-like strings;
- nested `payload.type` values;
- event categories such as session metadata, token usage, tool/command, message/event, or unknown;
- field paths containing `token`;
- short identifier-like tool names, never arguments or outputs;
- timestamp-like fields;
- text-like field shape metadata with all values redacted.

Malformed lines, oversized lines, missing metadata records, and scan truncation are counted and
reported instead of crashing the audit.

### History JSONL

The history audit reports approximate line count, sampled valid and malformed records, top-level
field names, timestamp range when parseable, and sampled session-ID coverage. Prompt or text values
are represented only by redacted field-shape observations.

### Tool-call encodings used by the current adapter

A separate bounded metadata audit observed both direct `function_call` records and newer
`custom_tool_call` records whose outer tool is `exec`. The latter can contain multiple nested
`tools.<name>(...)` operations, including shell execution, patching, polling, plans, image/web calls,
and collaboration operations. Stable call identifiers often connect calls to function/custom-tool
outputs; structured results may expose exit code, wall time, or patch success.

The adapter normalizes a recognized nested operation rather than counting the outer `exec` wrapper.
Arguments and outputs are parsed transiently. Only bounded/redacted command metadata, normalized
category, digested call identity, status/exit/duration, and exact commit-hash evidence are eligible
for persistence. Unknown or malformed wrappers remain explicit and do not crash indexing. These
observations are version-specific, not a contract for future Codex releases.

## Assumptions

- JSONL files beneath `sessions/` and `archived_sessions/` are rollout candidates.
- File modification time is only an approximate activity signal.
- Record and payload `type` values are structural identifiers rather than user-authored prose.
- Fields named like timestamps are comparable activity signals after conservative normalization.
- A relative rollout path stored in SQLite is relative to the selected Codex home.
- Timeline-spread file sampling is more useful for version discovery than sampling only newest files.

Every assumption must remain inside the source adapter. Unknown fields are evidence to record, not
errors and not permission to infer content.

## Unstable, undocumented details

The following must not become hard-coded application contracts:

- state database version numbers, table names, schemas, and relationships;
- rollout directory depth and filename conventions;
- JSONL event names, nesting, optional fields, and token accounting shapes;
- timestamp units and encodings;
- whether a database row has a live rollout file;
- how archived sessions are represented;
- whether Git or model metadata is present.

Schema differences are reported so a future adapter can recognize versions explicitly and fail
closed when a source is not understood.

## Intended normalized concepts

The indexer translates recognized source records into stable internal concepts such as:

- session identity and lifecycle timestamps;
- repository, working directory, and Git correlation;
- model usage;
- input, output, cached, and total token metrics with provenance;
- origin-aware tool and command metadata without raw output;
- archived state;
- cautiously inferred outcome and rework signals;
- adapter version, source location, and source-format confidence.

The normalized index is derived, rebuildable state stored outside Codex home. The audit command
itself does not create that index and does not copy raw records.

## Indexer v1 recognition and limits

The first `CodexLocalAdapter` index mapping is based on the observations above. It discovers
top-level `state_*.sqlite` or legacy `state.sqlite` files and scores them by readable compatible
schema, timestamps, catalogue size, and bounded valid/missing rollout references. It does not pick
solely by numeric suffix or combine multiple catalogues. Within the selected read-only database it
recognizes a catalogue table by structural column roles—an
ID plus a rollout path, strengthened by timestamp, working-directory, model, archive, and Git
metadata—rather than depending on the literal name `threads` or the version `state_5.sqlite`.

Recognized source values are mapped inside the adapter:

- catalogue timestamps, working directory, archive state, model/provider, Git metadata, and
  rollout path become stable session fields; scalar catalogue sources are normalized to bounded
  `client_kind` values (`cli`, `editor`, `subagent`, `other`, or `unknown`) separately from the stable
  `codex-local` adapter type;
- known structured subagent sources may provide a bounded subagent kind and an explicit parent
  session reference; raw structured encodings are not persisted or displayed, and malformed or
  unknown structured shapes fail closed for interactive prompt authorship;
- `session_meta` may fill missing working-directory, model/provider, and Codex-version metadata;
- `total_token_usage` is interpreted as a cumulative session snapshot: the final observed snapshot
  remains the session-level total, while content-free timestamped snapshots are retained solely to
  derive successive nonnegative temporal increments; cumulative snapshots are never summed;
- `last_token_usage` is interpreted as a last-turn delta; recognized legacy delta records are
  summed only when the rollout contains no cumulative snapshot, and the result is labelled
  `summed_event_deltas`;
- when one record exposes both cumulative and last-turn values, the cumulative snapshot wins;
  absent token fields remain unknown rather than becoming zero, and `total_tokens` is derived only
  when both input and output are known;
- source-specific message, token, function/custom-tool, shell, patch, failure, and unknown records
  become aggregate event categories;
- selected non-token semantic records also become content-free fingerprint observations for
  origin analysis; all other payload values are discarded after the streaming record is classified.

Some audited state databases also expose an explicit relationship table with parent-thread ID,
child-thread ID, and status fields. The adapter normalizes that relationship by column role rather
than depending on the literal table name. It does not infer parenthood from `model`, agent labels,
or path naming.

The cumulative total is authoritative only for the rollout that reports it. A bounded audit found
that some explicitly related child rollouts replay an exact, multi-event cumulative vector prefix
from their parent. For aggregate accounting, Codex Insights subtracts only an exact parent-final
baseline or an exact multi-vector parent segment. A partial or merely similar total is ambiguous
and is not subtracted. `last_token_usage` is supporting evidence; a mismatch does not override an
exact cumulative prefix because local delta telemetry can be incomplete or reset.

For daily and weekly analytics, the same proven inherited baseline is removed before successive
cumulative vectors are differenced. Each nonnegative increment is assigned to its token record's
UTC timestamp. Missing timestamps, non-monotonic cumulative vectors, timestamp regressions, or a
mismatch with the reconciled final vector produce an explicit temporal fallback; no guessed date or
negative usage is emitted.

The first parse streams the entire referenced rollout because complete per-session totals require
the final cumulative token record. Subsequent runs skip a rollout when file identity, size,
nanosecond mtime, parser version, and recognized source-schema fingerprint are unchanged. A changed
file is conservatively reparsed from byte zero; the recorded byte offset is provenance for a future
append-only strategy, not yet a resume point. No raw-content fingerprint is calculated.

The parser counts bounded unknown record/payload/field/tool shapes without storing their payloads.
Diagnostics distinguish field passthrough and recognized-but-ignored structure from semantic,
tool-result, lifecycle, and unclassified gaps. Each aggregate includes affected-session coverage,
first/last observation, newly-seen status relative to successful index runs, and a bounded capability
impact. A consumed lifecycle or tool signal is not reported as wholly unsupported merely because its
source wrapper remains undocumented.
It verifies file identity/size/mtime across a parse, defers repeated live mutations, and treats an
incomplete final line as a partial append. Previous-good normalized rows are retained on failure.
See `docs/schema-compatibility.md` for capability and recovery semantics.

This version indexes sessions represented in a recognized catalogue. A missing, deleted, or
out-of-home rollout is retained as metadata-only session provenance and reported as skipped; it is
never followed outside the configured Codex home. Catalogue-free orphan rollouts are not guessed
into synthetic session identities in this version.

The cumulative-versus-delta and cross-thread interpretations are based on audited field names and observed event
placement in the current undocumented format. It is an adapter rule, not a Codex API guarantee.
The parser version changes when this interpretation changes so existing rollouts are conservatively
reparsed into the separate analyzer index.

The same bounded audit found that child rollouts can replay non-token records selectively. Exact
ordered replay was observed for user-message, patch-result, and task-lifecycle families, while
tool-call families did not show universal replay. A user message can also be emitted as adjacent
`response_item/message(role=user)` and `event_msg/user_message` wrappers. Event provenance
therefore preserves observations separately from inferred origins and treats unordered or
single-fingerprint overlap as ambiguous unless a stable source identifier corroborates it.

Prompt extraction uses the normalized user-message family only after provenance reconciliation.
It prefers the explicit `event_msg/user_message` member of an adjacent mirrored pair and accepts an
unmirrored `response_item/message(role=user)` when no mirror exists. Developer/system roles are not
user prompts. Structured client-source metadata identifying a subagent or guardian is treated as
non-user-authored; unrecognized structured sources fail closed. These are adapter rules over an
undocumented format and are versioned for controlled re-indexing.
