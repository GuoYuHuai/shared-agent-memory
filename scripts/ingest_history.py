#!/usr/bin/env python3
"""Incrementally capture useful Codex and Claude messages into SQLite.

The importer is deliberately deterministic and quiet: message bodies are kept
verbatim in SQLite but are never printed.  Only complete JSONL lines advance a
per-device checkpoint, so an interrupted writer is safe to revisit later.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import sqlite3
import subprocess
import sys
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import device_identity

TOOL_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("AI_KNOWLEDGE_ROOT", str(TOOL_ROOT))).expanduser().resolve()
CONFIG_PATH = ROOT / "config" / "kb.json"
SCHEMA_PATH = TOOL_ROOT / "schema" / "events.sql"
DEFAULT_DB = ROOT / ".kb-data" / "history.sqlite"
DEFAULT_CHECKPOINT_DIR = ROOT / ".kb-data" / "checkpoints"
PREFIX_LIMIT = 64 * 1024
BUNDLE_FORMAT = "ai-knowledge-history-bundle-v1"
EVENT_HASH_VERSION = 1
EVENTS_SCHEMA_VERSION = 6
COMMIT_INTERVAL = 500
UNPARSED_PARSER_VERSION = 3
UNPARSED_REPLAY_LIMIT = 500
CHECKPOINT_PARSER_VERSION = 4
TOOL_RESULT_EVIDENCE_TYPE = "tool-result-evidence"
RELATION_PROVENANCE_KINDS = frozenset(
    ("source-history", "bundle-event", "legacy-projection")
)
EVIDENCE_TEXT_MAX_BYTES = 64 * 1024
EVIDENCE_BINARY_MARKER = "[tool-result-evidence omitted: image-or-binary]"
EVIDENCE_TRUNCATION_MARKER = (
    "[tool-result-evidence truncated; full raw record remains local]"
)
CODEX_TOOL_RESULT_TYPES = frozenset(
    (
        "function_call_output",
        "custom_tool_call_output",
        "computer_tool_call_output",
    )
)
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
DATA_URL_RE = re.compile(
    r"data:(?:image|audio|video|application/octet-stream)"
    r"[^,\s\"']*;base64,[A-Za-z0-9+/=_-]+",
    re.IGNORECASE,
)
ENCODED_IMAGE_PREFIXES = (
    "iVBORw0KGgo",
    "/9j/",
    "R0lGOD",
    "UklGR",
)
BINARY_EVIDENCE_BLOCK_TYPES = frozenset(
    (
        "input_image",
        "output_image",
        "image",
        "image_url",
        "screenshot",
        "audio",
        "video",
        "binary",
        "blob",
    )
)
BINARY_EVIDENCE_FIELD_NAMES = frozenset(
    (
        "image",
        "image_url",
        "input_image",
        "output_image",
        "screenshot",
        "audio",
        "video",
        "binary",
        "base64",
        "blob",
        "bytes",
        "byte_data",
        "file_data",
    )
)
BINARY_MIME_FIELD_NAMES = frozenset(("mime_type", "media_type", "content_type"))
BINARY_MIME_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "application/octet-stream",
)


class IngestError(RuntimeError):
    """An expected, body-safe ingest failure."""


@dataclass(frozen=True)
class SourceFile:
    source: str
    path: Path
    thread_id: str = ""
    cwd: str = ""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_config() -> Mapping[str, Any]:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngestError("invalid-config") from exc
    return value if isinstance(value, dict) else {}


def configured_path(value: Optional[str], fallback: Path) -> Path:
    if not value:
        return fallback
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def default_locations() -> Tuple[Path, Path]:
    config = load_config()
    db = configured_path(config.get("history_database"), DEFAULT_DB)
    checkpoints = configured_path(config.get("checkpoint_directory"), DEFAULT_CHECKPOINT_DIR)
    return db, checkpoints


def platform_identifier_material() -> str:
    """Use the same stable platform contract as foreground knowledge capture."""
    return device_identity.platform_material()


def derive_device_id() -> str:
    try:
        return device_identity.resolve_device_id(ROOT)
    except device_identity.DeviceIdentityError as error:
        raise IngestError("device-identity-unavailable") from error


def resolve_device_id(override: Optional[str]) -> str:
    # An explicit source ID remains available for historical imports and
    # fixtures. Automatic execution always passes the current bound identity.
    device_id = override or os.environ.get("KB_SOURCE_DEVICE_ID") or derive_device_id()
    if not isinstance(device_id, str) or not device_id.strip():
        raise IngestError("invalid-device-id")
    device_id = device_id.strip()
    if len(device_id) > 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", device_id):
        raise IngestError("invalid-device-id")
    return device_id


def device_dir_name(device_id: str) -> str:
    return device_id


def migrate_source_events_fts_to_trigram(connection: sqlite3.Connection) -> None:
    """Atomically replace the legacy FTS index and verify its full rebuild."""
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TRIGGER IF EXISTS source_events_ai")
        connection.execute("DROP TRIGGER IF EXISTS source_events_ad")
        connection.execute("DROP TRIGGER IF EXISTS source_events_au")
        connection.execute("DROP TABLE IF EXISTS source_events_fts")
        connection.execute(
            """
            CREATE VIRTUAL TABLE source_events_fts USING fts5(
                content,
                role UNINDEXED,
                source UNINDEXED,
                thread_id UNINDEXED,
                content='source_events',
                content_rowid='id',
                tokenize='trigram'
            )
            """
        )
        # FTS5 owns the rebuild operation for external-content tables.  Keeping
        # it inside this transaction means a full disk or interruption cannot
        # commit the new table before every source_events row is indexed.
        connection.execute(
            "INSERT INTO source_events_fts(source_events_fts) VALUES('rebuild')"
        )
        connection.execute(
            """
            INSERT INTO source_events_fts(source_events_fts, rank)
            VALUES('integrity-check', 1)
            """
        )
        connection.execute(
            """
            CREATE TRIGGER source_events_ai
            AFTER INSERT ON source_events BEGIN
                INSERT INTO source_events_fts(rowid, content, role, source, thread_id)
                VALUES (new.id, new.content, new.role, new.source, new.thread_id);
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER source_events_ad
            AFTER DELETE ON source_events BEGIN
                INSERT INTO source_events_fts(
                    source_events_fts, rowid, content, role, source, thread_id
                )
                VALUES ('delete', old.id, old.content, old.role, old.source, old.thread_id);
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER source_events_au
            AFTER UPDATE ON source_events BEGIN
                INSERT INTO source_events_fts(
                    source_events_fts, rowid, content, role, source, thread_id
                )
                VALUES ('delete', old.id, old.content, old.role, old.source, old.thread_id);
                INSERT INTO source_events_fts(rowid, content, role, source, thread_id)
                VALUES (new.id, new.content, new.role, new.source, new.thread_id);
            END
            """
        )
        connection.commit()
    except sqlite3.Error:
        if connection.in_transaction:
            connection.rollback()
        raise


def existing_events_schema_version(
    connection: sqlite3.Connection,
) -> Optional[int]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "schema_meta" not in tables:
        return None
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='events_schema_version'"
    ).fetchone()
    if row is None:
        return None
    try:
        version = int(row[0])
    except (TypeError, ValueError) as error:
        raise IngestError("unsupported-events-schema-version") from error
    if version > EVENTS_SCHEMA_VERSION:
        # Never let an older binary rewrite or downgrade a database whose
        # invariants were established by a newer schema.
        raise IngestError("unsupported-events-schema-version")
    return version


def migrate_evidence_schema(connection: sqlite3.Connection) -> None:
    """Add evidence/relation tracking before advertising schema version 6.

    ``CREATE TABLE IF NOT EXISTS`` cannot add columns to a version-2 database.
    Keep the version update in the same transaction as every migration write
    so a failed or interrupted migration remains safely retryable.
    """

    # Daily capture opens the same large history database repeatedly.  Once
    # version 6 and its required objects are present, do not rescan every
    # source event through the INSERT OR IGNORE backfill on each invocation.
    # A partial migration cannot take this path because the version marker is
    # committed in the same transaction as all required objects below.
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    version = existing_events_schema_version(connection)
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(source_events)")
    }
    if (
        version == EVENTS_SCHEMA_VERSION
        and {
            "related_user_event_hash",
            "evidence_content_complete",
            "semantic_revision",
        }.issubset(columns)
        and {
            "source_event_user_relations",
            "source_event_user_relation_observations",
            "relation_export_cursors",
            "semantic_evidence_changes",
        }.issubset(tables)
    ):
        return

    try:
        connection.execute("BEGIN IMMEDIATE")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(source_events)")
        }
        if "related_user_event_hash" not in columns:
            connection.execute(
                "ALTER TABLE source_events ADD COLUMN related_user_event_hash TEXT"
            )
        if "evidence_content_complete" not in columns:
            connection.execute(
                """
                ALTER TABLE source_events
                ADD COLUMN evidence_content_complete INTEGER NOT NULL DEFAULT 1
                CHECK (evidence_content_complete IN (0, 1))
                """
            )
        if "semantic_revision" not in columns:
            connection.execute(
                """
                ALTER TABLE source_events
                ADD COLUMN semantic_revision INTEGER NOT NULL DEFAULT 1
                """
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS source_events_linked_evidence_idx
            ON source_events(
                source, thread_id, related_user_event_hash, role, event_type
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_event_user_relations (
                event_hash TEXT NOT NULL
                    REFERENCES source_events(event_hash) ON DELETE CASCADE,
                related_user_event_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                PRIMARY KEY(event_hash, related_user_event_hash)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS source_event_user_relations_target_idx
            ON source_event_user_relations(related_user_event_hash, event_hash)
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO source_event_user_relations(
                event_hash,related_user_event_hash,first_seen_at
            )
            SELECT event_hash,related_user_event_hash,ingested_at
            FROM source_events
            WHERE related_user_event_hash IS NOT NULL
            """
        )
        connection.execute(
            """
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
                    provenance_kind IN (
                        'source-history', 'bundle-event', 'legacy-projection'
                    )
                ),
                first_seen_at TEXT NOT NULL,
                UNIQUE(
                    event_hash, related_user_event_hash, source_device_id,
                    source_path, source_offset, source_line
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                source_event_user_relation_observations_device_idx
            ON source_event_user_relation_observations(source_device_id, id)
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO source_event_user_relation_observations(
                event_hash,related_user_event_hash,source_device_id,source_path,
                source_offset,source_line,provenance_kind,first_seen_at
            )
            SELECT r.event_hash,r.related_user_event_hash,e.source_device_id,
                   e.source_path,e.source_offset,e.source_line,
                   'legacy-projection',r.first_seen_at
            FROM source_event_user_relations AS r
            JOIN source_events AS e ON e.event_hash=r.event_hash
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS relation_export_cursors (
                source_device_id TEXT PRIMARY KEY,
                last_relation_observation_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_evidence_changes (
                id INTEGER PRIMARY KEY,
                event_hash TEXT NOT NULL
                    REFERENCES source_events(event_hash) ON DELETE CASCADE,
                semantic_revision INTEGER NOT NULL,
                changed_at TEXT NOT NULL,
                UNIQUE(event_hash, semantic_revision)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS semantic_evidence_changes_event_idx
            ON semantic_evidence_changes(event_hash, semantic_revision)
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO semantic_evidence_changes(
                event_hash,semantic_revision,changed_at
            )
            SELECT event_hash,semantic_revision,ingested_at
            FROM source_events
            WHERE role='assistant' AND event_type=?
              AND related_user_event_hash IS NOT NULL
            """,
            (TOOL_RESULT_EVIDENCE_TYPE,),
        )
        connection.execute(
            """
            INSERT INTO schema_meta(key, value)
            VALUES ('events_schema_version', '6')
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """
        )
        connection.commit()
    except sqlite3.Error:
        if connection.in_transaction:
            connection.rollback()
        raise


