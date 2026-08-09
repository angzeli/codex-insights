CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    title TEXT,
    cwd TEXT,
    source TEXT,
    model TEXT,
    model_provider TEXT,
    created_at TEXT,
    updated_at TEXT,
    rollout_path TEXT,
    archived INTEGER,
    archived_at TEXT,
    git_branch TEXT,
    git_sha TEXT,
    git_origin_url TEXT
);

INSERT INTO threads VALUES (
    'synthetic-thread-modern',
    'SYNTHETIC PRIVATE TITLE',
    '/synthetic/project-modern',
    'cli',
    'synthetic-model',
    'synthetic-provider',
    '2026-08-08T00:00:00Z',
    '2026-08-09T00:00:00Z',
    'sessions/2026/08/09/rollout-modern.jsonl',
    0,
    NULL,
    'main',
    'deadbeef',
    'https://example.invalid/synthetic-modern.git'
);

INSERT INTO threads VALUES (
    'synthetic-thread-legacy',
    'SYNTHETIC LEGACY TITLE',
    '/synthetic/project-legacy',
    'cli',
    'synthetic-model-legacy',
    NULL,
    '2025-01-01T00:00:00Z',
    '2025-01-01T00:02:00Z',
    'sessions/2025/01/01/rollout-legacy.jsonl',
    0,
    NULL,
    'legacy',
    'feedface',
    NULL
);

INSERT INTO threads VALUES (
    'synthetic-thread-archived',
    'SYNTHETIC ARCHIVED TITLE',
    '/synthetic/project-archived',
    'editor',
    'synthetic-model-old',
    'synthetic-provider-old',
    '2024-01-01T00:00:00Z',
    '2024-01-01T00:01:00Z',
    'archived_sessions/rollout-archived.jsonl',
    1,
    '2024-01-01T00:01:00Z',
    'archive',
    'abc1234',
    NULL
);

INSERT INTO threads VALUES (
    'synthetic-thread-missing',
    'SYNTHETIC MISSING TITLE',
    '/synthetic/project-missing',
    'editor',
    'synthetic-model-legacy',
    NULL,
    '2025-01-01T00:00:00Z',
    '2025-01-02T00:00:00Z',
    'sessions/2025/01/rollout-deleted.jsonl',
    0,
    NULL,
    'missing',
    'badc0de',
    NULL
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
