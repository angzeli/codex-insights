# Offline dashboard

`codex-insights dashboard` generates one self-contained HTML analytics workbench from the
normalized Codex Insights database. It does not start a server, reopen rollout history, use a CDN,
or make network requests. The default output is `codex-insights-dashboard.html` in the current
directory; a browser opens only when `--open` is supplied.

```bash
codex-insights dashboard --since 30d --output codex-last-30-days.html
codex-insights dashboard --repo example-project --model example-model
codex-insights dashboard --task implementation --domain software_engineering
codex-insights dashboard --timezone Asia/Shanghai --open
```

Filters are applied while the dashboard is generated. This keeps the document static and avoids
embedding a database or an unbounded session/event warehouse in client-side JavaScript. `--since`
is inclusive; timestamp `--until` values are exclusive, while a date-only `--until` includes the
complete local calendar day. Repository, model, task action, and task domain filters use normalized
indexed values.

## Sections and semantics

The dashboard contains switchable Daily/Weekly/Overall overview cards, daily activity, repository and model activity, originated
tool/command metadata, task taxonomy, outcomes, Git provenance, interesting sessions, and data
quality/methodology sections.

- Additive token totals are reconciled local contributions.
- Daily token bars use nonnegative token-event increments; session bars use logical-session starts.
- Activity can be ordered by date, sessions, or tokens. Both panels share the exact same row order,
  and fixed label/value columns prevent long formatted token values from shrinking the bar track.
- Median and p90 values are observed per-rollout cumulative totals.
- Prompt counts are logical and origin-aware.
- Tool and command counts include originated observations only.
- Git associations keep HIGH confirmed evidence separate from MEDIUM/LOW candidates.
- Outcome and task classifiers retain UNKNOWN and confidence coverage.
- Missing values remain `unknown`; they are not rendered as zero.

The dashboard uses the same analytics functions as `usage`, `tasks`, `tools`, `commits`, `outcomes`,
and periodic reports. Repository/model token breakdowns therefore reconcile with the selected
headline total where attribution is complete. Data quality reports event-time attribution coverage,
fallback sessions, and temporally unattributed tokens.

## Privacy and security

The generated document contains normalized aggregates and a small bounded set of content-free
session descriptors. It excludes prompt bodies, command text, tool stdout/stderr, patches,
environment dumps, hidden reasoning, and raw rollout records. When prompt or command-text retention
is disabled, generation continues and reports the setting in data quality.

All indexed labels are HTML-escaped. The renderer has regression coverage for markup, event-handler
attributes, quotes, entities, Unicode, and Traditional Chinese. It contains no external fonts,
remote JavaScript, analytics, tracking, or remote assets and works directly under `file://`.

Dashboard destinations are resolved before writing. Targets inside the configured Codex home,
targets aliasing source files, and the active Insights database itself are rejected. Existing output
is preserved unless `--overwrite` is explicit; missing parent directories require
`--create-parents`.

## Performance model

Dashboard generation queries only the normalized database and pre-aggregates by day, repository,
model, task, and category. It does not reread source rollouts. A deterministic 10,000-session test
guards against accidentally serializing every session into HTML; output remains bounded to the
aggregated views and a small interesting-session list.
