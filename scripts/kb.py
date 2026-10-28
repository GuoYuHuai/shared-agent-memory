#!/usr/bin/env python3
"""Local-first knowledge base CLI.

Markdown/JSON files and the immutable ledger are the sources of truth.  The
SQLite database under .kb-data is deliberately disposable and contains no
credential value in either of its FTS tables.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import device_identity
import file_lock
import workspace_lock


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4
EXIT_UNHEALTHY = 5
EXIT_BUSY = 6

SCHEMA_VERSION = "1"
INDEX_PROJECTION_VERSION = "6"
HOT_LOCATOR_VERSION = "2"
DEFAULT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIRS = ("profile", "projects", "resources", "playbooks")
IGNORED_PARTS = {
    ".git",
    ".kb-data",
    "generated",
    "inbox",
    "ledger",
    "schema",
    "scripts",
    "skills",
    "tests",
    "__pycache__",
}
SENSITIVE_KEYS = {
    "value",
    "secret",
    "password",
    "passphrase",
    "token",
    "api_key",
    "apikey",
    "api_secret",
    "access_key",
    "access_token",
    "auth_token",
    "bearer_token",
    "client_secret",
    "connection_string",
    "database_url",
    "refresh_token",
    "private_key",
    "session_cookie",
    "ssh_key",
    "certificate",
    "credential",
    "credentials",
    "account",
    "username",
}
CREDENTIAL_VALUE_KEYS = {
    "value",
    "secret",
    "password",
    "passphrase",
    "token",
    "api_key",
    "apikey",
    "api_secret",
    "access_key",
    "access_token",
    "auth_token",
    "bearer_token",
    "client_secret",
    "connection_string",
    "database_url",
    "refresh_token",
    "private_key",
    "session_cookie",
    "ssh_key",
    "certificate",
}
SAFE_CREDENTIAL_METADATA_KEYS = {
    "schema_version",
    "id",
    "kind",
    "type",
    "name",
    "title",
    "scope",
    "project",
    "provider",
    "purpose",
    "environment",
    "status",
    "tags",
    "authority",
    "source",
    "source_device_id",
    "created_at",
    "observed_at",
    "source_time",
    "confirmed_at",
    "valid_from",
    "fact_key",
    "conflicts_with",
    "updated_at",
    "expires_at",
    "supersedes",
}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
CREDENTIAL_HINT_RE = re.compile(
    r"(?is)(\bpasswords?\b|\bpassphrases?\b|api[ _-]?keys?|"
    r"\bsecrets?\b|access[ _-]?keys?|private[ _-]?keys?|ssh[ _-]?keys?|"
    r"refresh[ _-]?tokens?|\btokens?\b|\bcredentials?\b|"
    r"\bcertificates?\b|\bconnection[ _-]?strings?\b|"
    r"\baccounts?\b|\busernames?\b|bearer\s+[A-Za-z0-9._~+/=-]+|"
    r"\bsk-[A-Za-z0-9_-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"密码|密钥|秘钥|口令|访问令牌|账号)"
)
CREDENTIAL_VALUE_REQUIRED_HINT_RE = re.compile(
    r"(?is)(\bpasswords?\b|\bpassphrases?\b|api[ _-]?keys?|\bsecrets?\b|"
    r"access[ _-]?keys?|private[ _-]?keys?|ssh[ _-]?keys?|"
    r"refresh[ _-]?tokens?|\btokens?\b|"
    r"bearer\s+[A-Za-z0-9._~+/=-]+|\bsk-[A-Za-z0-9_-]{8,}|"
    r"\bcredentials?\b|\bcertificates?\b|\bconnection[ _-]?strings?\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"密码|密钥|秘钥|口令|访问令牌)"
)
CREDENTIAL_LITERAL_RE = re.compile(
    r"(?is)(?:bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\bsk-[A-Za-z0-9_-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
OPAQUE_CREDENTIAL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z0-9._~+/=-]{24,})(?![A-Za-z0-9])"
)
CREDENTIAL_REFERENCE_METADATA_RE = re.compile(
    r"(?is)(?:ssh\s*)?(?:key\s*fingerprint|密钥指纹)|"
    r"api[ _-]?key\s*(?:authentication|auth|support|认证|鉴权)|"
    r"resources?[/\\]credentials?"
)
CREDENTIAL_ABSENCE_RE = re.compile(
    r"(?is)(?:\b(?:missing|absent|unknown|unavailable|incomplete)\b|"
    r"\bnot\s+(?:stored|saved|captured|recorded|included|available|present)\b|"
    r"\bwithout\b|\bcannot\b|\bcan't\b|"
    r"未(?:保存|包含|捕获|记录|同步|找到|取得)|没有|缺失|未知|"
    r"不可(?:用|回读|访问)|无法|不在)"
)
CREDENTIAL_POLICY_RE = re.compile(
    r"(?is)(?:\b(?:should|must|shall|policy|default)\b|"
    r"应(?:当|该)?|必须|不得|默认|策略|规则)"
)
CREDENTIAL_DIRECT_CLAIM_RE = re.compile(
    r"(?is)(?:\b(?:stored|saved|located|available|kept|found)\b|"
    r"\bcontains?\b|\bholds?\b|\bvalue\s*(?:is|=)|"
    r"存储|保存|位于|放在|包含|含有|原值为|值为|"
    r"(?:api[ _-]?key|password|token|credential|ssh[ _-]?key|"
    r"certificate|connection[ _-]?string|密码|密钥|口令)"
    r"[^。；;\n]{0,80}(?:\bin\b|\bat\b|在))"
)
CREDENTIAL_LOCATION_CLAIM_RE = re.compile(
    r"(?is)(?:\b(?:is|are|was|were)\s+"
    r"(?:stored|saved|located|kept|available|found)\b|"
    r"(?:api[ _-]?key|password|token|credential|ssh[ _-]?key|"
    r"certificate|connection[ _-]?string)"
    r"[^。；;,\n]{0,80}\b(?:is|are|was|were)\s+(?:in|at)\b|"
    r"(?:密码|密钥|秘钥|口令|访问令牌)[^。；；,\n]{0,80}"
    r"(?:存储在|保存在|位于|放在|原值为|值为))"
)
CREDENTIAL_PLACEHOLDER_RE = re.compile(
    r"(?is)(?:redacted(?:[_ -]?value)?|masked(?:[_ -]?value)?|"
    r"hidden(?:[_ -]?value)?|omitted(?:[_ -]?value)?|"
    r"missing(?:[_ -]?value)?|placeholder|"
    r"(?:value\s+(?:is\s+)?)?(?:stored|saved|located)\s+"
    r"(?:elsewhere|in\b|at\b)|available\s+elsewhere|"
    r"see\s+(?:file|path)\b|file:.+|"
    r"已存储|存储在别处|另处存储|保存在.+|位于.+|"
    r"已脱敏|已隐藏|缺失|占位符|见.+文件)"
)


CREDENTIAL_PLACEHOLDER_EXACT_RE = re.compile(
    r"""(?ix)^\s*
    [\[\]{}()<>'"`_-]*
    (?:
        unknown |
        tbd |
        to[\s_-]+be[\s_-]+determined |
        n[\s._/-]*a |
        not[\s_-]+applicable |
        none |
        null |
        nil |
        unavailable |
        example |
        dummy |
        sample |
        not[\s_-]+available(?:[\s_-]+yet)? |
        not[\s_-]+captured |
        not[\s_-]+stored |
        not[\s_-]+saved |
        not[\s_-]+recorded |
        not[\s_-]+provided |
        change[\s_-]*me |
        replace[\s_-]*me |
        (?:
            replace |
            insert |
            enter |
            paste
        )[\s_-]*(?:with[\s_-]*)?(?:your[\s_-]*)?
        (?:
            api[\s_-]*key |
            access[\s_-]*key |
            key |
            token |
            password |
            secret |
            credential
        )(?:[\s_-]*here)? |
        your[\s_-]*(?:
            api[\s_-]*key |
            access[\s_-]*key |
            key |
            token |
            password |
            secret |
            credential
        )(?:[\s_-]*here)? |
        (?:
            api[\s_-]*key |
            access[\s_-]*key |
            token |
            password |
            secret |
            credential
        ) |
        pending |
        todo
    )
    [\[\]{}()<>'"`_.!?:;\s-]*$
    """
)


class KBError(Exception):
    exit_code = EXIT_ERROR


class NotFoundError(KBError):
    exit_code = EXIT_NOT_FOUND


class ConflictError(KBError):
    exit_code = EXIT_CONFLICT


class UnhealthyError(KBError):
    exit_code = EXIT_UNHEALTHY

    def __init__(self, report: Dict[str, Any]):
        super().__init__("knowledge base health check failed")
        self.report = report


class WorkspaceMutationBusyError(KBError):
    exit_code = EXIT_BUSY

    def __init__(self) -> None:
        super().__init__("workspace-mutation-running")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    # Lifecycle events need a strict local order even when several decisions
    # are recorded in the same wall-clock second.
    return utc_now().isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def scalar_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return stable_json(value)


def clean_scalar(value: Any, default: str = "") -> str:
    text = scalar_text(value).strip()
    return text if text else default


# POLICY.md §2 enumerates the credential-like categories users actually provide
# (服务器、域名、账号、密码、API Key、Token、证书、数据库连接、购买资源). Natural
# spellings must classify as credentials so their values stay hidden in default
# search output and materialize into resources/credentials for exact retrieval.
CREDENTIAL_KINDS = {
    "access-key",
    "access-token",
    "account-credential",
    "api-key",
    "api-secret",
    "apikey",
    "auth-token",
    "bearer-token",
    "cert",
    "certificate",
    "client-secret",
    "connection-string",
    "credential",
    "credentials",
    "database-connection",
    "database-credential",
    "database-url",
    "key",
    "passphrase",
    "password",
    "private-key",
    "refresh-token",
    "secret",
    "secrets",
    "session-cookie",
    "ssh-key",
    "token",
}


def normalize_field_name(value: Any) -> str:
    """Normalize snake/kebab/space/camel spellings to one field identifier."""
    text = clean_scalar(value)
    text = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def normalize_kind_name(kind: Any) -> str:
    return normalize_field_name(kind).replace("_", "-")


def is_credential_kind(kind: str) -> bool:
    return normalize_kind_name(kind) in CREDENTIAL_KINDS


def is_credential_path(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    return bool(parts & {"credential", "credentials", "secrets"})


def has_value_field(value: Any) -> bool:
    """Treat credential-valued aliases as sensitive regardless of free-form kind."""
    if isinstance(value, dict):
        for key, child in value.items():
            if normalize_field_name(key) in CREDENTIAL_VALUE_KEYS:
                return True
            if has_value_field(child):
                return True
    elif isinstance(value, list):
        return any(has_value_field(child) for child in value)
    return False


def usable_credential_value(raw_value: Any) -> bool:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return False
    stripped = raw_value.strip()
    if re.fullmatch(r"(?:[*xX•●·]|\.){3,}", stripped):
        return False
    if CREDENTIAL_PLACEHOLDER_EXACT_RE.fullmatch(stripped):
        return False
    return CREDENTIAL_PLACEHOLDER_RE.search(stripped) is None


def credential_value_items(value: Any) -> List[Tuple[str, str]]:
    """Return structurally credential-valued fields without printing their values."""
    found: List[Tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = normalize_field_name(key)
            if normalized_key in CREDENTIAL_VALUE_KEYS and isinstance(child, str):
                found.append((normalized_key, child))
            if isinstance(child, (dict, list)):
                found.extend(credential_value_items(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(credential_value_items(child))
    return found


def exact_credential_value(value: Any) -> Optional[str]:
    for _, candidate in credential_value_items(value):
        if usable_credential_value(candidate):
            return candidate
    return None


def credential_value_present(value: Any) -> bool:
    """Return whether a credential record contains a usable exact original value."""
    return exact_credential_value(value) is not None


def event_is_sensitive(event: Dict[str, Any]) -> bool:
    return event_is_credential_resource(event)


def normalize_device_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._:-]+", "-", value.strip()).strip("-.")
    return normalized[:160] or "unknown-device"


def configured_device_id(root: Path) -> str:
    return normalize_device_id(device_identity.resolve_device_id(root))


def configured_history_database(root: Path) -> Path:
    configured = ".kb-data/history.sqlite"
    config_path = root / "config" / "kb.json"
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KBError("cannot parse config %s: %s" % (config_path, error)) from error
        if isinstance(data, dict) and data.get("history_database"):
            configured = str(data["history_database"])
    path = Path(configured).expanduser()
    return path if path.is_absolute() else root / path


def retrieval_settings(root: Path) -> Dict[str, Any]:
    settings: Dict[str, Any] = {
        "default_limit": 12,
        "maximum_context_characters": 16000,
        "prefer_canonical_over_raw_history": True,
        "include_raw_history": True,  # Legacy compatibility; new installs explicitly opt out.
    }
    config_path = root / "config" / "kb.json"
    if not config_path.is_file():
        return settings
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        configured = payload.get("retrieval", {}) if isinstance(payload, dict) else {}
        settings["default_limit"] = max(1, min(1000, int(configured.get("default_limit", 12))))
        settings["maximum_context_characters"] = max(
            1000, min(1000000, int(configured.get("maximum_context_characters", 16000)))
        )
        settings["include_raw_history"] = configured.get("include_raw_history", True) is True
        settings["prefer_canonical_over_raw_history"] = bool(
            configured.get("prefer_canonical_over_raw_history", True)
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise KBError("cannot parse retrieval config %s: %s" % (config_path, error)) from error
    return settings


def paths_for(root: Path) -> Dict[str, Path]:
    return {
        "root": root,
        "database": root / ".kb-data" / "knowledge.sqlite",
        "history_database": configured_history_database(root),
        "schema": (root / "schema" / "knowledge.sql") if (root / "schema" / "knowledge.sql").is_file() else DEFAULT_ROOT / "schema" / "knowledge.sql",
        "ledger": root / "ledger",
        "candidates": root / "inbox" / "candidates",
        "resources": root / "resources",
        "devices": root / "config" / "devices.json",
    }


def cold_data_available(root: Path) -> bool:
    """Return whether the configured cold-data directory is actually reachable.

    A missing directory and a dangling symlink are intentionally different from
    an empty, mounted directory: read/capture commands must never create a local
    replacement merely because the external SSD is absent.
    """
    data_root = root / ".kb-data"
    if data_root.is_symlink():
        try:
            return data_root.resolve(strict=True).is_dir()
        except (OSError, RuntimeError):
            return False
    return data_root.is_dir()


def ensure_directories(root: Path) -> None:
    for relative in (
        ".kb-data",
        ".kb-data/checkpoints",
        ".kb-data/reports",
        "ledger",
        "inbox/candidates",
        "resources",
        "resources/credentials",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)


def connect_database(root: Path) -> sqlite3.Connection:
    paths = paths_for(root)
    schema_path = paths["schema"]
    if not schema_path.is_file():
        raise KBError("schema not found: %s" % schema_path)
    ensure_directories(root)
    connection = sqlite3.connect(str(paths["database"]), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        document_columns = {
            clean_scalar(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(canonical_documents)"
            )
        }
        if "occurred_at" not in document_columns:
            connection.execute(
                "ALTER TABLE canonical_documents "
                "ADD COLUMN occurred_at TEXT NOT NULL DEFAULT ''"
            )
    except Exception:
        connection.close()
        raise
    return connection


def connect_database_read_only(
    database: Path, timeout: float = 10
) -> sqlite3.Connection:
    """Open a disposable SQLite projection without granting write access.

    ``mode=ro`` makes the guarantee effective at SQLite's VFS boundary rather
    than relying on callers to avoid mutating statements. ``query_only`` is a
    second, connection-local guard and also protects attached databases.
    ``immutable=1`` is deliberately not used because it can hide committed WAL
    content from a concurrently refreshed projection.
    """
    database = database.expanduser().resolve()
    connection = sqlite3.connect(
        database.as_uri() + "?mode=ro",
        uri=True,
        timeout=timeout,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=%d" % max(0, int(timeout * 1000)))
    except Exception:
        connection.close()
        raise
    return connection


def fsync_directory(path: Path) -> None:
    """Persist directory entries before a write is allowed to report success."""
    if os.name == "nt":
        # Open a real directory handle, then flush it.  Python's os.open cannot
        # open directories on Windows, so use the documented Win32
        # FILE_FLAG_BACKUP_SEMANTICS path.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        flush = kernel32.FlushFileBuffers
        flush.argtypes = (wintypes.HANDLE,)
        flush.restype = wintypes.BOOL
        close = kernel32.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL
        handle = create_file(
            str(path),
            0x40000000,  # GENERIC_WRITE
            0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
            None,
            3,  # OPEN_EXISTING
            0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
            None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        failure: Optional[BaseException] = None
        if not flush(handle):
            flush_error = ctypes.get_last_error()
            # Windows does not expose a universal directory-fsync primitive.
            # In particular, NTFS directory handles opened with
            # FILE_FLAG_BACKUP_SEMANTICS can reject FlushFileBuffers with
            # ERROR_INVALID_HANDLE even though the handle itself is valid.
            # File contents have already been fsynced before this helper is
            # called, so treat only the documented unsupported-operation
            # results as a best-effort directory flush; preserve every other
            # error as a real durability failure.
            if flush_error not in {1, 6, 50}:  # INVALID_FUNCTION/HANDLE/NOT_SUPPORTED
                failure = ctypes.WinError(flush_error)
        if not close(handle):
            close_failure = ctypes.WinError(ctypes.get_last_error())
            if failure is None:
                failure = close_failure
        if failure is not None:
            raise failure
        return
    descriptor = os.open(
        str(path),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_mkdir_parents(path: Path) -> None:
    """Create missing parents and durably publish every new directory entry."""
    missing: List[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    for directory in reversed(missing):
        created = False
        try:
            directory.mkdir()
            created = True
        except FileExistsError:
            pass
        if created:
            fsync_directory(directory)
            fsync_directory(directory.parent)


def atomic_json_create(path: Path, payload: Dict[str, Any]) -> None:
    durable_mkdir_parents(path.parent)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(path.name + ".tmp-%s" % uuid.uuid4().hex[:8])
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        # Publish a hard link only after the complete temporary file is durable.
        # Unlike replace(), link() preserves immutable create-only semantics:
        # an existing final path fails atomically instead of being overwritten.
        os.link(temporary, path)
        fsync_directory(path.parent)
        temporary.unlink()
        fsync_directory(path.parent)
    except FileExistsError as error:
        raise ConflictError("refusing to overwrite immutable record: %s" % path) from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_json_replace(path: Path, payload: Dict[str, Any]) -> None:
    """Atomically rewrite a mutable state file (inbox candidates, never ledger)."""
    durable_mkdir_parents(path.parent)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(path.name + ".tmp-%s" % uuid.uuid4().hex[:8])
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def iter_json_values(path: Path) -> Iterator[Tuple[Dict[str, Any], str]]:
    """Yield event-shaped dictionaries and a stable path[#line] reference."""
    try:
        if path.suffix.lower() == ".jsonl":
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if isinstance(value, dict):
                        yield value, "%s#%d" % (path, line_number)
            return
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise KBError("cannot parse %s: %s" % (path, error)) from error
    if isinstance(value, dict) and isinstance(value.get("events"), list):
        for index, item in enumerate(value["events"]):
            if isinstance(item, dict):
                yield item, "%s#events[%d]" % (path, index)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, dict):
                yield item, "%s#[%d]" % (path, index)
    elif isinstance(value, dict):
        yield value, str(path)


def ledger_files(root: Path) -> List[Path]:
    ledger = paths_for(root)["ledger"]
    files = (
        path
        for path in ledger.rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl"}
    )
    return sorted({path.resolve() for path in files})


def nested_get(data: Dict[str, Any], *path: str) -> Any:
    current: Any = data
    for component in path:
        if not isinstance(current, dict):
            return None
        current = current.get(component)
    return current


def normalize_event(raw: Dict[str, Any], ledger_ref: str = "") -> Dict[str, Any]:
    event_id = raw.get("id") or raw.get("event_id") or raw.get("knowledge_id")
    created_at = raw.get("created_at") or raw.get("observed_at") or raw.get("timestamp")
    source_device = (
        raw.get("source_device_id")
        or raw.get("device_id")
        or nested_get(raw, "provenance", "source_device_id")
        or nested_get(raw, "bundle", "source_device_id")
        or ""
    )
    statement = raw.get("statement")
    if statement is None:
        statement = raw.get("fact", raw.get("text", raw.get("content", "")))
    source = raw.get("source", raw.get("source_ref", raw.get("origin", "")))
    authority = raw.get("authority", raw.get("source_authority", ""))
    bundle_id = raw.get("bundle_id") or nested_get(raw, "bundle", "id") or nested_get(raw, "provenance", "bundle_id")
    imported_from = raw.get("imported_from") or nested_get(raw, "provenance", "imported_from")
    normalized = dict(raw)
    normalized.update(
        {
            "id": clean_scalar(event_id),
            "created_at": clean_scalar(created_at),
            "kind": clean_scalar(raw.get("kind", raw.get("category")), "fact"),
            "scope": clean_scalar(raw.get("scope", raw.get("project")), "global"),
            "statement": clean_scalar(statement),
            "source": source,
            "authority": clean_scalar(authority),
            "status": clean_scalar(raw.get("status"), "candidate"),
            "source_device_id": normalize_device_id(clean_scalar(source_device, "unknown-device")),
            "bundle_id": clean_scalar(bundle_id),
            "imported_from": clean_scalar(imported_from),
            "ledger_ref": ledger_ref,
        }
    )
    return normalized


def event_validation_errors(event: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    for field in ("id", "created_at", "kind", "scope", "statement", "source", "authority", "status", "source_device_id"):
        if not clean_scalar(event.get(field)):
            errors.append("missing %s" % field)
    if event.get("id") and not ID_RE.match(clean_scalar(event["id"])):
        errors.append("invalid id")
    if event.get("created_at") and parse_time(event["created_at"]) is None:
        errors.append("invalid created_at")
    if clean_scalar(event.get("source_device_id")) == "unknown-device":
        errors.append("missing source_device_id")
    for field in ("source_time", "observed_at", "confirmed_at", "expires_at", "valid_from"):
        if event.get(field) and parse_time(event[field]) is None:
            errors.append("invalid %s" % field)
    return errors


def event_fingerprint(event: Dict[str, Any]) -> str:
    core = {
        key: event.get(key)
        for key in (
            "id",
            "created_at",
            "kind",
            "scope",
            "statement",
            "value",
            "source",
            "authority",
            "status",
            "source_device_id",
        )
    }
    core.update(retrieval_metadata(event))
    core["credential_value_fields"] = credential_value_items(event)
    return sha256_text(stable_json(core))


def capture_fingerprint(event: Dict[str, Any]) -> str:
    """Fingerprint caller-controlled capture content, excluding generated time."""
    core: Dict[str, Any] = {
        key: clean_scalar(event.get(key))
        for key in (
            "id",
            "kind",
            "scope",
            "statement",
            "source",
            "authority",
            "status",
            "source_device_id",
            "bundle_id",
        )
    }
    core.update(retrieval_metadata(event))
    core["value"] = event.get("value")
    return sha256_text(stable_json(core))


def find_events_by_id(root: Path, event_id: str) -> List[Dict[str, Any]]:
    """Resolve an ID through the persistent hot locator.

    The locator is a disposable projection of the immutable ledger.  Its
    implementation lives below the Git-delta helpers, but Python resolves this
    call only after the module has finished loading.
    """
    return hot_locator_events_by_id(root, event_id)


# Lifecycle tiers used as the first search sort key: live knowledge first,
# unreviewed backlog next, replaced values after that, rejected candidates
# always last. Unknown non-empty states are conservatively review-tier rather
# than silently being treated as current knowledge.
LIFECYCLE_STATUS_RANKS = {
    "active": 0,
    "accepted": 0,
    "current": 0,
    "observed-current": 0,
    "verified": 0,
    "active-question": 1,
    "candidate": 1,
    "draft": 1,
    "evaluating": 1,
    "legacy-unconfirmed": 1,
    "observed": 1,
    "partially-verified": 1,
    "pending": 1,
    "pending-build-verification": 1,
    "pending-review": 1,
    "pending-verification": 1,
    "proposed": 1,
    "reported": 1,
    "unverified": 1,
    "disputed": 1,
    "conflict": 1,
    "expired": 2,
    "not-yet-effective": 1,
    "invalid-time": 1,
    "source-unavailable": 1,
    "archived": 2,
    "deprecated": 2,
    "historical-snapshot": 2,
    "inactive": 2,
    "replaced": 2,
    "retired": 2,
    "stale": 2,
    "superseded": 2,
    "invalid": 3,
    "rejected": 3,
    "revoked": 3,
}


def select_lifecycle_events(
    records: Iterable[Tuple[Dict[str, Any], Dict[str, Any], str]]
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Select one deterministic active lifecycle decision per subject."""
    epoch = dt.datetime.fromtimestamp(0, dt.timezone.utc)
    edges: Dict[str, Dict[str, Any]] = {}
    resolutions: Dict[str, Dict[str, Any]] = {}
    for raw, event, reference in records:
        if event["status"] != "active":
            continue
        observed = parse_time(event["created_at"]) or epoch
        order = (observed, event["id"], reference)
        if event["kind"] == "supersession":
            subject = clean_scalar(raw.get("old_id"))
            target = clean_scalar(raw.get("new_id"))
            selected = edges
        elif event["kind"] == "candidate-resolution":
            subject = clean_scalar(raw.get("candidate_id"))
            target = clean_scalar(raw.get("resolution")).lower()
            selected = resolutions
            if target not in ("accepted", "rejected"):
                continue
        else:
            continue
        if not subject or not target or subject == target:
            continue
        previous = selected.get(subject)
        if (
            previous is not None
            and (event["kind"] == "supersession" or observed == previous["observed"])
            and target != previous["target"]
        ):
            raise ConflictError(
                "ambiguous lifecycle decisions for %s: %s and %s; preserve both sources and correct the current target explicitly"
                % (subject, previous["target"], target)
            )
        if previous is None or order > previous["order"]:
            selected[subject] = {
                "target": target,
                "observed": observed,
                "created_at": event["created_at"],
                "event_id": event["id"],
                "ledger_ref": reference,
                "order": order,
            }
    return edges, resolutions


def resolve_supersession_edges(direct_edges: Dict[str, str]) -> Dict[str, str]:
    """Resolve every replacement chain once while rejecting cycles.

    A naive walk from every old id makes a long rotation history quadratic
    (A->B->...->Z is traversed again for B, C, and so on).  Cache each resolved
    suffix so both ordinary graphs and pathological long chains stay linear.
    """
    superseded_by: Dict[str, str] = {}
    for old_id in direct_edges:
        if old_id in superseded_by:
            continue
        path: List[str] = []
        positions: Dict[str, int] = {}
        current = old_id
        while current in direct_edges and current not in superseded_by:
            if current in positions:
                cycle = path[positions[current]:] + [current]
                raise ConflictError(
                    "supersession cycle: %s" % " -> ".join(cycle)
                )
            positions[current] = len(path)
            path.append(current)
            current = direct_edges[current]
        target = superseded_by.get(current, current)
        for subject in reversed(path):
            superseded_by[subject] = target
    return superseded_by


def lifecycle_maps_from_records(
    records: Iterable[Tuple[Dict[str, Any], Dict[str, Any], str]]
) -> Tuple[Dict[str, str], Dict[str, str]]:
    edges, resolutions = select_lifecycle_events(records)
    direct_edges = {
        subject: clean_scalar(selected["target"])
        for subject, selected in edges.items()
    }
    return (
        resolve_supersession_edges(direct_edges),
        {
            subject: clean_scalar(selected["target"])
            for subject, selected in resolutions.items()
        },
    )


def lifecycle_maps_from_database(
    connection: sqlite3.Connection,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    projection = connection.execute(
        "SELECT value FROM kb_meta WHERE key='lifecycle_projection_version'"
    ).fetchone()
    if projection is None or clean_scalar(projection[0]) != "1":
        raise LookupError("lifecycle projection is not initialized")
    direct_edges = {
        clean_scalar(row["old_id"]): clean_scalar(row["new_id"])
        for row in connection.execute(
            "SELECT old_id, new_id FROM lifecycle_supersessions"
        )
    }
    resolutions = {
        clean_scalar(row["candidate_id"]): clean_scalar(row["resolution"])
        for row in connection.execute(
            "SELECT candidate_id, resolution FROM lifecycle_resolutions"
        )
    }
    return resolve_supersession_edges(direct_edges), resolutions


def lifecycle_maps(root: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Build lifecycle maps from the immutable ledger in one pass."""
    records = (
        (raw, normalize_event(raw, reference), reference)
        for path in ledger_files(root)
        for raw, reference in iter_json_values(path)
    )
    return lifecycle_maps_from_records(records)


def lifecycle_maps_for_read(
    root: Path, connection: Optional[sqlite3.Connection] = None
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Use the disposable projection on read hot paths, with a safe fallback."""
    if connection is not None:
        try:
            return lifecycle_maps_from_database(connection)
        except (LookupError, sqlite3.Error):
            return lifecycle_maps(root)
    database = paths_for(root)["database"]
    if cold_data_available(root) and database.is_file():
        projected: Optional[sqlite3.Connection] = None
        try:
            projected = sqlite3.connect(
                database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5
            )
            projected.row_factory = sqlite3.Row
            return lifecycle_maps_from_database(projected)
        except (LookupError, sqlite3.Error):
            pass
        finally:
            if projected is not None:
                projected.close()
    return lifecycle_maps(root)


RETRIEVAL_METADATA_FIELDS = ("source_time", "observed_at", "confirmed_at", "expires_at",
                             "valid_from", "fact_key", "conflicts_with")


def retrieval_metadata(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Optional additive metadata: older records remain readable unchanged."""
    metadata = {key: record[key] for key in RETRIEVAL_METADATA_FIELDS
                if record.get(key) not in (None, "", [])}
    if event_is_sensitive(dict(record)):
        return scrub_exact_values_in_structure(metadata, sensitive_values(record))
    return metadata


def annotate_lifecycle(
    results: List[Dict[str, Any]],
    superseded_by: Dict[str, str],
    resolutions: Dict[str, str],
) -> None:
    now = utc_now()
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for item in results:
        item_id = clean_scalar(item.get("id"))
        replacement = superseded_by.get(item_id)
        if replacement:
            item["superseded_by"] = replacement
        resolution = resolutions.get(item_id)
        if resolution:
            item["candidate_state"] = resolution
        expiry = parse_time(item.get("expires_at"))
        effective = parse_time(item.get("valid_from"))
        invalid_time = any(item.get(field) and parse_time(item[field]) is None
                           for field in ("source_time", "observed_at", "confirmed_at", "expires_at", "valid_from"))
        if item.get("source_available") is False:
            item["effective_status"] = "source-unavailable"
        elif item.get("projection_conflict"):
            item["effective_status"] = "disputed"
        elif invalid_time:
            item["effective_status"] = "invalid-time"
        elif expiry is not None and expiry <= now:
            item["effective_status"] = "expired"
        elif effective is not None and effective > now:
            item["effective_status"] = "not-yet-effective"
        elif item.get("conflicts_with"):
            item["effective_status"] = "disputed"
        elif replacement:
            item["effective_status"] = "superseded"
        else:
            item["effective_status"] = resolution or item.get("status", "active")
        if (item.get("fact_key") and not replacement and resolution != "rejected"
                and item.get("effective_status") not in ("expired", "not-yet-effective", "invalid-time", "source-unavailable")
                and LIFECYCLE_STATUS_RANKS.get(clean_scalar(item.get("status")), 1) == 0):
            groups.setdefault((clean_scalar(item.get("scope")), clean_scalar(item["fact_key"])), []).append(item)
    for group in groups.values():
        # A shared fact_key is an explicit identity assertion, not inferred
        # from similar wording. Independent active IDs need an explicit
        # supersession before any one of them can become the current answer.
        ids = sorted({item["id"] for item in group})
        if len(ids) > 1:
            for item in group:
                item["effective_status"] = "disputed"
                item["conflicts_with"] = [other for other in ids if other != item["id"]]
    for item in results:
        item["current_eligible"] = (lifecycle_status_rank(item) == 0
                                    and item.get("result_type") != "source-history")


def lifecycle_status_rank(item: Dict[str, Any]) -> int:
    effective = clean_scalar(item.get("effective_status")).strip().lower()
    if effective in ("expired", "not-yet-effective", "invalid-time", "source-unavailable", "disputed", "conflict"):
        return LIFECYCLE_STATUS_RANKS[effective]
    state = clean_scalar(item.get("candidate_state")).strip().lower()
    if state == "rejected":
        return LIFECYCLE_STATUS_RANKS["rejected"]
    if clean_scalar(item.get("superseded_by")):
        return LIFECYCLE_STATUS_RANKS["superseded"]
    if state == "accepted":
        return LIFECYCLE_STATUS_RANKS["accepted"]
    status = re.sub(
        r"[\s_]+", "-", clean_scalar(item.get("status")).strip().lower()
    ).strip("-")
    if not status:
        return LIFECYCLE_STATUS_RANKS["active"]
    if status in LIFECYCLE_STATUS_RANKS:
        return LIFECYCLE_STATUS_RANKS[status]
    if status.startswith(("active-", "accepted-", "verified-")):
        return LIFECYCLE_STATUS_RANKS["active"]
    if status.startswith(
        (
            "candidate-",
            "draft-",
            "evaluating-",
            "legacy-unconfirmed",
            "partially-verified",
            "pending-",
            "proposed-",
            "reported-",
            "unverified-",
        )
    ):
        return LIFECYCLE_STATUS_RANKS["candidate"]
    if status.startswith(
        (
            "archived-",
            "completed-",
            "deprecated-",
            "historical-",
            "inactive-",
            "observed-",
            "replaced-",
            "retired-",
            "stale-",
            "superseded-",
        )
    ):
        return LIFECYCLE_STATUS_RANKS["superseded"]
    if status.startswith(("invalid-", "rejected-", "revoked-")):
        return LIFECYCLE_STATUS_RANKS["rejected"]
    return LIFECYCLE_STATUS_RANKS["candidate"]


def retrieval_authority_rank(item: Dict[str, Any]) -> int:
    """Rank evidence authority without pretending the open vocabulary is closed."""
    result_type = clean_scalar(item.get("result_type")).strip().lower()
    if result_type == "source-history":
        # Verbatim messages are evidence, not curated current facts. User text
        # remains ahead of assistant text while both stay below reviewed facts.
        return 4 if clean_scalar(item.get("role")).lower() == "user" else 5

    authority = re.sub(
        r"[\s_+]+", "-", clean_scalar(item.get("authority")).strip().lower()
    ).strip("-")
    if not authority:
        return 2 if result_type == "canonical" else 3

    # Inference and recommendation labels must be recognized before looking
    # for embedded words such as "user" or "verified" in their provenance.
    if any(
        marker in authority
        for marker in (
            "agent-inference",
            "agent-recommendation",
            "agent-observation",
            "pending-verification",
            "suspected",
        )
    ):
        return 5

    user_markers = ("user", "explicit", "stated", "direct", "provided")
    if authority == "user" or (
        "user" in authority
        and any(marker in authority for marker in user_markers[1:])
    ):
        return 0
    if "user" in authority and "verified" in authority:
        return 0

    if any(
        marker in authority
        for marker in (
            "verified",
            "verification",
            "direct-observation",
            "external-primary-source",
            "official-",
            "source-verified",
            "tool-output",
        )
    ):
        return 1
    if "maintainer" in authority:
        return 2
    if "imported" in authority or "historical" in authority:
        return 4
    return 3


def retrieval_sort_key(
    item: Dict[str, Any], prefer_canonical: bool
) -> Tuple[int, int, float, float, int, str]:
    """Apply lifecycle, authority, relevance, source time, then layer."""
    observed = (
        parse_time(item.get("source_time"))
        or parse_time(item.get("confirmed_at"))
        or parse_time(item.get("observed_at"))
        or parse_time(item.get("updated_at"))
        or parse_time(item.get("created_at"))
        or parse_time(item.get("timestamp"))
        or dt.datetime.fromtimestamp(0, dt.timezone.utc)
    )
    type_priority = (
        {"canonical": 0, "history": 1, "source-history": 2}
        if prefer_canonical
        else {"history": 0, "source-history": 1, "canonical": 2}
    )
    return (
        lifecycle_status_rank(item),
        retrieval_authority_rank(item),
        float(item.get("rank") or 0.0),
        -observed.timestamp(),
        type_priority.get(clean_scalar(item.get("result_type")), 9),
        clean_scalar(item.get("id")),
    )


def fold_exact_result_duplicates(
    results: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Fold multiple projections of the same immutable record ID.

    The input must already be sorted: its first item is the preferred current
    representation. Different IDs are never folded merely because their text
    or values match; that requires an explicit identity/supersession decision.
    """
    folded: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    result_types: Dict[str, List[str]] = {}
    duplicate_counts: Dict[str, int] = {}

    def conflicting_projection(
        left: Mapping[str, Any], right: Mapping[str, Any]
    ) -> bool:
        # An ID is an identity claim, not permission to hide contradictory
        # payloads.  Compare every shared, non-empty current-view field before
        # folding; missing fields may still be filled from the richer layer.
        for key in (
            "kind",
            "scope",
            "authority",
            "status",
            "credential",
            "credential_value_status",
            "superseded_by",
            "candidate_state",
        ):
            left_value = left.get(key)
            right_value = right.get(key)
            if left_value in (None, "", [], {}) or right_value in (None, "", [], {}):
                continue
            if stable_json(left_value) != stable_json(right_value):
                return True

        # Public search replaces every stored credential with the same
        # placeholder. Compare transient digests of the exact in-memory values
        # before folding so redaction cannot hide a same-ID contradiction.
        left_credential = bool(
            left.get("credential")
            or left.get("credential_value_status") in ("stored", "missing")
        )
        right_credential = bool(
            right.get("credential")
            or right.get("credential_value_status") in ("stored", "missing")
        )
        if left_credential or right_credential:
            left_status = clean_scalar(left.get("credential_value_status"))
            right_status = clean_scalar(right.get("credential_value_status"))
            if (
                not left_credential
                or not right_credential
                or left_status not in ("stored", "missing")
                or right_status not in ("stored", "missing")
                or left_status != right_status
            ):
                return True
        left_stored = left.get("credential_value_status") == "stored"
        right_stored = right.get("credential_value_status") == "stored"
        if left_stored and right_stored:
            left_fingerprint = clean_scalar(
                left.get("_credential_value_fingerprint")
            )
            right_fingerprint = clean_scalar(
                right.get("_credential_value_fingerprint")
            )
            if (
                not left_fingerprint
                or not right_fingerprint
                or left_fingerprint != right_fingerprint
            ):
                return True

        # Canonical documents expose their identity as title/name while the
        # immutable ledger exposes the same field as statement. Compare the
        # normalized concept instead of requiring identical projection keys.
        left_identity = next(
            (
                left.get(key)
                for key in ("statement", "name", "title")
                if left.get(key) not in (None, "", [], {})
            ),
            None,
        )
        right_identity = next(
            (
                right.get(key)
                for key in ("statement", "name", "title")
                if right.get(key) not in (None, "", [], {})
            ),
            None,
        )
        if (
            left_identity is not None
            and right_identity is not None
            and stable_json(left_identity) != stable_json(right_identity)
        ):
            return True

        def exact_projection_value(item: Mapping[str, Any]) -> Any:
            direct = item.get("value")
            if direct not in (None, "", [], {}):
                return direct
            content = item.get("content")
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return None
                if isinstance(parsed, dict) and parsed.get("value") not in (
                    None,
                    "",
                    [],
                    {},
                ):
                    return parsed.get("value")
                return exact_credential_value(parsed)
            return None

        left_value = exact_projection_value(left)
        right_value = exact_projection_value(right)
        if (
            left_value is not None
            and right_value is not None
            and stable_json(left_value) != stable_json(right_value)
        ):
            return True
        return False

    for raw in results:
        item_id = clean_scalar(raw.get("id"))
        if not item_id:
            folded.append(dict(raw))
            continue
        result_type = clean_scalar(raw.get("result_type"))
        existing = by_id.get(item_id)
        if existing is None:
            existing = dict(raw)
            by_id[item_id] = existing
            folded.append(existing)
            result_types[item_id] = [result_type] if result_type else []
            duplicate_counts[item_id] = 1
            continue
        if conflicting_projection(existing, raw):
            existing["projection_conflict"] = "same-id-different-payload"
            conflicting = dict(raw)
            conflicting["projection_conflict"] = "same-id-different-payload"
            folded.append(conflicting)
            continue
        duplicate_counts[item_id] += 1
        if result_type and result_type not in result_types[item_id]:
            result_types[item_id].append(result_type)
        for key, value in raw.items():
            if key == "result_type":
                continue
            if key not in existing or existing[key] in (None, "", [], {}):
                existing[key] = value

    for item_id, count in duplicate_counts.items():
        if count <= 1:
            continue
        item = by_id[item_id]
        item["duplicate_count"] = count
        item["result_types"] = result_types[item_id]
    for item in folded:
        item.pop("_credential_value_fingerprint", None)
    return folded


def credential_value_fingerprint(value: Any) -> str:
    """Create an internal equality proof that is removed before output."""
    if value is None:
        return ""
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


RESOURCE_KINDS = {
    "account",
    "database",
    "domain",
    "endpoint",
    "host",
    "license",
    "purchase",
    "repository",
    "resource",
    "server",
    "service",
    "subscription",
} | CREDENTIAL_KINDS

LIFECYCLE_KINDS = {"candidate-resolution", "supersession"}


def authority_is_agent_derived(value: Any) -> bool:
    normalized = re.sub(
        r"[\s_]+", "-", clean_scalar(value).strip().lower()
    ).strip("-")
    if normalized == "agent-verified":
        return False
    return normalized == "agent" or normalized.startswith("agent-")


def is_materializable_resource(event: Dict[str, Any]) -> bool:
    """Return whether an active, source-backed event is safe to expose as a resource.

    Inference remains review-only.  User facts and verified current-system
    observations are immediately addressable because making the user search for
    them again defeats the purpose of this knowledge base.
    """
    kind = normalize_kind_name(event.get("kind"))
    event_id = clean_scalar(event.get("id"))
    authority = clean_scalar(event.get("authority")).strip().lower()
    requires_value = event_requires_credential_value(event)
    return bool(
        clean_scalar(event.get("status")).strip().lower() == "active"
        and not authority_is_agent_derived(authority)
        and (not requires_value or credential_value_present(event))
        and (
            kind in RESOURCE_KINDS
            or event_id.startswith("resource-")
            or has_value_field(event)
        )
    )


def event_requires_credential_value(event: Dict[str, Any]) -> bool:
    """Identify captures that claim to contain a directly usable credential."""
    kind = normalize_kind_name(event.get("kind"))
    if kind in LIFECYCLE_KINDS:
        return False
    statement = clean_scalar(event.get("statement"))
    label_text = " ".join(
        (clean_scalar(event.get("name")), clean_scalar(event.get("title")))
    )
    source = clean_scalar(event.get("source"))
    if is_credential_kind(kind) or has_value_field(event):
        return True
    if CREDENTIAL_LITERAL_RE.search(" ".join((statement, label_text, source))):
        return True
    source_claim = CREDENTIAL_REFERENCE_METADATA_RE.sub("", source)
    if CREDENTIAL_VALUE_REQUIRED_HINT_RE.search(source_claim):
        return True
    claim_text = CREDENTIAL_REFERENCE_METADATA_RE.sub(
        "", " ".join((statement, label_text))
    )
    if not CREDENTIAL_VALUE_REQUIRED_HINT_RE.search(claim_text):
        return False
    event_id = clean_scalar(event.get("id"))
    resource_shape = kind in RESOURCE_KINDS or event_id.startswith("resource-")
    if CREDENTIAL_VALUE_REQUIRED_HINT_RE.search(label_text):
        return not CREDENTIAL_ABSENCE_RE.search(label_text)
    for clause in re.split(r"[。；;,\n，]+", claim_text):
        if not CREDENTIAL_VALUE_REQUIRED_HINT_RE.search(clause):
            continue
        if CREDENTIAL_LOCATION_CLAIM_RE.search(clause):
            return True
        if CREDENTIAL_ABSENCE_RE.search(clause) or CREDENTIAL_POLICY_RE.search(
            clause
        ):
            continue
        if resource_shape or CREDENTIAL_DIRECT_CLAIM_RE.search(clause):
            return True
    return False


def event_is_credential_resource(event: Dict[str, Any]) -> bool:
    return has_value_field(event) or event_requires_credential_value(event)


def credential_value_missing(event: Dict[str, Any]) -> bool:
    return bool(
        event_is_credential_resource(event)
        and not credential_value_present(event)
    )


def active_credential_value_missing(event: Dict[str, Any]) -> bool:
    return bool(
        clean_scalar(event.get("status")).strip().lower() == "active"
        and credential_value_missing(event)
    )


def credential_resource_has_exact_value(root: Path, resource_id: str) -> bool:
    """Require a real credential record; an ordinary same-ID catalog entry is insufficient."""
    for path, record in resource_matches(root, resource_id):
        credential = (
            is_credential_path(path.relative_to(root))
            or event_is_credential_resource(record)
        )
        if credential and credential_value_present(record):
            return True
    return False


def missing_credential_is_resolved(
    root: Path,
    event_id: str,
    superseded_by: Optional[Dict[str, str]] = None,
) -> bool:
    if credential_resource_has_exact_value(root, event_id):
        return True
    replacement_id = (superseded_by or {}).get(event_id)
    return bool(
        replacement_id
        and credential_resource_has_exact_value(root, replacement_id)
    )


def canonical_ledger_ref(root: Path, ledger_ref: str) -> str:
    base, separator, suffix = clean_scalar(ledger_ref).partition("#")
    try:
        path = Path(base)
        if path.is_absolute():
            base = path.relative_to(root).as_posix()
    except (OSError, ValueError):
        pass
    return base + (separator + suffix if separator else "")


def canonical_resource_record(
    root: Path, event: Dict[str, Any], ledger_ref: str
) -> Dict[str, Any]:
    event_ids = [clean_scalar(event.get("id"))]
    acceptance_event_id = clean_scalar(event.get("acceptance_event_id"))
    if acceptance_event_id and acceptance_event_id not in event_ids:
        event_ids.append(acceptance_event_id)
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": clean_scalar(event.get("id")),
        "kind": clean_scalar(event.get("kind"), "resource"),
        "scope": clean_scalar(event.get("scope"), "global"),
        "name": clean_scalar(event.get("statement")),
        "status": clean_scalar(event.get("status"), "active"),
        "authority": clean_scalar(event.get("authority")),
        "observed_at": clean_scalar(event.get("created_at")),
        "source_device_id": normalize_device_id(
            clean_scalar(event.get("source_device_id"), "unknown-device")
        ),
        "source": {
            "kind": "ledger-event",
            "ref": canonical_ledger_ref(root, ledger_ref),
            "event_ids": event_ids,
            "original_ref": event.get("source"),
        },
    }
    record.update({key: event[key] for key in RETRIEVAL_METADATA_FIELDS if key in event})
    if clean_scalar(event.get("accepted_at")):
        record["accepted_at"] = clean_scalar(event.get("accepted_at"))
        record["accepted_candidate_id"] = clean_scalar(
            event.get("accepted_candidate_id"), event.get("id")
        )
    if not event_is_credential_resource(event):
        record["statement"] = clean_scalar(event.get("statement"))
    exact_value = exact_credential_value(event)
    if exact_value is not None:
        # Canonical resources expose one stable retrieval field even when an
        # imported ledger used api_key, apiKey, clientSecret, or another alias.
        record["value"] = exact_value
    elif "value" in event:
        record["value"] = event["value"]
    if clean_scalar(event.get("bundle_id")):
        record["bundle_id"] = clean_scalar(event.get("bundle_id"))
    return record


def materialize_resource(
    root: Path, event: Dict[str, Any], ledger_ref: str
) -> Optional[Path]:
    """Create one canonical resource without overwriting any prior record."""
    if credential_value_missing(event):
        raise KBError(
            "credential-value-missing: credential resources require "
            "an exact non-placeholder value"
        )
    if not is_materializable_resource(event):
        return None
    require_writable_schema(root)
    record = canonical_resource_record(root, event, ledger_ref)
    resource_id = clean_scalar(record.get("id"))
    existing = resource_matches(root, resource_id)
    if existing:
        expected = stable_json(record)
        if any(stable_json(item) != expected for _, item in existing):
            raise ConflictError(
                "resource id %s already resolves to a different canonical record"
                % resource_id
            )
        return existing[0][0]
    credential = event_is_credential_resource(event)
    directory = root / "resources" / ("credentials" if credential else "catalog")
    path = directory / (resource_id + ".json")
    atomic_json_create(path, record)
    hot_locator_record_resource_source(root, path)
    return path


def materialize_accepted_candidate(
    root: Path,
    candidate: Dict[str, Any],
    *,
    resolution_event_id: str,
    resolution_ledger_ref: str,
    accepted_at: str,
) -> Optional[Path]:
    """Promote an accepted inferred resource without rewriting its ledger event."""

    if credential_value_missing(candidate):
        raise KBError(
            "credential-value-missing: credential candidates cannot be accepted "
            "without an exact non-placeholder value"
        )
    promoted = dict(candidate)
    promoted.update(
        {
            "status": "active",
            "authority": "maintainer-accepted-inference",
            "acceptance_event_id": resolution_event_id,
            "accepted_candidate_id": clean_scalar(candidate.get("id")),
            "accepted_at": accepted_at,
        }
    )
    return materialize_resource(root, promoted, resolution_ledger_ref)


def accepted_candidate_resource_plan(
    root: Path,
    candidate: Dict[str, Any],
    *,
    resolution_event_id: str,
    resolution_ledger_ref: str,
    accepted_at: str,
) -> Tuple[Optional[Path], Optional[Dict[str, Any]], bool]:
    """Prove an acceptance can materialize before publishing its resolution."""

    if credential_value_missing(candidate):
        raise KBError(
            "credential-value-missing: credential candidates cannot be accepted "
            "without an exact non-placeholder value"
        )
    promoted = dict(candidate)
    promoted.update(
        {
            "status": "active",
            "authority": "maintainer-accepted-inference",
            "acceptance_event_id": resolution_event_id,
            "accepted_candidate_id": clean_scalar(candidate.get("id")),
            "accepted_at": accepted_at,
        }
    )
    if not is_materializable_resource(promoted):
        return None, None, False
    record = canonical_resource_record(root, promoted, resolution_ledger_ref)
    resource_id = clean_scalar(record.get("id"))
    credential = event_is_credential_resource(promoted)
    planned_path = (
        root
        / "resources"
        / ("credentials" if credential else "catalog")
        / (resource_id + ".json")
    )
    matches = resource_matches(root, resource_id)
    if planned_path.is_file() and all(
        path.resolve() != planned_path.resolve() for path, _ in matches
    ):
        direct_records = [
            item
            for item in iter_resource_records(planned_path)
            if clean_scalar(item.get("id")) == resource_id
        ]
        matches.extend((planned_path, item) for item in direct_records)
    if matches:
        expected = stable_json(record)
        if any(stable_json(item) != expected for _, item in matches):
            raise ConflictError(
                "resource id %s already resolves to a different canonical record"
                % resource_id
            )
        return matches[0][0], record, True
    if planned_path.exists():
        raise ConflictError(
            "resource path already exists; refusing to overwrite %s"
            % resource_id
        )
    return planned_path, record, False


def materialize_resources(root: Path) -> Dict[str, Any]:
    """Backfill canonical resource records from authoritative active ledger events."""
    created = 0
    existing = 0
    skipped = 0
    records: List[Tuple[Dict[str, Any], str]] = []
    for ledger_path in ledger_files(root):
        records.extend(
            (normalize_event(raw, reference), reference)
            for raw, reference in iter_json_values(ledger_path)
        )
    superseded_by, _ = lifecycle_maps(root)
    complete_credential_ids = {
        event["id"]
        for event, _ in records
        if clean_scalar(event.get("status")).strip().lower() == "active"
        and event_is_credential_resource(event)
        and credential_value_present(event)
    }
    blocked = [
        (
            event["id"],
            reference,
        )
        for event, reference in records
        if active_credential_value_missing(event)
        and not missing_credential_is_resolved(root, event["id"], superseded_by)
        and superseded_by.get(event["id"]) not in complete_credential_ids
    ]
    if blocked:
        raise UnhealthyError(
            {
                "ok": False,
                "action": "materialize-resources",
                "error": "credential-value-missing",
                "issues": [
                    "credential-value-missing: event %s in %s"
                    % (event_id, reference)
                    for event_id, reference in blocked
                ],
                "created": 0,
                "existing": 0,
                "skipped": 0,
            }
        )
    for event, reference in records:
        if not is_materializable_resource(event):
            skipped += 1
            continue
        before = bool(resource_matches(root, event["id"]))
        path = materialize_resource(root, event, reference)
        if path is None:
            skipped += 1
        elif before:
            existing += 1
        else:
            created += 1
    index = update_index(root) if cold_data_available(root) else None
    return {
        "ok": True,
        "action": "materialize-resources",
        "created": created,
        "existing": existing,
        "skipped": skipped,
        "index_status": "current" if index is not None else "pending-cold-data-unavailable",
    }


def scrub_exact_values(text: str, values: Iterable[Any]) -> str:
    result = text
    candidates = sorted(
        {clean_scalar(value) for value in values if clean_scalar(value)},
        key=len,
        reverse=True,
    )
    for value in candidates:
        result = result.replace(value, "[credential value omitted]")
    return result


def scrub_exact_values_in_structure(value: Any, values: Iterable[Any]) -> Any:
    exact_values = tuple(values)
    if isinstance(value, dict):
        return {
            key: scrub_exact_values_in_structure(child, exact_values)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            scrub_exact_values_in_structure(child, exact_values)
            for child in value
        ]
    if isinstance(value, str):
        return scrub_exact_values(value, exact_values)
    return value


def sensitive_values(value: Any, force_all: bool = False) -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = normalize_field_name(key)
            if force_all and normalized_key not in SAFE_CREDENTIAL_METADATA_KEYS:
                if not isinstance(child, (dict, list)):
                    found.append(clean_scalar(child))
                else:
                    found.append(stable_json(child))
            if normalized_key in SENSITIVE_KEYS:
                if not isinstance(child, (dict, list)):
                    found.append(clean_scalar(child))
                else:
                    found.append(stable_json(child))
            found.extend(sensitive_values(child, force_all=False))
    elif isinstance(value, list):
        for child in value:
            found.extend(sensitive_values(child, force_all=False))
    return [item for item in found if item]


def credential_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, child in value.items():
            normalized_key = normalize_field_name(key)
            if normalized_key in SAFE_CREDENTIAL_METADATA_KEYS:
                if normalized_key == "source" and isinstance(child, dict):
                    result[key] = {
                            k: v
                            for k, v in child.items()
                            if normalize_field_name(k)
                        in {"type", "thread_id", "message_id", "path", "observed_at", "created_at"}
                    }
                else:
                    result[key] = child
        return result
    return {}


def markdown_metadata(text: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not text.startswith("---\n"):
        return result
    end = text.find("\n---", 4)
    if end < 0:
        return result
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if re.match(r"^[A-Za-z0-9_-]+$", key.strip()):
            result[key.strip()] = value.strip().strip("\"'")
    return result


def canonical_files(root: Path) -> List[Path]:
    files: List[Path] = []
    for directory in CANONICAL_DIRS:
        base = root / directory
        if base.is_dir():
            files.extend(
                path
                for path in base.rglob("*")
                if path.is_file() and path.suffix.lower() in {".md", ".json"}
            )
    for path in root.iterdir() if root.is_dir() else []:
        if path.is_file() and path.suffix.lower() in {".md", ".json"}:
            files.append(path)
    return sorted(
        {
            path.resolve()
            for path in files
            if path.is_file() and not (set(path.relative_to(root).parts) & IGNORED_PARTS)
        }
    )


def canonical_document(root: Path, path: Path) -> Dict[str, Any]:
    root = root.expanduser().resolve()
    path = path.expanduser().resolve()
    relative = path.relative_to(root).as_posix()
    raw_text = path.read_text(encoding="utf-8")
    metadata: Dict[str, Any] = {}
    parsed: Any = None
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as error:
            raise KBError("cannot parse canonical JSON %s: %s" % (relative, error)) from error
        if isinstance(parsed, dict):
            metadata = parsed
    else:
        metadata = markdown_metadata(raw_text)
    default_kind = path.parts[-2] if len(path.parts) > 1 else "document"
    kind = clean_scalar(metadata.get("kind", metadata.get("type")), default_kind)
    authority = clean_scalar(metadata.get("authority"))
    status = clean_scalar(metadata.get("status"), "active")
    credential = (
        is_credential_path(path.relative_to(root))
        or event_is_credential_resource(metadata)
    )
    scope = clean_scalar(metadata.get("scope", metadata.get("project")), "global")
    source_device_id = normalize_device_id(clean_scalar(metadata.get("source_device_id"), "repository"))
    document_id = clean_scalar(metadata.get("id"), relative)
    occurred_at = next((clean_scalar(metadata.get(field)) for field in
                        ("source_time", "confirmed_at", "observed_at", "created_at", "updated_at")
                        if parse_time(metadata.get(field)) is not None), "")
    title = clean_scalar(metadata.get("title", metadata.get("name")))
    if not title and path.suffix.lower() == ".md":
        heading = re.search(r"^#\s+(.+)$", raw_text, flags=re.MULTILINE)
        if heading:
            title = heading.group(1).strip()
    title = title or path.stem
    safe_path = relative
    safe_source_device_id = source_device_id
    if credential:
        exact_values = sensitive_values(
            parsed if parsed is not None else metadata,
            force_all=True,
        )
        safe_path = scrub_exact_values(safe_path, exact_values)
        title = scrub_exact_values(title, exact_values)
        kind = scrub_exact_values(kind, exact_values)
        scope = scrub_exact_values(scope, exact_values)
        authority = scrub_exact_values(authority, exact_values)
        status = scrub_exact_values(status, exact_values)
        safe_source_device_id = scrub_exact_values(
            safe_source_device_id,
            exact_values,
        )
        if parsed is not None:
            safe_metadata = scrub_exact_values_in_structure(
                credential_metadata(parsed),
                exact_values,
            )
            safe_content = json.dumps(
                safe_metadata,
                ensure_ascii=False,
                sort_keys=True,
            )
            safe_content = scrub_exact_values(safe_content, exact_values)
        else:
            safe_content = "credential metadata %s %s %s %s" % (document_id, title, kind, scope)
            safe_content = scrub_exact_values(safe_content, exact_values)
    else:
        safe_content = raw_text
    return {
        **retrieval_metadata(metadata),
        "source": (scrub_exact_values_in_structure(metadata.get("source", relative), exact_values)
                   if credential else metadata.get("source", relative)),
        "document_id": document_id,
        "path": relative,
        "safe_path": safe_path,
        "title": title,
        "kind": kind,
        "scope": scope,
        "authority": authority,
        "status": status,
        "source_device_id": source_device_id,
        "safe_source_device_id": safe_source_device_id,
        "occurred_at": occurred_at,
        "is_credential": credential,
        "credential_value_present": (
            credential_value_present(parsed) if credential else None
        ),
        "credential_value_fingerprint": (
            credential_value_fingerprint(exact_credential_value(parsed))
            if credential and credential_value_present(parsed)
            else ""
        ),
        "safe_content": safe_content,
        "full_content": raw_text,
        "content_sha256": sha256_text(raw_text),
    }


def history_search_text(event: Dict[str, Any]) -> Tuple[str, str]:
    statement = clean_scalar(event.get("statement"))
    value = event.get("value")
    credential = event_is_sensitive(event)
    # Every event is findable by id/kind/scope/provenance, not only by its
    # statement wording; any structurally present value is kept out of FTS.
    searchable_fields = " ".join(
        clean_scalar(event.get(field))
        for field in (
            "id",
            "kind",
            "scope",
            "statement",
            "source",
            "authority",
            "status",
            "source_device_id",
            "bundle_id",
            "fact_key",
        )
    )
    if credential:
        exact_values = sensitive_values(event)
        statement = scrub_exact_values(statement, exact_values)
        searchable = scrub_exact_values(searchable_fields, exact_values)
    else:
        searchable = " ".join((searchable_fields, clean_scalar(value)))
    return statement, searchable


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def stat_signature(path: Path) -> Tuple[int, int, int, int]:
    stat = path.stat()
    return (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


def assert_stable_source(path: Path, before: Tuple[int, int, int, int]) -> os.stat_result:
    after = path.stat()
    current = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
    )
    if current != before:
        raise KBError("index source changed during read: %s" % path)
    return after


def canonical_projection(document: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: document[key]
        for key in (
            "document_id",
            "path",
            "safe_path",
            "title",
            "kind",
            "scope",
            "source_device_id",
            "safe_source_device_id",
            "occurred_at",
            "is_credential",
            "safe_content",
            "content_sha256",
        )
    }


def insert_canonical_projection(
    connection: sqlite3.Connection,
    projection: Dict[str, Any],
    indexed_at: str,
) -> None:
    cursor = connection.execute(
        """
        INSERT INTO canonical_documents(
            document_id, path, title, kind, scope, source_device_id,
            is_credential, content_sha256, occurred_at, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            projection["document_id"],
            projection["path"],
            projection["title"],
            projection["kind"],
            projection["scope"],
            projection["source_device_id"],
            int(projection["is_credential"]),
            projection["content_sha256"],
            projection["occurred_at"],
            indexed_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO canonical_fts(
            rowid, path, title, kind, scope, source_device_id, content
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cursor.lastrowid,
            projection["safe_path"],
            projection["title"],
            projection["kind"],
            projection["scope"],
            projection["safe_source_device_id"],
            projection["safe_content"],
        ),
    )


def refresh_canonical_document_id(
    connection: sqlite3.Connection, document_id: str, indexed_at: str
) -> None:
    rows = connection.execute(
        """
        SELECT path, content_sha256, projection_json
        FROM canonical_index_sources
        WHERE document_id=?
        ORDER BY path
        """,
        (document_id,),
    ).fetchall()
    hashes = {clean_scalar(row["content_sha256"]) for row in rows}
    if len(hashes) > 1:
        raise ConflictError(
            "canonical id %s has conflicting documents: %s"
            % (document_id, ", ".join(clean_scalar(row["path"]) for row in rows))
        )
    existing = connection.execute(
        "SELECT rowid FROM canonical_documents WHERE document_id=?",
        (document_id,),
    ).fetchall()
    for row in existing:
        connection.execute("DELETE FROM canonical_fts WHERE rowid=?", (row["rowid"],))
    connection.execute(
        "DELETE FROM canonical_documents WHERE document_id=?", (document_id,)
    )
    if not rows:
        return
    try:
        projection = json.loads(rows[0]["projection_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise KBError(
            "invalid canonical index projection for %s" % document_id
        ) from error
    if not isinstance(projection, dict):
        raise KBError("invalid canonical index projection for %s" % document_id)
    insert_canonical_projection(connection, projection, indexed_at)


def index_history_event(
    connection: sqlite3.Connection,
    event: Dict[str, Any],
    reference: str,
    indexed_at: str,
) -> bool:
    fingerprint = event_fingerprint(event)
    existing = connection.execute(
        "SELECT event_sha256 FROM history_events WHERE event_id=?",
        (event["id"],),
    ).fetchone()
    if existing is not None:
        if clean_scalar(existing["event_sha256"]) != fingerprint:
            raise ConflictError(
                "event id %s has conflicting immutable payloads: indexed and %s"
                % (event["id"], reference)
            )
        return False
    statement, searchable = history_search_text(event)
    credential = event_is_sensitive(event)
    cursor = connection.execute(
        """
        INSERT INTO history_events(
            event_id, created_at, kind, scope, source, authority,
            status, source_device_id, bundle_id, imported_from,
            ledger_ref, event_sha256, is_credential, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["id"],
            event["created_at"],
            event["kind"],
            event["scope"],
            clean_scalar(event["source"]),
            event["authority"],
            event["status"],
            event["source_device_id"],
            event["bundle_id"] or None,
            event["imported_from"] or None,
            reference,
            fingerprint,
            int(credential),
            indexed_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO history_fts(
            rowid, event_id, kind, scope, statement, source,
            authority, status, source_device_id, bundle_id, content
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cursor.lastrowid,
            event["id"],
            event["kind"],
            event["scope"],
            statement,
            clean_scalar(event["source"]),
            event["authority"],
            event["status"],
            event["source_device_id"],
            event["bundle_id"],
            searchable,
        ),
    )
    return True


def projected_lifecycle_records(
    connection: sqlite3.Connection,
) -> List[Tuple[Dict[str, Any], Dict[str, Any], str]]:
    records: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    for row in connection.execute(
        """
        SELECT old_id, new_id, event_id, created_at, ledger_ref
        FROM lifecycle_supersessions
        """
    ):
        raw = {"old_id": row["old_id"], "new_id": row["new_id"]}
        event = {
            "id": row["event_id"],
            "created_at": row["created_at"],
            "kind": "supersession",
            "status": "active",
        }
        records.append((raw, event, row["ledger_ref"]))
    for row in connection.execute(
        """
        SELECT candidate_id, resolution, event_id, created_at, ledger_ref
        FROM lifecycle_resolutions
        """
    ):
        raw = {
            "candidate_id": row["candidate_id"],
            "resolution": row["resolution"],
        }
        event = {
            "id": row["event_id"],
            "created_at": row["created_at"],
            "kind": "candidate-resolution",
            "status": "active",
        }
        records.append((raw, event, row["ledger_ref"]))
    return records


def replace_lifecycle_projection(
    connection: sqlite3.Connection,
    records: Iterable[Tuple[Dict[str, Any], Dict[str, Any], str]],
) -> None:
    selected_edges, selected_resolutions = select_lifecycle_events(records)
    resolve_supersession_edges(
        {
            subject: clean_scalar(selected["target"])
            for subject, selected in selected_edges.items()
        }
    )
    connection.execute("DELETE FROM lifecycle_supersessions")
    connection.execute("DELETE FROM lifecycle_resolutions")
    for subject, selected in selected_edges.items():
        connection.execute(
            """
            INSERT INTO lifecycle_supersessions(
                old_id, new_id, event_id, created_at, ledger_ref
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                subject,
                selected["target"],
                selected["event_id"],
                selected["created_at"],
                selected["ledger_ref"],
            ),
        )
    for subject, selected in selected_resolutions.items():
        connection.execute(
            """
            INSERT INTO lifecycle_resolutions(
                candidate_id, resolution, event_id, created_at, ledger_ref
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                subject,
                selected["target"],
                selected["event_id"],
                selected["created_at"],
                selected["ledger_ref"],
            ),
        )


def table_max_rowid(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute("SELECT max(rowid) FROM %s" % table).fetchone()
    return int(row[0] or 0)


def record_index_projection_meta(
    connection: sqlite3.Connection,
    indexed_at: str,
    canonical_count: int,
    history_count: int,
    git_head: str = "",
    git_dirty_paths: Sequence[str] = (),
) -> None:
    values = {
        "index_projection_version": INDEX_PROJECTION_VERSION,
        "canonical_max_rowid": str(table_max_rowid(connection, "canonical_documents")),
        "history_max_rowid": str(table_max_rowid(connection, "history_events")),
        "canonical_document_count": str(canonical_count),
        "history_event_count": str(history_count),
        "last_index_update_at": indexed_at,
        "index_git_head": clean_scalar(git_head),
        "index_git_dirty_paths": stable_json(sorted(set(git_dirty_paths))),
    }
    for key, value in values.items():
        connection.execute(
            """
            INSERT INTO kb_meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )


def index_projection_ready(connection: sqlite3.Connection) -> Tuple[bool, str]:
    values = {
        clean_scalar(row["key"]): clean_scalar(row["value"])
        for row in connection.execute(
            """
            SELECT key, value FROM kb_meta
            WHERE key IN (
                'index_projection_version',
                'canonical_max_rowid',
                'history_max_rowid',
                'canonical_document_count',
                'history_event_count'
            )
            """
        )
    }
    if values.get("index_projection_version") != INDEX_PROJECTION_VERSION:
        return False, "projection-version"
    try:
        expected_canonical = int(values["canonical_max_rowid"])
        expected_history = int(values["history_max_rowid"])
        int(values["canonical_document_count"])
        int(values["history_event_count"])
    except (KeyError, ValueError):
        return False, "projection-metadata"
    actual_canonical = table_max_rowid(connection, "canonical_documents")
    actual_history = table_max_rowid(connection, "history_events")
    canonical_fts = table_max_rowid(connection, "canonical_fts")
    history_fts = table_max_rowid(connection, "history_fts")
    if (
        actual_canonical != expected_canonical
        or actual_history != expected_history
        or canonical_fts != actual_canonical
        or history_fts != actual_history
    ):
        return False, "projection-structure"
    if actual_canonical and connection.execute(
        "SELECT 1 FROM canonical_index_sources LIMIT 1"
    ).fetchone() is None:
        return False, "canonical-source-state"
    if actual_history and connection.execute(
        "SELECT 1 FROM ledger_index_sources LIMIT 1"
    ).fetchone() is None:
        return False, "ledger-source-state"
    return True, "current"


def index_projection_counts(connection: sqlite3.Connection) -> Tuple[int, int]:
    values = {
        clean_scalar(row["key"]): int(row["value"])
        for row in connection.execute(
            """
            SELECT key, value FROM kb_meta
            WHERE key IN ('canonical_document_count', 'history_event_count')
            """
        )
    }
    return values["canonical_document_count"], values["history_event_count"]


def index_projection_git_state(
    connection: sqlite3.Connection,
) -> Optional[Tuple[str, List[str]]]:
    values = {
        clean_scalar(row["key"]): clean_scalar(row["value"])
        for row in connection.execute(
            """
            SELECT key, value FROM kb_meta
            WHERE key IN ('index_git_head', 'index_git_dirty_paths')
            """
        )
    }
    head = values.get("index_git_head", "")
    raw_dirty = values.get("index_git_dirty_paths", "")
    if not head or not raw_dirty:
        return None
    try:
        dirty = json.loads(raw_dirty)
    except json.JSONDecodeError:
        return None
    if not isinstance(dirty, list) or not all(
        isinstance(path, str) and index_source_kind(path) is not None
        for path in dirty
    ):
        return None
    return head, sorted(set(dirty))


def index_source_kind(relative: str) -> Optional[str]:
    path = Path(relative)
    parts = path.parts
    suffix = path.suffix.lower()
    if parts and parts[0] == "ledger" and suffix in {".json", ".jsonl"}:
        return "ledger"
    if (
        suffix in {".md", ".json"}
        and (
            (parts and parts[0] in CANONICAL_DIRS)
            or (len(parts) == 1 and not (set(parts) & IGNORED_PARTS))
        )
        and not (set(parts) & IGNORED_PARTS)
    ):
        return "canonical"
    return None


def run_git_paths(root: Path, arguments: Sequence[str]) -> Optional[List[str]]:
    try:
        completed = subprocess.run(
            ["git", "-c", "core.quotepath=false", *arguments],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    ]


def current_git_source_state(root: Path) -> Optional[Tuple[str, List[str]]]:
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if (
        top.returncode != 0
        or head.returncode != 0
        or Path(top.stdout.strip()).resolve() != root.resolve()
    ):
        return None
    entries = run_git_paths(
        root,
        [
            "-c",
            "status.renames=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
        ],
    )
    if entries is None:
        return None
    dirty_paths = set()
    for entry in entries:
        if len(entry) < 4:
            continue
        relative = entry[3:]
        parts = Path(relative.rstrip("/")).parts
        if (
            entry.startswith("!! ")
            and relative.endswith("/")
            and parts
            and (
                parts[0] == "ledger"
                or (
                    parts[0] in CANONICAL_DIRS
                    and not (set(parts[1:]) & IGNORED_PARTS)
                )
            )
        ):
            # Git collapses a wholly ignored directory to one status entry, so
            # it cannot provide a complete per-file delta. Disable the fast
            # path and let the filesystem inventory preserve source semantics.
            return None
        if index_source_kind(relative) is not None:
            dirty_paths.add(relative)
    dirty = sorted(dirty_paths)
    return head.stdout.strip(), dirty


def git_source_delta(
    root: Path, old_head: str, old_dirty: Sequence[str]
) -> Optional[Tuple[str, List[str], List[str]]]:
    current = current_git_source_state(root)
    if current is None:
        return None
    current_head, current_dirty = current
    changed: List[str] = []
    if old_head != current_head:
        changed_paths = run_git_paths(
            root,
            [
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                old_head,
                current_head,
            ],
        )
        if changed_paths is None:
            return None
        changed = [
            path for path in changed_paths if index_source_kind(path) is not None
        ]
    candidates = sorted(set(old_dirty) | set(current_dirty) | set(changed))
    return current_head, current_dirty, candidates


def current_git_ledger_state(root: Path) -> Optional[Tuple[str, List[str]]]:
    """Git state scoped to ledger, so unrelated ignored hot files cannot
    disable the locator fast path.
    """
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if (
        top.returncode != 0
        or head.returncode != 0
        or Path(top.stdout.strip()).resolve() != root.resolve()
    ):
        return None
    entries = run_git_paths(
        root,
        [
            "-c",
            "status.renames=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
            "--",
            "ledger",
        ],
    )
    if entries is None:
        return None
    dirty = set()
    for entry in entries:
        if len(entry) < 4:
            continue
        relative = entry[3:]
        if entry.startswith("!! ") and relative.rstrip("/") == "ledger":
            # A wholly ignored ledger is not a usable per-file delta source.
            return None
        if index_source_kind(relative) == "ledger":
            dirty.add(relative)
    return head.stdout.strip(), sorted(dirty)


def git_ledger_delta(
    root: Path, old_head: str, old_dirty: Sequence[str]
) -> Optional[Tuple[str, List[str], List[str]]]:
    current = current_git_ledger_state(root)
    if current is None:
        return None
    current_head, current_dirty = current
    changed: List[str] = []
    if old_head != current_head:
        changed_paths = run_git_paths(
            root,
            [
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                old_head,
                current_head,
                "--",
                "ledger",
            ],
        )
        if changed_paths is None:
            return None
        changed = [
            path for path in changed_paths if index_source_kind(path) == "ledger"
        ]
    return (
        current_head,
        current_dirty,
        sorted(set(old_dirty) | set(current_dirty) | set(changed)),
    )


def is_resource_source(relative: str) -> bool:
    path = Path(relative)
    return bool(
        path.parts
        and path.parts[0] == "resources"
        and path.suffix.lower() == ".json"
    )


def current_git_resource_state(root: Path) -> Optional[Tuple[str, List[str]]]:
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if (
        top.returncode != 0
        or head.returncode != 0
        or Path(top.stdout.strip()).resolve() != root.resolve()
    ):
        return None
    entries = run_git_paths(
        root,
        [
            "-c",
            "status.renames=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
            "--",
            "resources",
        ],
    )
    if entries is None:
        return None
    dirty = set()
    for entry in entries:
        if len(entry) < 4:
            continue
        relative = entry[3:]
        if entry.startswith("!! ") and relative.rstrip("/") == "resources":
            return None
        if is_resource_source(relative):
            dirty.add(relative)
    return head.stdout.strip(), sorted(dirty)


def git_resource_delta(
    root: Path, old_head: str, old_dirty: Sequence[str]
) -> Optional[Tuple[str, List[str], List[str]]]:
    current = current_git_resource_state(root)
    if current is None:
        return None
    current_head, current_dirty = current
    changed: List[str] = []
    if old_head != current_head:
        changed_paths = run_git_paths(
            root,
            [
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                old_head,
                current_head,
                "--",
                "resources",
            ],
        )
        if changed_paths is None:
            return None
        changed = [path for path in changed_paths if is_resource_source(path)]
    return (
        current_head,
        current_dirty,
        sorted(set(old_dirty) | set(current_dirty) | set(changed)),
    )


def source_inventory(
    root: Path, files: Iterable[Path]
) -> Dict[str, Tuple[Path, int, int]]:
    inventory: Dict[str, Tuple[Path, int, int]] = {}
    for path in files:
        stat = path.stat()
        relative = path.relative_to(root).as_posix()
        inventory[relative] = (path, int(stat.st_size), int(stat.st_mtime_ns))
    return inventory


def candidate_source_inventory(
    root: Path, relative_paths: Iterable[str], kind: str
) -> Dict[str, Tuple[Path, int, int]]:
    """Stat only Git-reported source candidates, never the full source tree."""
    inventory: Dict[str, Tuple[Path, int, int]] = {}
    for relative in relative_paths:
        if index_source_kind(relative) != kind:
            continue
        path = root / relative
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            stat = resolved.stat()
        except (OSError, RuntimeError, ValueError):
            continue
        if not resolved.is_file():
            continue
        inventory[relative] = (resolved, int(stat.st_size), int(stat.st_mtime_ns))
    return inventory


def stored_source_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: str,
    relative_paths: Iterable[str],
) -> Dict[str, sqlite3.Row]:
    """Fetch source state with work proportional to the candidate path set."""
    rows: Dict[str, sqlite3.Row] = {}
    query = "SELECT %s FROM %s WHERE path=?" % (columns, table)
    for relative in relative_paths:
        row = connection.execute(query, (relative,)).fetchone()
        if row is not None:
            rows[relative] = row
    return rows


HOT_LOCATOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS locator_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS locator_sources (
    path TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS locator_resource_sources (
    path TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS locator_resources (
    resource_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    PRIMARY KEY(resource_id, source_path)
);
CREATE INDEX IF NOT EXISTS locator_resources_source
    ON locator_resources(source_path);
CREATE TABLE IF NOT EXISTS locator_events (
    location_key TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    reference TEXT NOT NULL,
    selector_kind TEXT NOT NULL,
    selector_value TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL,
    capture_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    safe_statement TEXT NOT NULL,
    safe_source TEXT NOT NULL,
    authority TEXT NOT NULL,
    status TEXT NOT NULL,
    source_device_id TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    credential INTEGER NOT NULL,
    searchable TEXT NOT NULL,
    lifecycle_subject TEXT NOT NULL,
    lifecycle_target TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS locator_events_id
    ON locator_events(event_id);
CREATE INDEX IF NOT EXISTS locator_events_source
    ON locator_events(source_path);
CREATE INDEX IF NOT EXISTS locator_events_lifecycle
    ON locator_events(kind);
CREATE TABLE IF NOT EXISTS locator_sensitive_terms (
    location_key TEXT NOT NULL,
    term_hash TEXT NOT NULL,
    PRIMARY KEY(location_key, term_hash)
);
CREATE INDEX IF NOT EXISTS locator_sensitive_terms_hash
    ON locator_sensitive_terms(term_hash);
CREATE VIRTUAL TABLE IF NOT EXISTS locator_event_fts USING fts5(
    location_key UNINDEXED,
    searchable,
    tokenize='trigram'
);
CREATE VIRTUAL TABLE IF NOT EXISTS locator_event_short_fts USING fts5(
    location_key UNINDEXED,
    tokens,
    tokenize='unicode61'
);
"""


def hot_locator_path(root: Path) -> Path:
    """Keep the hot cache local to a checkout and independent of cold storage."""
    root = root.expanduser().resolve()
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        location = subprocess.run(
            ["git", "rev-parse", "--git-path", "ai-knowledge/hot-locator.sqlite"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        top = location = None
    if (
        top is not None
        and location is not None
        and top.returncode == 0
        and location.returncode == 0
        and Path(top.stdout.strip()).resolve() == root
    ):
        configured = Path(location.stdout.strip())
        return configured if configured.is_absolute() else (root / configured).resolve()
    # Unit-test and exported/non-Git roots still need cold-independent capture
    # deduplication.  Real synchronized installations use the Git-private path.
    return root / "generated" / "local-state" / "hot-locator.sqlite"


class HotLocatorLock:
    """Cross-platform OS lock; process death releases it automatically."""

    def __init__(self, database: Path):
        self.path = database.with_suffix(database.suffix + ".lock")
        self.handle: Any = None

    def __enter__(self) -> "HotLocatorLock":
        durable_mkdir_parents(self.path.parent)
        self.handle = self.path.open("a+", encoding="utf-8")
        file_lock.acquire(self.handle, exclusive=True, blocking=True)
        return self

    def __exit__(self, *_unused: Any) -> None:
        if self.handle is not None:
            file_lock.release(self.handle)
            self.handle.close()


def locator_json_values(
    path: Path,
) -> Iterator[Tuple[Dict[str, Any], str, str, str]]:
    """Yield values plus a selector that can reopen only the matching record."""
    try:
        if path.suffix.lower() == ".jsonl":
            with path.open("rb") as handle:
                line_number = 0
                while True:
                    offset = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    line_number += 1
                    if not line.strip():
                        continue
                    value = json.loads(line.decode("utf-8"))
                    if isinstance(value, dict):
                        yield (
                            value,
                            "%s#%d" % (path, line_number),
                            "jsonl-offset",
                            str(offset),
                        )
            return
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise KBError("cannot parse %s: %s" % (path, error)) from error
    if isinstance(value, dict) and isinstance(value.get("events"), list):
        for index, item in enumerate(value["events"]):
            if isinstance(item, dict):
                yield item, "%s#events[%d]" % (path, index), "events-index", str(index)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, dict):
                yield item, "%s#[%d]" % (path, index), "list-index", str(index)
    elif isinstance(value, dict):
        yield value, str(path), "object", ""


def locator_short_tokens(text: str) -> str:
    folded = text.casefold()
    tokens = {
        folded[index : index + width]
        for width in (1, 2)
        for index in range(max(0, len(folded) - width + 1))
        if folded[index : index + width].strip()
    }
    return " ".join(sorted(tokens))


def locator_term_hashes(text: str) -> List[str]:
    return sorted(
        {
            sha256_text("hot-locator-sensitive-term\0" + term.casefold())
            for term in re.findall(r"\S+", text)
            if term
        }
    )


def locator_meta_set(
    connection: sqlite3.Connection, key: str, value: str
) -> None:
    connection.execute(
        """
        INSERT INTO locator_meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def locator_record_git_state(
    connection: sqlite3.Connection,
    state: Optional[Tuple[str, List[str]]],
) -> None:
    locator_meta_set(connection, "version", HOT_LOCATOR_VERSION)
    locator_meta_set(connection, "mode", "git" if state is not None else "non-git")
    locator_meta_set(connection, "git_head", state[0] if state is not None else "")
    locator_meta_set(
        connection,
        "git_dirty",
        stable_json(state[1] if state is not None else []),
    )


def locator_record_resource_git_state(
    connection: sqlite3.Connection,
    state: Optional[Tuple[str, List[str]]],
) -> None:
    locator_meta_set(
        connection, "resource_mode", "git" if state is not None else "non-git"
    )
    locator_meta_set(
        connection,
        "resource_git_head",
        state[0] if state is not None else "",
    )
    locator_meta_set(
        connection,
        "resource_git_dirty",
        stable_json(state[1] if state is not None else []),
    )


def locator_stored_git_state(
    connection: sqlite3.Connection,
) -> Tuple[str, Optional[Tuple[str, List[str]]]]:
    values = {
        clean_scalar(row["key"]): clean_scalar(row["value"])
        for row in connection.execute("SELECT key, value FROM locator_meta")
    }
    if values.get("version") != HOT_LOCATOR_VERSION:
        raise KBError("hot event locator schema is stale")
    mode = values.get("mode", "")
    if mode == "non-git":
        return mode, None
    if mode != "git" or not values.get("git_head"):
        raise KBError("hot event locator metadata is incomplete")
    try:
        dirty = json.loads(values.get("git_dirty", "[]"))
    except json.JSONDecodeError as error:
        raise KBError("hot event locator metadata is corrupt") from error
    if not isinstance(dirty, list) or not all(isinstance(item, str) for item in dirty):
        raise KBError("hot event locator metadata is corrupt")
    return mode, (values["git_head"], sorted(set(dirty)))


def locator_stored_resource_git_state(
    connection: sqlite3.Connection,
) -> Tuple[str, Optional[Tuple[str, List[str]]]]:
    values = {
        clean_scalar(row["key"]): clean_scalar(row["value"])
        for row in connection.execute(
            """
            SELECT key, value FROM locator_meta
            WHERE key IN ('resource_mode', 'resource_git_head',
                          'resource_git_dirty')
            """
        )
    }
    mode = values.get("resource_mode", "")
    if mode == "non-git":
        return mode, None
    if mode != "git" or not values.get("resource_git_head"):
        raise KBError("hot resource locator metadata is incomplete")
    try:
        dirty = json.loads(values.get("resource_git_dirty", "[]"))
    except json.JSONDecodeError as error:
        raise KBError("hot resource locator metadata is corrupt") from error
    if not isinstance(dirty, list) or not all(isinstance(item, str) for item in dirty):
        raise KBError("hot resource locator metadata is corrupt")
    return mode, (values["resource_git_head"], sorted(set(dirty)))


def locator_delete_source(connection: sqlite3.Connection, relative: str) -> None:
    keys = [
        clean_scalar(row["location_key"])
        for row in connection.execute(
            "SELECT location_key FROM locator_events WHERE source_path=?",
            (relative,),
        )
    ]
    for key in keys:
        connection.execute(
            "DELETE FROM locator_event_fts WHERE location_key=?", (key,)
        )
        connection.execute(
            "DELETE FROM locator_event_short_fts WHERE location_key=?", (key,)
        )
        connection.execute(
            "DELETE FROM locator_sensitive_terms WHERE location_key=?", (key,)
        )
    connection.execute("DELETE FROM locator_events WHERE source_path=?", (relative,))
    connection.execute("DELETE FROM locator_sources WHERE path=?", (relative,))


def locator_replace_source(
    connection: sqlite3.Connection, root: Path, relative: str, path: Path
) -> bool:
    before = stat_signature(path)
    digest = sha256_file(path)
    stored = connection.execute(
        "SELECT content_sha256, size, mtime_ns FROM locator_sources WHERE path=?",
        (relative,),
    ).fetchone()
    if (
        stored is not None
        and clean_scalar(stored["content_sha256"]) == digest
        and int(stored["size"]) == before[2]
        and int(stored["mtime_ns"]) == before[3]
    ):
        return False
    parsed = list(locator_json_values(path))
    after = assert_stable_source(path, before)
    locator_delete_source(connection, relative)
    for raw, reference, selector_kind, selector_value in parsed:
        event = normalize_event(raw, reference)
        errors = event_validation_errors(event)
        if errors:
            raise KBError(
                "invalid ledger event %s: %s" % (reference, ", ".join(errors))
            )
        immutable_fingerprint = event_fingerprint(event)
        conflicts = connection.execute(
            """
            SELECT reference, event_fingerprint
            FROM locator_events WHERE event_id=?
            """,
            (event["id"],),
        ).fetchall()
        for previous in conflicts:
            if clean_scalar(previous["event_fingerprint"]) != immutable_fingerprint:
                raise ConflictError(
                    "event id %s has conflicting immutable payloads: %s and %s"
                    % (event["id"], previous["reference"], reference)
                )
        sensitive = event_is_sensitive(event)
        if sensitive:
            # A credential may be pasted directly into statement/source while
            # value contains something else (or is absent). Never persist
            # either free-text field in the hot cache.
            safe_statement = "[credential statement hidden]"
            safe_source = "[credential source hidden]"
        else:
            safe_statement, _unused_value = history_search_text(event)
            raw_source = clean_scalar(event["source"])
            safe_source = (
                "[credential-like source hidden]"
                if content_may_contain_credential(raw_source)
                else raw_source
            )
        searchable = " ".join(
            (
                event["id"],
                event["kind"],
                event["scope"],
                safe_statement,
                safe_source,
                event["authority"],
                event["status"],
                event["source_device_id"],
                event["bundle_id"],
            )
        )
        lifecycle_subject = ""
        lifecycle_target = ""
        if event["kind"] == "supersession":
            lifecycle_subject = clean_scalar(raw.get("old_id"))
            lifecycle_target = clean_scalar(raw.get("new_id"))
        elif event["kind"] == "candidate-resolution":
            lifecycle_subject = clean_scalar(raw.get("candidate_id"))
            lifecycle_target = clean_scalar(raw.get("resolution")).lower()
        location_key = sha256_text("%s\0%s" % (relative, reference))
        connection.execute(
            """
            INSERT INTO locator_events(
                location_key, event_id, source_path, reference,
                selector_kind, selector_value, event_fingerprint,
                capture_fingerprint, created_at, kind, scope,
                safe_statement, safe_source, authority, status,
                source_device_id, bundle_id, credential, searchable,
                lifecycle_subject, lifecycle_target
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                location_key,
                event["id"],
                relative,
                reference,
                selector_kind,
                selector_value,
                immutable_fingerprint,
                capture_fingerprint(event),
                event["created_at"],
                event["kind"],
                event["scope"],
                safe_statement,
                safe_source,
                event["authority"],
                event["status"],
                event["source_device_id"],
                event["bundle_id"],
                int(sensitive),
                searchable,
                lifecycle_subject,
                lifecycle_target,
            ),
        )
        connection.execute(
            "INSERT INTO locator_event_fts(location_key, searchable) VALUES (?, ?)",
            (location_key, searchable),
        )
        connection.execute(
            """
            INSERT INTO locator_event_short_fts(location_key, tokens)
            VALUES (?, ?)
            """,
            (location_key, locator_short_tokens(searchable)),
        )
        if sensitive:
            for term_hash in locator_term_hashes(
                "%s %s"
                % (
                    clean_scalar(event.get("statement")),
                    clean_scalar(event.get("source")),
                )
            ):
                connection.execute(
                    """
                    INSERT INTO locator_sensitive_terms(location_key, term_hash)
                    VALUES (?, ?)
                    """,
                    (location_key, term_hash),
                )
    connection.execute(
        """
        INSERT INTO locator_sources(path, content_sha256, size, mtime_ns)
        VALUES (?, ?, ?, ?)
        """,
        (relative, digest, int(after.st_size), int(after.st_mtime_ns)),
    )
    return True


def resource_json_files(root: Path) -> List[Path]:
    resources = paths_for(root)["resources"]
    if not resources.is_dir():
        return []
    return sorted(
        path.resolve()
        for path in resources.rglob("*")
        if path.is_file() and path.suffix.lower() == ".json"
    )


def locator_delete_resource_source(
    connection: sqlite3.Connection, relative: str
) -> None:
    connection.execute(
        "DELETE FROM locator_resources WHERE source_path=?", (relative,)
    )
    connection.execute(
        "DELETE FROM locator_resource_sources WHERE path=?", (relative,)
    )


def locator_replace_resource_source(
    connection: sqlite3.Connection, root: Path, relative: str, path: Path
) -> bool:
    before = stat_signature(path)
    digest = sha256_file(path)
    stored = connection.execute(
        """
        SELECT content_sha256, size, mtime_ns
        FROM locator_resource_sources WHERE path=?
        """,
        (relative,),
    ).fetchone()
    if (
        stored is not None
        and clean_scalar(stored["content_sha256"]) == digest
        and int(stored["size"]) == before[2]
        and int(stored["mtime_ns"]) == before[3]
    ):
        return False
    records = list(iter_resource_records(path))
    after = assert_stable_source(path, before)
    locator_delete_resource_source(connection, relative)
    for resource_id in sorted(
        {clean_scalar(record.get("id")) for record in records if record.get("id")}
    ):
        connection.execute(
            """
            INSERT INTO locator_resources(resource_id, source_path)
            VALUES (?, ?)
            """,
            (resource_id, relative),
        )
    connection.execute(
        """
        INSERT INTO locator_resource_sources(
            path, content_sha256, size, mtime_ns
        ) VALUES (?, ?, ?, ?)
        """,
        (relative, digest, int(after.st_size), int(after.st_mtime_ns)),
    )
    return True


def connect_hot_locator(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
    except Exception:
        connection.close()
        raise
    return connection


def rebuild_hot_locator_locked(root: Path, database: Path) -> None:
    durable_mkdir_parents(database.parent)
    temporary = database.with_name(
        database.name + ".tmp-" + uuid.uuid4().hex[:12]
    )
    connection = sqlite3.connect(str(temporary), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        # Capture the baseline before enumeration. Any pull or dirty change
        # racing the scan remains visible to the next delta; recording a newer
        # state after an older scan could permanently hide a clean new file.
        ledger_git_state = current_git_ledger_state(root)
        resource_git_state = current_git_resource_state(root)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(HOT_LOCATOR_SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        for path in ledger_files(root):
            relative = path.relative_to(root).as_posix()
            locator_replace_source(connection, root, relative, path)
        for path in resource_json_files(root):
            relative = path.relative_to(root).as_posix()
            locator_replace_resource_source(connection, root, relative, path)
        locator_record_git_state(connection, ledger_git_state)
        locator_record_resource_git_state(connection, resource_git_state)
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    else:
        connection.close()
    # Windows rejects FlushFileBuffers/os.fsync on a read-only descriptor.
    # Open the completed SQLite file read/write even though no bytes change.
    with temporary.open("r+b") as handle:
        os.fsync(handle.fileno())
    for suffix in ("-wal", "-shm"):
        try:
            Path(str(database) + suffix).unlink()
        except FileNotFoundError:
            pass
    os.replace(temporary, database)
    fsync_directory(database.parent)


def ensure_hot_locator_locked(root: Path, database: Path) -> sqlite3.Connection:
    # A comprehensive negative answer is safe only when Git can describe every
    # incoming, removed, or dirty source.  An exported/non-Git directory has no
    # portable bounded way to prove that an out-of-band writer did not add or
    # mutate a file, so never rebuild on every query or return a false miss.
    # Canonical installations are private Git checkouts; exported roots must be
    # initialized as a checkout before they can use capture/search/get-resource.
    if (
        current_git_ledger_state(root) is None
        or current_git_resource_state(root) is None
    ):
        raise KBError(
            "hot locator completeness unavailable outside the canonical Git "
            "checkout; clone or initialize the private AI-Knowledge repository"
        )
    rebuilt = False
    if not database.is_file():
        rebuild_hot_locator_locked(root, database)
        rebuilt = True
    try:
        connection = connect_hot_locator(database)
        ledger_mode, ledger_stored = locator_stored_git_state(connection)
        resource_mode, resource_stored = locator_stored_resource_git_state(
            connection
        )
    except (sqlite3.DatabaseError, KBError):
        try:
            connection.close()
        except UnboundLocalError:
            pass
        rebuild_hot_locator_locked(root, database)
        rebuilt = True
        connection = connect_hot_locator(database)
        ledger_mode, ledger_stored = locator_stored_git_state(connection)
        resource_mode, resource_stored = locator_stored_resource_git_state(
            connection
        )

    current_ledger = current_git_ledger_state(root)
    current_resources = current_git_resource_state(root)
    ledger_delta: Optional[Tuple[str, List[str], List[str]]] = None
    resource_delta: Optional[Tuple[str, List[str], List[str]]] = None
    rebuild_required = False
    if ledger_mode == "non-git" and current_ledger is None:
        rebuild_required = not rebuilt
    elif ledger_mode != "git" or current_ledger is None or ledger_stored is None:
        rebuild_required = True
    else:
        ledger_delta = git_ledger_delta(
            root, ledger_stored[0], ledger_stored[1]
        )
        rebuild_required = ledger_delta is None
    if resource_mode == "non-git" and current_resources is None:
        rebuild_required = rebuild_required or not rebuilt
    elif (
        resource_mode != "git"
        or current_resources is None
        or resource_stored is None
    ):
        rebuild_required = True
    else:
        resource_delta = git_resource_delta(
            root, resource_stored[0], resource_stored[1]
        )
        rebuild_required = rebuild_required or resource_delta is None
    if rebuild_required:
        connection.close()
        rebuild_hot_locator_locked(root, database)
        return connect_hot_locator(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if ledger_delta is not None:
            head, dirty, ledger_candidates = ledger_delta
            inventory = candidate_source_inventory(
                root, ledger_candidates, "ledger"
            )
            for relative in ledger_candidates:
                current_source = inventory.get(relative)
                if current_source is None:
                    locator_delete_source(connection, relative)
                else:
                    locator_replace_source(
                        connection, root, relative, current_source[0]
                    )
            locator_record_git_state(connection, (head, dirty))
        if resource_delta is not None:
            head, dirty, resource_candidates = resource_delta
            for relative in resource_candidates:
                path = root / relative
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(root)
                except (OSError, RuntimeError, ValueError):
                    locator_delete_resource_source(connection, relative)
                    continue
                if not resolved.is_file() or not is_resource_source(relative):
                    locator_delete_resource_source(connection, relative)
                    continue
                locator_replace_resource_source(
                    connection, root, relative, resolved
                )
            locator_record_resource_git_state(connection, (head, dirty))
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        raise
    return connection


def load_locator_event(root: Path, row: sqlite3.Row) -> Dict[str, Any]:
    relative = clean_scalar(row["source_path"])
    relative_parts = Path(relative).parts
    if not relative_parts or relative_parts[0] != "ledger" or ".." in relative_parts:
        raise KBError("unsafe hot event locator path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise KBError("hot event locator escaped knowledge root") from error
    kind = clean_scalar(row["selector_kind"])
    selector = clean_scalar(row["selector_value"])
    try:
        if kind == "jsonl-offset":
            with path.open("rb") as handle:
                handle.seek(int(selector))
                raw = json.loads(handle.readline().decode("utf-8"))
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
            if kind == "events-index":
                raw = value["events"][int(selector)]
            elif kind == "list-index":
                raw = value[int(selector)]
            elif kind == "object":
                raw = value
            else:
                raise KeyError(kind)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as error:
        raise KBError(
            "hot event locator is stale for %s: %s" % (relative, error)
        ) from error
    if not isinstance(raw, dict):
        raise KBError("hot event locator target is not an event: %s" % relative)
    _, separator, suffix = clean_scalar(row["reference"]).partition("#")
    current_reference = str(path) + ("#" + suffix if separator else "")
    event = normalize_event(raw, current_reference)
    if event["id"] != clean_scalar(row["event_id"]):
        raise KBError("hot event locator id mismatch for %s" % relative)
    if event_fingerprint(event) != clean_scalar(row["event_fingerprint"]):
        raise ConflictError("hot event locator fingerprint mismatch for %s" % relative)
    return event


def hot_locator_rows(
    root: Path, operation: Any
) -> Any:
    root = root.expanduser().resolve()
    database = hot_locator_path(root)
    with HotLocatorLock(database):
        connection = ensure_hot_locator_locked(root, database)
        try:
            return operation(connection)
        except sqlite3.DatabaseError:
            connection.close()
            rebuild_hot_locator_locked(root, database)
            connection = connect_hot_locator(database)
            try:
                return operation(connection)
            finally:
                connection.close()
        finally:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def hot_locator_events_by_id(root: Path, event_id: str) -> List[Dict[str, Any]]:
    root = root.expanduser().resolve()

    def resolve(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT * FROM locator_events
            WHERE event_id=? ORDER BY source_path, reference
            """,
            (event_id,),
        ).fetchall()
        return [load_locator_event(root, row) for row in rows]

    matches = hot_locator_rows(root, resolve)
    if matches:
        return matches
    # A no-Git crash after the immutable ledger write but before locator update
    # leaves its candidate pointing directly at the source.  Repair that one
    # source without re-enumerating historical ledger files.
    candidate_path = root / "inbox" / "candidates" / (event_id + ".json")
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        relative = clean_scalar(candidate.get("ledger_path"))
        source = (root / relative).resolve()
        source.relative_to(root)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []
    if not relative or index_source_kind(relative) != "ledger" or not source.is_file():
        return []
    hot_locator_record_source(root, source)
    return hot_locator_rows(root, resolve)


def hot_locator_record_source(root: Path, source: Path) -> None:
    root = root.expanduser().resolve()
    source = source.resolve()
    relative = source.relative_to(root).as_posix()
    if index_source_kind(relative) != "ledger":
        raise KBError("not a ledger source: %s" % relative)
    database = hot_locator_path(root)
    with HotLocatorLock(database):
        connection = ensure_hot_locator_locked(root, database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            locator_replace_source(connection, root, relative, source)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def hot_locator_record_resource_source(root: Path, source: Path) -> None:
    root = root.expanduser().resolve()
    source = source.resolve()
    relative = source.relative_to(root).as_posix()
    if not is_resource_source(relative):
        raise KBError("not a resource source: %s" % relative)
    database = hot_locator_path(root)
    with HotLocatorLock(database):
        connection = ensure_hot_locator_locked(root, database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            locator_replace_resource_source(
                connection, root, relative, source
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def hot_locator_resource_matches(
    root: Path, resource_id: str
) -> List[Tuple[Path, Dict[str, Any]]]:
    root = root.expanduser().resolve()

    def resolve(
        connection: sqlite3.Connection,
    ) -> List[Tuple[Path, Dict[str, Any]]]:
        rows = connection.execute(
            """
            SELECT source_path FROM locator_resources
            WHERE resource_id=? ORDER BY source_path
            """,
            (resource_id,),
        ).fetchall()
        matches: List[Tuple[Path, Dict[str, Any]]] = []
        for row in rows:
            path = (root / clean_scalar(row["source_path"])).resolve()
            try:
                path.relative_to(root)
            except ValueError as error:
                raise KBError("hot resource locator escaped knowledge root") from error
            for record in iter_resource_records(path):
                if clean_scalar(record.get("id")) == resource_id:
                    matches.append((path, record))
        return matches

    return hot_locator_rows(root, resolve)


def locator_fts_expression(parts: Sequence[str]) -> str:
    return " AND ".join('"%s"' % part.replace('"', '""') for part in parts)


def search_hot_locator_events(
    root: Path, args: argparse.Namespace, query: str
) -> Tuple[List[Dict[str, Any]], List[Tuple[Dict[str, Any], Dict[str, Any], str]]]:
    root = root.expanduser().resolve()

    def query_locator(
        connection: sqlite3.Connection,
    ) -> Tuple[List[Dict[str, Any]], List[Tuple[Dict[str, Any], Dict[str, Any], str]]]:
        parts = [part.casefold() for part in re.findall(r"\S+", query) if part]
        long_parts = [part for part in parts if len(part) >= 3]
        short_parts = [part for part in parts if len(part) < 3]
        conditions: List[str] = []
        parameters: List[Any] = []
        text_conditions: List[str] = []
        text_parameters: List[Any] = []
        if long_parts:
            text_conditions.append(
                """
                location_key IN (
                    SELECT location_key FROM locator_event_fts
                    WHERE locator_event_fts MATCH ?
                )
                """
            )
            text_parameters.append(locator_fts_expression(long_parts))
        if short_parts:
            text_conditions.append(
                """
                location_key IN (
                    SELECT location_key FROM locator_event_short_fts
                    WHERE locator_event_short_fts MATCH ?
                )
                """
            )
            text_parameters.append(locator_fts_expression(short_parts))
        query_hashes = locator_term_hashes(query)
        sensitive_keys = set()
        if query_hashes:
            placeholders = ",".join("?" for _ in query_hashes)
            sensitive_keys = {
                clean_scalar(row["location_key"])
                for row in connection.execute(
                    """
                    SELECT location_key
                    FROM locator_sensitive_terms
                    WHERE term_hash IN (%s)
                    GROUP BY location_key
                    HAVING COUNT(DISTINCT term_hash)=?
                    """
                    % placeholders,
                    query_hashes + [len(query_hashes)],
                )
            }
        if text_conditions or sensitive_keys:
            alternatives: List[str] = []
            if text_conditions:
                alternatives.append("(" + " AND ".join(text_conditions) + ")")
                parameters.extend(text_parameters)
            if sensitive_keys:
                alternatives.append(
                    "location_key IN (%s)"
                    % ",".join("?" for _ in sensitive_keys)
                )
                parameters.extend(sorted(sensitive_keys))
            conditions.append("(" + " OR ".join(alternatives) + ")")
        if args.kind:
            conditions.append("kind=?")
            parameters.append(args.kind)
        if args.scope:
            conditions.append("scope=?")
            parameters.append(args.scope)
        where = " AND ".join(conditions) if conditions else "0"
        fetch_limit = min(1000, max(args.limit * 10, args.limit))
        rows = connection.execute(
            "SELECT * FROM locator_events WHERE %s LIMIT ?" % where,
            parameters + [fetch_limit],
        ).fetchall()
        seen: Dict[str, str] = {}
        results: List[Dict[str, Any]] = []
        for row in rows:
            searchable = clean_scalar(row["searchable"])
            if (
                not direct_text_match(query, searchable)
                and clean_scalar(row["location_key"]) not in sensitive_keys
            ):
                continue
            event_id = clean_scalar(row["event_id"])
            fingerprint = clean_scalar(row["event_fingerprint"])
            previous = seen.get(event_id)
            if previous is not None:
                if previous != fingerprint:
                    raise ConflictError(
                        "event id %s has conflicting immutable payloads" % event_id
                    )
                continue
            seen[event_id] = fingerprint
            event = load_locator_event(root, row)
            credential = bool(row["credential"])
            statement = (
                event["statement"]
                if args.include_values
                else clean_scalar(row["safe_statement"])
            )
            snippet, _ = focused_excerpt(statement or searchable, query, 360)
            item: Dict[str, Any] = {
                **retrieval_metadata(event),
                "result_type": "history",
                "id": event_id,
                "created_at": clean_scalar(row["created_at"]),
                "kind": clean_scalar(row["kind"]),
                "scope": clean_scalar(row["scope"]),
                "source": (
                    clean_scalar(event["source"])
                    if args.include_values
                    else clean_scalar(row["safe_source"])
                ),
                "authority": clean_scalar(row["authority"]),
                "status": clean_scalar(row["status"]),
                "source_device_id": clean_scalar(row["source_device_id"]),
                "bundle_id": clean_scalar(row["bundle_id"]) or None,
                "credential": credential,
                "snippet": snippet,
                "rank": direct_rank(query, "", searchable),
                "statement": statement,
            }
            if credential:
                value_present = credential_value_present(event)
                item["credential_value_status"] = (
                    "stored" if value_present else "missing"
                )
                if value_present:
                    item["_credential_value_fingerprint"] = (
                        credential_value_fingerprint(exact_credential_value(event))
                    )
                if not args.include_values:
                    item["value"] = (
                        "[stored; use --include-values]"
                        if value_present
                        else "[missing; exact credential value was not captured]"
                    )
                elif value_present:
                    item["value"] = exact_credential_value(event)
            elif "value" in event:
                item["value"] = event["value"]
            results.append(item)

        lifecycle_rows = connection.execute(
            """
            SELECT * FROM locator_events
            WHERE kind IN ('supersession', 'candidate-resolution')
            ORDER BY event_id, source_path, reference
            """
        ).fetchall()
        lifecycle_records: List[
            Tuple[Dict[str, Any], Dict[str, Any], str]
        ] = []
        lifecycle_seen = set()
        for row in lifecycle_rows:
            event_id = clean_scalar(row["event_id"])
            if event_id in lifecycle_seen:
                continue
            lifecycle_seen.add(event_id)
            raw: Dict[str, Any] = {}
            if row["kind"] == "supersession":
                raw["old_id"] = row["lifecycle_subject"]
                raw["new_id"] = row["lifecycle_target"]
            else:
                raw["candidate_id"] = row["lifecycle_subject"]
                raw["resolution"] = row["lifecycle_target"]
            event = {
                "id": event_id,
                "created_at": clean_scalar(row["created_at"]),
                "kind": clean_scalar(row["kind"]),
                "status": clean_scalar(row["status"]),
            }
            lifecycle_records.append((raw, event, clean_scalar(row["reference"])))
        return results, lifecycle_records

    return hot_locator_rows(root, query_locator)


def indexed_source_changed(
    stored: sqlite3.Row, current: Tuple[Path, int, int]
) -> bool:
    """Detect content changes even when size and mtime were preserved."""
    if (int(stored["size"]), int(stored["mtime_ns"])) != (
        current[1],
        current[2],
    ):
        return True
    return sha256_file(current[0]) != clean_scalar(stored["content_sha256"])


def rebuild_index(root: Path) -> Dict[str, Any]:
    if not cold_data_available(root):
        raise KBError(
            "cold-data-unavailable: mount the cold-data volume before rebuilding the index"
    )
    root = root.expanduser().resolve()
    indexed_at = iso_now()
    connection = connect_database(root)
    duplicate_events = 0
    try:
        # Serialize the complete source snapshot -> projection -> metadata
        # operation so concurrent automations cannot publish stale counts.
        connection.execute("BEGIN IMMEDIATE")
        git_state = current_git_source_state(root)
        canonical_paths = canonical_files(root)
        ledger_paths = ledger_files(root)
        with connection:
            connection.execute("DELETE FROM canonical_fts")
            connection.execute("DELETE FROM canonical_documents")
            connection.execute("DELETE FROM history_fts")
            connection.execute("DELETE FROM history_events")
            connection.execute("DELETE FROM lifecycle_supersessions")
            connection.execute("DELETE FROM lifecycle_resolutions")
            connection.execute("DELETE FROM canonical_index_sources")
            connection.execute("DELETE FROM ledger_index_sources")
            document_count = 0
            seen_documents: Dict[str, Tuple[str, str]] = {}
            for path in canonical_paths:
                before = stat_signature(path)
                document = canonical_document(root, path)
                after = assert_stable_source(path, before)
                projection = canonical_projection(document)
                connection.execute(
                    """
                    INSERT INTO canonical_index_sources(
                        path, document_id, content_sha256, size, mtime_ns,
                        projection_json, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document["path"],
                        document["document_id"],
                        document["content_sha256"],
                        int(after.st_size),
                        int(after.st_mtime_ns),
                        stable_json(projection),
                        indexed_at,
                    ),
                )
                previous_document = seen_documents.get(document["document_id"])
                if previous_document:
                    if previous_document[0] != document["content_sha256"]:
                        raise ConflictError(
                            "canonical id %s has conflicting documents: %s and %s"
                            % (document["document_id"], previous_document[1], document["path"])
                        )
                    continue
                seen_documents[document["document_id"]] = (
                    document["content_sha256"],
                    document["path"],
                )
                insert_canonical_projection(connection, projection, indexed_at)
                document_count += 1

            seen_events: Dict[str, Tuple[str, str]] = {}
            event_count = 0
            lifecycle_records: List[
                Tuple[Dict[str, Any], Dict[str, Any], str]
            ] = []
            for path in ledger_paths:
                before = stat_signature(path)
                for raw, reference in iter_json_values(path):
                    event = normalize_event(raw, reference)
                    errors = event_validation_errors(event)
                    if errors:
                        raise KBError("invalid ledger event %s: %s" % (reference, ", ".join(errors)))
                    fingerprint = event_fingerprint(event)
                    previous = seen_events.get(event["id"])
                    if previous:
                        if previous[0] != fingerprint:
                            raise ConflictError(
                                "event id %s has conflicting immutable payloads: %s and %s"
                                % (event["id"], previous[1], reference)
                            )
                        duplicate_events += 1
                        continue
                    seen_events[event["id"]] = (fingerprint, reference)
                    lifecycle_records.append((raw, event, reference))
                    index_history_event(connection, event, reference, indexed_at)
                    event_count += 1
                digest = sha256_file(path)
                after = assert_stable_source(path, before)
                connection.execute(
                    """
                    INSERT INTO ledger_index_sources(
                        path, content_sha256, size, mtime_ns, indexed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        path.relative_to(root).as_posix(),
                        digest,
                        int(after.st_size),
                        int(after.st_mtime_ns),
                        indexed_at,
                    ),
                )
            replace_lifecycle_projection(connection, lifecycle_records)
            connection.execute(
                "INSERT INTO kb_meta(key, value) VALUES ('last_rebuild_at', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (indexed_at,),
            )
            connection.execute(
                """
                INSERT INTO kb_meta(key, value)
                VALUES ('lifecycle_projection_version', '1')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
            record_index_projection_meta(
                connection,
                indexed_at,
                document_count,
                event_count,
                git_state[0] if git_state is not None else "",
                git_state[1] if git_state is not None else (),
            )
    finally:
        connection.close()
    return {
        "ok": True,
        "database": str(paths_for(root)["database"]),
        "canonical_documents": document_count,
        "history_events": event_count,
        "deduplicated_events": duplicate_events,
        "indexed_at": indexed_at,
        "mode": "full-rebuild",
        "detection_mode": "full-scan",
        "canonical_sources_read": len(canonical_paths),
        "ledger_sources_read": len(ledger_paths),
    }


def sqlite_corruption_error(error: sqlite3.DatabaseError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    corruption_codes = {
        getattr(sqlite3, "SQLITE_CORRUPT", 11),
        getattr(sqlite3, "SQLITE_NOTADB", 26),
    }
    text = str(error).lower()
    return code in corruption_codes or any(
        marker in text
        for marker in ("database disk image is malformed", "file is not a database")
    )


def quarantine_corrupt_index(root: Path) -> List[str]:
    """Move the disposable index aside before a source-backed reconstruction."""
    ensure_directories(root)
    database = paths_for(root)["database"]
    reports = root / ".kb-data" / "reports"
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    moved: List[str] = []
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(database) + suffix)
        if not source.exists():
            continue
        target = reports / (
            "knowledge-corrupt-%s%s" % (stamp, suffix or ".sqlite")
        )
        if target.exists():
            target = reports / (
                "knowledge-corrupt-%s-%s%s"
                % (stamp, uuid.uuid4().hex[:8], suffix or ".sqlite")
            )
        os.replace(str(source), str(target))
        moved.append(target.relative_to(root).as_posix())
    return moved


def full_rebuild_fallback(root: Path, reason: str) -> Dict[str, Any]:
    report = rebuild_index(root)
    report["fallback_reason"] = reason
    return report


def update_index(root: Path) -> Dict[str, Any]:
    """Incrementally project changed hot sources into the disposable index.

    In the normal private-Git layout, HEAD transitions plus the previous and
    current dirty sets bound discovery work to paths changed since the prior
    successful projection. Non-Git roots retain a safe metadata-scan fallback.
    The ledger is immutable: deletion or mutation of an indexed ledger source
    deliberately falls back to a source-backed full rebuild.
    """
    root = root.expanduser().resolve()
    if not cold_data_available(root):
        raise KBError(
            "cold-data-unavailable: mount the cold-data volume before updating the index"
        )
    database = paths_for(root)["database"]
    if not database.is_file():
        return full_rebuild_fallback(root, "index-missing")
    try:
        connection = connect_database(root)
    except sqlite3.DatabaseError as error:
        if not sqlite_corruption_error(error):
            raise
        quarantined = quarantine_corrupt_index(root)
        report = full_rebuild_fallback(root, "database-corrupt")
        report["quarantined"] = quarantined
        return report

    try:
        # Detection and source-state reads are part of the write transaction.
        # A second update waits and then re-evaluates against the committed
        # baseline instead of racing on stale counts or duplicate new sources.
        connection.execute("BEGIN IMMEDIATE")
        ready, reason = index_projection_ready(connection)
        if not ready:
            connection.close()
            return full_rebuild_fallback(root, reason)

        stored_git_state = index_projection_git_state(connection)
        git_delta = (
            git_source_delta(root, stored_git_state[0], stored_git_state[1])
            if stored_git_state is not None
            else None
        )
        if git_delta is not None:
            git_head, git_dirty_paths, candidate_paths = git_delta
            detection_mode = "git-delta"
            canonical_candidates = [
                path
                for path in candidate_paths
                if index_source_kind(path) == "canonical"
            ]
            ledger_candidates = [
                path
                for path in candidate_paths
                if index_source_kind(path) == "ledger"
            ]
            canonical_inventory = candidate_source_inventory(
                root, canonical_candidates, "canonical"
            )
            ledger_inventory = candidate_source_inventory(
                root, ledger_candidates, "ledger"
            )
            stored_canonical = stored_source_rows(
                connection,
                "canonical_index_sources",
                "path, document_id, content_sha256, size, mtime_ns",
                canonical_candidates,
            )
            stored_ledger = stored_source_rows(
                connection,
                "ledger_index_sources",
                "path, content_sha256, size, mtime_ns",
                ledger_candidates,
            )
            canonical_sources_checked = len(canonical_candidates)
            ledger_sources_checked = len(ledger_candidates)
        else:
            # Capture state before enumeration: a concurrent HEAD transition
            # is then necessarily visible to the following update.
            captured_git_state = current_git_source_state(root)
            git_head = captured_git_state[0] if captured_git_state is not None else ""
            git_dirty_paths = (
                captured_git_state[1] if captured_git_state is not None else []
            )
            detection_mode = "filesystem-scan"
            canonical_inventory = source_inventory(root, canonical_files(root))
            ledger_inventory = source_inventory(root, ledger_files(root))
            stored_canonical = {
                clean_scalar(row["path"]): row
                for row in connection.execute(
                    """
                    SELECT path, document_id, content_sha256, size, mtime_ns
                    FROM canonical_index_sources
                    """
                )
            }
            stored_ledger = {
                clean_scalar(row["path"]): row
                for row in connection.execute(
                    """
                    SELECT path, content_sha256, size, mtime_ns
                    FROM ledger_index_sources
                    """
                )
            }
            canonical_sources_checked = len(canonical_inventory)
            ledger_sources_checked = len(ledger_inventory)
        removed_ledger = sorted(set(stored_ledger) - set(ledger_inventory))
        changed_ledger = sorted(
            path
            for path in set(stored_ledger) & set(ledger_inventory)
            if indexed_source_changed(stored_ledger[path], ledger_inventory[path])
        )
        if removed_ledger or changed_ledger:
            connection.close()
            report = full_rebuild_fallback(root, "ledger-source-mutated")
            report["ledger_sources_removed"] = len(removed_ledger)
            report["ledger_sources_changed"] = len(changed_ledger)
            return report

        removed_canonical = sorted(set(stored_canonical) - set(canonical_inventory))
        changed_canonical = sorted(
            path
            for path in set(canonical_inventory)
            if path not in stored_canonical
            or indexed_source_changed(
                stored_canonical[path], canonical_inventory[path]
            )
        )
        new_ledger = sorted(set(ledger_inventory) - set(stored_ledger))
        indexed_at = iso_now()
        canonical_count, history_count = index_projection_counts(connection)
        inserted_events = 0
        duplicate_events = 0
        lifecycle_records: List[
            Tuple[Dict[str, Any], Dict[str, Any], str]
        ] = []

        with connection:
            affected_document_ids = {
                clean_scalar(stored_canonical[path]["document_id"])
                for path in removed_canonical
            }
            previous_presence = {
                document_id: connection.execute(
                    "SELECT 1 FROM canonical_documents WHERE document_id=?",
                    (document_id,),
                ).fetchone()
                is not None
                for document_id in affected_document_ids
            }
            for path in removed_canonical:
                connection.execute(
                    "DELETE FROM canonical_index_sources WHERE path=?", (path,)
                )

            for relative in changed_canonical:
                path = canonical_inventory[relative][0]
                before = stat_signature(path)
                document = canonical_document(root, path)
                after = assert_stable_source(path, before)
                projection = canonical_projection(document)
                previous = stored_canonical.get(relative)
                if previous is not None:
                    old_document_id = clean_scalar(previous["document_id"])
                    affected_document_ids.add(old_document_id)
                    if old_document_id not in previous_presence:
                        previous_presence[old_document_id] = (
                            connection.execute(
                                "SELECT 1 FROM canonical_documents WHERE document_id=?",
                                (old_document_id,),
                            ).fetchone()
                            is not None
                        )
                affected_document_ids.add(document["document_id"])
                if document["document_id"] not in previous_presence:
                    previous_presence[document["document_id"]] = (
                        connection.execute(
                            "SELECT 1 FROM canonical_documents WHERE document_id=?",
                            (document["document_id"],),
                        ).fetchone()
                        is not None
                    )
                connection.execute(
                    """
                    INSERT INTO canonical_index_sources(
                        path, document_id, content_sha256, size, mtime_ns,
                        projection_json, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        document_id=excluded.document_id,
                        content_sha256=excluded.content_sha256,
                        size=excluded.size,
                        mtime_ns=excluded.mtime_ns,
                        projection_json=excluded.projection_json,
                        indexed_at=excluded.indexed_at
                    """,
                    (
                        relative,
                        document["document_id"],
                        document["content_sha256"],
                        int(after.st_size),
                        int(after.st_mtime_ns),
                        stable_json(projection),
                        indexed_at,
                    ),
                )
            for document_id in sorted(affected_document_ids):
                refresh_canonical_document_id(connection, document_id, indexed_at)
                now_present = (
                    connection.execute(
                        "SELECT 1 FROM canonical_documents WHERE document_id=?",
                        (document_id,),
                    ).fetchone()
                    is not None
                )
                canonical_count += int(now_present) - int(
                    previous_presence.get(document_id, False)
                )

            existing_lifecycle = projected_lifecycle_records(connection)
            for relative in new_ledger:
                path = ledger_inventory[relative][0]
                before = stat_signature(path)
                for raw, reference in iter_json_values(path):
                    event = normalize_event(raw, reference)
                    errors = event_validation_errors(event)
                    if errors:
                        raise KBError(
                            "invalid ledger event %s: %s"
                            % (reference, ", ".join(errors))
                        )
                    if index_history_event(
                        connection, event, reference, indexed_at
                    ):
                        inserted_events += 1
                        lifecycle_records.append((raw, event, reference))
                    else:
                        duplicate_events += 1
                digest = sha256_file(path)
                after = assert_stable_source(path, before)
                connection.execute(
                    """
                    INSERT INTO ledger_index_sources(
                        path, content_sha256, size, mtime_ns, indexed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        relative,
                        digest,
                        int(after.st_size),
                        int(after.st_mtime_ns),
                        indexed_at,
                    ),
                )
            if lifecycle_records:
                replace_lifecycle_projection(
                    connection, [*existing_lifecycle, *lifecycle_records]
                )
            history_count += inserted_events
            record_index_projection_meta(
                connection,
                indexed_at,
                canonical_count,
                history_count,
                git_head,
                git_dirty_paths,
            )
    except sqlite3.DatabaseError as error:
        try:
            connection.close()
        except sqlite3.Error:
            pass
        if not sqlite_corruption_error(error):
            raise
        quarantined = quarantine_corrupt_index(root)
        report = full_rebuild_fallback(root, "database-corrupt")
        report["quarantined"] = quarantined
        return report
    finally:
        try:
            connection.close()
        except sqlite3.Error:
            pass

    return {
        "ok": True,
        "database": str(database),
        "mode": "incremental",
        "detection_mode": detection_mode,
        "canonical_documents": canonical_count,
        "history_events": history_count,
        "canonical_sources_checked": canonical_sources_checked,
        "ledger_sources_checked": ledger_sources_checked,
        "canonical_sources_read": len(changed_canonical),
        "ledger_sources_read": len(new_ledger),
        "canonical_sources_removed": len(removed_canonical),
        "history_events_inserted": inserted_events,
        "deduplicated_events": duplicate_events,
        "indexed_at": indexed_at,
    }


def init_knowledge_base(root: Path) -> Dict[str, Any]:
    ensure_directories(root)
    report = rebuild_index(root)
    report.update({"root": str(root), "source_device_id": configured_device_id(root)})
    return report


def stamped_id(prefix: str) -> str:
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return "%s-%s-%s" % (prefix, stamp, uuid.uuid4().hex[:12])


def new_event_id() -> str:
    return stamped_id("evt")


def require_writable_schema(root: Path) -> None:
    manifest = root / "MANIFEST.json"
    if manifest.is_file():
        try:
            version = str(json.loads(manifest.read_text(encoding="utf-8")).get("schema_version", ""))
        except (ValueError, OSError) as error:
            raise KBError("invalid knowledge manifest") from error
        if version != SCHEMA_VERSION:
            raise KBError("unsupported knowledge schema for writes: %s" % version)


def capture(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    require_writable_schema(root)
    created_at = iso_now()
    event_id = args.id or new_event_id()
    if not ID_RE.match(event_id):
        raise KBError("--id must match %s" % ID_RE.pattern)
    source_device_id = normalize_device_id(args.source_device_id or configured_device_id(root))
    authority = args.authority.strip()
    requested_status = (args.status or "").strip()
    status = requested_status or (
        "candidate" if authority_is_agent_derived(authority) else "active"
    )
    review_required = bool(
        status.casefold() == "candidate"
        or authority_is_agent_derived(authority)
    )
    event: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": event_id,
        "created_at": created_at,
        "kind": args.kind.strip(),
        "scope": args.scope.strip(),
        "statement": args.statement.strip(),
        "source": args.source.strip(),
        "authority": authority,
        "status": status,
        "source_device_id": source_device_id,
    }
    for field in ("source_time", "observed_at", "confirmed_at", "expires_at", "valid_from", "fact_key"):
        value = getattr(args, field, None)
        if value:
            if field != "fact_key" and parse_time(value) is None:
                raise KBError("invalid %s" % field)
            event[field] = value
    if args.value_file is not None:
        value_path = args.value_file.expanduser()
        try:
            # Preserve line endings and the final newline exactly.  This is
            # important for PEM/OpenSSH material and keeps the value itself
            # out of argv and process listings.
            with value_path.open("r", encoding="utf-8", newline="") as handle:
                event["value"] = handle.read()
        except (OSError, UnicodeError) as error:
            raise KBError("cannot read --value-file %s: %s" % (value_path, error)) from error
    elif args.value is not None:
        event["value"] = args.value
    if args.bundle_id:
        event["bundle_id"] = args.bundle_id
    errors = event_validation_errors(event)
    if errors:
        raise KBError("invalid capture: %s" % ", ".join(errors))
    if credential_value_missing(event):
        raise KBError(
            "credential-value-missing: credential captures require "
            "an exact non-placeholder value"
        )

    existing = find_events_by_id(root, event_id)
    if existing:
        fingerprint = capture_fingerprint(event)
        if all(capture_fingerprint(item) == fingerprint for item in existing):
            original = existing[0]
            materialized = materialize_resource(
                root,
                original,
                clean_scalar(original.get("ledger_ref")),
            )
            index_status = (
                "unchanged"
                if cold_data_available(root)
                else "pending-cold-data-unavailable"
            )
            return {
                "ok": True,
                "id": event_id,
                "duplicate": True,
                "source_device_id": source_device_id,
                "message": "identical immutable event already exists",
                "index_status": index_status,
                "candidate": (
                    "inbox/candidates/%s.json" % event_id
                    if (root / "inbox" / "candidates" / (event_id + ".json")).is_file()
                    else None
                ),
                "resource": (
                    materialized.relative_to(root).as_posix()
                    if materialized is not None
                    else None
                ),
            }
        raise ConflictError("event id already exists with different content: %s" % event_id)

    ledger_path = ledger_path_for_event(root, event)
    candidate_path: Optional[Path] = (
        root / "inbox" / "candidates" / (event_id + ".json")
        if review_required
        else None
    )
    if ledger_path.exists() or (
        candidate_path is not None and candidate_path.exists()
    ):
        raise ConflictError("event path already exists; refusing to overwrite %s" % event_id)
    if candidate_path is not None:
        candidate = dict(event)
        candidate.update(
            {
                "candidate_state": "pending-review",
                "ledger_path": ledger_path.relative_to(root).as_posix(),
            }
        )
        # Candidate first: a crash can leave an obvious pending item, never a
        # review-only ledger event that silently lost its decision surface.
        atomic_json_create(candidate_path, candidate)
    try:
        atomic_json_create(ledger_path, event)
    except Exception:
        # A review candidate is intentionally retained as recoverable evidence.
        raise
    # Keep the cold-independent ID/FTS projection current before reporting
    # success.  If this process dies between the ledger write and this step,
    # the next Git delta (or the candidate's direct ledger_path) repairs it.
    hot_locator_record_source(root, ledger_path)
    materialized = materialize_resource(
        root, event, ledger_path.relative_to(root).as_posix()
    )
    index_report: Optional[Dict[str, Any]] = None
    index_status = "pending-cold-data-unavailable"
    if cold_data_available(root):
        try:
            index_report = update_index(root)
            index_status = "current"
        except (KBError, OSError, sqlite3.Error):
            # A disk can be unplugged after the hot files have been atomically
            # committed.  Preserve the successful capture and defer only the
            # disposable index; do not hide unrelated index failures while the
            # cold layer is still reachable.
            if cold_data_available(root):
                raise
    return {
        "ok": True,
        "id": event_id,
        "created_at": created_at,
        "source_device_id": source_device_id,
        "ledger": ledger_path.relative_to(root).as_posix(),
        "candidate": (
            candidate_path.relative_to(root).as_posix()
            if candidate_path is not None
            else None
        ),
        "resource": (
            materialized.relative_to(root).as_posix()
            if materialized is not None
            else None
        ),
        "indexed": index_report["history_events"] if index_report else None,
        "index_status": index_status,
    }


def ledger_path_for_event(root: Path, event: Dict[str, Any]) -> Path:
    # Stable path makes concurrent cross-device claims for the same ID conflict in
    # Git even when creation timestamps differ. Legacy date partitions remain readable.
    return root / "ledger" / "by-id" / (clean_scalar(event.get("id")) + ".json")


def write_ledger_event(root: Path, event: Dict[str, Any]) -> Path:
    errors = event_validation_errors(event)
    if errors:
        raise KBError("invalid ledger event: %s" % ", ".join(errors))
    ledger_path = ledger_path_for_event(root, event)
    atomic_json_create(ledger_path, event)
    hot_locator_record_source(root, ledger_path)
    return ledger_path


def refresh_index_after_write(root: Path) -> str:
    """Refresh the disposable index after a hot-layer write, mirroring capture.

    The ledger write is already durable; a cold layer that is absent (or
    unplugged mid-run) only defers indexing, it never fails the command.
    """
    if not cold_data_available(root):
        return "pending-cold-data-unavailable"
    try:
        update_index(root)
        return "current"
    except (KBError, OSError, sqlite3.Error):
        if cold_data_available(root):
            raise
        return "pending-cold-data-unavailable"


def supersede(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    require_writable_schema(root)
    """Record an immutable supersession edge between two existing records.

    POLICY.md §4: the superseded record and its exact value are kept; the
    replacement relationship is expressed as a new ledger event, never by
    rewriting or deleting the old file.
    """
    old_id = clean_scalar(args.old_id)
    new_id = clean_scalar(args.new_id)
    if not old_id or not new_id:
        raise KBError("--old-id and --new-id are required")
    if old_id == new_id:
        raise KBError("--old-id and --new-id must be different ids")
    found = {old_id: False, new_id: False}
    old_scope = ""
    existing: Optional[Dict[str, Any]] = None
    lifecycle_records: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    for path in ledger_files(root):
        for raw, reference in iter_json_values(path):
            event = normalize_event(raw, reference)
            lifecycle_records.append((raw, event, reference))
            if event["id"] in found:
                found[event["id"]] = True
                if event["id"] == old_id and not old_scope:
                    old_scope = event["scope"]
            if (
                existing is None
                and event["kind"] == "supersession"
                and event["status"] == "active"
                and clean_scalar(raw.get("old_id")) == old_id
                and clean_scalar(raw.get("new_id")) == new_id
            ):
                existing = event
    for record_id in (old_id, new_id):
        if found[record_id]:
            continue
        matches = resource_matches(root, record_id)
        if not matches:
            raise NotFoundError("cannot supersede unknown id: %s" % record_id)
        if record_id == old_id and not old_scope:
            old_scope = clean_scalar(matches[0][1].get("scope"))
    if existing is not None:
        return {
            "ok": True,
            "id": existing["id"],
            "duplicate": True,
            "old_id": old_id,
            "new_id": new_id,
            "ledger": canonical_ledger_ref(root, existing["ledger_ref"]),
            "message": "active supersession already recorded",
            "index_status": (
                "unchanged" if cold_data_available(root) else "pending-cold-data-unavailable"
            ),
        }
    selected_edges, _ = select_lifecycle_events(lifecycle_records)
    proposed_edges = {
        subject: clean_scalar(selected["target"])
        for subject, selected in selected_edges.items()
    }
    proposed_edges[old_id] = new_id
    proposed_map = resolve_supersession_edges(proposed_edges)
    event = {
        "schema_version": SCHEMA_VERSION,
        "id": stamped_id("sup"),
        "created_at": iso_now(),
        "kind": "supersession",
        "scope": old_scope or "global",
        "statement": "%s is superseded by %s" % (old_id, new_id),
        "source": clean_scalar(args.source),
        "authority": "maintainer",
        "status": "active",
        "source_device_id": normalize_device_id(
            args.source_device_id or configured_device_id(root)
        ),
        "old_id": old_id,
        "new_id": new_id,
    }
    if args.note:
        event["note"] = args.note
    ledger_path = write_ledger_event(root, event)
    return {
        "ok": True,
        "id": event["id"],
        "duplicate": False,
        "old_id": old_id,
        "new_id": new_id,
        "superseded_by": proposed_map.get(old_id, new_id),
        "ledger": ledger_path.relative_to(root).as_posix(),
        "index_status": refresh_index_after_write(root),
    }


def remove_json_if_exact(path: Path, expected: Dict[str, Any]) -> bool:
    """Rollback only a record created by this exact failed operation."""

    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if stable_json(current) != stable_json(expected):
        return False
    try:
        path.unlink()
        fsync_directory(path.parent)
        return True
    except OSError:
        return False


def forget_hot_locator_source(root: Path, path: Path) -> None:
    """Best-effort repair of the disposable locator after rollback."""

    database = hot_locator_path(root)
    if not database.is_file():
        return
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        with HotLocatorLock(database):
            connection = sqlite3.connect(str(database), timeout=30)
            try:
                connection.execute("BEGIN IMMEDIATE")
                if index_source_kind(relative) == "ledger":
                    locator_delete_source(connection, relative)
                elif is_resource_source(relative):
                    locator_delete_resource_source(connection, relative)
                connection.commit()
            except Exception:
                connection.rollback()
            finally:
                connection.close()
    except Exception:
        # The locator is disposable and the next Git delta can rebuild it.
        return


def resolve_candidate(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    require_writable_schema(root)
    """Accept or reject an inbox review candidate, keeping a ledger trail.

    Only the mutable inbox/candidates state file changes; the original ledger
    event stays immutable and a kind=candidate-resolution event records who
    decided what.  Rejected candidates are demoted in search, never deleted.
    """
    candidate_id = clean_scalar(args.event_id)
    if not candidate_id:
        raise KBError("candidate event id is required")
    candidate_path = paths_for(root)["candidates"] / (candidate_id + ".json")
    if not candidate_path.is_file():
        raise NotFoundError("candidate not found: %s" % candidate_id)
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise KBError("cannot parse candidate %s: %s" % (candidate_path, error)) from error
    if not isinstance(candidate, dict):
        raise KBError("candidate %s is not a JSON object" % candidate_path)
    if (
        args.action == "accept"
        and event_is_credential_resource(candidate)
        and not credential_value_present(candidate)
    ):
        # This must precede both the resolution ledger write and inbox rewrite:
        # a failed credential acceptance must leave no accepted audit record.
        raise KBError(
            "credential-value-missing: credential candidates cannot be accepted "
            "without an exact non-placeholder value"
        )
    target_state = "accepted" if args.action == "accept" else "rejected"
    if clean_scalar(candidate.get("candidate_state")) == target_state:
        materialized: Optional[Path] = None
        resolution_event_id = clean_scalar(candidate.get("resolution_event"))
        resolution_ledger_ref = clean_scalar(candidate.get("resolution_ledger"))
        if (
            target_state == "accepted"
            and resolution_event_id
            and resolution_ledger_ref
        ):
            materialized = materialize_accepted_candidate(
                root,
                candidate,
                resolution_event_id=resolution_event_id,
                resolution_ledger_ref=resolution_ledger_ref,
                accepted_at=clean_scalar(
                    candidate.get("resolved_at"), candidate.get("created_at")
                ),
            )
        return {
            "ok": True,
            "id": candidate_id,
            "candidate_state": target_state,
            "duplicate": True,
            "resolved_at": candidate.get("resolved_at"),
            "candidate": candidate_path.relative_to(root).as_posix(),
            "resource": (
                materialized.relative_to(root).as_posix()
                if materialized is not None
                else None
            ),
            "message": "candidate already %s" % target_state,
        }
    resolved_at = iso_now()
    event = {
        "schema_version": SCHEMA_VERSION,
        "id": stamped_id("cres"),
        "created_at": resolved_at,
        "kind": "candidate-resolution",
        "scope": clean_scalar(candidate.get("scope"), "global"),
        "statement": "candidate %s %s" % (candidate_id, target_state),
        "source": "kb:resolve-candidate",
        "authority": "maintainer",
        "status": "active",
        "source_device_id": configured_device_id(root),
        "candidate_id": candidate_id,
        "resolution": target_state,
    }
    if args.note:
        event["note"] = args.note
    ledger_path = ledger_path_for_event(root, event)
    ledger_ref = ledger_path.relative_to(root).as_posix()
    updated = dict(candidate)
    updated["candidate_state"] = target_state
    updated["resolved_at"] = resolved_at
    updated["resolution_event"] = event["id"]
    updated["resolution_ledger"] = ledger_ref
    if args.note:
        updated["resolution_note"] = args.note
    planned_resource: Optional[Path] = None
    expected_resource: Optional[Dict[str, Any]] = None
    resource_preexisting = False
    if target_state == "accepted":
        (
            planned_resource,
            expected_resource,
            resource_preexisting,
        ) = accepted_candidate_resource_plan(
            root,
            candidate,
            resolution_event_id=event["id"],
            resolution_ledger_ref=ledger_ref,
            accepted_at=resolved_at,
        )
    materialized: Optional[Path] = None
    try:
        # All canonical conflicts are proven above.  Publish the immutable
        # resolution and resource before exposing accepted mutable state.
        written_ledger = write_ledger_event(root, event)
        if written_ledger.resolve() != ledger_path.resolve():
            raise KBError("candidate resolution ledger path changed")
        if target_state == "accepted":
            materialized = materialize_accepted_candidate(
                root,
                updated,
                resolution_event_id=event["id"],
                resolution_ledger_ref=ledger_ref,
                accepted_at=resolved_at,
            )
        atomic_json_replace(candidate_path, updated)
        index_status = refresh_index_after_write(root)
    except Exception:
        try:
            current = json.loads(candidate_path.read_text(encoding="utf-8"))
            if stable_json(current) == stable_json(updated):
                atomic_json_replace(candidate_path, candidate)
        except (OSError, json.JSONDecodeError):
            pass
        if (
            planned_resource is not None
            and expected_resource is not None
            and not resource_preexisting
            and remove_json_if_exact(planned_resource, expected_resource)
        ):
            forget_hot_locator_source(root, planned_resource)
        if remove_json_if_exact(ledger_path, event):
            forget_hot_locator_source(root, ledger_path)
        raise
    return {
        "ok": True,
        "id": candidate_id,
        "candidate_state": target_state,
        "resolved_at": resolved_at,
        "candidate": candidate_path.relative_to(root).as_posix(),
        "resolution_event": event["id"],
        "ledger": ledger_path.relative_to(root).as_posix(),
        "resource": (
            materialized.relative_to(root).as_posix()
            if materialized is not None
            else None
        ),
        "index_status": index_status,
    }


def query_terms(query: str) -> List[str]:
    return re.findall(r"\S+", query.strip())


def short_query_terms(query: str) -> List[str]:
    """Terms the trigram tokenizer cannot see (under 3 characters).

    This covers most two-character Chinese words; dropping them silently made
    indexed search both over-broad (extra terms ignored) and inconsistent
    with the hot-only path, which requires every term to match.
    """

    return [part for part in query_terms(query) if len(part) < 3]


def fts_query(query: str) -> Optional[str]:
    parts = [part for part in query_terms(query) if len(part) >= 3]
    if not parts:
        return None
    quoted = []
    for part in parts:
        quoted.append('"%s"' % part.replace('"', '""'))
    return " AND ".join(quoted)


def load_event_reference(
    reference: str, root: Path, expected_id: str, expected_fingerprint: str
) -> Optional[Dict[str, Any]]:
    """Resolve a disposable index reference against this knowledge checkout.

    Older projections contain absolute host paths. Never follow them: consider
    only existing files below this root's ledger, then verify immutable ID and
    content fingerprint before using the record. Source provenance is untouched.
    """
    base, separator, selector = str(reference).partition("#")
    parts = re.split(r"[\\/]", base)
    if ".." in parts or "\x00" in base:
        raise KBError("unsafe ledger index reference")
    root = root.resolve(strict=True)
    try:
        relative = Path(base).relative_to(root)
        suffixes = [relative.parts] if relative.parts and relative.parts[0] == "ledger" else []
    except ValueError:
        suffixes = [tuple(parts[index:]) for index, part in enumerate(parts) if part == "ledger"]
    if not suffixes:
        raise KBError("ledger index reference has no ledger-relative path")
    candidates = set()
    for suffix in suffixes:
        if any(part in ("", ".", "..") for part in suffix):
            continue
        path = root.joinpath(*suffix)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise KBError("ledger index reference escapes knowledge root") from error
        if resolved.is_file():
            candidates.add(resolved)
    matched = []
    found_selector = False
    for path in sorted(candidates):
        target = str(path) + ("#" + selector if separator else "")
        for raw, ref in iter_json_values(path):
            if separator and ref != target:
                continue
            event = normalize_event(raw, ref)
            found_selector = True
            if event["id"] != expected_id:
                continue
            if event_fingerprint(event) != expected_fingerprint:
                raise ConflictError("ledger index fingerprint differs from current source: %s" % expected_id)
            matched.append(event)
            if not separator:
                break
    if not matched and found_selector:
        raise ConflictError("ledger index source id differs from current source: %s" % expected_id)
    if len(matched) > 1:
        raise ConflictError("ambiguous ledger index reference: %s" % expected_id)
    return matched[0] if matched else None


def opaque_credential_token_present(content: str) -> bool:
    """Conservatively recognize standalone token-shaped secret material."""

    for match in OPAQUE_CREDENTIAL_TOKEN_RE.finditer(content):
        candidate = match.group(1).strip(".")
        if (
            len(candidate) < 24
            or candidate.startswith("//")
            or "://" in candidate
            or not any(character.isalpha() for character in candidate)
            or not any(character.isdigit() for character in candidate)
        ):
            continue
        # A long natural-language sentence contains whitespace and cannot
        # match.  The diversity floor avoids treating repeated padding as a
        # credential while retaining hashes, JWT-like values, and API keys.
        if len(set(candidate)) >= min(10, max(6, len(candidate) // 8)):
            return True
    return False


def content_may_contain_credential(content: str) -> bool:
    return bool(
        CREDENTIAL_HINT_RE.search(content)
        or opaque_credential_token_present(content)
    )


def focused_excerpt(content: str, query: str, maximum: int) -> Tuple[str, bool]:
    if len(content) <= maximum:
        return content, False
    folded = content.casefold()
    positions = [folded.find(part.casefold()) for part in re.findall(r"\S+", query) if part]
    positions = [position for position in positions if position >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - maximum // 3)
    end = min(len(content), start + maximum)
    if end - start < maximum:
        start = max(0, end - maximum)
    excerpt = content[start:end]
    if start:
        excerpt = "…" + excerpt[1:]
    if end < len(content):
        excerpt = excerpt[:-1] + "…"
    return excerpt, True


def enforce_context_budget(results: List[Dict[str, Any]], query: str, maximum: int) -> int:
    remaining = maximum
    truncated = 0
    for item in results:
        for field in ("snippet", "statement", "content", "value"):
            if field not in item:
                continue
            raw = scalar_text(item[field])
            if not raw:
                continue
            if remaining <= 0:
                item.pop(field, None)
                item[field + "_omitted"] = True
                truncated += 1
                continue
            bounded, was_truncated = focused_excerpt(raw, query, remaining)
            if field == "value" and not isinstance(item[field], str):
                # Structured values are returned intact only when they fit;
                # get-resource remains the exact unbounded retrieval endpoint.
                if was_truncated:
                    item.pop(field, None)
                    item[field + "_omitted"] = True
                else:
                    remaining -= len(raw)
                truncated += int(was_truncated)
                continue
            item[field] = bounded
            item[field + "_truncated"] = was_truncated
            remaining -= len(bounded)
            truncated += int(was_truncated)
    return truncated


def search_source_history(root: Path, args: argparse.Namespace, match: Optional[str]) -> List[Dict[str, Any]]:
    """Search opted-in verbatim history; an existing file is not new consent."""
    if not retrieval_settings(root)["include_raw_history"]:
        return []
    database = configured_history_database(root)
    if not database.is_file():
        return []
    connection = connect_database_read_only(database, timeout=10)
    history_filters: List[str] = []
    parameters: List[Any] = []
    if args.kind:
        normalized_kind = args.kind.strip().lower()
        if normalized_kind in {"user", "assistant"}:
            history_filters.append("e.role = ?")
            parameters.append(normalized_kind)
        elif normalized_kind not in {"history", "message", "credential", "credentials"}:
            connection.close()
            return []
    if args.scope:
        scope_hint = args.scope.split(":", 1)[-1].lower()
        history_filters.append("lower(COALESCE(e.cwd, '') || ' ' || e.thread_id) LIKE ?")
        parameters.append("%%%s%%" % scope_hint)
    filter_sql = (" AND " + " AND ".join(history_filters)) if history_filters else ""
    fetch_limit = min(3000, max(args.limit * 5, args.limit))
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_events'"
        ).fetchone()
        fts_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_events_fts'"
        ).fetchone()
        if not table or not fts_table:
            return []
        shorts = short_query_terms(args.query)
        short_likes = ["%%%s%%" % term.lower() for term in shorts]
        if match:
            short_sql = "".join(" AND lower(e.content) LIKE ?" for _ in short_likes)
            rows = connection.execute(
                """
                SELECT e.id, e.event_hash, e.source_device_id, e.source,
                       e.source_path, e.source_line, e.thread_id, e.event_id,
                       e.role, e.event_type, e.event_timestamp, e.cwd,
                       e.content, e.ingested_at,
                       snippet(source_events_fts, 0, '[', ']', '…', 28) AS snippet,
                       bm25(source_events_fts) AS rank
                FROM source_events_fts
                JOIN source_events e ON e.id = source_events_fts.rowid
                WHERE source_events_fts MATCH ?%s%s
                ORDER BY rank LIMIT ?
                """ % (short_sql, filter_sql),
                [match] + short_likes + parameters + [fetch_limit],
            ).fetchall()
        else:
            terms = query_terms(args.query) or [args.query]
            likes = ["%%%s%%" % term.lower() for term in terms]
            rows = connection.execute(
                """
                SELECT e.id, e.event_hash, e.source_device_id, e.source,
                       e.source_path, e.source_line, e.thread_id, e.event_id,
                       e.role, e.event_type, e.event_timestamp, e.cwd,
                       e.content, e.ingested_at,
                       substr(e.content, 1, 300) AS snippet, 0.0 AS rank
                FROM source_events e
                WHERE %s%s
                LIMIT ?
                """ % (" AND ".join("lower(e.content) LIKE ?" for _ in likes), filter_sql),
                likes + parameters + [fetch_limit],
            ).fetchall()
    finally:
        connection.close()

    results: List[Dict[str, Any]] = []
    requested_kind = (args.kind or "").strip().lower()
    for row in rows:
        content = row["content"]
        credential_suspected = bool(
            row["event_type"] == "tool-result-evidence"
            or content_may_contain_credential(content)
        )
        if requested_kind in {"credential", "credentials"} and not credential_suspected:
            continue
        timestamp = row["event_timestamp"] or row["ingested_at"]
        snippet = row["snippet"]
        if credential_suspected and not args.include_values:
            snippet = "[credential-like source hidden; rerun with --include-values]"
        item: Dict[str, Any] = {
            "result_type": "source-history",
            "id": "history:%s" % row["event_hash"],
            "event_id": row["event_id"],
            "source": row["source"],
            "source_ref": "%s:%s" % (row["source_path"], row["source_line"]),
            "source_device_id": row["source_device_id"],
            "thread_id": row["thread_id"],
            "timestamp": timestamp,
            "role": row["role"],
            "kind": "history-message",
            "scope": row["cwd"] or "global",
            "credential_suspected": credential_suspected,
            "snippet": snippet,
            "rank": row["rank"],
        }
        if args.include_values:
            item["content"] = content
        results.append(item)
        if len(results) >= args.limit:
            break
    return results


def hot_canonical_files(root: Path) -> List[Path]:
    """Canonical files that belong to the Git-synchronized hot fact layer."""
    root = root.expanduser().resolve()
    result = []
    for path in canonical_files(root):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in CANONICAL_DIRS:
            result.append(path)
    return result


def direct_text_match(query: str, text: str) -> bool:
    folded = text.casefold()
    parts = [part.casefold() for part in re.findall(r"\S+", query) if part]
    return bool(parts) and all(part in folded for part in parts)


def direct_rank(query: str, title: str, content: str) -> float:
    folded_query = query.casefold()
    folded_title = title.casefold()
    folded_content = content.casefold()
    if folded_title == folded_query:
        return 0.0
    title_position = folded_title.find(folded_query)
    if title_position >= 0:
        return 1.0 + title_position / 10000.0
    content_position = folded_content.find(folded_query)
    if content_position >= 0:
        return 2.0 + content_position / 1000000.0
    # Multi-term queries can match without the exact phrase.
    return 3.0


def search_hot_layer(
    root: Path,
    args: argparse.Namespace,
    query: str,
    *,
    include_source_history: bool = False,
) -> Dict[str, Any]:
    """Search Git-hot canonical files and ledger without touching .kb-data."""
    results: List[Dict[str, Any]] = []
    for path in hot_canonical_files(root):
        document = canonical_document(root, path)
        if args.kind and document["kind"] != args.kind:
            continue
        if args.scope and document["scope"] != args.scope:
            continue
        searchable = " ".join(
            (
                document["safe_path"],
                document["title"],
                document["kind"],
                document["scope"],
                document["safe_source_device_id"],
                document["safe_content"],
            )
        )
        if not direct_text_match(query, searchable):
            continue
        snippet, _ = focused_excerpt(document["safe_content"], query, 480)
        item: Dict[str, Any] = {
            **retrieval_metadata(document),
            "source": document.get("source", document["path"]),
            "result_type": "canonical",
            "id": document["document_id"],
            "path": (
                document["path"]
                if args.include_values
                else document["safe_path"]
            ),
            "title": document["title"],
            "kind": document["kind"],
            "scope": document["scope"],
            "authority": document["authority"],
            "status": document["status"],
            "source_device_id": (
                document["source_device_id"]
                if args.include_values
                else document["safe_source_device_id"]
            ),
            "created_at": document["occurred_at"] or None,
            "credential": bool(document["is_credential"]),
            "snippet": snippet,
            "rank": direct_rank(query, document["title"], searchable),
        }
        if document["is_credential"]:
            item["credential_value_status"] = (
                "stored"
                if document["credential_value_present"]
                else "missing"
            )
            if document["credential_value_present"]:
                item["_credential_value_fingerprint"] = document[
                    "credential_value_fingerprint"
                ]
        if args.include_values:
            item["content"] = document["full_content"]
        elif document["is_credential"]:
            item["value"] = (
                "[stored; use --include-values]"
                if document["credential_value_present"]
                else "[missing; exact credential value was not captured]"
            )
        results.append(item)

    ledger_results, lifecycle_records = search_hot_locator_events(
        root, args, query
    )
    results.extend(ledger_results)
    if include_source_history:
        results.extend(search_source_history(root, args, fts_query(query)))

    settings = retrieval_settings(root)
    superseded_by, resolutions = lifecycle_maps_from_records(lifecycle_records)
    annotate_lifecycle(results, superseded_by, resolutions)
    results.sort(
        key=lambda item: retrieval_sort_key(
            item, settings["prefer_canonical_over_raw_history"]
        )
    )
    results = fold_exact_result_duplicates(results)[: args.limit]
    truncated_fields = enforce_context_budget(
        results, query, settings["maximum_context_characters"]
    )
    reported_query = query
    if content_may_contain_credential(query) and not args.include_values:
        reported_query = "[credential-like query hidden]"
    return {
        "ok": True,
        "query": reported_query,
        "count": len(results),
        "results": results,
        "context_budget_characters": settings["maximum_context_characters"],
        "truncated_fields": truncated_fields,
        "retrieval_mode": "hot-only",
        "cold_data_available": False,
        "history_available": bool(
            include_source_history and configured_history_database(root).is_file()
        ),
    }


def search_once(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    query = args.query.strip()
    if not query:
        raise KBError("search query cannot be empty")
    if not cold_data_available(root):
        return search_hot_layer(root, args, query)

    def index_unavailable_fallback(reason: str) -> Dict[str, Any]:
        fallback = search_hot_layer(
            root, args, query, include_source_history=True
        )
        fallback["cold_data_available"] = True
        fallback["knowledge_index_status"] = reason
        return fallback

    database = paths_for(root)["database"]
    if not database.is_file():
        return index_unavailable_fallback("missing-read-only-fallback")
    try:
        connection = connect_database_read_only(database, timeout=2)
    except sqlite3.Error:
        return index_unavailable_fallback("unreadable-read-only-fallback")
    match = fts_query(query)
    filters: List[str] = []
    parameters: List[Any] = []
    if args.kind:
        filters.append("d.kind = ?")
        parameters.append(args.kind)
    if args.scope:
        filters.append("d.scope = ?")
        parameters.append(args.scope)
    document_where = (" AND " + " AND ".join(filters)) if filters else ""
    event_filters = [item.replace("d.", "e.") for item in filters]
    event_where = (" AND " + " AND ".join(event_filters)) if event_filters else ""
    # Fetch beyond --limit so superseded/rejected rows cannot crowd live
    # records out of the candidate set before status-aware re-ranking.
    fetch_limit = min(1000, max(args.limit * 5, args.limit))
    document_text = (
        "lower(canonical_fts.title || ' ' || canonical_fts.kind || ' ' || "
        "canonical_fts.scope || ' ' || canonical_fts.content)"
    )
    event_text = (
        "lower(history_fts.kind || ' ' || history_fts.scope || ' ' || "
        "history_fts.statement || ' ' || history_fts.content)"
    )
    # Terms under 3 characters are invisible to the trigram tokenizer; apply
    # them as additional LIKE predicates so 中文双字词 still constrain indexed
    # results exactly like they constrain the hot-only path.
    shorts = short_query_terms(query)
    short_likes = ["%%%s%%" % term.lower() for term in shorts]
    try:
        if match:
            document_short_sql = "".join(" AND %s LIKE ?" % document_text for _ in short_likes)
            event_short_sql = "".join(" AND %s LIKE ?" % event_text for _ in short_likes)
            documents = connection.execute(
                """
                SELECT d.*, snippet(canonical_fts, 5, '[', ']', '…', 22) AS snippet,
                       bm25(canonical_fts) AS rank
                FROM canonical_fts
                JOIN canonical_documents d ON d.rowid = canonical_fts.rowid
                WHERE canonical_fts MATCH ?%s%s
                ORDER BY rank LIMIT ?
                """ % (document_short_sql, document_where),
                [match] + short_likes + parameters + [fetch_limit],
            ).fetchall()
            events = connection.execute(
                """
                SELECT e.*, snippet(history_fts, 3, '[', ']', '…', 22) AS snippet,
                       bm25(history_fts) AS rank
                FROM history_fts
                JOIN history_events e ON e.rowid = history_fts.rowid
                WHERE history_fts MATCH ?%s%s
                ORDER BY rank LIMIT ?
                """ % (event_short_sql, event_where),
                [match] + short_likes + parameters + [fetch_limit],
            ).fetchall()
        else:
            terms = query_terms(query) or [query]
            likes = ["%%%s%%" % term.lower() for term in terms]
            documents = connection.execute(
                """
                SELECT d.*, substr(canonical_fts.content, 1, 240) AS snippet, 0.0 AS rank
                FROM canonical_fts
                JOIN canonical_documents d ON d.rowid = canonical_fts.rowid
                WHERE %s%s
                LIMIT ?
                """ % (" AND ".join("%s LIKE ?" % document_text for _ in likes), document_where),
                likes + parameters + [fetch_limit],
            ).fetchall()
            events = connection.execute(
                """
                SELECT e.*, substr(history_fts.statement, 1, 240) AS snippet, 0.0 AS rank
                FROM history_fts
                JOIN history_events e ON e.rowid = history_fts.rowid
                WHERE %s%s
                LIMIT ?
                """ % (" AND ".join("%s LIKE ?" % event_text for _ in likes), event_where),
                likes + parameters + [fetch_limit],
            ).fetchall()
        superseded_by, resolutions = lifecycle_maps_for_read(root, connection)
    except sqlite3.Error:
        return index_unavailable_fallback("invalid-read-only-fallback")
    finally:
        connection.close()

    results: List[Dict[str, Any]] = []
    for row in documents:
        document = canonical_document(root, root / row["path"])
        full_content: Optional[str] = (
            document["full_content"] if args.include_values else None
        )
        value_present: Optional[bool] = document["credential_value_present"]
        item = {
            **retrieval_metadata(document),
            "source": document.get("source", document["path"]),
            "result_type": "canonical",
            "id": row["document_id"],
            "path": (
                row["path"]
                if args.include_values or not row["is_credential"]
                else document["safe_path"]
            ),
            "title": row["title"],
            "kind": row["kind"],
            "scope": row["scope"],
            "source_device_id": (
                row["source_device_id"]
                if args.include_values or not row["is_credential"]
                else document["safe_source_device_id"]
            ),
            "created_at": row["occurred_at"] or None,
            "authority": document["authority"],
            "status": document["status"],
            "credential": bool(row["is_credential"]),
            "snippet": row["snippet"],
            "rank": row["rank"],
        }
        if row["is_credential"]:
            item["credential_value_status"] = (
                "stored" if value_present else "missing"
            )
            if value_present:
                item["_credential_value_fingerprint"] = document[
                    "credential_value_fingerprint"
                ]
        if args.include_values:
            item["content"] = full_content
        elif row["is_credential"]:
            item["value"] = (
                "[stored; use --include-values]"
                if value_present
                else "[missing; exact credential value was not captured]"
            )
        results.append(item)
    for row in events:
        item = {
            "result_type": "history",
            "id": row["event_id"],
            "created_at": row["created_at"],
            "kind": row["kind"],
            "scope": row["scope"],
            "source": row["source"],
            "authority": row["authority"],
            "status": row["status"],
            "source_device_id": row["source_device_id"],
            "bundle_id": row["bundle_id"],
            "credential": bool(row["is_credential"]),
            "snippet": row["snippet"],
            "rank": row["rank"],
        }
        event = load_event_reference(row["ledger_ref"], root, row["event_id"], row["event_sha256"])
        item["source_available"] = event is not None
        if event is not None:
            item.update(retrieval_metadata(event))
            item["statement"] = (
                scrub_exact_values(event["statement"], sensitive_values(event))
                if row["is_credential"] and not args.include_values
                else event["statement"]
            )
            if row["is_credential"]:
                value_present = credential_value_present(event)
                item["credential_value_status"] = (
                    "stored" if value_present else "missing"
                )
                if value_present:
                    item["_credential_value_fingerprint"] = (
                        credential_value_fingerprint(exact_credential_value(event))
                    )
                if not args.include_values:
                    item["value"] = (
                        "[stored; use --include-values]"
                        if value_present
                        else "[missing; exact credential value was not captured]"
                    )
                elif value_present:
                    item["value"] = exact_credential_value(event)
            elif "value" in event:
                item["value"] = event["value"]
        results.append(item)
    results.extend(search_source_history(root, args, match))
    annotate_lifecycle(results, superseded_by, resolutions)
    settings = retrieval_settings(root)
    results.sort(
        key=lambda item: retrieval_sort_key(
            item, settings["prefer_canonical_over_raw_history"]
        )
    )
    results = fold_exact_result_duplicates(results)[: args.limit]
    truncated_fields = enforce_context_budget(
        results, query, settings["maximum_context_characters"]
    )
    reported_query = query
    if content_may_contain_credential(query) and not args.include_values:
        reported_query = "[credential-like query hidden]"
    return {
        "ok": True,
        "query": reported_query,
        "count": len(results),
        "results": results,
        "context_budget_characters": settings["maximum_context_characters"],
        "truncated_fields": truncated_fields,
        "retrieval_mode": "indexed",
        "cold_data_available": True,
        "history_available": configured_history_database(root).is_file(),
    }


def search(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    """Read-only search with bounded, configured aliases and explicit coverage."""
    import copy
    settings_path = root / "config" / "kb.json"
    settings = {}
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text(encoding="utf-8")).get("retrieval", {})
    local_args = copy.copy(args)
    for name in ("scope_aliases", "query_aliases"):
        mapping = settings.get(name, {})
        if (not isinstance(mapping, dict)
                or any(not isinstance(k, str) or not k or not isinstance(v, list)
                       or any(not isinstance(a, str) or not a for a in v)
                       for k, v in mapping.items())):
            raise KBError("invalid retrieval %s" % name)
    scopes = settings.get("scope_aliases", {})
    candidates = [canonical for canonical, aliases in scopes.items()
                  if args.scope == canonical or args.scope in aliases]
    if len(candidates) > 1:
        raise KBError("ambiguous scope alias")
    if candidates:
        local_args.scope = candidates[0]
    queries = [args.query]
    for canonical, aliases in settings.get("query_aliases", {}).items():
        for alias in [canonical, *aliases]:
            if alias and alias.casefold() in args.query.casefold():
                for target in [canonical, *aliases]:
                    variant = re.sub(re.escape(alias), lambda _: target, args.query, flags=re.IGNORECASE)
                    if variant not in queries and len(queries) < 8:
                        queries.append(variant)
    reports = []
    for query in queries:
        local_args.query = query
        reports.append(search_once(root, local_args))
    identities = {(item.get("scope"), item.get("fact_key"))
                  for result in reports for item in result["results"] if item.get("fact_key")}
    for scope, key in sorted(identities)[:20]:
        local_args.query, local_args.scope = key, scope
        local_args.limit = 1000
        reports.append(search_once(root, local_args))
    report = reports[0]
    combined = [item for result in reports for item in result["results"]]
    for item in combined:
        # SQL BM25 includes IDs and metadata lengths. Rank semantic fields
        # consistently across indexed and hot-only modes so a short imported
        # record ID cannot outweigh an otherwise identical newer source.
        item["rank"] = min(direct_rank(q, clean_scalar(item.get("title")),
                           clean_scalar(item.get("statement", item.get("snippet"))))
                           for q in queries)
    # Lifecycle annotations for sibling retrieval must be applied together.
    superseded_by = {item['id']: item['superseded_by'] for item in combined if item.get('superseded_by')}
    resolutions = {item['id']: item['candidate_state'] for item in combined if item.get('candidate_state')}
    annotate_lifecycle(combined, superseded_by, resolutions)
    combined.sort(key=lambda item: retrieval_sort_key(item, retrieval_settings(root)["prefer_canonical_over_raw_history"]))
    report["results"] = fold_exact_result_duplicates(combined)[:args.limit]
    report["count"] = len(report["results"])
    report["current_count"] = sum(bool(item.get("current_eligible")) for item in report["results"])
    report["conclusion"] = "current-evidence" if report["current_count"] else (
        "current-value-unconfirmed" if report["count"] else "not-found-in-local-snapshot")
    report["coverage"] = "local-snapshot; remote freshness and unshared history not established"
    report["alias_queries_attempted"] = len(queries)
    report["conflict_identity_coverage"] = "bounded-to-20-identities-and-1000-siblings"
    report["raw_history_included"] = retrieval_settings(root)["include_raw_history"]
    report["result_limit"] = args.limit
    report["candidate_coverage"] = "bounded-lexical-candidates"
    report["truncated_fields"] += enforce_context_budget(report["results"], args.query, report["context_budget_characters"])
    return report


def get_history(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    if not cold_data_available(root):
        raise NotFoundError(
            "cold-data-unavailable: raw history requires the mounted cold-data volume"
        )
    database = configured_history_database(root)
    if not database.is_file():
        raise NotFoundError("history database is missing")
    identifier = args.history_id.removeprefix("history:").strip()
    connection = sqlite3.connect(str(database), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT event_hash, source_device_id, source, source_path, source_line,
                   thread_id, event_id, role, event_type, event_timestamp, cwd,
                   content, ingested_at
            FROM source_events
            WHERE event_hash = ? OR event_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (identifier, identifier),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise NotFoundError("history event not found: %s" % identifier)
    suspected = bool(
        row["event_type"] == "tool-result-evidence"
        or content_may_contain_credential(row["content"])
    )
    payload: Dict[str, Any] = {
        "ok": True,
        "id": "history:%s" % row["event_hash"],
        "event_id": row["event_id"],
        "source": row["source"],
        "source_ref": "%s:%s" % (row["source_path"], row["source_line"]),
        "source_device_id": row["source_device_id"],
        "thread_id": row["thread_id"],
        "timestamp": row["event_timestamp"] or row["ingested_at"],
        "role": row["role"],
        "scope": row["cwd"] or "global",
        "credential_suspected": suspected,
    }
    if suspected and not args.include_values:
        payload["content"] = "[credential-like source hidden; rerun with --include-values]"
    else:
        payload["content"] = row["content"]
    return payload


def iter_resource_records(path: Path) -> Iterator[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise KBError("cannot parse resource %s: %s" % (path, error)) from error

    def walk(node: Any) -> Iterator[Dict[str, Any]]:
        if isinstance(node, dict):
            if node.get("id"):
                yield node
            for child in node.values():
                if isinstance(child, (dict, list)):
                    yield from walk(child)
        elif isinstance(node, list):
            for child in node:
                yield from walk(child)

    yield from walk(value)


def resource_matches(root: Path, resource_id: str) -> List[Tuple[Path, Dict[str, Any]]]:
    return hot_locator_resource_matches(root, resource_id)


def redact_resource(record: Any) -> Any:
    exact_values = sensitive_values(record)

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            result: Dict[str, Any] = {}
            for key, child in value.items():
                normalized_key = normalize_field_name(key)
                if normalized_key in SENSITIVE_KEYS:
                    result[key] = "[stored; use --include-values]"
                else:
                    result[key] = redact(child)
            return result
        if isinstance(value, list):
            return [redact(child) for child in value]
        if isinstance(value, str):
            return scrub_exact_values(value, exact_values)
        return value

    return redact(record)


def get_resource(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    matches = resource_matches(root, args.resource_id)
    if not matches:
        raise NotFoundError("resource not found: %s" % args.resource_id)
    fingerprints = {sha256_text(stable_json(record)) for _, record in matches}
    if len(fingerprints) > 1:
        raise ConflictError(
            "resource id %s resolves to conflicting records: %s"
            % (args.resource_id, ", ".join(str(path.relative_to(root)) for path, _ in matches))
        )
    path, record = matches[0]
    kind = clean_scalar(record.get("kind", record.get("type")), "resource")
    credential = (
        is_credential_path(path.relative_to(root))
        or event_is_credential_resource(record)
    )
    relative_path = path.relative_to(root).as_posix()
    safe_relative_path = (
        scrub_exact_values(relative_path, sensitive_values(record))
        if credential
        else relative_path
    )
    if credential and not credential_value_present(record):
        raise UnhealthyError(
            {
                "ok": False,
                "error": "credential-value-missing",
                "id": args.resource_id,
                "path": safe_relative_path,
                "credential": True,
                "credential_value_status": "missing",
                "record": redact_resource(record),
            }
        )
    result_record = record if args.include_values or not credential else redact_resource(record)
    payload = {
        "ok": True,
        "id": args.resource_id,
        "path": (
            relative_path
            if args.include_values or not credential
            else safe_relative_path
        ),
        "credential": credential,
        "record": result_record,
    }
    # Superseded resources stay readable (rotation history), but every reader
    # is told which id is current now.
    superseded_by, _ = lifecycle_maps_for_read(root)
    replacement = superseded_by.get(clean_scalar(args.resource_id))
    if replacement:
        payload["superseded_by"] = replacement
    return payload


def expected_devices(root: Path, cli_devices: Sequence[str], stale_days: Optional[int]) -> Tuple[List[Dict[str, Any]], int]:
    registration_fields = (
        "platform",
        "history_storage",
        "history_status",
        "initial_scan_verified",
        "history_events",
        "source_files",
        "last_capture_completed_at",
        "last_registered_at",
        "first_initialized_at",
        "hot_only_reason",
        "raw_history_shared",
        "bundle_shared",
    )

    def expected_entry(entry: Mapping[str, Any], device_id: str) -> Dict[str, Any]:
        normalized = {
            "id": normalize_device_id(device_id),
            "name": entry.get("name"),
            "expected": True,
            "stale_after_days": entry.get(
                "stale_after_days",
                default_stale,
            ),
        }
        for key in registration_fields:
            if key in entry:
                normalized[key] = entry[key]
        return normalized

    path = paths_for(root)["devices"]
    configured: List[Dict[str, Any]] = []
    default_stale = stale_days if stale_days is not None else 14
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KBError("cannot parse device registry %s: %s" % (path, error)) from error
        if isinstance(data, dict):
            if stale_days is None and isinstance(data.get("stale_after_days"), int):
                default_stale = data["stale_after_days"]
            entries = data.get("devices", [])
        elif isinstance(data, list):
            entries = data
        else:
            entries = []
        for entry in entries:
            if isinstance(entry, str):
                configured.append({"id": normalize_device_id(entry), "expected": True})
            elif isinstance(entry, dict):
                device_id = entry.get("id") or entry.get("source_device_id") or entry.get("device_id")
                if device_id and entry.get("expected", True):
                    configured.append(expected_entry(entry, str(device_id)))
    device_directory = root / "config" / "devices"
    if device_directory.is_dir():
        by_id = {entry["id"]: entry for entry in configured}
        for device_path in sorted(device_directory.glob("*.json")):
            try:
                entry = json.loads(device_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(entry, dict) or not entry.get("id") or not entry.get("expected", True):
                continue
            device_id = normalize_device_id(str(entry["id"]))
            by_id[device_id] = expected_entry(entry, device_id)
        configured = [by_id[key] for key in sorted(by_id)]
    known = {entry["id"] for entry in configured}
    for device_id in cli_devices:
        normalized = normalize_device_id(device_id)
        if normalized not in known:
            configured.append({"id": normalized, "expected": True})
            known.add(normalized)
    return configured, default_stale


def remote_registration_evidence(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate Git-synchronized evidence for Windows-local history."""
    windows_local = bool(
        clean_scalar(entry.get("platform")).casefold() == "windows"
        and entry.get("history_storage") == "local"
    )
    if not windows_local:
        return {
            "ok": False,
            "status": "not-applicable",
            "last_seen": None,
            "history_events": 0,
        }
    try:
        history_events = max(0, int(entry.get("history_events", 0)))
        source_files = max(0, int(entry.get("source_files", 0)))
    except (TypeError, ValueError):
        return {
            "ok": False,
            "status": "invalid-registration-counts",
            "last_seen": None,
            "history_events": 0,
        }
    private_history = bool(
        entry.get("raw_history_shared") is False
        and entry.get("bundle_shared") is False
    )
    verified_capture = bool(
        entry.get("initial_scan_verified") is True
        and entry.get("history_status") == "local-history-captured"
        and history_events > 0
        and source_files > 0
        and private_history
    )
    hot_only = bool(
        entry.get("initial_scan_verified") is False
        and entry.get("history_status") == "hot-only"
        and entry.get("hot_only_reason")
        in ("no-history-sources-found", "history-sources-empty")
        and history_events == 0
        and source_files == 0
        and private_history
    )
    if verified_capture:
        timestamp = entry.get("last_capture_completed_at")
    elif hot_only:
        timestamp = entry.get("last_registered_at") or entry.get(
            "first_initialized_at"
        )
    else:
        timestamp = None
    parsed = parse_time(timestamp)
    complete = bool((verified_capture or hot_only) and parsed is not None)
    return {
        "ok": complete,
        "status": (
            "verified-on-source-device"
            if complete and verified_capture
            else "hot-only-no-local-history"
            if complete and hot_only
            else "incomplete"
        ),
        "last_seen": clean_scalar(timestamp) if complete else None,
        "history_events": history_events if verified_capture else 0,
    }


def canonical_resource_health(
    root: Path, superseded_by: Optional[Dict[str, str]] = None
) -> List[str]:
    issues: List[str] = []
    seen: Dict[str, Tuple[str, str]] = {}
    replacements = superseded_by or {}
    resources = paths_for(root)["resources"]
    if not resources.is_dir():
        return issues
    for path in sorted(resources.rglob("*.json")):
        try:
            records = list(iter_resource_records(path))
        except KBError as error:
            issues.append(str(error))
            continue
        for record in records:
            record_id = clean_scalar(record.get("id"), "<missing-id>")
            fingerprint = sha256_text(stable_json(record))
            previous = seen.get(record_id)
            if previous and previous[0] != fingerprint:
                issues.append(
                    "resource id %s conflicts between %s and %s"
                    % (record_id, previous[1], path.relative_to(root))
                )
            elif not previous:
                seen[record_id] = (fingerprint, path.relative_to(root).as_posix())
            if not record.get("source"):
                issues.append("resource %s in %s has no source" % (record_id, path.relative_to(root)))
            if not (record.get("created_at") or record.get("observed_at") or record.get("updated_at")):
                issues.append("resource %s in %s has no timestamp" % (record_id, path.relative_to(root)))
            credential = (
                is_credential_path(path.relative_to(root))
                or event_is_credential_resource(record)
            )
            if credential and not credential_value_present(record):
                replacement_id = replacements.get(record_id)
                if replacement_id and credential_resource_has_exact_value(
                    root, replacement_id
                ):
                    continue
                issues.append(
                    "credential-value-missing: resource %s in %s"
                    % (record_id, path.relative_to(root))
                )
    return issues


def accepted_candidate_health(root: Path) -> List[str]:
    """Verify accepted review state has a matching immutable/materialized trail."""

    issues: List[str] = []
    candidates = paths_for(root)["candidates"]
    if not candidates.is_dir():
        return issues
    for path in sorted(candidates.glob("*.json")):
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            issues.append(
                "accepted-candidate-state-invalid: %s: %s"
                % (path.relative_to(root), error)
            )
            continue
        if (
            not isinstance(candidate, dict)
            or clean_scalar(candidate.get("candidate_state")).lower()
            != "accepted"
        ):
            continue
        candidate_id = clean_scalar(candidate.get("id"), path.stem)
        resolution_id = clean_scalar(candidate.get("resolution_event"))
        resolution_ref = clean_scalar(candidate.get("resolution_ledger"))
        resolved_at = clean_scalar(
            candidate.get("resolved_at"), candidate.get("created_at")
        )
        valid_resolution = False
        if resolution_id and resolution_ref:
            try:
                ledger_path = (root / resolution_ref).resolve()
                ledger_path.relative_to(root.resolve())
                for raw, _ in iter_json_values(ledger_path):
                    if (
                        clean_scalar(raw.get("id")) == resolution_id
                        and normalize_kind_name(raw.get("kind"))
                        == "candidate-resolution"
                        and clean_scalar(raw.get("candidate_id"))
                        == candidate_id
                        and clean_scalar(raw.get("resolution")).lower()
                        == "accepted"
                    ):
                        valid_resolution = True
                        break
            except (OSError, ValueError, KBError):
                valid_resolution = False
        if not valid_resolution:
            issues.append(
                "accepted-candidate-resolution-missing: %s" % candidate_id
            )
        if credential_value_missing(candidate):
            issues.append(
                "credential-value-missing: accepted candidate %s"
                % candidate_id
            )
            continue
        promoted = dict(candidate)
        promoted.update(
            {
                "status": "active",
                "authority": "maintainer-accepted-inference",
                "acceptance_event_id": resolution_id,
                "accepted_candidate_id": candidate_id,
                "accepted_at": resolved_at,
            }
        )
        if not is_materializable_resource(promoted):
            continue
        if not resolution_ref:
            issues.append(
                "accepted-candidate-resource-missing: %s" % candidate_id
            )
            continue
        expected = canonical_resource_record(root, promoted, resolution_ref)
        matches: List[Tuple[Path, Dict[str, Any]]] = []
        for resource_path in resource_json_files(root):
            try:
                matches.extend(
                    (resource_path, record)
                    for record in iter_resource_records(resource_path)
                    if clean_scalar(record.get("id")) == candidate_id
                )
            except KBError:
                continue
        if not matches:
            issues.append(
                "accepted-candidate-resource-missing: %s" % candidate_id
            )
        elif any(
            stable_json(record) != stable_json(expected)
            for _, record in matches
        ):
            issues.append(
                "accepted-candidate-resource-conflict: %s" % candidate_id
            )
    return issues


def credential_leak_issues(root: Path, connection: sqlite3.Connection) -> List[str]:
    issues: List[str] = []
    for row in connection.execute(
        """
        SELECT e.event_id, e.ledger_ref, e.event_sha256, f.statement, f.content
        FROM history_events e JOIN history_fts f ON f.rowid = e.rowid
        WHERE e.is_credential = 1
        """
    ):
        event = load_event_reference(row["ledger_ref"], root, row["event_id"], row["event_sha256"])
        if event is None:
            continue
        for _, value in credential_value_items(event):
            if value and (value in row["statement"] or value in row["content"]):
                issues.append(
                    "credential event %s value appears in history FTS"
                    % row["event_id"]
                )
                break
    for row in connection.execute(
        """
        SELECT d.document_id, d.path, f.path AS fts_path, f.title, f.kind,
               f.scope, f.source_device_id AS fts_source_device_id, f.content
        FROM canonical_documents d JOIN canonical_fts f ON f.rowid = d.rowid
        WHERE d.is_credential = 1
        """
    ):
        path = root / row["path"]
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        indexed = " ".join(
            (
                row["fts_path"],
                row["title"],
                row["kind"],
                row["scope"],
                row["fts_source_device_id"],
                row["content"],
            )
        )
        for value in sensitive_values(parsed, force_all=True):
            encoded = (
                json.dumps(value, ensure_ascii=False)[1:-1]
                if value
                else ""
            )
            if value and (value in indexed or (encoded and encoded in indexed)):
                issues.append("credential resource %s value appears in canonical FTS" % row["document_id"])
                break
    return issues


def empty_device_stats() -> Dict[str, Any]:
    return {
        "event_count": 0,
        "knowledge_event_count": 0,
        "history_event_count": 0,
        "last_seen": None,
    }


def history_database_health(root: Path) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], List[str]]:
    database = configured_history_database(root)
    report: Dict[str, Any] = {
        "path": str(database),
        "exists": database.is_file(),
        "source_events": None,
        "fts_events": None,
        "evidence_relation_conflicts": None,
    }
    device_stats: Dict[str, Dict[str, Any]] = {}
    issues: List[str] = []
    if not database.is_file():
        issues.append("history database is missing; run ingest_history.py")
        return report, device_stats, issues
    connection = connect_database_read_only(database, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        if "source_events" not in tables or "source_events_fts" not in tables:
            issues.append("history database is missing source_events/source_events_fts")
            return report, device_stats, issues
        report["source_events"] = connection.execute("SELECT count(*) FROM source_events").fetchone()[0]
        report["fts_events"] = connection.execute("SELECT count(*) FROM source_events_fts").fetchone()[0]
        if report["source_events"] != report["fts_events"]:
            issues.append(
                "history FTS is stale: %d source events vs %d FTS rows"
                % (report["source_events"], report["fts_events"])
            )
        if "source_event_user_relations" in tables:
            report["evidence_relation_conflicts"] = int(
                connection.execute(
                    """
                    SELECT count(*) FROM (
                        SELECT event_hash
                        FROM source_event_user_relations
                        GROUP BY event_hash
                        HAVING count(DISTINCT related_user_event_hash)>1
                    )
                    """
                ).fetchone()[0]
            )
        else:
            report["evidence_relation_conflicts"] = 0
        rows = connection.execute(
            """
            SELECT source_device_id, count(*) AS event_count,
                   max(ingested_at) AS last_ingested_at,
                   max(event_timestamp) AS last_event_at
            FROM source_events GROUP BY source_device_id
            """
        ).fetchall()
        report["devices"] = []
        for row in rows:
            device_id = normalize_device_id(row["source_device_id"])
            last_seen = row["last_ingested_at"] or row["last_event_at"]
            stats = empty_device_stats()
            stats.update(
                {
                    "event_count": row["event_count"],
                    "history_event_count": row["event_count"],
                    "last_seen": last_seen,
                }
            )
            device_stats[device_id] = stats
            report["devices"].append(
                {
                    "id": device_id,
                    "event_count": row["event_count"],
                    "last_event_at": row["last_event_at"],
                    "last_ingested_at": row["last_ingested_at"],
                }
            )
    except sqlite3.Error as error:
        issues.append("cannot inspect history database: %s" % error)
    finally:
        connection.close()
    return report, device_stats, issues


def health(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    issues: List[str] = []
    warnings: List[str] = []
    if not paths_for(root)["database"].is_file():
        issues.append("index database is missing; run init or rebuild-index")
        connection = None
    else:
        connection = connect_database(root)

    try:
        superseded_by, _ = lifecycle_maps(root)
    except KBError as error:
        issues.append(str(error))
        superseded_by = {}

    events_by_id: Dict[str, Tuple[str, str]] = {}
    source_stats: Dict[str, Dict[str, Any]] = {}
    ledger_count = 0
    for path in ledger_files(root):
        try:
            values = list(iter_json_values(path))
        except KBError as error:
            issues.append(str(error))
            continue
        for raw, reference in values:
            ledger_count += 1
            event = normalize_event(raw, reference)
            errors = event_validation_errors(event)
            if errors:
                issues.append("%s: %s" % (reference, ", ".join(errors)))
                continue
            if credential_value_missing(event) and not missing_credential_is_resolved(
                root, event["id"], superseded_by
            ):
                issues.append(
                    "credential-value-missing: event %s in %s"
                    % (event["id"], reference)
                )
            fingerprint = event_fingerprint(event)
            previous = events_by_id.get(event["id"])
            if previous and previous[0] != fingerprint:
                issues.append("conflicting event id %s in %s and %s" % (event["id"], previous[1], reference))
            else:
                events_by_id[event["id"]] = (fingerprint, reference)
            stats = source_stats.setdefault(event["source_device_id"], empty_device_stats())
            stats["event_count"] += 1
            stats["knowledge_event_count"] += 1
            current_time = parse_time(event["created_at"])
            previous_time = parse_time(stats["last_seen"]) if stats["last_seen"] else None
            if current_time and (previous_time is None or current_time > previous_time):
                stats["last_seen"] = event["created_at"]

    history_report, history_devices, history_issues = history_database_health(root)
    issues.extend(history_issues)
    relation_conflicts = int(
        history_report.get("evidence_relation_conflicts") or 0
    )
    if relation_conflicts:
        warnings.append(
            "history evidence relation conflicts preserved: %d"
            % relation_conflicts
        )
    for device_id, history_stats in history_devices.items():
        stats = source_stats.setdefault(device_id, empty_device_stats())
        stats["event_count"] += history_stats["history_event_count"]
        stats["history_event_count"] += history_stats["history_event_count"]
        history_time = parse_time(history_stats["last_seen"])
        current_time = parse_time(stats["last_seen"]) if stats["last_seen"] else None
        if history_time and (current_time is None or history_time > current_time):
            stats["last_seen"] = history_stats["last_seen"]

    expected, default_stale = expected_devices(root, args.expected_device, args.stale_days)
    now = utc_now()
    device_report: List[Dict[str, Any]] = []
    expected_ids = {entry["id"] for entry in expected}
    for entry in expected:
        device_id = entry["id"]
        stats = source_stats.get(device_id, empty_device_stats())
        registration = remote_registration_evidence(entry)
        registration_verified = bool(registration["ok"])
        last_seen = stats["last_seen"]
        last_seen_source = "local-index" if last_seen else None
        registration_time = parse_time(registration["last_seen"])
        observed_time = parse_time(last_seen) if last_seen else None
        if registration_time and (
            observed_time is None or registration_time > observed_time
        ):
            last_seen = registration["last_seen"]
            last_seen_source = "windows-local-registration"
        stale_after = entry.get("stale_after_days", default_stale)
        never_synced = stats["event_count"] == 0 and not registration_verified
        stale = False
        if last_seen and isinstance(stale_after, int) and stale_after >= 0:
            observed = parse_time(last_seen)
            stale = observed is not None and (now - observed).days > stale_after
        item = {
            "id": device_id,
            "name": entry.get("name"),
            "expected": True,
            "event_count": stats["event_count"],
            "knowledge_event_count": stats["knowledge_event_count"],
            "history_event_count": stats["history_event_count"],
            "reported_remote_history_events": registration["history_events"],
            "registration_verified": registration_verified,
            "registration_status": registration["status"],
            "last_seen": last_seen,
            "last_seen_source": last_seen_source,
            "never_synced": never_synced,
            "stale": stale,
            "stale_after_days": stale_after,
        }
        device_report.append(item)
        if never_synced:
            issues.append("expected device %s has never synced" % device_id)
        elif stale:
            issues.append(
                "expected device %s is stale (last seen %s)"
                % (device_id, last_seen)
            )
    for device_id, stats in sorted(source_stats.items()):
        if device_id not in expected_ids:
            device_report.append(
                {
                    "id": device_id,
                    "name": None,
                    "expected": False,
                    "event_count": stats["event_count"],
                    "knowledge_event_count": stats["knowledge_event_count"],
                    "history_event_count": stats["history_event_count"],
                    "last_seen": stats["last_seen"],
                    "never_synced": False,
                    "stale": False,
                    "stale_after_days": None,
                }
            )
    if not expected:
        warnings.append("no expected-device registry; never-synced devices cannot be detected")

    issues.extend(canonical_resource_health(root, superseded_by))
    issues.extend(accepted_candidate_health(root))
    index_counts = {"canonical_documents": None, "history_events": None}
    if connection is not None:
        try:
            schema_version_row = connection.execute("SELECT value FROM kb_meta WHERE key = 'schema_version'").fetchone()
            if not schema_version_row or schema_version_row[0] != SCHEMA_VERSION:
                issues.append("unexpected index schema version")
            index_counts = {
                "canonical_documents": connection.execute("SELECT count(*) FROM canonical_documents").fetchone()[0],
                "history_events": connection.execute("SELECT count(*) FROM history_events").fetchone()[0],
            }
            indexed_documents = {
                row["path"]: row["content_sha256"]
                for row in connection.execute("SELECT path, content_sha256 FROM canonical_documents")
            }
            current_documents: Dict[str, str] = {}
            for path in canonical_files(root):
                try:
                    document = canonical_document(root, path)
                except KBError as error:
                    issues.append(str(error))
                    continue
                current_documents[document["path"]] = document["content_sha256"]
            if current_documents != indexed_documents:
                issues.append(
                    "canonical index is stale: %d source documents vs %d indexed documents"
                    % (len(current_documents), len(indexed_documents))
                )
            if index_counts["history_events"] != len(events_by_id):
                issues.append(
                    "history index is stale: %d indexed vs %d unique ledger events"
                    % (index_counts["history_events"], len(events_by_id))
                )
            indexed_events = {
                row["event_id"]: row["event_sha256"]
                for row in connection.execute("SELECT event_id, event_sha256 FROM history_events")
            }
            current_events = {event_id: value[0] for event_id, value in events_by_id.items()}
            if indexed_events != current_events and index_counts["history_events"] == len(events_by_id):
                issues.append("history index hashes do not match immutable ledger payloads")
            issues.extend(credential_leak_issues(root, connection))
        finally:
            connection.close()

    report = {
        "ok": not issues,
        "root": str(root),
        "database": str(paths_for(root)["database"]),
        "ledger_records": ledger_count,
        "unique_events": len(events_by_id),
        "index": index_counts,
        "history": history_report,
        "devices": device_report,
        "issues": issues,
        "warnings": warnings,
        "checked_at": iso_now(),
    }
    if issues:
        raise UnhealthyError(report)
    return report


def show_paths(root: Path) -> Dict[str, Any]:
    payload = {key: str(value) for key, value in paths_for(root).items()}
    payload.update({"ok": True, "source_device_id": configured_device_id(root)})
    return payload


def emit(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if "results" in payload:
        print("%d result(s) for %r" % (payload["count"], payload["query"]))
        for item in payload["results"]:
            print("\n[%s] %s (%s/%s)" % (item["result_type"], item["id"], item["kind"], item["scope"]))
            print("  source_device_id: %s" % item.get("source_device_id", ""))
            if item.get("source"):
                print("  source: %s" % item["source"])
            if item.get("thread_id"):
                print("  thread: %s" % item["thread_id"])
            if item.get("timestamp"):
                print("  timestamp: %s" % item["timestamp"])
            if item.get("path"):
                print("  path: %s" % item["path"])
            if item.get("superseded_by"):
                print("  superseded_by: %s" % item["superseded_by"])
            if item.get("candidate_state"):
                print("  candidate_state: %s" % item["candidate_state"])
            if item.get("snippet"):
                print("  %s" % str(item["snippet"]).replace("\n", " "))
            if "value" in item:
                print("  value: %s" % scalar_text(item["value"]))
            if "content" in item:
                print(item["content"])
        return
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local-first AI knowledge base")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="knowledge-base root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create directories and build the disposable index")
    add_json_flag(init_parser)

    capture_parser = subparsers.add_parser(
        "capture",
        help="write an immutable ledger event; create review state only when needed",
    )
    capture_parser.add_argument(
        "--kind",
        required=True,
        help=(
            "free-form classification; credential kinds (%s) hide the value in "
            "default search and, when status=active and not agent inference, "
            "materialize a stable resources/ record; the same applies to "
            "resource kinds (%s)"
            % (
                ", ".join(sorted(CREDENTIAL_KINDS)),
                ", ".join(sorted(RESOURCE_KINDS - CREDENTIAL_KINDS)),
            )
        ),
    )
    capture_parser.add_argument("--scope", required=True)
    capture_parser.add_argument("--statement", required=True)
    capture_value = capture_parser.add_mutually_exclusive_group()
    capture_value.add_argument("--value")
    capture_value.add_argument(
        "--value-file",
        type=Path,
        help="read the exact UTF-8 value from a file instead of placing it in argv",
    )
    capture_parser.add_argument("--source", required=True)
    capture_parser.add_argument("--authority", required=True)
    capture_parser.add_argument(
        "--status",
        help=(
            "event lifecycle status; defaults to active for authoritative "
            "captures and candidate for agent-derived authority labels"
        ),
    )
    for field in ("source-time", "observed-at", "confirmed-at", "expires-at", "valid-from", "fact-key"):
        capture_parser.add_argument("--" + field)
    capture_parser.add_argument("--id")
    capture_parser.add_argument("--source-device-id")
    capture_parser.add_argument("--bundle-id")
    add_json_flag(capture_parser)

    search_parser = subparsers.add_parser("search", help="search canonical knowledge and history FTS")
    search_parser.add_argument("query")
    search_parser.add_argument("--kind")
    search_parser.add_argument("--scope")
    search_parser.add_argument("--limit", type=int)
    search_parser.add_argument("--include-values", action="store_true")
    add_json_flag(search_parser)

    history_parser = subparsers.add_parser("get-history", help="read one exact raw history event by search result id")
    history_parser.add_argument("history_id")
    history_parser.add_argument("--include-values", action="store_true")
    add_json_flag(history_parser)

    resource_parser = subparsers.add_parser("get-resource", help="read a canonical resource by stable id")
    resource_parser.add_argument("resource_id")
    resource_parser.add_argument("--include-values", action="store_true")
    add_json_flag(resource_parser)

    materialize_parser = subparsers.add_parser(
        "materialize-resources",
        help="make authoritative active ledger resources directly addressable",
    )
    add_json_flag(materialize_parser)

    supersede_parser = subparsers.add_parser(
        "supersede",
        help=(
            "record an immutable supersession event so --new-id replaces "
            "--old-id in ranking; the old record and value are kept"
        ),
    )
    supersede_parser.add_argument("--old-id", required=True)
    supersede_parser.add_argument("--new-id", required=True)
    supersede_parser.add_argument("--source", required=True)
    supersede_parser.add_argument("--note")
    supersede_parser.add_argument("--source-device-id")
    add_json_flag(supersede_parser)

    resolve_parser = subparsers.add_parser(
        "resolve-candidate",
        help=(
            "accept or reject an inbox review candidate; rejection demotes "
            "the event in search without deleting anything"
        ),
    )
    resolve_parser.add_argument("event_id")
    resolve_parser.add_argument("--action", required=True, choices=("accept", "reject"))
    resolve_parser.add_argument("--note")
    add_json_flag(resolve_parser)

    health_parser = subparsers.add_parser("health", help="validate sources, index, credentials, and device freshness")
    health_parser.add_argument("--expected-device", action="append", default=[])
    health_parser.add_argument("--stale-days", type=int)
    add_json_flag(health_parser)

    rebuild_parser = subparsers.add_parser("rebuild-index", help="recreate SQLite from Markdown/JSON and ledger")
    add_json_flag(rebuild_parser)

    update_parser = subparsers.add_parser(
        "update-index",
        help="incrementally index changed canonical/ledger sources with safe rebuild fallback",
    )
    add_json_flag(update_parser)

    paths_parser = subparsers.add_parser("show-paths", help="print resolved storage paths and local device id")
    add_json_flag(paths_parser)
    return parser


def dispatch(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "init":
        return init_knowledge_base(root)
    if args.command == "capture":
        return capture(root, args)
    if args.command == "search":
        if args.limit is None:
            args.limit = retrieval_settings(root)["default_limit"]
        if args.limit < 1 or args.limit > 1000:
            raise KBError("--limit must be between 1 and 1000")
        return search(root, args)
    if args.command == "get-history":
        return get_history(root, args)
    if args.command == "get-resource":
        return get_resource(root, args)
    if args.command == "materialize-resources":
        return materialize_resources(root)
    if args.command == "supersede":
        return supersede(root, args)
    if args.command == "resolve-candidate":
        return resolve_candidate(root, args)
    if args.command == "health":
        if args.stale_days is not None and args.stale_days < 0:
            raise KBError("--stale-days cannot be negative")
        return health(root, args)
    if args.command == "rebuild-index":
        return rebuild_index(root)
    if args.command == "update-index":
        return update_index(root)
    if args.command == "show-paths":
        return show_paths(root)
    raise KBError("unknown command")


def run(args: argparse.Namespace) -> Dict[str, Any]:
    root = args.root.expanduser().resolve()
    if args.command in (
        "capture",
        "materialize-resources",
        "supersede",
        "resolve-candidate",
    ):
        try:
            with workspace_lock.acquire(root, blocking=False):
                return dispatch(root, args)
        except workspace_lock.WorkspaceBusy as error:
            raise WorkspaceMutationBusyError() from error
    return dispatch(root, args)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = run(args)
        emit(payload, bool(getattr(args, "json", False)))
        return EXIT_OK
    except UnhealthyError as error:
        emit(error.report, bool(getattr(args, "json", False)))
        return error.exit_code
    except WorkspaceMutationBusyError as error:
        payload = {
            "ok": False,
            "error": str(error),
            "exit_code": error.exit_code,
            "retryable": True,
        }
        if getattr(args, "json", False):
            emit(payload, True)
        else:
            print("error: %s" % error, file=sys.stderr)
        return error.exit_code
    except KBError as error:
        payload = {"ok": False, "error": str(error), "exit_code": error.exit_code}
        if getattr(args, "json", False):
            emit(payload, True)
        else:
            print("error: %s" % error, file=sys.stderr)
        return error.exit_code
    except (OSError, sqlite3.Error) as error:
        payload = {"ok": False, "error": str(error), "exit_code": EXIT_ERROR}
        if getattr(args, "json", False):
            emit(payload, True)
        else:
            print("error: %s" % error, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
