# Prompt search privacy policy

Prompt search is local and intentionally narrow. Codex Insights persists only redacted text from
user-role records that both:

1. originate in the current thread according to the shared event-provenance layer; and
2. belong to a source identified as an interactive client rather than a subagent, guardian, or
   unrecognized structured source.

System/developer instructions, assistant messages and reasoning, inter-agent instructions, tool
arguments and output, shell stdout/stderr, patches, environment dumps, and raw rollout records are
not added to the prompt index. A thread can legitimately have no searchable prompt.

## Lineage and identity

The default unit is one logical prompt at its known origin, not every physical descendant copy.
Adjacent source wrappers for the same user message are one logical prompt. Exact replay into child,
sibling, or nested descendant rollouts becomes `prompt_observations` metadata. Ambiguous overlap is
not attached to a prompt and does not invent an origin.

Prompt IDs are deterministic SHA-256 identities derived from source adapter/home/session identity,
source ordinal, event fingerprint, and content-schema version. The source home itself is not stored
inside the public ID. Deliberately identical prompts in independent root sessions therefore retain
different IDs, while an unchanged re-index preserves existing IDs.

## Redaction and size limit

Before insertion, `prompt-content-v1` redacts a deliberately small set of obvious high-risk forms:

- PEM private-key blocks;
- `Authorization: Bearer` and Basic authorization values;
- common OpenAI/GitHub/Slack/AWS-style token shapes;
- obvious API-key, token, secret, and password assignments.

The original secret is not written to another database column, log, error, or temporary table.
Redaction is conservative rather than a complete DLP guarantee. Users should still treat the
derived database as sensitive local data.

Stored prompt text is capped at 100,000 characters after redaction and receives an explicit
truncation marker. Original and stored character counts are metadata only. `prompt PROMPT_ID`
requires `--full` before displaying a stored prompt longer than 4,000 characters.

## Full-text search

`prompts_fts` is a local SQLite FTS5 external-content index over the redacted `prompts.text` column.
It uses the Unicode tokenizer and supports normal FTS5 terms and quoted phrases. Traditional
Chinese, English, Unicode, and mixed-language prompts are covered by synthetic tests. Invalid FTS5
syntax is reported as an error; Codex Insights does not silently replace it with substring search.

Search results show bounded snippets. `prompts` and `search` are origin-aware by default and report
replay-session counts without returning replay copies as separate hits.

## Local-data caveat

The analyzer database is separate from Codex home and never modifies source data, but it now holds
searchable user text. It is not encrypted by Codex Insights. Backups, filesystem permissions, and
device access should be managed accordingly. Deleting the derived database removes the local index;
it does not alter Codex history.
