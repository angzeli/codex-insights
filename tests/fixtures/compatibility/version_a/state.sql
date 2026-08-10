CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT,
    archived INTEGER DEFAULT 0
);
INSERT INTO threads VALUES (
    'version-a', 'rollout.jsonl', '2026-01-01T00:00:00Z',
    '2026-01-01T00:01:00Z', 0
);