def initialize_database(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not SCHEMA_PATH.is_file():
        raise IngestError("missing-events-schema")
    connection = sqlite3.connect(str(db_path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        # Fail closed before migration or bootstrap writes when a newer binary
        # owns the schema.
        existing_events_schema_version(connection)
        existing_events_schema = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='source_events'"
        ).fetchone()
        # Do not let the current bootstrap schema create version-6 tables
        # outside the migration transaction for an existing database.  A
        # failed ALTER must leave the entire evidence schema at its prior
        # version so the next run can retry it atomically.
        if existing_events_schema is not None:
            migrate_evidence_schema(connection)
        connection.executescript(schema)
        if existing_events_schema is None:
            migrate_evidence_schema(connection)
        fts_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_events_fts'"
        ).fetchone()
        fts_sql = str(fts_sql_row[0] or "") if fts_sql_row else ""
        if "tokenize='trigram'" not in fts_sql.replace(" ", ""):
            # unicode61 indexes an unspaced Chinese sentence as one token, so
            # ordinary substring retrieval silently misses known raw evidence.
            migrate_source_events_fts_to_trigram(connection)
    except IngestError:
        connection.close()
        raise
    except sqlite3.Error as exc:
        connection.close()
        if "fts5" in str(exc).lower():
            raise IngestError("sqlite-fts5-unavailable") from exc
        raise
    return connection


def normalize_source_path(raw_path: str, home: Path) -> Path:
    if raw_path.startswith("~/"):
        path = home / raw_path[2:]
    else:
        path = Path(raw_path)
        if not path.is_absolute():
            path = home / path
    return path.resolve()


def source_path_identity(path: Any) -> str:
    """Return one stable identity for ordinary and Win32 extended paths."""

    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    resolved = str(Path(text).resolve())
    if resolved.startswith("\\\\?\\UNC\\"):
        resolved = "\\\\" + resolved[8:]
    elif resolved.startswith("\\\\?\\"):
        resolved = resolved[4:]
    return os.path.normcase(os.path.normpath(resolved))


def read_codex_meta(path: Path) -> Tuple[str, str]:
    try:
        with path.open("rb") as handle:
            for _ in range(64):
                raw = handle.readline()
                if not raw or not raw.endswith(b"\n"):
                    break
                try:
                    item = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if item.get("type") != "session_meta":
                    continue
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                thread_id = str(payload.get("id") or payload.get("session_id") or "")
                cwd = str(payload.get("cwd") or "")
                return thread_id, cwd
    except OSError:
        pass
    match = UUID_RE.search(path.name)
    return (match.group(0) if match else path.stem), ""


def codex_thread_registry(home: Path) -> Dict[str, Tuple[str, str, Path]]:
    state_db = home / ".codex" / "state_5.sqlite"
    if not state_db.is_file():
        return {}
    registry: Dict[str, Tuple[str, str, Path]] = {}
    connection: Optional[sqlite3.Connection] = None
    try:
        uri = state_db.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(threads)")
        }
        if not {"id", "rollout_path"}.issubset(columns):
            return {}
        cwd_expr = "cwd" if "cwd" in columns else "''"
        query = "SELECT id, rollout_path, %s AS cwd FROM threads" % cwd_expr
        for thread_id, rollout_path, cwd in connection.execute(query):
            if not rollout_path:
                continue
            path = normalize_source_path(str(rollout_path), home)
            registry[source_path_identity(path)] = (
                str(thread_id or ""),
                str(cwd or ""),
                path,
            )
    except sqlite3.Error:
        return {}
    finally:
        if connection is not None:
            connection.close()
    return registry


def discover_codex(home: Path) -> List[SourceFile]:
    registry = codex_thread_registry(home)
    paths: Dict[str, Path] = {
        identity: entry[2] for identity, entry in registry.items()
    }
    codex_home = home / ".codex"
    for directory_name in ("sessions", "archived_sessions"):
        directory = codex_home / directory_name
        if directory.is_dir():
            for path in directory.rglob("*.jsonl"):
                resolved = path.resolve()
                # Prefer the filesystem enumeration spelling over a registry
                # spelling such as ``\\?\C:\...`` while retaining registry
                # metadata under the shared identity.
                paths[source_path_identity(resolved)] = resolved
    result: List[SourceFile] = []
    for identity in sorted(paths):
        path = paths[identity]
        if not path.is_file():
            continue
        registered = registry.get(identity)
        thread_id, cwd = (
            (registered[0], registered[1]) if registered else ("", "")
        )
        if not thread_id:
            thread_id, discovered_cwd = read_codex_meta(path)
            cwd = cwd or discovered_cwd
        result.append(SourceFile("codex", path, thread_id, cwd))
    return result


def discover_claude(home: Path) -> List[SourceFile]:
    projects = home / ".claude" / "projects"
    if not projects.is_dir():
        return []
    return [
        SourceFile("claude", path.resolve())
        for path in sorted(projects.rglob("*.jsonl"))
        if path.is_file()
    ]


def discover_sources(source: str, home: Optional[Path] = None) -> List[SourceFile]:
    home = (home or Path.home()).resolve()
    files: List[SourceFile] = []
    if source in ("codex", "all"):
        files.extend(discover_codex(home))
    if source in ("claude", "all"):
        files.extend(discover_claude(home))
    return files


