# Weekly and monthly reports

Reports combine the existing normalized analytics into one privacy-safe local artifact. They do not
reparse rollout files or implement separate token, provenance, prompt, command, Git, outcome, or task
logic.

```bash
codex-insights report weekly
codex-insights report weekly --date 2026-08-09 --timezone Asia/Shanghai
codex-insights report monthly --repo codex-insights --model MODEL
codex-insights report monthly --format json --output ~/reports/codex-month.json
codex-insights report weekly --format html --output ~/reports/codex-week.html
```

`--date` selects any local date inside the desired period. Weeks run Monday through Sunday; months use
calendar-month boundaries. `--repo`, `--model`, `--timezone`, and `--db` use the same normalized
semantics as the underlying analytics commands. Output goes to stdout unless `--output` is explicit.
Reports cannot be written inside the selected Codex home or over the Codex Insights database.

## Formats

- **Markdown** is a compact archive suitable for notes or GitHub. Long group names are bounded and
  tables show the leading groups without changing the underlying totals.
- **JSON** exposes the complete, versioned `codex-insights-report-v1` structure. Repository, model,
  task, and originated-command groups remain complete so reconciliation invariants can be checked.
- **HTML** is a static self-contained file with escaped content, inline CSS, no scripts, no external
  CDN, no tracking, and no remote assets. It includes a small activity chart derived from the same
  day buckets used in JSON.

## Semantics and coverage

Additive totals use reconciled local token contributions. Session median and p90 use observed rollout
totals. Prompts are logical origin-aware prompts; tools and commands are originated events; Git
associations retain confidence; outcomes and task classifications retain UNKNOWN.

Daily and Monday-starting weekly token totals use token-event timestamps in the requested timezone.
Successive cumulative snapshots become nonnegative increments after proven lineage baselines are
removed. Missing or inconsistent event time remains explicit in temporal coverage and is not
assigned to a guessed reporting day.

The data-quality section reports token coverage, event-time attribution and fallback coverage,
child-thread and reconciliation coverage, ambiguous
lineage, originated tool-event coverage, prompt-feature coverage, HIGH-confidence Git attribution,
and UNKNOWN outcome/task counts. No prompt text or raw tool output appears in a report.

Previous-period percentages are emitted only when token-coverage fractions differ by at most ten
percentage points. Otherwise absolute current/previous values remain visible, the report explains the
coverage change, and percentage changes are suppressed. Missing token data is never treated as zero.

Weekly activity is shown by day. Monthly reports add week-level activity while keeping the same metric
definitions. Heavy-tailed usage is summarized with observed median and p90 plus bounded presentation
tables; a high-token session is not labeled inefficient.

For a complete selection, the JSON invariants are:

```text
sum(repository reconciled contribution) = global reconciled contribution
sum(model reconciled contribution)      = global reconciled contribution
sum(task action contribution)           = global reconciled contribution
sum(originated command categories)      = originated commands
```

Explicit outside-Git and unknown groups preserve unattributed sessions instead of dropping them.
