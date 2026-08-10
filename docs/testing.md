# Testing and continuous integration

Codex Insights tests never inspect `~/.codex` or any other real Codex home. Every source adapter,
indexing, analytics, dashboard, export, backup, reset, and migration test uses committed small
synthetic fixtures or a deterministic corpus generated in a temporary directory. When a real local
format quirk is discovered, only its structural shape may be reproduced in a synthetic fixture.

## Local validation

Use the project development environment:

```bash
./scripts/setup-dev.sh
venv-acceptance/bin/python -m pytest
venv-acceptance/bin/python -m ruff check .
venv-acceptance/bin/python -m mypy src
```

The end-to-end integration test generates hundreds of synthetic sessions and exercises `doctor`,
`doctor --deep`, `audit-source`, fresh and unchanged indexing, history/usage/prompt/tool/Git/outcome/
task analytics, reports, dashboard, privacy inspection, export, backup, reset, and rebuild. It also
checks additive reconciliation and hashes the disposable Codex source tree before and after the
workflow to prove that normal operations did not mutate it.

## Synthetic corpus

The shared generator is `scripts/synthetic_corpus.py`. It creates a disposable Codex home and
sibling synthetic Git repositories. It includes root/child/nested threads, token inheritance,
ambiguous lineage, prompt replay, partial coverage, multiple repositories/models, multilingual and
redaction-shaped prompts, originated tools/commands, Git confidence tiers, outcomes/tasks,
archives, unknown events, malformed/partial files, missing rollouts, and competing state DB shapes.
No output is derived from a user history.

```bash
python scripts/synthetic_corpus.py /tmp/codex-insights-corpus --sessions 240
```

Large corpora are generated during tests or benchmarks rather than committed as giant JSONL/SQLite
artifacts. The seed and requested proportions make regeneration deterministic.

## CI matrix

GitHub Actions runs the full test suite on Python 3.11, 3.12, 3.13, and 3.14. Separate jobs run Ruff
and mypy, build wheel and sdist artifacts, install each artifact in a clean environment, and smoke
test the CLI. A macOS job validates both normal and editable installation, fresh-process imports,
the entry point, and the narrow hidden-`.pth` guard that protects against the previously observed
editable-install failure. The guard does not monkeypatch Python site behavior.

CI sets `CODEX_HOME` to an empty disposable directory. It does not upload, cache, or discover real
Codex source state. The 10,000-session benchmark is not part of every test matrix cell; see
[benchmarks.md](benchmarks.md) for the manual profile.

## Developer-only real smoke

The optional real-history smoke script is deliberately separate from CI. It uses the same bounded,
read-only source adapter and emits aggregate diagnostics only. It must never be used to generate
fixtures or committed outputs. See the benchmark/smoke instructions in
[benchmarks.md](benchmarks.md).
