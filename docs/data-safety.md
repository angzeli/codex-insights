# Data safety policy

Codex session history can contain proprietary code, unpublished research, credentials accidentally
printed by tools, personal paths, prompts, command output, and other sensitive environment data.
Safety is therefore an architectural invariant, not an optional operating mode.

## Non-negotiable rules

- Treat the selected Codex home as strictly read-only.
- Never write, rename, move, delete, vacuum, migrate, chmod, or modify anything under it.
- Never modify Codex-owned SQLite databases or their schema.
- Open source SQLite databases with explicit read-only access whenever technically possible and set
  the connection to query-only mode.
- Keep the Codex Insights database outside the selected Codex home and reject unsafe index paths.
- Never recursively dump a Codex home or all rollout files into an agent context.
- Never run unrestricted recursive `cat`, `rg`, `grep`, or equivalents over rollout history.
- Use bounded discovery and parse only the minimum records needed for a declared analytic purpose.
- Do not retain tool stdout/stderr, raw command output, assistant transcript, or environment content.
  Searchable user prompts and bounded command text are allowed only through the separately reviewed
  origin, redaction, size, retention, purge, and export policy in `docs/privacy.md`.
- Tests must use committed synthetic fixtures or test-created temporary files, never real user
  history.

## Data minimization

The preferred index stores normalized metadata and aggregates: timestamps, stable session identity,
project or repository association, model name, token counts, coarse tool names and counts, and
explainable classifications. Redacted user prompts and bounded redacted command text are the two
reviewed content exceptions, and both can be disabled for future indexing and purged separately.
Raw source records should not be copied. New fields require a purpose, a retention decision, and a
sensitivity review.

## Safe discovery

Discovery is shallow and bounded. The initial `doctor` command checks only runtime metadata and the
existence of a fixed list of likely paths. It does not enumerate or parse session histories. Future
discovery should impose explicit file, byte, and record limits and report when a limit prevents a
complete result.

## Test isolation

Tests set an explicit synthetic Codex home. Test helpers must fail if they would fall back to a real
home directory. Fixtures must contain invented identifiers, paths, and content, and must not be
generated from real rollout histories.

## Derived database separation

The Codex Insights index is writable application state in the documented platform application-data
directory, never inside Codex home. Path checks resolve symlinks, reject descendants, and compare
existing targets against known source-file inodes so a hard-link alias cannot bypass separation.

## Future review gates

Before implementing ingestion, validate source format recognition, read-only failure behavior,
bounded parsing, redaction/minimization, fixture realism without user data, and index provenance.
Exports select one normalized dataset explicitly, obey current content-retention policy, and use
the stable schemas and destination safeguards defined in `docs/privacy.md`. A future dashboard must
undergo the same field-by-field review.
