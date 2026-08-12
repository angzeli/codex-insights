# Analytics metric definitions

Codex Insights reports derived local telemetry. Missing values remain unknown; they are never
silently converted to zero. Local telemetry is not guaranteed to reproduce OpenAI server-side
billing, quota, or private accounting algorithms.

## Core units

- **Session:** one normalized Codex source thread/rollout in `source_sessions`. Explicit spawn edges
  connect related sessions without merging them.
- **Session-start active days:** distinct UTC calendar dates represented by session start timestamps
  in the indexed population. This is the human-facing `stats` metric; its stable JSON field remains
  `active_days`.
- **Token-active days:** distinct local calendar buckets containing temporally attributed
  reconciled token activity. This is a useful precise concept, but it is not currently exposed as a
  standalone headline.
- **Session/token activity days:** distinct local calendar buckets, in the selected
  report/dashboard timezone, containing at least one selected session start or temporally
  attributed reconciled token contribution. Reports and dashboards use this label; their stable
  JSON field remains `active_days`.
- **Observed rollout tokens:** the final trustworthy cumulative token vector reported by one rollout.
  This remains the session-level value shown by `session` and used for median/p90 distributions.
- **Reconciled local tokens:** the additive contribution used across sessions. Exact inherited or
  replayed ancestor baselines are subtracted from a child; independent children retain their full
  contribution; ambiguous children retain observed usage and remain flagged.
- **Token coverage:** sessions with a known token field divided by selected sessions. Coverage is
  field-specific. For additive totals it is the number of selected session contributions with a
  usable reconciled value, which can include an ambiguous child's retained observed contribution.
- **Inherited/replayed usage:** an ancestor token baseline that exact vector/order evidence shows is
  physically represented again in a descendant rollout. It is removed once from additive totals;
  partial or merely similar vectors are not treated as inherited.
- **Event-time token contribution:** a nonnegative difference between successive trustworthy
  cumulative snapshots after any proven inherited baseline is removed, assigned to the snapshot's
  UTC timestamp. Delta-only source variants use each normalized delta's timestamp.
- **Temporally unattributed usage:** reconciled usage whose event time cannot be defended because
  token events are absent, timestamps are missing/regress, or cumulative values are non-monotonic.
  It remains in all-time totals but is not guessed into a bounded day/week window.
- **Logical prompt:** one redacted, bounded, confidently originated user prompt. Descendant replay
  observations do not create additional logical prompts.
- **Observed event:** one normalized semantic event physically present in a rollout. Physical
  presence alone does not establish that the observing thread originated it.
- **Originated event:** an observed event attributable to its current thread through stable source
  identity, non-overlapping ordered evidence, or another versioned provenance rule. Exact ancestor
  replay and mirrored wrapper observations are excluded from additive originated counts.
- **Originated tool call:** a normalized tool operation attributed to the session that originated it.
  Exact inherited/replayed observations are excluded from additive activity counts.
- **Originated command:** an originated tool call with privacy-filtered command metadata. It excludes
  raw stdout/stderr and inherited observations.
- **Command executable:** the first defensibly executable command head. Leading assignments and
  recognized wrappers are resolved, and a setup-only `cd` may be skipped before `&&`/`;`. For a
  pipeline or chain, the field describes the first workload command; shell control flow, grouping,
  option-like heads, and unsupported wrapper options remain null instead of being guessed. `uv run`
  retains public executable `uv`, while a safely resolved workload drives its command category.
- **Repeated command:** the same privacy-filtered command fingerprint invoked more than once in the
  selected scope. The fingerprint covers the privacy-filtered normalized command, not merely its
  executable, so distinct commands using the same executable are not collapsed. It is descriptive
  and does not by itself imply rework.
- **Confirmed commit:** a HIGH-confidence session/commit association based on exact, originated Git
  result evidence. MEDIUM and LOW candidates are always reported separately.
- **Candidate commit:** a MEDIUM- or LOW-confidence repository-local association supported by weaker
  command/lifecycle or timing evidence. It is never counted as a confirmed commit.
- **Outcome:** a deterministic classification from originated validation, edit, error, and commit
  evidence. Source lifecycle is reported separately and does not make completion synonymous with
  success. `unknown` is retained when task evidence is insufficient.
- **Task action:** the primary requested operation, such as implementation, review, or diagnosis.
- **Task domain:** the subject area, such as scientific computing or software engineering. Facets
  preserve narrower topics without creating giant combined labels.
- **Repository:** a normalized identity derived from an explicitly resolved Git root and stable
  repository evidence. Arbitrary working-directory substrings do not create repository identity;
  sessions without one remain in an explicit outside-Git bucket.
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

Repository and model attribution uses the contributing session's normalized metadata. When a child
has an exact inherited baseline, only its child-exclusive event-time increments are attributed to
the child's repository and model. A parent outside a reporting window does not add historical
ancestor usage to a child inside the window. Ambiguous lineage retains observed usage but does not
subtract a guessed baseline.

Git report periods use commit timestamps; token usage periods use token-event timestamps; logical
session counts, outcome, and task periods use session start times; tool periods use normalized
activity time with session start as a fallback. Reports disclose
these distinct evidence clocks rather than treating missing timestamps as zero activity.

Session-start active days, token-active days, and session/token activity days can legitimately
differ. A session may resume and emit token activity on a later date, and temporally unattributed
usage contributes to neither a token-event day bucket nor a guessed date. `stats` currently uses
UTC session-start dates, while report and dashboard session/token activity days use the explicitly
selected rendering timezone.

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

## Confidence and uncertainty

Confidence labels are subsystem-specific evidence tiers, not a single cross-product probability:

- **Token lineage:** `high` requires exact cumulative-vector/prefix evidence; `explicit` identifies
  an independent child from source evidence; `none` accompanies unavailable, cyclic, or ambiguous
  accounting. Ambiguous/cyclic contributions retain observed usage instead of subtracting a guess.
- **Event and prompt provenance:** `high` requires exact stable identity or ordered replay evidence;
  `none` preserves ambiguous, unknown, or otherwise unsupported origin without guessing.
- **Git:** HIGH requires an exact originated commit-result hash in the same normalized repository;
  MEDIUM requires one compatible candidate descended from the session-captured starting SHA with
  no competing session; LOW is a bounded conservative timing candidate. Present checkout branch
  state never upgrades historical evidence. Ambiguous and omitted candidates remain explicit and
  are never promoted by coverage goals.
- **Outcomes:** HIGH/MEDIUM/LOW reflects the strength and consistency of originated validation,
  edit, error, and commit signals. `turn_completed`/`aborted` lifecycle status is separate; missing
  or inherited-only task evidence remains `unknown`.
- **Tasks:** HIGH/MEDIUM/LOW reflects deterministic rule coverage over origin-aware user intent and
  facets. A descendant does not inherit its parent's task solely because that prompt was replayed.

**Ambiguous** means available evidence has more than one defensible attribution or only partial
ancestor overlap. **UNKNOWN** means the source capability or evidence is insufficient for a value.
Neither is converted to zero, failure, success, or a guessed class. Coverage reports include these
states explicitly so improved-looking percentages cannot be obtained by loosening evidence rules.
