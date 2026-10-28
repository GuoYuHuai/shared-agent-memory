PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Bootstrap schema only. ingest_history.initialize_database() applies versioned,
-- transactional migrations before use; do not open this file as a finished DB.
-- This version describes the optional history store, not the knowledge schema.
INSERT OR IGNORE INTO schema_meta(key, value)
VALUES ('events_schema_version', '0');

CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY,
    source_device_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('codex', 'claude')),
    path TEXT NOT NULL,
    thread_id TEXT,
    inode INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    prefix_hash TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    malformed_lines INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_device_id, source, path)
);

CREATE INDEX IF NOT EXISTS source_files_device_source_idx
ON source_files(source_device_id, source);

CREATE TABLE IF NOT EXISTS source_events (
    id INTEGER PRIMARY KEY,
    event_hash TEXT NOT NULL UNIQUE,
    source_device_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('codex', 'claude')),
    source_path TEXT NOT NULL,
    source_offset INTEGER NOT NULL,
    source_line INTEGER NOT NULL,
    thread_id TEXT NOT NULL,
    event_id TEXT,
    parent_event_id TEXT,
    related_user_event_hash TEXT,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    event_type TEXT NOT NULL,
    event_timestamp TEXT,
    cwd TEXT,
    content TEXT NOT NULL,
    evidence_content_complete INTEGER NOT NULL DEFAULT 1
        CHECK (evidence_content_complete IN (0, 1)),
    semantic_revision INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS source_events_thread_idx
ON source_events(thread_id, event_timestamp);

CREATE INDEX IF NOT EXISTS source_events_device_idx
ON source_events(source_device_id, id);

-- Semantic context lookup is source-order-sensitive.  Without this composite
-- index every bounded neighbour lookup scans and sorts the full device
-- history, which multiplies badly when a review-contract change rewinds a
-- selection window.
CREATE INDEX IF NOT EXISTS source_events_context_position_idx
ON source_events(
    source_device_id, source, thread_id, source_path,
    source_offset, source_line, id
);

CREATE INDEX IF NOT EXISTS source_events_context_position_nocase_idx
ON source_events(
    source_device_id, source, thread_id, source_path COLLATE NOCASE,
    source_offset, source_line, id
);

CREATE INDEX IF NOT EXISTS source_events_role_idx
ON source_events(role, id);

CREATE INDEX IF NOT EXISTS source_events_semantic_identity_idx
ON source_events(source, event_id, role, event_type);

CREATE INDEX IF NOT EXISTS source_events_linked_evidence_idx
ON source_events(
    source, thread_id, related_user_event_hash, role, event_type
);

-- One immutable tool-result event can be observed from forked/replayed source
-- histories with more than one plausible preceding user event.  Preserve all
-- observed relations here; the scalar projection above is set to NULL when
-- the relation is ambiguous so semantic curation cannot silently choose one.
CREATE TABLE IF NOT EXISTS source_event_user_relations (
    event_hash TEXT NOT NULL
        REFERENCES source_events(event_hash) ON DELETE CASCADE,
    related_user_event_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY(event_hash, related_user_event_hash)
);

CREATE INDEX IF NOT EXISTS source_event_user_relations_target_idx
ON source_event_user_relations(related_user_event_hash, event_hash);

-- Candidate relations are projections; every source occurrence that supports a
-- candidate is retained independently so a later fork/replay remains
-- exportable even after the canonical event itself crossed its export cursor.
CREATE TABLE IF NOT EXISTS source_event_user_relation_observations (
    id INTEGER PRIMARY KEY,
    event_hash TEXT NOT NULL
        REFERENCES source_events(event_hash) ON DELETE CASCADE,
    related_user_event_hash TEXT NOT NULL,
    source_device_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_offset INTEGER NOT NULL,
    source_line INTEGER NOT NULL,
    provenance_kind TEXT NOT NULL CHECK (
        provenance_kind IN ('source-history', 'bundle-event', 'legacy-projection')
    ),
    first_seen_at TEXT NOT NULL,
    UNIQUE(
        event_hash, related_user_event_hash, source_device_id,
        source_path, source_offset, source_line
    )
);

CREATE INDEX IF NOT EXISTS source_event_user_relation_observations_device_idx
ON source_event_user_relation_observations(source_device_id, id);

CREATE TABLE IF NOT EXISTS semantic_evidence_changes (
    id INTEGER PRIMARY KEY,
    event_hash TEXT NOT NULL REFERENCES source_events(event_hash) ON DELETE CASCADE,
    semantic_revision INTEGER NOT NULL,
    changed_at TEXT NOT NULL,
    UNIQUE(event_hash, semantic_revision)
);

CREATE INDEX IF NOT EXISTS semantic_evidence_changes_event_idx
ON semantic_evidence_changes(event_hash, semantic_revision);

-- Complete JSONL records that the current parser cannot safely understand
-- must remain replayable.  Advancing a source checkpoint is only safe after
-- either a normalized event or an exact local quarantine record is durable.
CREATE TABLE IF NOT EXISTS unparsed_source_records (
    id INTEGER PRIMARY KEY,
    record_hash TEXT NOT NULL UNIQUE,
    source_device_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('codex', 'claude')),
    source_path TEXT NOT NULL,
    source_offset INTEGER NOT NULL,
    source_line INTEGER NOT NULL,
    thread_id TEXT,
    reason TEXT NOT NULL,
    raw_record BLOB NOT NULL,
    parser_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'resolved', 'ignored')),
    resolved_event_hash TEXT REFERENCES source_events(event_hash),
    first_seen_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS unparsed_source_records_pending_idx
ON unparsed_source_records(
    source_device_id, source, status, parser_version, id
);

CREATE TABLE IF NOT EXISTS event_observations (
    id INTEGER PRIMARY KEY,
    event_hash TEXT NOT NULL REFERENCES source_events(event_hash) ON DELETE CASCADE,
    source_device_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE(event_hash, source_device_id)
);

CREATE INDEX IF NOT EXISTS event_observations_device_idx
ON event_observations(source_device_id, id);

CREATE TABLE IF NOT EXISTS export_cursors (
    source_device_id TEXT PRIMARY KEY,
    last_observation_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relation_export_cursors (
    source_device_id TEXT PRIMARY KEY,
    last_relation_observation_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imported_bundles (
    bundle_id TEXT PRIMARY KEY,
    source_device_id TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS source_events_fts USING fts5(
    content,
    role UNINDEXED,
    source UNINDEXED,
    thread_id UNINDEXED,
    content='source_events',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS source_events_ai
AFTER INSERT ON source_events BEGIN
    INSERT INTO source_events_fts(rowid, content, role, source, thread_id)
    VALUES (new.id, new.content, new.role, new.source, new.thread_id);
END;

CREATE TRIGGER IF NOT EXISTS source_events_ad
AFTER DELETE ON source_events BEGIN
    INSERT INTO source_events_fts(source_events_fts, rowid, content, role, source, thread_id)
    VALUES ('delete', old.id, old.content, old.role, old.source, old.thread_id);
END;

CREATE TRIGGER IF NOT EXISTS source_events_au
AFTER UPDATE ON source_events BEGIN
    INSERT INTO source_events_fts(source_events_fts, rowid, content, role, source, thread_id)
    VALUES ('delete', old.id, old.content, old.role, old.source, old.thread_id);
    INSERT INTO source_events_fts(rowid, content, role, source, thread_id)
    VALUES (new.id, new.content, new.role, new.source, new.thread_id);
END;
