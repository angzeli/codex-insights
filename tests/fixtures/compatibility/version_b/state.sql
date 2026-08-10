CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    session_path TEXT NOT NULL,
    start_time TEXT,
    modified_at TEXT,
    working_directory TEXT,
    is_archived INTEGER DEFAULT 0,
    future_catalogue_hint TEXT
);
INSERT INTO sessions VALUES (
    'version-b', 'rollout.jsonl', '2026-01-02T00:00:00Z',
    '2026-01-02T00:01:00Z', '/synthetic/missing/repository', 0, 'shape-only'
);
