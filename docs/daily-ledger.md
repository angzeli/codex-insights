# Daily ledger V1 setup

`daily-ledger` captures local Codex Stop and SessionEnd activity into a local atomic queue, rebuilds
privacy-filtered daily JSON, and optionally commits and pushes only the allowlisted ledger paths in a
dedicated private Git repository. It does not install a daemon, call the OpenAI API, automate a
browser, or create cloud reports. The cloud ChatGPT task is intentionally outside this V1.

The existing Codex Insights parser remains the source adapter. Lifecycle completion, workload
outcome, validation, Git state, and assistant prose stay separate. Assistant prose never upgrades an
activity to completed. Missing starting HEAD, pushed state, test results, or worktree state remain
`null` or `unknown`.

## One-time setup

Run these commands from the existing `codex-insights` checkout. They create only the dedicated
ledger repository, an isolated Python environment, the daily-ledger config, and sample helpers. They
do not modify `~/.codex/hooks.json` until the explicit `manage_hooks.py install` command.

1. Create the private GitHub repository and clone it:

   ```bash
   gh repo create angze-daily-ledger --private
   git clone git@github.com:angze/angze-daily-ledger.git "/Users/liangze/Documents/angze-daily-ledger"
   ```

2. Install this checkout into an isolated local environment:

   ```bash
   python3 -m venv "$HOME/.local/share/codex-insights/venv"
   "$HOME/.local/share/codex-insights/venv/bin/python" -m pip install -e "/Users/liangze/Desktop/squiddy tools/codex-insights"
   ```

3. Install the sample config and helper files:

   ```bash
   mkdir -p "$HOME/.config/codex-insights" "$HOME/.local/share/codex-insights/daily-ledger"
   install -m 0600 examples/daily-ledger/daily-ledger.toml "$HOME/.config/codex-insights/daily-ledger.toml"
   install -m 0755 examples/daily-ledger/capture_hook.py "$HOME/.local/share/codex-insights/daily-ledger/capture_hook.py"
   install -m 0755 examples/daily-ledger/manage_hooks.py "$HOME/.local/share/codex-insights/daily-ledger/manage_hooks.py"
   ```

4. Confirm `device_id`, checkout, remote, branch, timezone, and `push_enabled` in
   `~/.config/codex-insights/daily-ledger.toml`. Do not put credentials in this file.

5. Run the bounded doctor. Its authentication result checks only local Git identity and remote
   configuration; it deliberately does not make a network request:

   ```bash
   "$HOME/.local/share/codex-insights/venv/bin/codex-insights" daily-ledger doctor
   ```

6. Backfill without pushing, then inspect the generated checkout:

   ```bash
   "$HOME/.local/share/codex-insights/venv/bin/codex-insights" daily-ledger backfill --since 2026-08-27 --until 2026-09-02 --no-push
   git -C "/Users/liangze/Documents/angze-daily-ledger" status --short
   ```

7. Flush and push manually after inspection:

   ```bash
   "$HOME/.local/share/codex-insights/venv/bin/codex-insights" daily-ledger flush
   ```

8. Review the exact sample hook merge, then install it. The utility makes a timestamped backup and
   preserves unrelated existing hook definitions:

   ```bash
   python3 "$HOME/.local/share/codex-insights/daily-ledger/manage_hooks.py" install
   ```

9. Restart or reload Codex, inspect the hook trust prompt, and trust only the two commands pointing
   to `capture_hook.py`. Codex requires review for non-managed hooks. See the
   [official OpenAI Hooks documentation](https://learn.chatgpt.com/docs/hooks).

10. Complete one ordinary Codex turn, then verify capture and flush:

    ```bash
    "$HOME/.local/share/codex-insights/venv/bin/codex-insights" daily-ledger doctor
    "$HOME/.local/share/codex-insights/venv/bin/codex-insights" daily-ledger flush
    ```

## Routine operations

Inspect queue and checkout state:

```bash
"$HOME/.local/share/codex-insights/venv/bin/codex-insights" daily-ledger doctor
```

Rebuild one day without pushing:

```bash
"$HOME/.local/share/codex-insights/venv/bin/codex-insights" daily-ledger export --date 2026-09-01 --no-push
```

Flush pending jobs and push normally:

```bash
"$HOME/.local/share/codex-insights/venv/bin/codex-insights" daily-ledger flush
```

Disable only the daily-ledger hook entries, preserving other hooks and creating a backup:

```bash
python3 "$HOME/.local/share/codex-insights/daily-ledger/manage_hooks.py" disable
```

## Queue and recovery

Local state uses `pending/`, `processing/`, `processed/`, `failed/`, `cache/`, `locks/`, and `logs/`
under `~/.local/state/codex-insights/daily-ledger/`. One JSON file represents one job. Capture writes
to a temporary sibling and atomically renames it; several detached workers converge on one process
lock. A second worker exits successfully when another owns the lock.

`--no-push`, authentication errors, network errors, and rejected pushes retain valid jobs in
`pending/`. Fix the external problem and run `daily-ledger flush` again. Malformed transcripts move
to `failed/` with content-free error metadata instead of disappearing. Inspect counts with doctor;
do not copy failed local jobs into the ledger repository.

If the Mac is offline overnight, Stop jobs remain local and a later flush rebuilds the affected
calendar dates. The cloud report cannot see unsynchronized activity until a later successful push.
Late evidence increments that date's manifest revision and records new or changed session keys.

## Troubleshooting

- **Git authentication failure:** confirm the existing `git` or `gh` login outside the hook, then
  rerun `daily-ledger flush`. Credentials do not belong in the ledger config.
- **No remote:** add the configured remote inside the dedicated checkout only, then rerun doctor.
- **Network unavailable:** leave the queue intact and flush when connectivity returns.
- **Checkout dirty:** doctor distinguishes allowlisted generated changes from non-allowlisted files.
  Move or commit unrelated files yourself; the synchronizer never resets, cleans, or stages them.
- **Malformed transcript:** inspect the content-free `failed/*.error.json`; Codex transcript formats
  are unstable, so update the adapter before retrying an unrecognized format.
- **Session outcome unknown:** this is expected when validation, commit, or workload evidence is
  absent. A confident assistant message is not verification.
- **Queue backlog:** run doctor, then a manual flush. No continuously running daemon is required.
- **Hook not trusted:** reopen Codex's hook trust review and verify the helper path before approval.
- **Multiple existing hook definitions:** use `manage_hooks.py install`; matching hooks may run
  concurrently, and the utility appends only the missing daily-ledger groups.
- **Timezone boundary issues:** keep `timezone = "Asia/Singapore"`; event timestamps determine daily
  slices, while missing timestamps are marked with capture-time fallback coverage.

No `reports/` directory is generated. The read-only ChatGPT GitHub connection can later read this
private repository, while reports remain in the persistent ChatGPT scheduled-task thread.
