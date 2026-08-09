# Explainable task taxonomy

Codex Insights classifies indexed sessions with deterministic, versioned rules. Classification is
descriptive: it does not use an LLM, call an external service, or assign a prompt-quality score.

## Dimensions

Each session has one primary **action** and one **domain**, plus zero or more facets. Actions describe
the requested work, such as `implementation`, `bug_fix`, `code_review`, `documentation`, or
`git_or_release`. Domains describe the subject area, such as `software_engineering`,
`scientific_computing`, `developer_tooling`, or `data_analysis`. Facets retain useful narrower signals
such as ORCA, CP2K, XPS, or workflow without multiplying action/domain combinations.

## Evidence and lineage

Evidence precedence is:

1. logical prompts authored in the current origin thread;
2. normalized repository metadata;
3. originated command/tool categories;
4. HIGH-confidence Git associations.

Prompt intent outweighs incidental activity. A child thread's replayed ancestor prompts are not
classified as child intent. For example, a child that inherits “implement the workflow” but originates
“review this implementation” is classified as `code_review`. If no defensible origin intent or
originated fallback evidence exists, the action and domain remain `unknown`.

Explicit line-level non-goals such as “Do not implement a dashboard” are removed before positive
intent rules run. This is intentionally conservative and does not claim to solve general-language
negation.

## Explainability and confidence

`session_tasks` stores the selected action, domain, facets, confidence, matched rule identifiers, and
taxonomy version. A first origin prompt match is HIGH confidence; activity-only fallback is MEDIUM;
insufficient evidence is LOW. The matched identifiers explain which rules participated, including
rules that lost a deterministic score or priority tie.

Rules count up to three matches per prompt so a detailed implementation request is not reclassified
as release work merely because it also asks for a final commit. Earlier prompts have slightly higher
weight than follow-up prompts, while every originated prompt remains eligible evidence.

## Metric semantics

`codex-insights tasks` uses reconciled contributions for additive token totals and observed
per-rollout totals for median and p90 session distributions. Command counts include originated
commands only, Git counts include HIGH-confidence associations, prompt counts are logical
origin-aware prompts, and outcome groups use the provenance-aware outcome classifier. Missing data
and `unknown` classifications are retained rather than converted to zero or dropped.

The rules are a maintainable approximation of user intent, not an objective ground truth. Aggregate
comparisons must show sample size, retain `unknown`, and avoid causal claims about prompt wording or
task success.

## Prompt features

`prompt_features` contains versioned descriptors computed from the redacted, bounded logical prompt:
original and stored character counts, stored line count, structured-heading count, acceptance-criteria
presence, validation request, path-reference count, commit and multiple-commit requests, explicit
non-goals, read-only constraints, and an approximate requirement count. It also records whether the
safe source text was truncated. No raw source text is copied into this table.

The requirement heuristic counts explicit list, numbered, and requirement-like lines. A non-empty
unstructured prompt has an approximate count of one. This deliberately simple heuristic is labeled
`approx-requirements-v1`; it is neither exact natural-language parsing nor a quality score.

Outcome comparisons for prompt features are descriptive only. Both the feature-present and
feature-absent populations must contain at least five sessions before outcome distributions are
shown. Sample sizes and `unknown` outcomes remain visible; insufficient samples emit no outcome
composition. These comparisons cannot establish that a prompt feature caused an outcome.
