# Session outcomes

Outcome classification is deterministic, versioned, and conservative. It operates only on
normalized evidence that originated in the session being classified. Replayed ancestor tests,
failures, commits, lifecycle markers, or aborts do not independently classify a child thread.

## Labels

- `success`: originated validation passed or a HIGH-confidence commit exists, with no later
  unrecovered failure. A turn-completion lifecycle marker alone is insufficient.
- `success_with_warnings`: originated validation failed and a later originated validation passed.
- `partial`: originated edit activity exists without defensible completion or validation evidence.
- `failed`: a final originated validation failure or critical normalized error was not recovered.
- `abandoned`: an originated abort has no later completion evidence.
- `no_change`: reserved for future task-aware evidence that a completed task intentionally required
  no change. The current classifier does not infer this merely from absence of a commit.
- `unknown`: evidence is missing, inherited, ambiguous, or insufficient.

Every row includes `high`, `medium`, or `low` confidence; bounded evidence labels; and a classifier
version. `UNKNOWN` is always included in aggregates and is never converted to failure or zero.

## Lifecycle and aggregate coverage

Task outcome and source lifecycle are separate concepts. `lifecycle_status` records
`turn_completed`, `aborted`, or `unknown` from originated lifecycle evidence. It does not by itself
make the task outcome non-UNKNOWN. An originated edit followed only by turn completion is reported
as LOW-confidence `partial`, while completion without task evidence remains `unknown`.

`strongly_evidenced` means a non-UNKNOWN outcome with HIGH or MEDIUM confidence. User-facing reports
show the strongly evidenced count separately from the broader non-UNKNOWN count; the latter remains
available for compatibility and diagnosis but is not presented as equally trustworthy coverage.

## Evidence and recovery

The current classifier uses originated test/lint/type-check results, patch/edit activity,
normalized error markers, and HIGH-confidence Git associations. It records trusted source lifecycle
markers separately. It does not read assistant message text and cannot classify success solely from
words such as “done.”

Ordered evidence distinguishes `failure → later pass` from `final failure → session end`. Failures
from unrelated exploratory tools are not treated as validation failures. Task taxonomy can later
provide context through the same evidence boundary without moving classification rules into SQL or
CLI rendering.

These labels describe local observable evidence. They are not guarantees of task correctness,
productivity scores, or reproductions of private OpenAI accounting or evaluation systems.
