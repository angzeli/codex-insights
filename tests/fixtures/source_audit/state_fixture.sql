CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    title TEXT,
    cwd TEXT,
    source TEXT,
    model TEXT,
    created_at TEXT,
    updated_at TEXT,
    rollout_path TEXT,
    archived INTEGER,
    git_branch TEXT,
    git_sha TEXT
);

INSERT INTO threads VALUES (
    'synthetic-thread-modern',
    'SYNTHETIC PRIVATE TITLE',
    '/synthetic/project-modern',
    'cli',
    'synthetic-model',
    '2026-08-08T00:00:00Z',
    '2026-08-09T00:00:00Z',
    'sessions/2026/08/09/rollout-modern.jsonl',
    0,
    'main',
    'deadbeef'
);

INSERT INTO threads VALUES (
    'synthetic-thread-missing',
    'SYNTHETIC MISSING TITLE',
    '/synthetic/project-missing',
    'editor',
    'synthetic-model-legacy',
    '2025-01-01T00:00:00Z',
    '2025-01-02T00:00:00Z',
    'sessions/2025/01/rollout-deleted.jsonl',
    1,
    'archive',
    'badc0de'
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

INSERT INTO settings VALUES ('synthetic-setting', 'SYNTHETIC SETTING VALUE');

CREATE TABLE backfill_state (
    id TEXT PRIMARY KEY,
    last_success_at TEXT
);

INSERT INTO backfill_state VALUES ('synthetic-backfill', '2026-08-09T00:00:00Z');