def discovery_report(home: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Body-safe summary telling apart missing roots, empty roots, and files.

    A machine with no history at all is indistinguishable from a wrong home
    path when discovery silently returns empty lists; this report keeps the
    two cases separate without ever reading message bodies.
    """
    home = (home or Path.home()).resolve()
    roots = {
        "codex": home / ".codex",
        "claude": home / ".claude" / "projects",
    }
    counts = {
        "codex": len(discover_codex(home)),
        "claude": len(discover_claude(home)),
    }
    return {
        source: {
            "root_exists": roots[source].is_dir(),
            "session_files": counts[source],
        }
        for source in ("codex", "claude")
    }


def hash_prefix(path: Path, length: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = max(0, length)
        while remaining:
            chunk = handle.read(min(remaining, 64 * 1024))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def checkpoint_path(base: Path, device_id: str, source_file: SourceFile) -> Path:
    path_hash = hashlib.sha256(str(source_file.path).encode("utf-8")).hexdigest()
    return base / device_dir_name(device_id) / source_file.source / (path_hash + ".json")


def load_checkpoint(path: Path) -> Optional[Mapping[str, Any]]:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def resume_position(source_file: SourceFile, checkpoint: Optional[Mapping[str, Any]]) -> Tuple[int, int]:
    if not checkpoint:
        return 0, 0
    try:
        if int(checkpoint.get("parser_version", 0)) != CHECKPOINT_PARSER_VERSION:
            # A one-time full replay is required when a formerly ignored record
            # class becomes durable evidence. Existing messages deduplicate by
            # canonical event hash, after which the new checkpoint resumes
            # ordinary incremental scanning.
            return 0, 0
        stat = source_file.path.stat()
        expected_path = str(source_file.path)
        old_offset = int(checkpoint["offset"])
        old_size = int(checkpoint["size"])
        old_inode = int(checkpoint["inode"])
        prefix_length = int(checkpoint.get("prefix_length", min(old_size, PREFIX_LIMIT)))
        if checkpoint.get("path") != expected_path:
            return 0, 0
        if stat.st_ino != old_inode or stat.st_size < old_offset:
            return 0, 0
        if prefix_length < 0 or prefix_length > stat.st_size:
            return 0, 0
        if hash_prefix(source_file.path, prefix_length) != checkpoint.get("prefix_hash"):
            return 0, 0
        return old_offset, int(checkpoint.get("line_number", 0))
    except (KeyError, TypeError, ValueError, OSError):
        return 0, 0


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        # Losing the directory rename only causes harmless replay, but syncing
        # it makes a reported successful checkpoint persist across power loss.
        if os.name != "nt":
            descriptor = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def checkpoint_wal_before_cursor(connection: sqlite3.Connection) -> None:
    """Make committed rows durable before publishing an external cursor.

    SQLite's FULL checkpoint can return normally with ``busy=1`` when another
    connection holds an older read snapshot.  Publishing the JSON checkpoint
    in that state would let a power loss discard WAL-only rows while the next
    scan skips past them.  Treat any incomplete checkpoint as a retryable,
    body-safe ingest failure instead.
    """
    synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
    fullfsync = int(connection.execute("PRAGMA fullfsync").fetchone()[0])
    busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    try:
        # NORMAL is sufficient for ordinary WAL commits, but the cursor is an
        # external promise.  FULL makes this checkpoint sync both WAL and main
        # database; fullfsync strengthens the flush on macOS.  Bound reader
        # waiting so a long-lived search cannot stall an entire capture run.
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA fullfsync = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        row = connection.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
        if row is None or len(row) < 3:
            raise IngestError("wal-checkpoint-incomplete")
        busy, log_frames, checkpointed_frames = (
            int(row[0]),
            int(row[1]),
            int(row[2]),
        )
        if busy or checkpointed_frames < log_frames:
            raise IngestError("wal-checkpoint-busy")
    finally:
        connection.execute("PRAGMA busy_timeout = %d" % busy_timeout)
        connection.execute("PRAGMA fullfsync = %d" % fullfsync)
        connection.execute("PRAGMA synchronous = %d" % synchronous)


def text_from_content(content: Any, allowed_types: Sequence[str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces: List[str] = []
    for block in content:
        if isinstance(block, str):
            pieces.append(block)
            continue
        if not isinstance(block, dict) or block.get("type") not in allowed_types:
            continue
        text = block.get("text")
        if isinstance(text, str):
            pieces.append(text)
    return "\n".join(pieces)


def generated_codex_context(content: str) -> bool:
    stripped = content.strip()
    wrappers = (
        ("<codex_internal_context", "</codex_internal_context>"),
        ("<environment_context", "</environment_context>"),
    )
    return any(stripped.startswith(start) and stripped.endswith(end) for start, end in wrappers)


def codex_message_text(content: Any) -> str:
    if isinstance(content, str):
        return "" if generated_codex_context(content) else content
    if not isinstance(content, list):
        return ""
    pieces: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "input_image":
            # Preserve the fact that the user supplied a non-text image
            # without indexing a potentially multi-megabyte data URL. The
            # exact block remains available in source_events.raw_json.
            pieces.append("[non-text input: image]")
            continue
        if block_type not in ("input_text", "output_text", "text"):
            continue
        text = block.get("text")
        if isinstance(text, str) and not generated_codex_context(text):
            pieces.append(text)
    return "\n".join(pieces)


def truncate_evidence_text(text: str) -> Tuple[str, bool]:
    """Bound FTS content by UTF-8 bytes without splitting a code point."""

    encoded = text.encode("utf-8")
    if len(encoded) <= EVIDENCE_TEXT_MAX_BYTES:
        return text, True
    marker = "\n" + EVIDENCE_TRUNCATION_MARKER
    marker_bytes = marker.encode("utf-8")
    prefix_limit = max(0, EVIDENCE_TEXT_MAX_BYTES - len(marker_bytes))
    prefix = encoded[:prefix_limit].decode("utf-8", errors="ignore")
    return prefix + marker, False


def sanitize_evidence_text(text: str) -> Tuple[str, bool]:
    """Remove binary/image payloads while preserving ordinary text exactly."""

    complete = True
    stripped = text.strip()
    if len(stripped) >= 1024 and stripped.startswith(ENCODED_IMAGE_PREFIXES):
        return EVIDENCE_BINARY_MARKER, False
    if "\x00" in text:
        text = text.replace("\x00", EVIDENCE_BINARY_MARKER)
        complete = False
    text, replacements = DATA_URL_RE.subn(EVIDENCE_BINARY_MARKER, text)
    if replacements:
        complete = False
    text, within_limit = truncate_evidence_text(text)
    return text, complete and within_limit


def _normalized_evidence_name(value: Any) -> str:
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", str(value or "").strip())
    return text.replace("-", "_").lower()


def _evidence_structure_is_complete(value: Any, depth: int = 0) -> bool:
    """Fail closed if any nested field carries image or binary material."""

    if depth > 16:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        if "\x00" in value or DATA_URL_RE.search(value):
            return False
        return not (
            len(stripped) >= 1024
            and stripped.startswith(ENCODED_IMAGE_PREFIXES)
        )
    if isinstance(value, (bytes, bytearray, memoryview)):
        return False
    if isinstance(value, list):
        return all(
            _evidence_structure_is_complete(item, depth + 1)
            for item in value
        )
    if not isinstance(value, dict):
        return True

    block_type = _normalized_evidence_name(value.get("type"))
    if block_type in BINARY_EVIDENCE_BLOCK_TYPES or block_type.startswith(
        BINARY_MIME_PREFIXES
    ):
        return False
    for raw_key, nested in value.items():
        key = _normalized_evidence_name(raw_key)
        if key in BINARY_EVIDENCE_FIELD_NAMES and nested not in (
            None,
            "",
            [],
            {},
        ):
            return False
        if (
            key in BINARY_MIME_FIELD_NAMES
            and isinstance(nested, str)
            and nested.strip().lower().startswith(BINARY_MIME_PREFIXES)
        ):
            return False
        if not _evidence_structure_is_complete(nested, depth + 1):
            return False
    return True


def _evidence_text_parts(value: Any, depth: int = 0) -> Tuple[List[str], bool]:
    """Extract only explicit text fields from a bounded set of tool shapes."""

    if depth > 4:
        return [EVIDENCE_BINARY_MARKER], False
    if isinstance(value, str):
        return [value], True
    if isinstance(value, list):
        pieces: List[str] = []
        complete = True
        for block in value:
            block_pieces, block_complete = _evidence_text_parts(block, depth + 1)
            pieces.extend(block_pieces)
            complete = complete and block_complete
        return pieces, complete
    if not isinstance(value, dict):
        return [], False

    block_type = _normalized_evidence_name(value.get("type"))
    if block_type in BINARY_EVIDENCE_BLOCK_TYPES:
        return [EVIDENCE_BINARY_MARKER], False
    if block_type in ("text", "input_text", "output_text"):
        text = value.get("text")
        return ([text], True) if isinstance(text, str) else ([], False)

    for key in ("text", "content", "output"):
        if key in value:
            return _evidence_text_parts(value.get(key), depth + 1)
    # Unknown structured tool payloads are retained in raw_json, but never
    # serialized wholesale into FTS where image bytes or opaque blobs could
    # become searchable.
    return [], False


def evidence_text_from_value(value: Any) -> Tuple[str, bool]:
    pieces, structurally_complete = _evidence_text_parts(value)
    recursively_complete = _evidence_structure_is_complete(value)
    text = "\n".join(piece for piece in pieces if isinstance(piece, str))
    if not text and not structurally_complete:
        return EVIDENCE_BINARY_MARKER, False
    sanitized, text_complete = sanitize_evidence_text(text)
    return (
        sanitized,
        structurally_complete and recursively_complete and text_complete,
    )


def codex_thread_id(source_file: SourceFile) -> str:
    thread_id = source_file.thread_id
    if not thread_id:
        match = UUID_RE.search(source_file.path.name)
        thread_id = match.group(0) if match else source_file.path.stem
    return thread_id


def extract_codex_events(
    item: Mapping[str, Any],
    source_file: SourceFile,
) -> List[Dict[str, Any]]:
    if item.get("type") != "response_item":
        return []
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return []
    payload_type = payload.get("type")
    if payload_type in CODEX_TOOL_RESULT_TYPES:
        content, complete = evidence_text_from_value(payload.get("output"))
        if not content.strip():
            return []
        call_id = str(payload.get("call_id") or "") or None
        event_id = str(payload.get("id") or "") or None
        if event_id is None and call_id is not None:
            event_id = "%s:%s" % (payload_type, call_id)
        return [{
            "source": "codex",
            "thread_id": codex_thread_id(source_file),
            "event_id": event_id,
            "parent_event_id": call_id,
            "related_user_event_hash": None,
            # Tool output is evidence produced by the runtime, never a user
            # statement even though it may contain a file the user requested.
            "role": "assistant",
            "event_type": TOOL_RESULT_EVIDENCE_TYPE,
            "event_timestamp": str(item.get("timestamp") or "") or None,
            "cwd": source_file.cwd or None,
            "content": content,
            "evidence_content_complete": complete,
        }]
    if payload_type != "message":
        return []
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return []
    content = codex_message_text(payload.get("content"))
    if not content.strip():
        return []
    return [{
        "source": "codex",
        "thread_id": codex_thread_id(source_file),
        "event_id": str(payload.get("id") or "") or None,
        "parent_event_id": None,
        "related_user_event_hash": None,
        "role": role,
        "event_type": "message",
        "event_timestamp": str(item.get("timestamp") or "") or None,
        "cwd": source_file.cwd or None,
        "content": content,
        "evidence_content_complete": True,
    }]


def extract_claude_events(
    item: Mapping[str, Any],
    source_file: SourceFile,
) -> List[Dict[str, Any]]:
    if item.get("type") not in ("user", "assistant") or item.get("isMeta") is True:
        return []
    message = item.get("message")
    if not isinstance(message, dict):
        return []
    role = message.get("role") or item.get("type")
    if role not in ("user", "assistant"):
        return []
    message_content = message.get("content")
    tool_blocks = (
        [
            (index, block)
            for index, block in enumerate(message_content)
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        if isinstance(message_content, list)
        else []
    )
    thread_id = str(item.get("sessionId") or source_file.thread_id or source_file.path.stem)
    if tool_blocks:
        events: List[Dict[str, Any]] = []
        record_id = str(item.get("uuid") or message.get("id") or "")
        for index, block in tool_blocks:
            content, complete = evidence_text_from_value(block.get("content"))
            if not content.strip():
                continue
            tool_use_id = str(block.get("tool_use_id") or "") or None
            event_id = (
                "%s:tool-result:%d" % (record_id, index)
                if record_id
                else None
            )
            events.append({
                "source": "claude",
                "thread_id": thread_id,
                "event_id": event_id,
                "parent_event_id": (
                    tool_use_id
                    or str(item.get("parentUuid") or "")
                    or None
                ),
                "related_user_event_hash": None,
                # Claude serializes tool_result inside a role=user envelope.
                # Normalizing it to assistant prevents accidental promotion to
                # a first-party user fact.
                "role": "assistant",
                "event_type": TOOL_RESULT_EVIDENCE_TYPE,
                "event_timestamp": str(item.get("timestamp") or "") or None,
                "cwd": str(item.get("cwd") or source_file.cwd or "") or None,
                "content": content,
                "evidence_content_complete": complete,
            })
        return events
    content = text_from_content(message.get("content"), ("text",))
    if not content.strip():
        return []
    return [{
        "source": "claude",
        "thread_id": thread_id,
        "event_id": str(item.get("uuid") or message.get("id") or "") or None,
        "parent_event_id": str(item.get("parentUuid") or "") or None,
        "related_user_event_hash": None,
        "role": role,
        "event_type": "message",
        "event_timestamp": str(item.get("timestamp") or "") or None,
        "cwd": str(item.get("cwd") or source_file.cwd or "") or None,
        "content": content,
        "evidence_content_complete": True,
    }]


def extract_events(
    item: Mapping[str, Any],
    source_file: SourceFile,
) -> List[Dict[str, Any]]:
    if source_file.source == "codex":
        return extract_codex_events(item, source_file)
    if source_file.source == "claude":
        return extract_claude_events(item, source_file)
    return []


def extract_event(
    item: Mapping[str, Any],
    source_file: SourceFile,
) -> Optional[Dict[str, Any]]:
    """Compatibility wrapper for callers that only expect one event."""

    events = extract_events(item, source_file)
    return events[0] if events else None


def _codex_content_is_known_noise(content: Any) -> bool:
    if isinstance(content, str):
        return not content.strip() or generated_codex_context(content)
    if not isinstance(content, list):
        return False
    if not content:
        return True
    for block in content:
        if not isinstance(block, dict):
            return False
        block_type = block.get("type")
        if block_type not in ("input_text", "output_text", "text"):
            return False
        text = block.get("text")
        if not isinstance(text, str):
            return False
        if text.strip() and not generated_codex_context(text):
            return False
    return True


def _claude_content_is_known_noise(content: Any) -> bool:
    if isinstance(content, str):
        return not content.strip()
    if not isinstance(content, list):
        return False
    if not content:
        return True
    known_non_text = {"thinking", "tool_result", "tool_use", "fallback"}
    for block in content:
        if not isinstance(block, dict):
            return False
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str) or text.strip():
                return False
        elif block_type not in known_non_text:
            return False
    return True


def classify_record(
    item: Mapping[str, Any],
    source_file: SourceFile,
) -> Tuple[str, Optional[Any], Optional[str]]:
    """Classify one complete JSON object without silently discarding new shapes.

    `ignored` is reserved for formats that are already understood to be
    metadata/tool noise.  Anything that might be a new message representation
    is quarantined as `unsupported`, preserving its exact bytes for a later
    parser version.
    """
    events = extract_events(item, source_file)
    if len(events) == 1:
        return "event", events[0], None
    if events:
        return "events", events, None

    if source_file.source == "codex":
        item_type = item.get("type")
        if item_type in (
            "session_meta",
            "turn_context",
            "event_msg",
            "world_state",
            "compacted",
            "inter_agent_communication_metadata",
        ):
            return "ignored", None, None
        if item_type != "response_item":
            return "unsupported", None, "codex-unknown-record-type"
        payload = item.get("payload")
        if not isinstance(payload, dict):
            return "unsupported", None, "codex-response-payload-invalid"
        payload_type = payload.get("type")
        if payload_type in CODEX_TOOL_RESULT_TYPES:
            return "ignored", None, None
        if payload_type != "message":
            if payload_type == "agent_message":
                return "ignored", None, None
            if (
                payload.get("role") in ("user", "assistant")
                or "message" in str(payload_type or "").lower()
            ):
                return "unsupported", None, "codex-new-message-shape"
            return "ignored", None, None
        role = payload.get("role")
        if role not in ("user", "assistant"):
            return "ignored", None, None
        if _codex_content_is_known_noise(payload.get("content")):
            return "ignored", None, None
        return "unsupported", None, "codex-message-content-unsupported"

    if source_file.source == "claude":
        item_type = item.get("type")
        if item_type in ("user", "assistant"):
            if item.get("isMeta") is True:
                return "ignored", None, None
            message = item.get("message")
            if not isinstance(message, dict):
                return "unsupported", None, "claude-message-invalid"
            role = message.get("role") or item_type
            if role not in ("user", "assistant"):
                return "unsupported", None, "claude-message-role-unsupported"
            if _claude_content_is_known_noise(message.get("content")):
                return "ignored", None, None
            return "unsupported", None, "claude-message-content-unsupported"
        if item_type in (
            "system",
            "summary",
            "progress",
            "file-history-snapshot",
            "queue-operation",
            "last-prompt",
            "attachment",
            "ai-title",
            "mode",
            "started",
            "result",
            "custom-title",
            "frame-link",
        ):
            return "ignored", None, None
        return "unsupported", None, "claude-unknown-record-type"

    return "unsupported", None, "unknown-source"


def unparsed_record_hash(
    source_file: SourceFile,
    device_id: str,
    source_offset: int,
    raw_record: bytes,
) -> str:
    digest = hashlib.sha256()
    for value in (
        device_id,
        source_file.source,
        str(source_file.path),
        str(source_offset),
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    digest.update(raw_record)
    return digest.hexdigest()


def record_unparsed(
    connection: sqlite3.Connection,
    source_file: SourceFile,
    device_id: str,
    source_offset: int,
    source_line: int,
    raw_record: bytes,
    reason: str,
) -> bool:
    timestamp = utc_now()
    record_hash = unparsed_record_hash(
        source_file, device_id, source_offset, raw_record
    )
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO unparsed_source_records(
            record_hash, source_device_id, source, source_path, source_offset,
            source_line, thread_id, reason, raw_record, parser_version, status,
            first_seen_at, last_checked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            record_hash,
            device_id,
            source_file.source,
            str(source_file.path),
            source_offset,
            source_line,
            source_file.thread_id or None,
            reason,
            sqlite3.Binary(raw_record),
            UNPARSED_PARSER_VERSION,
            timestamp,
            timestamp,
        ),
    )
    return cursor.rowcount == 1


def replay_unparsed_records(
    connection: sqlite3.Connection,
    source: str,
    device_id: str,
) -> Dict[str, int]:
    """Retry quarantined records once per parser version, in bounded chunks."""
    sources = ("codex", "claude") if source == "all" else (source,)
    placeholders = ",".join("?" for _ in sources)
    rows = list(
        connection.execute(
            """
            SELECT id, source, source_path, source_offset, source_line,
                   thread_id, raw_record
            FROM unparsed_source_records
            WHERE source_device_id = ?
              AND source IN (%s)
              AND status = 'pending'
              AND parser_version < ?
            ORDER BY id
            LIMIT ?
            """ % placeholders,
            (device_id, *sources, UNPARSED_PARSER_VERSION, UNPARSED_REPLAY_LIMIT),
        )
    )
    retried = 0
    resolved = 0
    ignored = 0
    for row in rows:
        retried += 1
        raw_record = bytes(row["raw_record"])
        reason = "invalid-json-or-utf8"
        classification = "unsupported"
        classified_value: Optional[Any] = None
        text: Optional[str] = None
        try:
            text = raw_record.decode("utf-8")
            item = json.loads(text)
            if isinstance(item, dict):
                source_file = SourceFile(
                    source=str(row["source"]),
                    path=Path(str(row["source_path"])),
                    thread_id=str(row["thread_id"] or ""),
                )
                classification, classified_value, classified_reason = classify_record(
                    item, source_file
                )
                reason = classified_reason or "known-non-message"
            else:
                reason = "json-not-object"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

        timestamp = utc_now()
        events = normalized_classified_events(classification, classified_value)
        if events and text is not None:
            resolved_hashes: List[str] = []
            related_user_hash: Optional[str] = None
            for event in events:
                if event.get("event_type") == TOOL_RESULT_EVIDENCE_TYPE:
                    if related_user_hash is None:
                        related_user_hash = nearest_user_event_hash(
                            connection,
                            source_file,
                            device_id,
                            int(row["source_offset"]),
                            str(event.get("thread_id") or ""),
                        )
                    event["related_user_event_hash"] = related_user_hash
                event_hash = canonical_event_hash(event)
                insert_event(
                    connection,
                    event,
                    event_hash,
                    device_id,
                    str(row["source_path"]),
                    int(row["source_offset"]),
                    int(row["source_line"]),
                    text,
                )
                resolved_hashes.append(event_hash)
            connection.execute(
                """
                UPDATE unparsed_source_records
                SET status='resolved', resolved_event_hash=?, parser_version=?,
                    reason='resolved', last_checked_at=?, resolved_at=?
                WHERE id=?
                """,
                (
                    resolved_hashes[0],
                    UNPARSED_PARSER_VERSION,
                    timestamp,
                    timestamp,
                    int(row["id"]),
                ),
            )
            resolved += 1
        elif classification == "ignored":
            connection.execute(
                """
                UPDATE unparsed_source_records
                SET status='ignored', parser_version=?, reason=?,
                    last_checked_at=?, resolved_at=?
                WHERE id=?
                """,
                (
                    UNPARSED_PARSER_VERSION,
                    reason,
                    timestamp,
                    timestamp,
                    int(row["id"]),
                ),
            )
            ignored += 1
        else:
            connection.execute(
                """
                UPDATE unparsed_source_records
                SET parser_version=?, reason=?, last_checked_at=?
                WHERE id=?
                """,
                (
                    UNPARSED_PARSER_VERSION,
                    reason,
                    timestamp,
                    int(row["id"]),
                ),
            )
    connection.commit()
    return {"retried": retried, "resolved": resolved, "ignored": ignored}


def unparsed_counts(
    connection: sqlite3.Connection,
    source: str,
    device_id: str,
) -> Dict[str, int]:
    sources = ("codex", "claude") if source == "all" else (source,)
    placeholders = ",".join("?" for _ in sources)
    counts = {"pending": 0, "resolved": 0, "ignored": 0}
    for status, count in connection.execute(
        """
        SELECT status, count(*)
        FROM unparsed_source_records
        WHERE source_device_id = ? AND source IN (%s)
        GROUP BY status
        """ % placeholders,
        (device_id, *sources),
    ):
        if status in counts:
            counts[str(status)] = int(count)
    return counts


def canonical_event_hash(event: Mapping[str, Any]) -> str:
    event_id = event.get("event_id")
    identity: Dict[str, Any] = {
        "version": EVENT_HASH_VERSION,
        "source": event.get("source"),
        "thread_id": event.get("thread_id"),
        "role": event.get("role"),
        "event_type": event.get("event_type"),
    }
    if event_id:
        identity["event_id"] = event_id
    else:
        identity.update({
            "event_timestamp": event.get("event_timestamp"),
            "content": event.get("content"),
        })
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalized_classified_events(
    classification: str,
    value: Optional[Any],
) -> List[Dict[str, Any]]:
    if classification == "event" and isinstance(value, Mapping):
        return [dict(value)]
    if classification == "events" and isinstance(value, list):
        return [dict(event) for event in value if isinstance(event, Mapping)]
    return []


def nearest_user_event_hash(
    connection: sqlite3.Connection,
    source_file: SourceFile,
    device_id: str,
    before_offset: int,
    thread_id: Optional[str] = None,
) -> Optional[str]:
    row = connection.execute(
        """
        SELECT event_hash
        FROM source_events
        WHERE source_device_id=?
          AND source=?
          AND source_path=?
          AND thread_id=?
          AND role='user'
          AND event_type='message'
          AND source_offset<?
        ORDER BY source_offset DESC, id DESC
        LIMIT 1
        """,
        (
            device_id,
            source_file.source,
            str(source_file.path),
            thread_id or source_file.thread_id or source_file.path.stem,
            before_offset,
        ),
    ).fetchone()
    return str(row[0]) if row else None


def record_relation_observation(
    connection: sqlite3.Connection,
    event_hash: str,
    related_user_event_hash: str,
    device_id: str,
    source_path: str,
    source_offset: int,
    source_line: int,
    timestamp: str,
    provenance_kind: str,
) -> Tuple[bool, bool, int]:
    """Append one provenance record and maintain its candidate projection.

    The observation cursor is independent from ``event_observations``: a later
    relation for an already-exported canonical event must still become a bundle
    delta.  The aggregate candidate table remains a deterministic projection
    used by semantic selection and conflict counts.
    """

    if (
        not re.fullmatch(r"[0-9a-f]{64}", related_user_event_hash or "")
        or provenance_kind not in RELATION_PROVENANCE_KINDS
    ):
        raise IngestError("invalid-related-user-relation")
    if connection.execute(
        "SELECT 1 FROM source_events WHERE event_hash=?", (event_hash,)
    ).fetchone() is None:
        raise IngestError("relation-event-missing")
    candidate = connection.execute(
        """
        INSERT OR IGNORE INTO source_event_user_relations(
            event_hash,related_user_event_hash,first_seen_at
        ) VALUES (?,?,?)
        """,
        (event_hash, related_user_event_hash, timestamp),
    )
    observation = connection.execute(
        """
        INSERT OR IGNORE INTO source_event_user_relation_observations(
            event_hash,related_user_event_hash,source_device_id,source_path,
            source_offset,source_line,provenance_kind,first_seen_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            event_hash,
            related_user_event_hash,
            device_id,
            source_path,
            int(source_offset),
            int(source_line),
            provenance_kind,
            timestamp,
        ),
    )
    relation_count = int(
        connection.execute(
            """
            SELECT count(*) FROM source_event_user_relations
            WHERE event_hash=?
            """,
            (event_hash,),
        ).fetchone()[0]
    )
    return observation.rowcount == 1, candidate.rowcount == 1, relation_count


