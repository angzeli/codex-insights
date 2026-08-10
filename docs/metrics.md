# Analytics metric definitions

Codex Insights reports derived local telemetry. Missing values remain unknown; they are never
silently converted to zero. Local telemetry is not guaranteed to reproduce OpenAI server-side
billing, quota, or private accounting algorithms.

## Core units

- **Session:** one normalized Codex source thread/rollout in `source_sessions`. Explicit spawn edges
  connect related sessions without merging them.
- **Observed rollout tokens:** the final trustworthy cumulative token vector reported by one rollout.
  This remains the session-level value shown by `session` and used for median/p90 distributions.
- **Reconciled local tokens:** the additive contribution used across sessions. Exact inherited or
  replayed ancestor baselines are subtracted from a child; independent children retain their full
  contribution; ambiguous children retain observed usage and remain flagged.
- **Token coverage:** sessions with a known token field divided by selected sessions. Coverage is
  field-specific.
- **Logical prompt:** one redacted, bounded, confidently originated user prompt. Descendant replay
  observations do not create additional logical prompts.
- **Originated tool call:** a normalized tool operation attributed to the session that originated it.
  Exact inherited/replayed observations are excluded from additive activity counts.
- **Originated command:** an originated tool call with privacy-filtered command metadata. It excludes
  raw stdout/stderr and inherited observations.
- **Repeated command:** the same privacy-filtered command fingerprint invoked more than once in the
  selected scope. It is descriptive and does not by itself imply rework.
- **Confirmed commit:** a HIGH-confidence session/commit association based on exact, originated Git
  result evidence. MEDIUM and LOW candidates are always reported separately.
- **Outcome:** a deterministic classification from originated validation, edit, error, commit, and
  lifecycle evidence. `unknown` is retained when evidence is insufficient.
- **Task action:** the primary requested operation, such as implementation, review, or diagnosis.
- **Task domain:** the subject area, such as scientific computing or software engineering. Facets
  preserve narrower topics without creating giant combined labels.
- **Provenance ambiguity:** evidence that overlaps an ancestor but cannot support exact origin
  attribution. Ambiguous activity is not silently deducted.

## Additive and distribution semantics

The following are additive within a fixed selection:

- reconciled local token contributions;
- originated command/tool counts;
- logical prompt counts;
- unique HIGH-confidence commit counts where repository identity is complete.

Observed rollout totals are not additive across related threads. They are appropriate for individual
session inspection and per-session distributions. Reports therefore label median, mean, and p90 as
observed while labeling account-wide totals as reconciled.

Repository, model, task, and time attribution uses the contributing session's normalized metadata.
When a child has an exact inherited baseline, only its child-exclusive contribution is attributed to
the child's repository, model, and start-time bucket. A parent outside a reporting window does not add
historical ancestor usage to a child inside the window.

Git report periods use commit timestamps; session, usage, outcome, and task periods use session start
times; tool periods use normalized activity time with session start as a fallback. Reports disclose
these distinct evidence clocks rather than treating missing timestamps as zero activity.

## Coverage and ratios

Source capability coverage is separate from metric value coverage. Each session records
`available`, `degraded`, `not_observed`, or `unknown` for parser capabilities such as tokens,
prompts, tools, provenance, repository/model attribution, lifecycle, archive state, and duration.
`not_observed` and `unknown` never mean a zero event count. `doctor --deep` compares the latest
successful snapshot with the preceding successful run and labels material regressions explicitly.
These warnings do not alter token, provenance, outcome, or taxonomy algorithms.

Tokens per confirmed commit is a descriptive ratio of reconciled contributions for sessions with
HIGH-confidence commit associations. It is not a productivity score. Failed-command rates use only
tool results with known success/failure status. UNKNOWN outcomes and tasks remain in denominators and
are displayed explicitly.

Prompt-pattern outcome comparisons require at least five sessions both with and without the feature.
They retain UNKNOWN outcomes, expose sample sizes, and make no causal or prompt-quality claim.
