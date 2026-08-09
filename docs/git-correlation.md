# Git correlation

Codex Insights correlates sessions with Git history conservatively. Repository inspection is
read-only: it uses metadata queries such as `git log`, `git cat-file`, and `git symbolic-ref`; it
never checks out, resets, commits, tags, merges, rebases, cleans, or pushes a user repository.

## Repository identity

Repository basenames are display labels, not identities. The stable identity order is:

1. a normalized, credential-free remote host and path;
2. the shared Git directory, so linked worktrees can resolve to one repository;
3. the exact resolved repository path when no stronger evidence exists.

This keeps same-named unrelated repositories separate, connects moved checkouts when their remote
is stable, supports repositories without remotes, and preserves historical sessions whose checkout
has since disappeared. Non-Git sessions retain no repository identity.

## Source Git metadata

`threads.git_sha`, `threads.git_branch`, and `threads.git_origin_url` are treated as repository
context captured around the session. In particular, `git_sha` is not evidence that Codex created
that commit. It is used only to exclude an already-present contextual commit from timing candidates.

## Confidence tiers

- **HIGH**: an originated, successful `git commit` flow exposes an exact result hash, or an
  abbreviated result hash that resolves uniquely in the same repository.
- **MEDIUM**: exactly one compatible commit follows an originated successful commit action, the
  current branch matches the session branch, and no competing session overlaps the commit.
- **LOW**: timing is compatible with an originated commit action, but branch, result, concurrency,
  or multiplicity evidence is incomplete.

MEDIUM and LOW rows are candidates, never definitive attribution. Time proximity alone cannot
produce HIGH confidence. The bounded `evidence_type` and explanation stored with every association
make the rule inspectable.

## Provenance and concurrency

Only tool activity marked as originating in the current thread can support its association.
Inherited parent `git commit` or inspection events cannot independently create a child association.
A child that performs its own commit action remains eligible. Concurrent sessions, multiple nearby
commits, branch mismatches, detached HEAD, missing repositories, and deleted rollout data reduce
coverage or confidence rather than being guessed away.

`reconciled tokens per confirmed commit` is a descriptive ratio over HIGH-confidence commits. It is
not a productivity score and local telemetry is not server-side billing data.