def insert_event(
    connection: sqlite3.Connection,
    event: Mapping[str, Any],
    event_hash: str,
    device_id: str,
    source_path: str,
    source_offset: int,
    source_line: int,
    raw_json: str,
    ingested_at: Optional[str] = None,
    relation_provenance_kind: str = "source-history",
) -> bool:
    timestamp = ingested_at or utc_now()
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO source_events(
            event_hash, source_device_id, source, source_path, source_offset,
            source_line, thread_id, event_id, parent_event_id,
            related_user_event_hash, role, event_type, event_timestamp, cwd,
            content, evidence_content_complete, raw_json, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_hash,
            device_id,
            event["source"],
            source_path,
            source_offset,
            source_line,
            event["thread_id"],
            event.get("event_id"),
            event.get("parent_event_id"),
            event.get("related_user_event_hash"),
            event["role"],
            event["event_type"],
            event.get("event_timestamp"),
            event.get("cwd"),
            event["content"],
            int(bool(event.get("evidence_content_complete", True))),
            raw_json,
            timestamp,
        ),
    )
    inserted = cursor.rowcount == 1
    incoming_relation = event.get("related_user_event_hash")
    candidate_inserted = False
    relation_count = 0
    if incoming_relation is not None:
        _, candidate_inserted, relation_count = record_relation_observation(
            connection,
            event_hash,
            str(incoming_relation),
            device_id,
            source_path,
            source_offset,
            source_line,
            timestamp,
            relation_provenance_kind,
        )
    if not inserted:
        existing = connection.execute(
            """
            SELECT source, thread_id, event_id, role, event_type, content,
                   related_user_event_hash, evidence_content_complete
            FROM source_events WHERE event_hash = ?
            """,
            (event_hash,),
        ).fetchone()
        expected = (
            event["source"],
            event["thread_id"],
            event.get("event_id"),
            event["role"],
            event["event_type"],
            event["content"],
        )
        if existing is None or tuple(existing[:6]) != expected:
            raise IngestError("event-hash-conflict")
        existing_relation = existing["related_user_event_hash"]
        semantic_changed = False
        if incoming_relation is None:
            relation_count = int(
                connection.execute(
                    """
                    SELECT count(*) FROM source_event_user_relations
                    WHERE event_hash=?
                    """,
                    (event_hash,),
                ).fetchone()[0]
            )
        if relation_count > 1:
            # Forked/replayed histories can attach the same immutable result to
            # different preceding user messages. Preserve both observations and
            # remove only the single-value projection. Content completeness is
            # orthogonal: ambiguity is derived from candidate count.
            changed = connection.execute(
                """
                UPDATE source_events
                SET related_user_event_hash=NULL
                WHERE event_hash=? AND related_user_event_hash IS NOT NULL
                """,
                (event_hash,),
            )
            semantic_changed = bool(candidate_inserted or changed.rowcount > 0)
        elif existing_relation is None and incoming_relation is not None:
            connection.execute(
                """
                UPDATE source_events
                SET related_user_event_hash=?
                WHERE event_hash=? AND related_user_event_hash IS NULL
                """,
                (incoming_relation, event_hash),
            )
            semantic_changed = True
        existing_complete = int(existing["evidence_content_complete"])
        incoming_complete = int(
            bool(event.get("evidence_content_complete", True))
        )
        if existing_complete == 1 and incoming_complete == 0:
            # Completeness is a fail-closed, monotonic projection. A newer
            # parser or another observation may discover hidden binary data;
            # that may downgrade exactness, but no later observation may
            # promote an incomplete event back to byte-exact evidence.
            connection.execute(
                """
                UPDATE source_events
                SET evidence_content_complete=0
                WHERE event_hash=? AND evidence_content_complete=1
                """,
                (event_hash,),
            )
            semantic_changed = True
        if semantic_changed:
            connection.execute(
                """
                UPDATE source_events
                SET semantic_revision=semantic_revision+1, ingested_at=?
                WHERE event_hash=?
                """,
                (timestamp, event_hash),
            )
            revision = int(
                connection.execute(
                    "SELECT semantic_revision FROM source_events WHERE event_hash=?",
                    (event_hash,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO semantic_evidence_changes(
                    event_hash,semantic_revision,changed_at
                ) VALUES (?,?,?)
                """,
                (event_hash, revision, timestamp),
            )
    elif (
        event["role"] == "assistant"
        and event["event_type"] == TOOL_RESULT_EVIDENCE_TYPE
        and event.get("related_user_event_hash") is not None
    ):
        connection.execute(
            """
            INSERT OR IGNORE INTO semantic_evidence_changes(
                event_hash,semantic_revision,changed_at
            ) VALUES (?,1,?)
            """,
            (event_hash, timestamp),
        )
    connection.execute(
        """
        INSERT OR IGNORE INTO event_observations(
            event_hash, source_device_id, first_seen_at
        ) VALUES (?, ?, ?)
        """,
        (event_hash, device_id, timestamp),
    )
    return inserted


def attach_relation_observation(
    connection: sqlite3.Connection,
    event_hash: str,
    related_user_event_hash: str,
    device_id: str,
    source_path: str,
    source_offset: int,
    source_line: int,
    timestamp: str,
    provenance_kind: str,
) -> Tuple[bool, bool]:
    """Attach an independently transported relation observation."""

    observation_inserted, candidate_inserted, relation_count = (
        record_relation_observation(
            connection,
            event_hash,
            related_user_event_hash,
            device_id,
            source_path,
            source_offset,
            source_line,
            timestamp,
            provenance_kind,
        )
    )
    if not candidate_inserted:
        return observation_inserted, False
    event = connection.execute(
        """
        SELECT related_user_event_hash,role,event_type
        FROM source_events WHERE event_hash=?
        """,
        (event_hash,),
    ).fetchone()
    if event is None:
        raise IngestError("relation-event-missing")
    if relation_count > 1:
        connection.execute(
            """
            UPDATE source_events SET related_user_event_hash=NULL
            WHERE event_hash=?
            """,
            (event_hash,),
        )
    else:
        connection.execute(
            """
            UPDATE source_events SET related_user_event_hash=?
            WHERE event_hash=?
            """,
            (related_user_event_hash, event_hash),
        )
    connection.execute(
        """
        UPDATE source_events
        SET semantic_revision=semantic_revision+1,ingested_at=?
        WHERE event_hash=?
        """,
        (timestamp, event_hash),
    )
    if (
        event["role"] == "assistant"
        and event["event_type"] == TOOL_RESULT_EVIDENCE_TYPE
    ):
        revision = int(
            connection.execute(
                "SELECT semantic_revision FROM source_events WHERE event_hash=?",
                (event_hash,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO semantic_evidence_changes(
                event_hash,semantic_revision,changed_at
            ) VALUES (?,?,?)
            """,
            (event_hash, revision, timestamp),
        )
    return observation_inserted, True


def scan_file(
    connection: sqlite3.Connection,
    source_file: SourceFile,
    device_id: str,
    checkpoint_base: Path,
    max_lines: Optional[int] = None,
) -> Dict[str, int]:
    counters = {
        "files": 1,
        "lines": 0,
        "budget_exhausted": 0,
        "accepted": 0,
        "inserted": 0,
        "duplicates": 0,
        "evidence": 0,
        "evidence_incomplete": 0,
        "malformed": 0,
        "unsupported": 0,
        "ignored": 0,
        "unparsed_new": 0,
        "unparsed_duplicates": 0,
        "incomplete": 0,
    }
    cp_path = checkpoint_path(checkpoint_base, device_id, source_file)
    loaded_checkpoint = load_checkpoint(cp_path)
    offset, line_number = resume_position(source_file, loaded_checkpoint)
    last_user_by_thread: Dict[str, str] = {}
    if offset and loaded_checkpoint:
        stored_links = loaded_checkpoint.get("last_user_event_hashes")
        if isinstance(stored_links, dict):
            for thread_id, event_hash in stored_links.items():
                if (
                    isinstance(thread_id, str)
                    and isinstance(event_hash, str)
                    and re.fullmatch(r"[0-9a-f]{64}", event_hash)
                ):
                    last_user_by_thread[thread_id] = event_hash
    initial_stat = source_file.path.stat()
    safe_offset = offset
    pending = 0
    with source_file.path.open("rb") as handle:
        handle.seek(offset)
        while True:
            if max_lines is not None and counters["lines"] >= max_lines:
                counters["budget_exhausted"] = int(handle.tell() < initial_stat.st_size)
                break
            line_start = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                counters["incomplete"] += 1
                safe_offset = line_start
                break
            next_offset = handle.tell()
            line_number += 1
            counters["lines"] += 1
            body = raw[:-1]
            if body.endswith(b"\r"):
                body = body[:-1]
            try:
                text = body.decode("utf-8")
                item = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                counters["malformed"] += 1
                if record_unparsed(
                    connection,
                    source_file,
                    device_id,
                    line_start,
                    line_number,
                    body,
                    "invalid-json-or-utf8",
                ):
                    counters["unparsed_new"] += 1
                else:
                    counters["unparsed_duplicates"] += 1
                pending += 1
                safe_offset = next_offset
                if pending >= COMMIT_INTERVAL:
                    connection.commit()
                    pending = 0
                continue
            if not isinstance(item, dict):
                counters["unsupported"] += 1
                if record_unparsed(
                    connection,
                    source_file,
                    device_id,
                    line_start,
                    line_number,
                    body,
                    "json-not-object",
                ):
                    counters["unparsed_new"] += 1
                else:
                    counters["unparsed_duplicates"] += 1
                pending += 1
                safe_offset = next_offset
                if pending >= COMMIT_INTERVAL:
                    connection.commit()
                    pending = 0
                continue
            classification, classified_value, reason = classify_record(item, source_file)
            if classification == "ignored":
                counters["ignored"] += 1
                safe_offset = next_offset
                continue
            events = normalized_classified_events(classification, classified_value)
            if not events:
                counters["unsupported"] += 1
                if record_unparsed(
                    connection,
                    source_file,
                    device_id,
                    line_start,
                    line_number,
                    body,
                    reason or "unsupported-record",
                ):
                    counters["unparsed_new"] += 1
                else:
                    counters["unparsed_duplicates"] += 1
                pending += 1
                safe_offset = next_offset
                if pending >= COMMIT_INTERVAL:
                    connection.commit()
                    pending = 0
                continue
            counters["accepted"] += len(events)
            for event in events:
                thread_id = str(event.get("thread_id") or "")
                if event.get("event_type") == TOOL_RESULT_EVIDENCE_TYPE:
                    event["related_user_event_hash"] = last_user_by_thread.get(
                        thread_id
                    )
                    counters["evidence"] += 1
                    if not bool(event.get("evidence_content_complete", True)):
                        counters["evidence_incomplete"] += 1
                event_hash = canonical_event_hash(event)
                if insert_event(
                    connection,
                    event,
                    event_hash,
                    device_id,
                    str(source_file.path),
                    line_start,
                    line_number,
                    text,
                ):
                    counters["inserted"] += 1
                else:
                    counters["duplicates"] += 1
                if (
                    event.get("role") == "user"
                    and event.get("event_type") == "message"
                    and thread_id
                ):
                    last_user_by_thread[thread_id] = event_hash
            pending += 1
            safe_offset = next_offset
            if pending >= COMMIT_INTERVAL:
                connection.commit()
                pending = 0
        opened_stat = os.fstat(handle.fileno())
    connection.commit()

    current_stat = source_file.path.stat()
    stable_file = (
        initial_stat.st_ino == opened_stat.st_ino == current_stat.st_ino
        and current_stat.st_size == opened_stat.st_size
    )
    if not stable_file:
        return counters

    prefix_length = min(current_stat.st_size, PREFIX_LIMIT)
    prefix_hash = hash_prefix(source_file.path, prefix_length)
    checkpoint = {
        "version": 1,
        "parser_version": CHECKPOINT_PARSER_VERSION,
        "source_device_id": device_id,
        "source": source_file.source,
        "path": str(source_file.path),
        "thread_id": source_file.thread_id,
        "inode": current_stat.st_ino,
        "offset": safe_offset,
        "line_number": line_number,
        "size": current_stat.st_size,
        "mtime_ns": current_stat.st_mtime_ns,
        "prefix_length": prefix_length,
        "prefix_hash": prefix_hash,
        "last_user_event_hashes": dict(sorted(last_user_by_thread.items())),
        "updated_at": utc_now(),
    }
    connection.execute(
        """
        INSERT INTO source_files(
            source_device_id, source, path, thread_id, inode, byte_offset,
            size, mtime_ns, prefix_hash, scanned_at, malformed_lines
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_device_id, source, path) DO UPDATE SET
            thread_id=excluded.thread_id,
            inode=excluded.inode,
            byte_offset=excluded.byte_offset,
            size=excluded.size,
            mtime_ns=excluded.mtime_ns,
            prefix_hash=excluded.prefix_hash,
            scanned_at=excluded.scanned_at,
            malformed_lines=source_files.malformed_lines + excluded.malformed_lines
        """,
        (
            device_id,
            source_file.source,
            str(source_file.path),
            source_file.thread_id or None,
            current_stat.st_ino,
            safe_offset,
            current_stat.st_size,
            current_stat.st_mtime_ns,
            prefix_hash,
            checkpoint["updated_at"],
            counters["malformed"],
        ),
    )
    connection.commit()
    # The checkpoint JSON below is fsynced, but with journal_mode=WAL and
    # synchronous=NORMAL the commit above is not yet power-loss durable. A
    # crash after the checkpoint became visible would permanently skip these
    # events on resume (the checkpoint says they were consumed) while the WAL
    # holding them is discarded. Force the WAL into the main database first so
    # "checkpoint visible" always implies "events durable".
    checkpoint_wal_before_cursor(connection)
    atomic_write_json(cp_path, checkpoint)
    return counters


def scan_history(
    connection: sqlite3.Connection,
    source: str,
    device_id: str,
    checkpoint_base: Path,
    home: Optional[Path] = None,
    max_lines: Optional[int] = None,
) -> Dict[str, Any]:
    if max_lines is not None and max_lines < 1:
        raise ValueError("max_lines must be positive")
    totals: Dict[str, Any] = {
        "action": "scan",
        "source": source,
        "source_device_id": device_id,
        "files": 0,
        "lines": 0,
        "budget_exhausted": 0,
        "accepted": 0,
        "inserted": 0,
        "duplicates": 0,
        "evidence": 0,
        "evidence_incomplete": 0,
        "malformed": 0,
        "unsupported": 0,
        "ignored": 0,
        "unparsed_new": 0,
        "unparsed_duplicates": 0,
        "incomplete": 0,
    }
    # Quarantined records have their own retry path; do not turn a bounded
    # ingestion run into an unbounded replay of old unsupported formats.
    replay = replay_unparsed_records(connection, source, device_id) if max_lines is None else {"retried": 0, "resolved": 0, "ignored": 0}
    for source_file in discover_sources(source, home):
        if max_lines is not None and totals["lines"] >= max_lines:
            totals["budget_exhausted"] = 1
            break
        remaining = None if max_lines is None else max_lines - totals["lines"]
        result = scan_file(connection, source_file, device_id, checkpoint_base, max_lines=remaining)
        for key in result:
            totals[key] += result[key]
    quarantine = unparsed_counts(connection, source, device_id)
    totals["unparsed_retried"] = int(replay["retried"])
    totals["unparsed_resolved"] = int(replay["resolved"])
    totals["unparsed_ignored"] = int(replay["ignored"])
    totals["unparsed_pending"] = int(quarantine["pending"])
    totals["coverage_status"] = "partial" if totals["budget_exhausted"] or totals["incomplete"] or totals["unparsed_pending"] else "scanned-discovered-sources"
    return totals


def bundle_record(
    row: sqlite3.Row,
    exporting_device_id: str,
    related_user_event_hashes: Sequence[str] = (),
) -> Dict[str, Any]:
    record = {
        "event_hash": row["event_hash"],
        "source_device_id": exporting_device_id,
        "source": row["source"],
        "source_ref": Path(row["source_path"]).name,
        "source_offset": row["source_offset"],
        "source_line": row["source_line"],
        "thread_id": row["thread_id"],
        "event_id": row["event_id"],
        "parent_event_id": row["parent_event_id"],
        "related_user_event_hash": row["related_user_event_hash"],
        "role": row["role"],
        "event_type": row["event_type"],
        "event_timestamp": row["event_timestamp"],
        "cwd": row["cwd"],
        "content": row["content"],
        "evidence_content_complete": bool(row["evidence_content_complete"]),
        "raw_json": row["raw_json"],
        "ingested_at": row["ingested_at"],
    }
    relations = sorted({str(value) for value in related_user_event_hashes if value})
    if relations:
        # Optional v1 extension: older readers retain the event; updated peers
        # also retain every observed relation instead of silently choosing one.
        record["related_user_event_hashes"] = relations
    return record


def relation_bundle_record(
    row: sqlite3.Row, exporting_device_id: str
) -> Dict[str, Any]:
    return {
        "event_hash": row["event_hash"],
        "related_user_event_hash": row["related_user_event_hash"],
        "source_device_id": exporting_device_id,
        "source_ref": Path(row["source_path"]).name,
        "source_offset": int(row["source_offset"]),
        "source_line": int(row["source_line"]),
        "provenance_kind": row["provenance_kind"],
        "first_seen_at": row["first_seen_at"],
    }


def bundle_payload_identity(events_sha256: str, relations_sha256: Optional[str]) -> str:
    """Identity used by the peer-import fast path for old and extended v1 bundles."""

    if relations_sha256 is None:
        return events_sha256
    return hashlib.sha256(
        (events_sha256 + "\0" + relations_sha256).encode("ascii")
    ).hexdigest()


def fsync_file(path: Path) -> None:
    # FlushFileBuffers, which backs os.fsync on Windows, rejects read-only
    # descriptors even when the caller only wants durability.
    flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows exposes no generally supported directory fsync operation.
        # The bundle file itself is flushed above and os.replace remains
        # atomic; attempting os.fsync on a directory/read-only handle raises
        # EBADF on NTFS.
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def export_bundle(connection: sqlite3.Connection, destination: Path, device_id: str) -> Dict[str, Any]:
    if destination.exists():
        raise IngestError("bundle-destination-exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    cursor_row = connection.execute(
        "SELECT last_observation_id FROM export_cursors WHERE source_device_id = ?",
        (device_id,),
    ).fetchone()
    cursor_from = int(cursor_row[0]) if cursor_row else 0
    relation_cursor_row = connection.execute(
        """
        SELECT last_relation_observation_id
        FROM relation_export_cursors WHERE source_device_id = ?
        """,
        (device_id,),
    ).fetchone()
    relation_cursor_from = int(relation_cursor_row[0]) if relation_cursor_row else 0
    rows = connection.execute(
        """
        SELECT o.id AS observation_id, e.*
        FROM event_observations AS o
        JOIN source_events AS e ON e.event_hash = o.event_hash
        WHERE o.source_device_id = ? AND o.id > ?
        ORDER BY o.id
        """,
        (device_id, cursor_from),
    )
    relation_rows = connection.execute(
        """
        SELECT * FROM source_event_user_relation_observations
        WHERE source_device_id=? AND id>?
        ORDER BY id
        """,
        (device_id, relation_cursor_from),
    )
    temporary = destination.with_name(".%s.%s.tmp" % (destination.name, uuid.uuid4().hex))
    payload_hash = hashlib.sha256()
    relation_payload_hash = hashlib.sha256()
    event_count = 0
    relation_observation_count = 0
    cursor_to = cursor_from
    relation_cursor_to = relation_cursor_from
    bundle_id = str(uuid.uuid4())
    try:
        with zipfile.ZipFile(
            str(temporary), "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            with archive.open("events.jsonl", "w") as member:
                for row in rows:
                    record = bundle_record(row, device_id)
                    encoded = (
                        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    ).encode("utf-8")
                    member.write(encoded)
                    payload_hash.update(encoded)
                    event_count += 1
                    cursor_to = int(row["observation_id"])
            with archive.open("relations.jsonl", "w") as member:
                for row in relation_rows:
                    record = relation_bundle_record(row, device_id)
                    encoded = (
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                    member.write(encoded)
                    relation_payload_hash.update(encoded)
                    relation_observation_count += 1
                    relation_cursor_to = int(row["id"])
            manifest = {
                "format": BUNDLE_FORMAT,
                "bundle_id": bundle_id,
                "created_at": utc_now(),
                "source_device_id": device_id,
                "event_count": event_count,
                "cursor_from": cursor_from,
                "cursor_to": cursor_to,
                "events_sha256": payload_hash.hexdigest(),
                "relation_observation_count": relation_observation_count,
                "relation_cursor_from": relation_cursor_from,
                "relation_cursor_to": relation_cursor_to,
                "relations_sha256": relation_payload_hash.hexdigest(),
            }
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
        os.replace(str(temporary), str(destination))
        # The export cursor is a promise that every preceding observation has a
        # durable bundle.  Persist both the renamed file and its directory entry
        # before making that promise in SQLite.  If either fsync fails, the
        # cursor remains unchanged and a later export can safely include the
        # same observations again; bundle imports are idempotent.
        fsync_file(destination)
        fsync_directory(destination.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    try:
        connection.execute(
            """
            INSERT INTO export_cursors(source_device_id, last_observation_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source_device_id) DO UPDATE SET
                last_observation_id=excluded.last_observation_id,
                updated_at=excluded.updated_at
            """,
            (device_id, cursor_to, utc_now()),
        )
        connection.execute(
            """
            INSERT INTO relation_export_cursors(
                source_device_id,last_relation_observation_id,updated_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(source_device_id) DO UPDATE SET
                last_relation_observation_id=excluded.last_relation_observation_id,
                updated_at=excluded.updated_at
            """,
            (device_id, relation_cursor_to, utc_now()),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "action": "export-bundle",
        "source_device_id": device_id,
        "bundle_id": bundle_id,
        "event_count": event_count,
        "cursor_from": cursor_from,
        "cursor_to": cursor_to,
        "relation_observation_count": relation_observation_count,
        "relation_cursor_from": relation_cursor_from,
        "relation_cursor_to": relation_cursor_to,
        "path": str(destination),
    }


def event_from_bundle(record: Mapping[str, Any]) -> Dict[str, Any]:
    required = ("source", "thread_id", "role", "event_type", "content")
    if any(key not in record for key in required):
        raise IngestError("bundle-event-missing-field")
    if record.get("source") not in ("codex", "claude"):
        raise IngestError("bundle-event-invalid-source")
    if record.get("role") not in ("user", "assistant"):
        raise IngestError("bundle-event-invalid-role")
    if (
        not isinstance(record.get("content"), str)
        or not isinstance(record.get("thread_id"), str)
        or not isinstance(record.get("event_type"), str)
    ):
        raise IngestError("bundle-event-invalid-content")
    related_user_event_hash = record.get("related_user_event_hash")
    if related_user_event_hash is not None and (
        not isinstance(related_user_event_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", related_user_event_hash)
    ):
        raise IngestError("bundle-event-invalid-related-user")
    relation_values = record.get("related_user_event_hashes")
    if relation_values is None:
        relations = (
            [related_user_event_hash]
            if isinstance(related_user_event_hash, str)
            else []
        )
    elif not isinstance(relation_values, list) or any(
        not isinstance(value, str)
        or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in relation_values
    ):
        raise IngestError("bundle-event-invalid-related-user")
    else:
        relations = sorted(set(relation_values))
        if (
            isinstance(related_user_event_hash, str)
            and related_user_event_hash not in relations
        ):
            raise IngestError("bundle-event-invalid-related-user")
    evidence_content_complete = record.get("evidence_content_complete", True)
    if not isinstance(evidence_content_complete, bool):
        raise IngestError("bundle-event-invalid-evidence-completeness")
    event = {
        "source": record["source"],
        "thread_id": record["thread_id"],
        "event_id": str(record.get("event_id") or "") or None,
        "parent_event_id": str(record.get("parent_event_id") or "") or None,
        "related_user_event_hash": related_user_event_hash,
        "role": record["role"],
        "event_type": record["event_type"],
        "event_timestamp": record.get("event_timestamp"),
        "cwd": record.get("cwd"),
        "content": record["content"],
        "evidence_content_complete": evidence_content_complete,
    }
    event["_related_user_event_hashes"] = relations
    return event


def relation_observation_from_bundle(record: Mapping[str, Any]) -> Dict[str, Any]:
    event_hash = record.get("event_hash")
    relation_hash = record.get("related_user_event_hash")
    source_device_id = record.get("source_device_id")
    source_ref = record.get("source_ref")
    provenance_kind = record.get("provenance_kind")
    first_seen_at = record.get("first_seen_at")
    if (
        not isinstance(event_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", event_hash)
        or not isinstance(relation_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", relation_hash)
        or not isinstance(source_device_id, str)
        or not source_device_id
        or not isinstance(source_ref, str)
        or not source_ref
        or "/" in source_ref
        or "\\" in source_ref
        or provenance_kind not in RELATION_PROVENANCE_KINDS
        or not isinstance(first_seen_at, str)
        or not first_seen_at
    ):
        raise IngestError("invalid-bundle-relation-observation")
    try:
        source_offset = int(record.get("source_offset"))
        source_line = int(record.get("source_line"))
    except (TypeError, ValueError) as error:
        raise IngestError("invalid-bundle-relation-observation") from error
    if source_offset < 0 or source_line < 0:
        raise IngestError("invalid-bundle-relation-observation")
    return {
        "event_hash": event_hash,
        "related_user_event_hash": relation_hash,
        "source_device_id": source_device_id,
        "source_ref": source_ref,
        "source_offset": source_offset,
        "source_line": source_line,
        "provenance_kind": provenance_kind,
        "first_seen_at": first_seen_at,
    }


def import_bundle(connection: sqlite3.Connection, bundle_path: Path) -> Dict[str, Any]:
    if not bundle_path.is_file():
        raise IngestError("bundle-not-found")
    with zipfile.ZipFile(str(bundle_path), "r") as archive:
        names = set(archive.namelist())
        if not {"manifest.json", "events.jsonl"}.issubset(names):
            raise IngestError("invalid-bundle-layout")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IngestError("invalid-bundle-manifest") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT:
            raise IngestError("unsupported-bundle-format")
        relation_fields = (
            "relation_observation_count",
            "relation_cursor_from",
            "relation_cursor_to",
            "relations_sha256",
        )
        relation_extension = "relations.jsonl" in names or any(
            field in manifest for field in relation_fields
        )
        if relation_extension and (
            "relations.jsonl" not in names
            or any(field not in manifest for field in relation_fields)
        ):
            raise IngestError("invalid-bundle-layout")
        bundle_id = str(manifest.get("bundle_id") or "")
        source_device_id = str(manifest.get("source_device_id") or "")
        if not bundle_id or not source_device_id:
            raise IngestError("invalid-bundle-identity")
        existing = connection.execute(
            "SELECT 1 FROM imported_bundles WHERE bundle_id = ?", (bundle_id,)
        ).fetchone()
        if existing:
            return {
                "action": "import-bundle",
                "bundle_id": bundle_id,
                "source_device_id": source_device_id,
                "already_imported": True,
                "events": 0,
                "inserted": 0,
                "duplicates": 0,
                "relation_observations": 0,
                "relation_observations_inserted": 0,
                "relation_candidates_inserted": 0,
            }

        payload_hash = hashlib.sha256()
        relation_payload_hash = hashlib.sha256()
        event_count = 0
        inserted = 0
        duplicates = 0
        relation_observation_count = 0
        relation_observations_inserted = 0
        relation_candidates_inserted = 0
        connection.execute("BEGIN IMMEDIATE")
        try:
            with archive.open("events.jsonl", "r") as member:
                while True:
                    raw = member.readline()
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        raise IngestError("incomplete-bundle-event")
                    payload_hash.update(raw)
                    try:
                        record = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise IngestError("invalid-bundle-event") from exc
                    if not isinstance(record, dict):
                        raise IngestError("invalid-bundle-event")
                    if str(record.get("source_device_id") or "") != source_device_id:
                        raise IngestError("bundle-device-mismatch")
                    event = event_from_bundle(record)
                    relations = list(
                        event.pop("_related_user_event_hashes", [])
                    )
                    if relation_extension:
                        # The independent provenance stream is authoritative.
                        # Avoid inventing an observation at the canonical
                        # event's first-seen location for a later candidate.
                        event["related_user_event_hash"] = None
                        relations = []
                    elif event.get("related_user_event_hash") is None and relations:
                        event["related_user_event_hash"] = relations[0]
                    event_hash = canonical_event_hash(event)
                    if event_hash != record.get("event_hash"):
                        raise IngestError("bundle-event-hash-mismatch")
                    source_ref = Path(str(record.get("source_ref") or "unknown.jsonl")).name
                    logical_path = "bundle:%s:%s" % (source_device_id, source_ref)
                    was_inserted = insert_event(
                        connection,
                        event,
                        event_hash,
                        source_device_id,
                        logical_path,
                        int(record.get("source_offset") or 0),
                        int(record.get("source_line") or 0),
                        str(record.get("raw_json") or ""),
                        str(record.get("ingested_at") or utc_now()),
                        relation_provenance_kind="bundle-event",
                    )
                    primary_relation = event.get("related_user_event_hash")
                    for relation in relations:
                        if relation == primary_relation:
                            continue
                        replay = dict(event, related_user_event_hash=relation)
                        insert_event(
                            connection,
                            replay,
                            event_hash,
                            source_device_id,
                            logical_path,
                            int(record.get("source_offset") or 0),
                            int(record.get("source_line") or 0),
                            str(record.get("raw_json") or ""),
                            str(record.get("ingested_at") or utc_now()),
                            relation_provenance_kind="bundle-event",
                        )
                    inserted += int(was_inserted)
                    duplicates += int(not was_inserted)
                    event_count += 1
            if relation_extension:
                with archive.open("relations.jsonl", "r") as member:
                    while True:
                        raw = member.readline()
                        if not raw:
                            break
                        if not raw.endswith(b"\n"):
                            raise IngestError("incomplete-bundle-relation")
                        relation_payload_hash.update(raw)
                        try:
                            value = json.loads(raw.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise IngestError("invalid-bundle-relation-observation") from exc
                        if not isinstance(value, dict):
                            raise IngestError("invalid-bundle-relation-observation")
                        relation = relation_observation_from_bundle(value)
                        if relation["source_device_id"] != source_device_id:
                            raise IngestError("bundle-device-mismatch")
                        logical_path = "bundle:%s:%s" % (
                            source_device_id,
                            relation["source_ref"],
                        )
                        try:
                            observation_added, candidate_added = (
                                attach_relation_observation(
                                    connection,
                                    relation["event_hash"],
                                    relation["related_user_event_hash"],
                                    source_device_id,
                                    logical_path,
                                    relation["source_offset"],
                                    relation["source_line"],
                                    relation["first_seen_at"],
                                    relation["provenance_kind"],
                                )
                            )
                        except IngestError as error:
                            if str(error) == "relation-event-missing":
                                raise IngestError("bundle-relation-event-missing") from error
                            raise
                        relation_observation_count += 1
                        relation_observations_inserted += int(observation_added)
                        relation_candidates_inserted += int(candidate_added)
            if event_count != int(manifest.get("event_count", -1)):
                raise IngestError("bundle-count-mismatch")
            digest = payload_hash.hexdigest()
            if digest != manifest.get("events_sha256"):
                raise IngestError("bundle-payload-hash-mismatch")
            relation_digest: Optional[str] = None
            if relation_extension:
                if relation_observation_count != int(
                    manifest.get("relation_observation_count", -1)
                ):
                    raise IngestError("bundle-relation-count-mismatch")
                relation_digest = relation_payload_hash.hexdigest()
                if relation_digest != manifest.get("relations_sha256"):
                    raise IngestError("bundle-relation-payload-hash-mismatch")
            payload_identity = bundle_payload_identity(digest, relation_digest)
            connection.execute(
                """
                INSERT INTO imported_bundles(
                    bundle_id, source_device_id, event_count, imported_at, payload_sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    bundle_id,
                    source_device_id,
                    event_count,
                    utc_now(),
                    payload_identity,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "action": "import-bundle",
        "bundle_id": bundle_id,
        "source_device_id": source_device_id,
        "already_imported": False,
        "events": event_count,
        "inserted": inserted,
        "duplicates": duplicates,
        "relation_observations": relation_observation_count,
        "relation_observations_inserted": relation_observations_inserted,
        "relation_candidates_inserted": relation_candidates_inserted,
    }


def checkpoint_coverage(
    source_rows: Sequence[sqlite3.Row],
    checkpoint_base: Path,
    device_id: str,
) -> Dict[str, Any]:
    """Return body-free proof that this device's source files reached checkpoints."""
    sources = ("codex", "claude")
    by_source: Dict[str, Dict[str, int]] = {
        source: {
            "source_files": 0,
            "valid_checkpoint_files": 0,
            "covered_source_files": 0,
            "fully_consumed_source_files": 0,
            "source_bytes": 0,
            "checkpointed_bytes": 0,
        }
        for source in sources
    }
    source_records: Dict[Tuple[str, str], sqlite3.Row] = {}
    alias_source_files = 0
    alias_source_divergences = 0
    for row in source_rows:
        source = str(row["source"])
        path = str(row["path"])
        if source not in by_source:
            continue
        key = (source, source_path_identity(path))
        previous = source_records.get(key)
        if previous is not None:
            alias_source_files += 1
            if (
                int(previous["byte_offset"]) != int(row["byte_offset"])
                or int(previous["size"]) != int(row["size"])
            ):
                alias_source_divergences += 1
            previous_rank = (
                str(previous["scanned_at"]),
                int(previous["byte_offset"]),
                int(previous["size"]),
            )
            current_rank = (
                str(row["scanned_at"]),
                int(row["byte_offset"]),
                int(row["size"]),
            )
            if current_rank > previous_rank:
                source_records[key] = row
                by_source[source]["source_bytes"] += (
                    max(0, int(row["size"]))
                    - max(0, int(previous["size"]))
                )
            continue
        source_records[key] = row
        by_source[source]["source_files"] += 1
        by_source[source]["source_bytes"] += max(0, int(row["size"]))

    checkpoint_root = checkpoint_base / device_dir_name(device_id)
    checkpoint_paths = (
        sorted(checkpoint_root.rglob("*.json")) if checkpoint_root.is_dir() else []
    )
    checkpoints: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    valid_checkpoint_files = 0
    invalid_checkpoint_files = 0
    alias_checkpoint_files = 0
    alias_checkpoint_divergences = 0
    for path in checkpoint_paths:
        value = load_checkpoint(path)
        if not value or value.get("source_device_id") != device_id:
            invalid_checkpoint_files += 1
            continue
        source = value.get("source")
        source_path = value.get("path")
        if source not in sources or not isinstance(source_path, str) or not source_path:
            invalid_checkpoint_files += 1
            continue
        try:
            offset = int(value["offset"])
            size = int(value["size"])
        except (KeyError, TypeError, ValueError):
            invalid_checkpoint_files += 1
            continue
        if offset < 0 or size < 0 or offset > size:
            invalid_checkpoint_files += 1
            continue
        key = (str(source), source_path_identity(source_path))
        if key in checkpoints:
            previous = checkpoints[key]
            equivalent = (
                int(previous["offset"]) == offset
                and int(previous["size"]) == size
                and previous.get("prefix_hash") == value.get("prefix_hash")
                and previous.get("prefix_length") == value.get("prefix_length")
            )
            alias_checkpoint_files += 1
            if not equivalent:
                alias_checkpoint_divergences += 1
            previous_rank = (
                str(previous.get("updated_at") or ""),
                int(previous["offset"]),
                int(previous["size"]),
            )
            current_rank = (
                str(value.get("updated_at") or ""),
                offset,
                size,
            )
            if current_rank > previous_rank:
                checkpoints[key] = value
            continue
        checkpoints[key] = value
        valid_checkpoint_files += 1
        by_source[str(source)]["valid_checkpoint_files"] += 1

    covered_source_files = 0
    fully_consumed_source_files = 0
    checkpointed_bytes = 0
    for key, row in source_records.items():
        checkpoint = checkpoints.get(key)
        if checkpoint is None:
            continue
        try:
            checkpoint_offset = int(checkpoint["offset"])
            checkpoint_size = int(checkpoint["size"])
            database_offset = int(row["byte_offset"])
            database_size = int(row["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if checkpoint_offset != database_offset or checkpoint_size != database_size:
            continue
        source = key[0]
        covered_source_files += 1
        checkpointed_bytes += checkpoint_offset
        by_source[source]["covered_source_files"] += 1
        by_source[source]["checkpointed_bytes"] += checkpoint_offset
        if checkpoint_offset == checkpoint_size:
            fully_consumed_source_files += 1
            by_source[source]["fully_consumed_source_files"] += 1

    source_files = len(source_records)
    source_bytes = sum(item["source_bytes"] for item in by_source.values())
    orphan_checkpoint_files = len(set(checkpoints) - set(source_records))
    coverage_complete = bool(
        source_files > 0
        and covered_source_files == source_files
        and invalid_checkpoint_files == 0
        and orphan_checkpoint_files == 0
    )
    return {
        "source_files": source_files,
        "alias_source_files": alias_source_files,
        "alias_source_divergences": alias_source_divergences,
        "checkpoint_files_found": len(checkpoint_paths),
        "valid_checkpoint_files": valid_checkpoint_files,
        "alias_checkpoint_files": alias_checkpoint_files,
        "alias_checkpoint_divergences": alias_checkpoint_divergences,
        "invalid_checkpoint_files": invalid_checkpoint_files,
        "orphan_checkpoint_files": orphan_checkpoint_files,
        "covered_source_files": covered_source_files,
        "uncovered_source_files": source_files - covered_source_files,
        "fully_consumed_source_files": fully_consumed_source_files,
        "partially_consumed_source_files": (
            covered_source_files - fully_consumed_source_files
        ),
        "source_bytes": source_bytes,
        "checkpointed_bytes": checkpointed_bytes,
        "coverage_complete": coverage_complete,
        "by_source": by_source,
    }


def database_stats(
    connection: sqlite3.Connection,
    device_id: str,
    checkpoint_base: Path,
) -> Dict[str, Any]:
    """Report only observations and scan proof attributable to one device."""
    by_source = {"codex": 0, "claude": 0}
    by_role = {"user": 0, "assistant": 0}
    by_source_role = {
        source: {role: 0 for role in by_role}
        for source in by_source
    }
    for source, role, count in connection.execute(
        """
        SELECT e.source, e.role, count(*)
        FROM event_observations AS o
        JOIN source_events AS e ON e.event_hash = o.event_hash
        WHERE o.source_device_id = ?
        GROUP BY e.source, e.role
        ORDER BY e.source, e.role
        """,
        (device_id,),
    ):
        if source not in by_source or role not in by_role:
            continue
        amount = int(count)
        by_source[str(source)] += amount
        by_role[str(role)] += amount
        by_source_role[str(source)][str(role)] += amount
    evidence_row = connection.execute(
        """
        SELECT count(*),
               coalesce(sum(CASE WHEN e.evidence_content_complete=0 THEN 1 ELSE 0 END), 0)
        FROM event_observations AS o
        JOIN source_events AS e ON e.event_hash = o.event_hash
        WHERE o.source_device_id=? AND e.event_type=?
        """,
        (device_id, TOOL_RESULT_EVIDENCE_TYPE),
    ).fetchone()
    evidence_events = int(evidence_row[0]) if evidence_row else 0
    evidence_incomplete = int(evidence_row[1]) if evidence_row else 0
    relation_conflicts = int(
        connection.execute(
            """
            SELECT count(*) FROM (
                SELECT r.event_hash
                FROM source_event_user_relations AS r
                JOIN event_observations AS o ON o.event_hash=r.event_hash
                WHERE o.source_device_id=?
                GROUP BY r.event_hash
                HAVING count(DISTINCT r.related_user_event_hash)>1
            )
            """,
            (device_id,),
        ).fetchone()[0]
    )

    source_rows = list(
        connection.execute(
            """
            SELECT source, path, byte_offset, size, scanned_at
            FROM source_files
            WHERE source_device_id = ?
            ORDER BY source, path
            """,
            (device_id,),
        )
    )
    coverage = checkpoint_coverage(source_rows, checkpoint_base, device_id)
    source_files_by_source = {
        source: int(coverage["by_source"][source]["source_files"])
        for source in by_source
    }
    checkpoint_files_by_source = {
        source: int(coverage["by_source"][source]["valid_checkpoint_files"])
        for source in by_source
    }
    unparsed_pending_by_source = {"codex": 0, "claude": 0}
    for source, count in connection.execute(
        """
        SELECT source, count(*)
        FROM unparsed_source_records
        WHERE source_device_id = ? AND status = 'pending'
        GROUP BY source
        """,
        (device_id,),
    ):
        if source in unparsed_pending_by_source:
            unparsed_pending_by_source[str(source)] = int(count)
    events = sum(by_source.values())
    return {
        "action": "stats",
        "scope": "device-observations",
        "source_device_id": device_id,
        "events": events,
        "observation_events": events,
        "by_source": by_source,
        "by_role": by_role,
        "by_source_role": by_source_role,
        "evidence_events": evidence_events,
        "evidence_incomplete": evidence_incomplete,
        "evidence_relation_conflicts": relation_conflicts,
        "source_files": int(coverage["source_files"]),
        "source_files_by_source": source_files_by_source,
        "checkpoint_files": int(coverage["valid_checkpoint_files"]),
        "checkpoint_files_by_source": checkpoint_files_by_source,
        "checkpoint_coverage": coverage,
        "unparsed_pending": sum(unparsed_pending_by_source.values()),
        "unparsed_pending_by_source": unparsed_pending_by_source,
        "initial_scan_proven": bool(coverage["coverage_complete"]),
    }


def build_parser(default_db: Path, default_checkpoints: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Incrementally ingest local AI histories")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--init-db", action="store_true", help="initialize the event database")
    action.add_argument("--scan", action="store_true", help="scan new complete history lines")
    action.add_argument("--stats", action="store_true", help="print body-free database counts")
    action.add_argument("--export-bundle", metavar="PATH", help="export this device's new events")
    action.add_argument("--import-bundle", metavar="PATH", help="idempotently import a bundle")
    parser.add_argument("--max-lines", type=int, help="bounded complete source lines per run; checkpoints retain remaining backlog")
    parser.add_argument("--source", choices=("codex", "claude", "all"), default="all")
    parser.add_argument("--db", default=str(default_db), help="history SQLite path")
    parser.add_argument(
        "--checkpoint-dir", default=str(default_checkpoints), help="checkpoint root directory"
    )
    parser.add_argument("--device-id", help="stable source device id; env KB_SOURCE_DEVICE_ID also works")
    return parser


def emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: Optional[Sequence[str]] = None) -> int:
    default_db, default_checkpoints = default_locations()
    args = build_parser(default_db, default_checkpoints).parse_args(argv)
    db_path = Path(args.db).expanduser().resolve()
    checkpoint_base = Path(args.checkpoint_dir).expanduser().resolve()
    device_id = resolve_device_id(args.device_id)
    connection = initialize_database(db_path)
    try:
        if args.init_db:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='events_schema_version'"
            ).fetchone()[0]
            result: Mapping[str, Any] = {
                "action": "init-db",
                "db": str(db_path),
                "events_schema_version": int(version),
                "source_device_id": device_id,
            }
        elif args.scan:
            result = scan_history(connection, args.source, device_id, checkpoint_base, max_lines=args.max_lines)
        elif args.stats:
            stats = database_stats(connection, device_id, checkpoint_base)
            # Additive field: lets callers tell "no history sources on this
            # machine" apart from "sources exist but nothing was scanned".
            stats["discovery"] = discovery_report()
            result = stats
        elif args.export_bundle:
            result = export_bundle(connection, Path(args.export_bundle).expanduser().resolve(), device_id)
        else:
            result = import_bundle(connection, Path(args.import_bundle).expanduser().resolve())
        emit(result)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    if not os.environ.get("AI_KNOWLEDGE_ROOT") and "--db" not in sys.argv and not any(x in sys.argv for x in ("--help", "-h")):
        print(json.dumps({"ok": False, "error": "explicit-AI_KNOWLEDGE_ROOT-or-db-required"}))
        raise SystemExit(2)
    try:
        raise SystemExit(main())
    except (IngestError, OSError, sqlite3.Error, ValueError, zipfile.BadZipFile) as exc:
        # Never echo exception text: malformed history can contain message bodies.
        failure = {"ok": False, "error": exc.__class__.__name__}
        # IngestError values are fixed, body-free reason codes authored in this
        # module.  Preserve them so an automation can distinguish a repairable
        # WAL/checkpoint conflict from an opaque generic failure.
        if isinstance(exc, IngestError):
            failure["reason"] = str(exc)
        print(
            json.dumps(failure, sort_keys=True, separators=(",", ":")),
            file=sys.stderr,
        )
        raise SystemExit(2)
