# Cross-thread event provenance

Codex Insights distinguishes a record that is **observed in a rollout** from an event that
**originated in a thread**. Physical presence is not sufficient attribution evidence: current
Codex child rollouts can replay selected parent records, and a single user message can also appear
as two adjacent source wrapper records in one rollout.

This rule applies to prompts, messages, tool calls, commands, patches, validation, Git evidence,
task completion, and future outcome signals. Token lineage uses a separate vector-aware algorithm,
but follows the same observed-versus-originated principle.

## Selected semantic events

The adapter extracts compact observations only for user and assistant messages, inter-agent
messages, tool calls and outputs, directly identifiable shell/validation/Git commands, patch calls
and results, task lifecycle records, and explicit error records. Token updates, hidden reasoning,
world state, turn context, session instructions, and unrelated structural records are excluded.

Each selected observation stores:

- observed session and source ordinal;
- normalized event family and family ordinal;
- source record/payload type;
- SHA-256 fingerprint and optional digest of source wrapper identifiers;
- event timestamp and approximate content length when available;
- origin session/event only when supported by lineage evidence;
- provenance status, confidence, evidence type, and algorithm versions.

It does not store message text, command arguments, patches, tool output, stdout/stderr, hidden
reasoning, or a raw rollout record. Canonicalization may inspect those values transiently and
stores only a digest. User prompt text is governed separately by the prompt-search privacy policy.

## Fingerprints and evidence

`event-fingerprint-v1` is category-aware. It removes timestamps and wrapper identifiers from the
semantic digest while retaining the fields that distinguish the event family. Message wrappers
normalize to their role, text, and attachment digests. Tool calls include tool name plus a digest
of normalized arguments. Tool outputs, patch results, and task messages contribute digests rather
than raw content.

A matching digest is not proof by itself. `event-provenance-v1` combines a fingerprint with an
explicit `thread_spawn_edges` relationship and one of these evidence shapes:

- an exact ordered multi-event child prefix matching a contiguous parent segment;
- an exact ordered multi-event prefix within one event family, for selective replay;
- one exact fingerprint whose stable source identifier digest has one unique parent match.

Single or unordered content overlap without stable source identity is `ambiguous`. It is not
deduplicated. Events absent from the explicit parent are `origin`; missing parent data is `unknown`.
Cycles remain explicit and do not recurse.

Adjacent `response_item/message(role=user)` and `event_msg/user_message` records with the same
fingerprint are retained as two observations, but the first is marked `observed_duplicate` and
points to the explicit user-message observation. This preserves source auditability without
turning one logical message into two originated events.

## Nested and sibling threads

For nested lineages, an inherited child observation resolves through the immediate parent to the
earliest confidently known origin event. If the parent's origin is ambiguous, the descendant is
also ambiguous. Each sibling is reconciled independently, so genuine sibling-specific activity is
not erased merely because both replay the same parent prefix.

## Persisted scope and future use

The compact `event_observations` and `event_replay_summary` tables are the shared provenance
substrate for later prompt search, command analytics, Git evidence, and outcomes. Future features
must query origin mappings rather than reimplementing replay detection. A normalization change
requires a new fingerprint or provenance algorithm version and a controlled re-index.

`codex-insights provenance` reports aggregate observations, originated/inherited/ambiguous counts,
and affected child threads. `--session`, `--family`, and `--json` remain content-free.

## Empirical status

A bounded local audit on 2026-08-09 confirmed meaningful but selective non-token replay across the
29 then-observed explicit spawn edges. User messages and patch-result/lifecycle records showed the
strongest exact ordered replay evidence. Ordinary tool-call and tool-output families did not show
the same broad pattern. These are installation-specific observations, not a Codex storage contract;
the diagnostic should be rerun after source-format changes.
