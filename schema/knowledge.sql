PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS kb_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO kb_meta(key, value)
VALUES ('schema_version', '1')
ON CONFLICT(key) DO UPDATE SET value = excluded.value;

CREATE TABLE IF NOT EXISTS canonical_documents (
    rowid INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    source_device_id TEXT NOT NULL,
    is_credential INTEGER NOT NULL CHECK (is_credential IN (0, 1)),
    content_sha256 TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT '',
    indexed_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS canonical_fts USING fts5(
    path UNINDEXED,
    title,
    kind,
    scope,
    source_device_id,
    content,
    tokenize = 'trigram'
);

CREATE TABLE IF NOT EXISTS history_events (
    rowid INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    source TEXT NOT NULL,
    authority TEXT NOT NULL,
    status TEXT NOT NULL,
    source_device_id TEXT NOT NULL,
    bundle_id TEXT,
    imported_from TEXT,
    ledger_ref TEXT NOT NULL,
    event_sha256 TEXT NOT NULL,
    is_credential INTEGER NOT NULL CHECK (is_credential IN (0, 1)),
    indexed_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
    event_id UNINDEXED,
    kind,
    scope,
    statement,
    source,
    authority,
    status,
    source_device_id,
    bundle_id,
    content,
    tokenize = 'trigram'
);

CREATE INDEX IF NOT EXISTS idx_documents_kind_scope
    ON canonical_documents(kind, scope);
CREATE INDEX IF NOT EXISTS idx_events_kind_scope
    ON history_events(kind, scope);
CREATE INDEX IF NOT EXISTS idx_events_source_device
    ON history_events(source_device_id, created_at);

CREATE TABLE IF NOT EXISTS lifecycle_supersessions (
    old_id TEXT PRIMARY KEY,
    new_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ledger_ref TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lifecycle_resolutions (
    candidate_id TEXT PRIMARY KEY,
    resolution TEXT NOT NULL CHECK (resolution IN ('accepted', 'rejected')),
    event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ledger_ref TEXT NOT NULL
);

-- Disposable change detector/projection state. These tables never replace
-- canonical files or the immutable ledger; they only let routine refreshes
-- avoid reparsing unchanged source bytes.
CREATE TABLE IF NOT EXISTS canonical_index_sources (
    path TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    projection_json TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_canonical_index_sources_document
    ON canonical_index_sources(document_id);

CREATE TABLE IF NOT EXISTS ledger_index_sources (
    path TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    indexed_at TEXT NOT NULL
);
