# Deterministic benchmarks

Codex Insights benchmarks use generated synthetic Codex state only. They never inspect, copy,
upload, or cache a real Codex home. Timing results are engineering diagnostics, not claims about a
user's productivity or about OpenAI server-side accounting.

## Profiles

The benchmark uses the same realistic generator as end-to-end tests. Useful local profiles are:

```bash
# Quick development probe
python scripts/benchmark.py --sessions 250 --output /tmp/codex-benchmark-250.json

# Representative local run
python scripts/benchmark.py --sessions 1000 --output /tmp/codex-benchmark-1000.json

# Large structural profile
python scripts/benchmark.py --sessions 10000 --output /tmp/codex-benchmark-10000.json
```

The 10,000-session profile includes multiple repositories/models, root and child threads, nested
and ambiguous lineage, replayed prompts, token inheritance, partial token coverage, originated
tools/commands, Git evidence, task/outcome classifications, archives, unknown records, and
malformed/partial inputs. It is large enough to expose quadratic lineage work, pathological joins,
unnecessary FTS rebuilding, and unbounded dashboard serialization.

## Measurements

`scripts/benchmark.py` records corpus parameters, Python and operating-system family, application
and DB schema versions, fresh index time, unchanged re-index time, one-session changed update,
common query latency, report/dashboard generation, peak process memory where supported, derived DB
size, and report/dashboard sizes. It emits a human summary plus stable
`codex-insights-benchmark-v1` JSON.

Non-binding engineering expectations are structural rather than hardware-specific:

- unchanged re-indexing should be dramatically faster than fresh indexing;
- one changed session should not trigger a full rollout reparse;
- common normalized analytics should remain interactive;
- report and dashboard generation should remain practical;
- database, HTML, and memory growth should remain approximately linear.

Normal pull-request CI does not enforce narrow timing thresholds and does not run the 10k profile on
every Python version. A separate manual GitHub Actions workflow can run it against synthetic data
and retain only the result JSON for 14 days.

## Developer-only real smoke

The optional real-history smoke is explicitly separate from tests and CI:

```bash
python scripts/real_local_smoke.py --confirm-read-only-source
```

Optional `--codex-home`, `--db`, and `--config` paths are supported. The script uses the bounded
read-only adapter for probe/audit/deep diagnostics, writes only the separate derived Insights DB,
runs an immediate re-index, and prints aggregate JSON. It does not print prompt bodies, command
text, tool output, hidden reasoning, or rollout records. Never use its output as a fixture.
