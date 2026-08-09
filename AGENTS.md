# Codex Insights working agreement

## Scope

Make the smallest coherent change that satisfies the task. Prefer the loop: locate, understand,
edit, run targeted validation, inspect the diff, and stop. Do not add adjacent features, frameworks,
dependencies, refactors, or generated artifacts without a demonstrated need.

## Absolute Codex data-safety constraints

- Treat every configured Codex home, including but not limited to `~/.codex`, as strictly read-only.
- Never write, rename, move, delete, vacuum, migrate, chmod, or otherwise modify anything under a
  Codex home.
- Never modify Codex-owned SQLite databases. Open them with explicit read-only access whenever
  technically possible and enable query-only mode.
- Store the Codex Insights database separately from every Codex home. Reject overlapping paths.
- Never recursively dump a Codex home or rollout collection into agent context.
- Never run unrestricted `cat`, `rg`, `grep -R`, or equivalents across rollout histories.
- Use bounded discovery and bounded reads. Inspect only the minimum source material required.
- Do not store raw tool stdout/stderr, command output, prompts, or sensitive environment content
  unless an explicit, separately reviewed feature requires it.
- Tests must never access a user's real Codex history. Use only committed synthetic fixtures and
  test-created temporary files with explicit Codex-home configuration.
- Treat Codex's local storage format as undocumented and unstable. Keep its assumptions behind the
  source-adapter boundary and fail closed on formats that are not recognized.

## Architecture

Use Python 3.11 or newer with the `src/` layout. Keep source adapters, normalized models, analyzer
database access, analytics, and presentation concerns separate. Prefer stdlib `sqlite3`, dataclasses,
Typer, and Rich. Do not introduce SQLAlchemy, a web framework, or a heavy abstraction without a
concrete requirement.

## Validation policy

- Text-only changes: inspect the changed text and focused diff.
- Small isolated code changes: run the directly relevant tests and lint/type check for touched code.
- Multi-module or high-risk changes: run affected tests plus one representative integration check.
- Do not run the full test suite repeatedly. One check per distinct validation purpose is enough.
- CLI work should smoke-test only the affected commands.
- Never claim a check ran when it did not. Separate environment failures and pre-existing failures
  from regressions.

## Git safety

Preserve unrelated work. Stage only intended paths and inspect the staged diff before each commit.
Do not push, rewrite history, alter remotes, or create a pull request unless explicitly requested.
