# Session outcomes

Outcome classification is deterministic, versioned, and conservative. It operates only on
normalized evidence that originated in the session being classified. Replayed ancestor tests,
failures, commits, lifecycle markers, or aborts do not independently classify a child thread.

## Labels

- `success`: originated validation passed or a HIGH-confidence commit exists, with no later
  unrecovered failure.
- `success_with_warnings`: originated validation failed and a later originated validation passed.
- `partial`: originated edit activity exists without defensible completion or validation evidence.
- `failed`: a final originated validation failure or critical normalized error was not recovered.
- `abandoned`: an originated abort has no later completion evidence.
- `no_change`: reserved for future task-aware evidence that a completed task intentionally required
  no change. The current classifier does not infer this merely from absence of a commit.
- `unknown`: evidence is missing, inherited, ambiguous, or insufficient.

Every row includes `high`, `medium`, or `low` confidence; bounded evidence labels; and a classifier
version. `UNKNOWN` is always included in aggregates and is never converted to failure or zero.

## Evidence and recovery

The current classifier uses originated test/lint/type-check results, patch/edit activity, trusted
source lifecycle markers, normalized error markers, and HIGH-confidence Git associations. It does
not read assistant message text and cannot classify success solely from words such as “done.”

Ordered evidence distinguishes `failure → later pass` from `final failure → session end`. Failures
from unrelated exploratory tools are not treated as validation failures. Task taxonomy can later
provide context through the same evidence boundary without moving classification rules into SQL or
CLI rendering.

These labels describe local observable evidence. They are not guarantees of task correctness,
productivity scores, or reproductions of private OpenAI accounting or evaluation systems.
