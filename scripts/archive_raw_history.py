#!/usr/bin/env python3
"""Archive complete Codex/Claude source logs into a cold, content-addressed store.

The searchable history database deliberately keeps only useful text messages.
This tool preserves the complete source JSONL files separately.  Fixed-size
chunks make an appended log incremental, while immutable manifests retain every
published source-file state.  No source body is ever written to stdout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat as stat_module
import sys
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ingest_history
import storage_lock
import device_identity
import file_lock


ROOT = Path(__file__).resolve().parents[1]
FORMAT = "ai-knowledge-raw-history-v1"
VERIFICATION_FORMAT = "ai-knowledge-raw-history-verification-v1"
DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024
DEFAULT_SAMPLE_OBJECTS = 8
SOURCE_PREFIX_STABLE_READ_ATTEMPTS = 3
DEVICE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
DEFERABLE_SCAN_ERRORS = frozenset({
    "source-changed-during-read",
    "source-is-not-regular-file",
    "source-unreadable",
})
DEFERABLE_PUBLISH_ERRORS = frozenset({
    "source-changed-during-prefix-check",
    "source-is-not-regular-file",
    "source-replaced-during-prefix-check",
    "source-shorter-than-archive",
    "source-unreadable",
})


class ArchiveError(RuntimeError):
    """A body-safe archive failure suitable for logs and JSON output."""

    def __init__(
        self,
        code: str,
        metrics: Optional[Mapping[str, int]] = None,
    ):
        super().__init__(code)
        self.code = code
        self.metrics = dict(metrics or {})


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some filesystems do not support fsync on directories.  File fsync and
        # atomic rename/link still provide the useful safety properties there.
        pass
    finally:
        os.close(descriptor)


def atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_create(path: Path, payload: bytes, expected_sha256: str) -> bool:
    """Create one immutable file without overwriting a concurrent writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.stat().st_size != len(payload):
            raise ArchiveError("archive-object-conflict")
        if sha256_file(path) != expected_sha256:
            raise ArchiveError("archive-object-conflict")
        return False

    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(str(temporary), str(path))
            created = True
        except FileExistsError:
            created = False
        if not created:
            if path.is_symlink() or not path.is_file() or path.stat().st_size != len(payload):
                raise ArchiveError("archive-object-conflict")
            if sha256_file(path) != expected_sha256:
                raise ArchiveError("archive-object-conflict")
        fsync_directory(path.parent)
        return created
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def resolve_device_id(value: Optional[str], root: Path = ROOT, home: Optional[Path] = None) -> str:
    # The stable identity is local state bound to this Mac, never shared cold
    # data and never a fresh hostname-derived alias on each invocation.
    try:
        device_id = device_identity.resolve_device_id(root, value, home)
    except device_identity.DeviceIdentityError as error:
        raise ArchiveError("device-identity-invalid") from error
    if not DEVICE_RE.fullmatch(device_id):
        raise ArchiveError("invalid-device-id")
    return device_id


def source_reference(path: Path, home: Path) -> str:
    resolved = path.resolve()
    try:
        return "~/" + resolved.relative_to(home.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def discover_source_files(home: Path, source: str) -> List[Tuple[str, Path, str]]:
    discovered: Dict[Tuple[str, str], Tuple[str, Path, str]] = {}
    try:
        candidates = ingest_history.discover_sources(source, home.resolve())
    except OSError as exc:
        raise ArchiveError("source-discovery-failed") from exc
    for item in candidates:
        path = item.path.resolve()
        if not path.is_file() or path.is_symlink():
            continue
        reference = source_reference(path, home)
        discovered[(item.source, reference)] = (item.source, path, reference)
    return [discovered[key] for key in sorted(discovered)]


def source_signature(path: Path) -> Dict[str, int]:
    try:
        stat = path.stat()
        regular = path.is_file() and not path.is_symlink()
    except OSError as exc:
        raise ArchiveError("source-unreadable") from exc
    if not regular:
        raise ArchiveError("source-is-not-regular-file")
    return {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "inode": int(stat.st_ino),
    }


def stat_signature(stat: os.stat_result) -> Dict[str, int]:
    return {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "inode": int(stat.st_ino),
    }


def object_path(archive_root: Path, digest: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ArchiveError("invalid-object-digest")
    return archive_root / "objects" / "sha256" / digest[:2] / (digest[2:] + ".chunk")


def manifest_path(archive_root: Path) -> Path:
    return archive_root / "manifest.json"


def verification_path(archive_root: Path) -> Path:
    return archive_root / "verification.json"


def object_specs(files: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for record in files:
        chunks = record.get("chunks")
        if not isinstance(chunks, list):
            raise ArchiveError("invalid-manifest-file")
        for chunk in chunks:
            if not isinstance(chunk, dict):
                raise ArchiveError("invalid-manifest-file")
            digest = str(chunk.get("sha256") or "")
            try:
                size = int(chunk.get("size"))
            except (TypeError, ValueError) as error:
                raise ArchiveError("invalid-manifest-file") from error
            if size < 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ArchiveError("invalid-manifest-file")
            if digest in result and result[digest] != size:
                raise ArchiveError("invalid-manifest-file")
            result[digest] = size
    return result


def verification_sample(digests: Sequence[str], sample_round: str) -> List[str]:
    ranked = sorted(
        digests,
        key=lambda digest: hashlib.sha256((sample_round + "\0" + digest).encode("ascii")).hexdigest(),
    )
    return ranked[:DEFAULT_SAMPLE_OBJECTS]


def save_verification_record(
    archive_root: Path,
    manifest: Mapping[str, Any],
    raw_manifest: bytes,
    full: bool,
    already_verified: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    specs = object_specs(manifest.get("files", []))
    sample_round = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    selected = sorted(specs) if full else verification_sample(list(specs), sample_round)
    certified = set(already_verified or [])
    pending = [digest for digest in selected if digest not in certified]
    verify_selected_objects(archive_root, specs, pending)
    record = {
        "format": VERIFICATION_FORMAT,
        "device_id": manifest.get("device_id"),
        "manifest_sha256": sha256_bytes(raw_manifest),
        "mode": "full" if full else "rotating-sample",
        "sample_round": sample_round,
        "verified_at": utc_now(),
        "objects_total": len(specs),
        "objects_verified": len(selected),
        "object_digests": selected,
    }
    atomic_replace(verification_path(archive_root), canonical_bytes(record))
    return record


def verify_selected_objects(
    archive_root: Path,
    specs: Mapping[str, int],
    selected: Sequence[str],
) -> None:
    """Hash a chosen object set without changing verification state."""
    for digest in selected:
        path = object_path(archive_root, digest)
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != specs[digest]
            or sha256_file(path) != digest
        ):
            raise ArchiveError("archive-object-corrupt")


def matching_full_verification(
    archive_root: Path,
    device_id: str,
    manifest_sha256: str,
    manifest: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return a structurally valid full proof for this immutable manifest."""
    path = verification_path(archive_root)
    try:
        if not path.is_file() or path.is_symlink():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("format") != VERIFICATION_FORMAT
            or value.get("device_id") != device_id
            or value.get("manifest_sha256") != manifest_sha256
            or value.get("mode") != "full"
            or not isinstance(value.get("object_digests"), list)
        ):
            return None
        digests = list(value["object_digests"])
        objects_total = int(value.get("objects_total"))
        objects_verified = int(value.get("objects_verified"))
        if (
            any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in digests)
            or len(set(digests)) != len(digests)
            or objects_total != len(digests)
            or objects_verified != len(digests)
        ):
            return None
        if manifest is not None and set(digests) != set(
            object_specs(manifest.get("files", []))
        ):
            return None
        return value
    except (
        ArchiveError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return None


def invalidate_verification_record(
    archive_root: Path,
    manifest: Mapping[str, Any],
    raw_manifest: bytes,
    mode: str,
) -> Dict[str, Any]:
    """Atomically replace any stale full proof with a non-ready status record."""
    if mode not in {"coverage-failed", "verification-failed"}:
        raise ArchiveError("invalid-verification-mode")
    try:
        objects_total = len(object_specs(manifest.get("files", [])))
    except ArchiveError:
        objects_total = 0
    record = {
        "format": VERIFICATION_FORMAT,
        "device_id": manifest.get("device_id"),
        "manifest_sha256": sha256_bytes(raw_manifest),
        "mode": mode,
        "sample_round": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "verified_at": utc_now(),
        "objects_total": objects_total,
        "objects_verified": 0,
        "object_digests": [],
    }
    atomic_replace(verification_path(archive_root), canonical_bytes(record))
    return record


def load_manifest(archive_root: Path, device_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[bytes]]:
    path = manifest_path(archive_root)
    if not path.exists():
        return None, None
    if path.is_symlink() or not path.is_file():
        raise ArchiveError("invalid-manifest")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("invalid-manifest") from exc
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        raise ArchiveError("invalid-manifest")
    if value.get("device_id") != device_id or not isinstance(value.get("files"), list):
        raise ArchiveError("manifest-device-mismatch")
    return value, raw


def file_key(record: Mapping[str, Any]) -> Tuple[str, str]:
    source = record.get("source")
    reference = record.get("source_ref")
    if source not in ("codex", "claude") or not isinstance(reference, str) or not reference:
        raise ArchiveError("invalid-manifest-file")
    return str(source), reference


def record_signature(record: Mapping[str, Any]) -> Dict[str, int]:
    try:
        return {
            "size": int(record["size"]),
            "mtime_ns": int(record["mtime_ns"]),
            "inode": int(record["inode"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ArchiveError("invalid-manifest-file") from exc


def _file_identity(metadata: os.stat_result) -> Tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _file_version(metadata: os.stat_result) -> Tuple[int, int, int]:
    return (
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def hash_source_prefix(path: Path, archived_size: int) -> Tuple[str, int]:
    """Hash exactly the archived high-water prefix under replacement guards.

    Appends are legal while the prefix is read.  A shrink, non-regular file, or
    path replacement is not: lstat/open/fstat/lstat must all describe the same
    regular file and both post-read sizes must still cover the high-water mark.
    The source body is consumed only by SHA-256 and is never returned or logged.
    """
    if archived_size < 0:
        raise ArchiveError("invalid-manifest-file")
    for _attempt in range(SOURCE_PREFIX_STABLE_READ_ATTEMPTS):
        try:
            before = os.lstat(path)
            if not stat_module.S_ISREG(before.st_mode):
                raise ArchiveError("source-is-not-regular-file")
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if (
                    not stat_module.S_ISREG(opened.st_mode)
                    or _file_identity(opened) != _file_identity(before)
                ):
                    raise ArchiveError("source-replaced-during-prefix-check")
                if int(opened.st_size) < archived_size:
                    raise ArchiveError("source-shorter-than-archive")

                digest = hashlib.sha256()
                remaining = archived_size
                while remaining:
                    block = handle.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ArchiveError("source-shorter-than-archive")
                    digest.update(block)
                    remaining -= len(block)
                closed = os.fstat(handle.fileno())

            after = os.lstat(path)
        except ArchiveError:
            raise
        except OSError as exc:
            raise ArchiveError("source-unreadable") from exc

        if (
            not stat_module.S_ISREG(closed.st_mode)
            or not stat_module.S_ISREG(after.st_mode)
            or _file_identity(closed) != _file_identity(opened)
            or _file_identity(after) != _file_identity(opened)
        ):
            raise ArchiveError("source-replaced-during-prefix-check")
        if int(closed.st_size) < archived_size or int(after.st_size) < archived_size:
            raise ArchiveError("source-shorter-than-archive")

        # An append or in-place write can occur after the last prefix byte was
        # read but before post-read fstat.  Never accept that first digest:
        # retry from byte zero and require one complete metadata-stable pass.
        versions = {
            _file_version(before),
            _file_version(opened),
            _file_version(closed),
            _file_version(after),
        }
        if len(versions) == 1:
            return digest.hexdigest(), int(after.st_size)

    raise ArchiveError("source-changed-during-prefix-check")


def assess_source_high_water_coverage(
    files: Sequence[Mapping[str, Any]],
    home: Path,
    source: str = "all",
) -> Dict[str, Any]:
    """Prove that every current source is covered by an archived prefix.

    A missing recorded source is a safe local retirement because its immutable
    objects remain in the archive.  A longer current file is covered only when
    its first ``record.size`` bytes hash to ``record.sha256``.  New paths,
    shortened files, prefix mutations, and read races fail closed.
    """
    issues: List[str] = []
    recorded: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    for record in files:
        try:
            key = file_key(record)
            size = int(record.get("size"))
            digest = str(record.get("sha256") or "")
            if size < 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ArchiveError("invalid-manifest-file")
            if key in recorded:
                raise ArchiveError("duplicate-manifest-file")
            recorded[key] = record
        except (ArchiveError, TypeError, ValueError):
            issues.append("invalid-manifest-file")

    try:
        initial = discover_source_files(home, source)
    except ArchiveError as error:
        initial = []
        issues.append(error.code)
    current = {(item[0], item[2]): item[1] for item in initial}

    for key in sorted(set(current) - set(recorded)):
        issues.append("source-not-archived:%s" % issue_id(*key))

    tail_bytes = 0
    tail_files = 0
    tail_ids: List[str] = []
    retired_keys = sorted(set(recorded) - set(current))
    retired_ids = [issue_id(*key) for key in retired_keys]
    for key in sorted(set(recorded) & set(current)):
        record = recorded[key]
        archived_size = int(record.get("size"))
        expected_digest = str(record.get("sha256"))
        try:
            actual_digest, current_size = hash_source_prefix(current[key], archived_size)
        except ArchiveError as error:
            issues.append("%s:%s" % (error.code, issue_id(*key)))
            continue
        if actual_digest != expected_digest:
            issues.append("source-prefix-digest-mismatch:%s" % issue_id(*key))
            continue
        if current_size > archived_size:
            tail_files += 1
            tail_bytes += current_size - archived_size
            tail_ids.append(issue_id(*key))

    # Catch a source path appearing or disappearing while prefixes were being
    # checked.  Content may continue to append; that is intentionally allowed.
    try:
        final_keys = {
            (item[0], item[2]) for item in discover_source_files(home, source)
        }
        if final_keys != set(current):
            issues.append("source-set-changed-during-prefix-check")
    except ArchiveError as error:
        issues.append(error.code)

    unique_issues = sorted(set(issues))
    coverage_ok = not unique_issues
    exact = bool(coverage_ok and not tail_files and not retired_keys)
    if not coverage_ok:
        coverage_mode = "failed"
    elif tail_files and retired_keys:
        coverage_mode = "append-only-high-water-with-retired-sources"
    elif tail_files:
        coverage_mode = "append-only-high-water"
    elif retired_keys:
        coverage_mode = "retired-sources-covered"
    else:
        coverage_mode = "exact"
    return {
        "ok": coverage_ok,
        "coverage_mode": coverage_mode,
        "source_exact_manifest": exact,
        "current_source_files": len(current),
        "unarchived_tail_files": tail_files,
        "unarchived_tail_bytes": tail_bytes,
        "unarchived_tail_source_ids": sorted(tail_ids),
        "retired_source_files": len(retired_keys),
        "retired_source_ids": retired_ids,
        "issues": unique_issues,
    }


def chunks_are_present(record: Mapping[str, Any], archive_root: Path) -> bool:
    chunks = record.get("chunks")
    if not isinstance(chunks, list):
        return False
    total = 0
    for chunk in chunks:
        if not isinstance(chunk, dict):
            return False
        digest = chunk.get("sha256")
        try:
            size = int(chunk.get("size"))
            path = object_path(archive_root, str(digest))
        except (TypeError, ValueError, ArchiveError):
            return False
        if size < 0 or not path.is_file() or path.is_symlink() or path.stat().st_size != size:
            return False
        total += size
    try:
        return total == int(record.get("size"))
    except (TypeError, ValueError):
        return False


def scan_source_file(
    source: str,
    path: Path,
    reference: str,
    archive_root: Path,
    chunk_bytes: int,
    write_objects: bool,
    previous: Optional[Mapping[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    before = source_signature(path)
    chunks: List[Dict[str, Any]] = []
    full_digest = hashlib.sha256()
    prefix_digest = hashlib.sha256()
    prefix_remaining = 0
    expected_prefix_digest: Optional[str] = None
    if previous is not None:
        try:
            prefix_remaining = int(previous.get("size"))
            expected_prefix_digest = str(previous.get("sha256") or "")
        except (TypeError, ValueError) as error:
            raise ArchiveError("invalid-manifest-file") from error
        if (
            prefix_remaining < 0
            or not re.fullmatch(r"[0-9a-f]{64}", expected_prefix_digest)
        ):
            raise ArchiveError("invalid-manifest-file")
        if before["size"] < prefix_remaining:
            raise ArchiveError("source-shorter-than-archive")
    metrics = {"objects_missing": 0, "objects_written": 0, "bytes_to_write": 0, "bytes_written": 0}

    try:
        with path.open("rb") as handle:
            opened = stat_signature(os.fstat(handle.fileno()))
            if opened != before:
                raise ArchiveError("source-changed-during-read")
            while True:
                payload = handle.read(chunk_bytes)
                if not payload:
                    break
                full_digest.update(payload)
                if prefix_remaining:
                    prefix_bytes = payload[:prefix_remaining]
                    prefix_digest.update(prefix_bytes)
                    prefix_remaining -= len(prefix_bytes)
                digest = sha256_bytes(payload)
                destination = object_path(archive_root, digest)
                present = (
                    destination.is_file()
                    and not destination.is_symlink()
                    and destination.stat().st_size == len(payload)
                )
                if not present:
                    metrics["objects_missing"] += 1
                    metrics["bytes_to_write"] += len(payload)
                if write_objects:
                    created = atomic_create(destination, payload, digest)
                    if created:
                        metrics["objects_written"] += 1
                        metrics["bytes_written"] += len(payload)
                chunks.append({"sha256": digest, "size": len(payload)})
            closed_signature = stat_signature(os.fstat(handle.fileno()))
    except OSError as exc:
        raise ArchiveError("source-unreadable") from exc

    after = source_signature(path)
    if before != closed_signature or before != after:
        raise ArchiveError("source-changed-during-read", metrics)
    if sum(int(chunk["size"]) for chunk in chunks) != before["size"]:
        raise ArchiveError("source-changed-during-read")
    if previous is not None:
        if prefix_remaining:
            raise ArchiveError("source-shorter-than-archive")
        if prefix_digest.hexdigest() != expected_prefix_digest:
            # Codex and Claude source JSONL files are append-only evidence.
            # Never let an in-place rewrite become the new canonical archive
            # merely because it happened between wrapper preflight and apply.
            raise ArchiveError("source-prefix-changed-before-archive")
    record = {
        "source": source,
        "source_ref": reference,
        "size": before["size"],
        "mtime_ns": before["mtime_ns"],
        "inode": before["inode"],
        "sha256": full_digest.hexdigest(),
        "chunks": chunks,
    }
    return record, metrics


def state_material(device_id: str, chunk_bytes: int, files: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return {
        "format": FORMAT,
        "device_id": device_id,
        "chunk_bytes": chunk_bytes,
        "files": list(files),
    }


def state_digest(device_id: str, chunk_bytes: int, files: Sequence[Mapping[str, Any]]) -> str:
    return sha256_bytes(canonical_bytes(state_material(device_id, chunk_bytes, files)))


def public_plan(plan: Mapping[str, Any], archive_root: Path, device_id: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "action": "plan",
        "device_id": device_id,
        "destination": str(archive_root),
        "source_files": len(plan["files"]),
        "source_bytes": int(plan["source_bytes"]),
        "new_files": int(plan["new_files"]),
        "changed_files": int(plan["changed_files"]),
        "repair_files": int(plan["repair_files"]),
        "removed_files": int(plan["removed_files"]),
        "objects_missing": int(plan["objects_missing"]),
        "bytes_to_write": int(plan["bytes_to_write"]),
        "state_changed": bool(plan["state_changed"]),
        "current_source_files": int(plan["current_source_files"]),
        "deferred_files": int(plan["deferred_files"]),
        "deferred_tail_bytes": int(plan["deferred_tail_bytes"]),
        "deferred_source_ids": list(plan["deferred_source_ids"]),
        "deferred_reasons": dict(plan["deferred_reasons"]),
        "publication_complete": bool(plan["publication_complete"]),
        "source_exact_manifest": bool(plan["source_exact_manifest"]),
        "pending": bool(plan["pending"]),
        "retry_next_cycle": bool(plan["pending"]),
    }


def mark_deferred(
    deferred: Dict[Tuple[str, str], Set[str]],
    key: Tuple[str, str],
    reason: str,
) -> None:
    """Record a body-free retry reason without exposing a source path."""

    deferred.setdefault(key, set()).add(reason)


def build_plan(
    archive_root: Path,
    device_id: str,
    home: Path,
    source: str,
    chunk_bytes: int,
    write_objects: bool,
) -> Dict[str, Any]:
    existing, _ = load_manifest(archive_root, device_id)
    existing_files: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    if existing:
        for record in existing["files"]:
            key = file_key(record)
            if key in existing_files:
                raise ArchiveError("duplicate-manifest-file")
            existing_files[key] = record

    discovered = discover_source_files(home, source)
    current_keys = {(item[0], item[2]) for item in discovered}
    files: List[Dict[str, Any]] = []
    deferred: Dict[Tuple[str, str], Set[str]] = {}
    deferred_tail_bytes = 0
    metrics = {
        "new_files": 0,
        "changed_files": 0,
        "repair_files": 0,
        "removed_files": len(set(existing_files) - current_keys),
        "objects_missing": 0,
        "objects_written": 0,
        "bytes_to_write": 0,
        "bytes_written": 0,
    }

    compatible_chunks = existing is None or int(existing.get("chunk_bytes", -1)) == chunk_bytes
    for source_name, path, reference in discovered:
        key = (source_name, reference)
        old = existing_files.get(key)
        try:
            signature = source_signature(path)
        except ArchiveError as error:
            if error.code not in DEFERABLE_SCAN_ERRORS:
                raise
            if old is None:
                metrics["new_files"] += 1
            else:
                metrics["changed_files"] += 1
                if not compatible_chunks:
                    raise ArchiveError("deferred-source-chunk-size-mismatch") from error
                if not chunks_are_present(old, archive_root):
                    raise ArchiveError("deferred-source-object-missing") from error
                files.append(dict(old))
            mark_deferred(deferred, key, error.code)
            continue
        reusable = (
            old is not None
            and compatible_chunks
            and record_signature(old) == signature
            and chunks_are_present(old, archive_root)
        )
        if reusable:
            files.append(dict(old))
            continue

        if old is None:
            metrics["new_files"] += 1
        elif record_signature(old) == signature:
            metrics["repair_files"] += 1
        else:
            metrics["changed_files"] += 1
        try:
            record, scanned = scan_source_file(
                source_name,
                path,
                reference,
                archive_root,
                chunk_bytes,
                write_objects,
                previous=old,
            )
        except ArchiveError as error:
            if error.code not in DEFERABLE_SCAN_ERRORS:
                raise
            # An actively appended source must not prevent stable siblings from
            # advancing.  Preserve the prior immutable record when one exists;
            # a new unstable source remains absent until a later stable pass.
            # Objects already created during the interrupted scan are harmless
            # unreferenced content-addressed objects and are never overwritten.
            if old is not None:
                if not compatible_chunks:
                    raise ArchiveError("deferred-source-chunk-size-mismatch") from error
                if not chunks_are_present(old, archive_root):
                    raise ArchiveError("deferred-source-object-missing") from error
                files.append(dict(old))
            for name in (
                "objects_missing",
                "objects_written",
                "bytes_to_write",
                "bytes_written",
            ):
                metrics[name] += int(error.metrics.get(name, 0))
            mark_deferred(deferred, key, error.code)
            continue
        files.append(record)
        for name in ("objects_missing", "objects_written", "bytes_to_write", "bytes_written"):
            metrics[name] += int(scanned[name])

    # A source log may be deliberately retired from local storage after its
    # immutable objects are archived.  Keep that record in the latest manifest
    # so the archive remains cumulative and the absence is reportable.  It
    # cannot be rechunked or repaired without the source body, so fail closed if
    # the requested format changed or any declared object is absent.
    retired_keys = sorted(set(existing_files) - current_keys)
    if retired_keys and not compatible_chunks:
        raise ArchiveError("retired-source-chunk-size-mismatch")
    for key in retired_keys:
        record = existing_files[key]
        if not chunks_are_present(record, archive_root):
            raise ArchiveError("retired-source-object-missing")
        files.append(dict(record))

    records_by_key = {file_key(record): record for record in files}

    # Reconcile path churn instead of discarding the safe work for every stable
    # sibling.  An old record remains immutable evidence; a newly appeared or
    # disappeared path is explicitly deferred and retried on the next pass.
    final_discovered = discover_source_files(home, source)
    final_paths = {
        (source_name, reference): path
        for source_name, path, reference in final_discovered
    }
    final_keys = set(final_paths)
    for key in sorted(current_keys - final_keys):
        if key in existing_files:
            records_by_key[key] = dict(existing_files[key])
        else:
            records_by_key.pop(key, None)
        mark_deferred(deferred, key, "source-disappeared-during-scan")
    for key in sorted(final_keys - current_keys):
        # A previously retired path already has its old immutable record in the
        # cumulative manifest.  A genuinely new path has no publishable record.
        mark_deferred(deferred, key, "source-appeared-during-scan")

    # A cheap exact-signature comparison handles almost every file.  Only a
    # source that changed after its scan is re-hashed at the proposed high-water
    # mark.  A pure append can therefore publish the already-read prefix while
    # exposing the remaining tail as pending.  If the prefix cannot be proved,
    # fall back to the prior record (or omit a new record) rather than blessing
    # a moving source or blocking unrelated stable progress.
    for key in sorted(current_keys & final_keys):
        record = records_by_key.get(key)
        if record is None:
            continue
        path = final_paths[key]
        try:
            signature = source_signature(path)
        except ArchiveError as error:
            if error.code not in DEFERABLE_PUBLISH_ERRORS:
                raise
            if key in existing_files:
                records_by_key[key] = dict(existing_files[key])
            else:
                records_by_key.pop(key, None)
            mark_deferred(deferred, key, error.code)
            continue
        if signature == record_signature(record):
            continue
        try:
            actual_digest, current_size = hash_source_prefix(
                path, int(record.get("size"))
            )
        except ArchiveError as error:
            if error.code not in DEFERABLE_PUBLISH_ERRORS:
                raise
            if key in existing_files:
                records_by_key[key] = dict(existing_files[key])
            else:
                records_by_key.pop(key, None)
            mark_deferred(deferred, key, error.code)
            continue
        if actual_digest != str(record.get("sha256") or ""):
            if key in existing_files:
                records_by_key[key] = dict(existing_files[key])
            else:
                records_by_key.pop(key, None)
            mark_deferred(deferred, key, "source-prefix-changed-before-publish")
            continue
        archived_size = int(record.get("size"))
        if current_size > archived_size:
            deferred_tail_bytes += current_size - archived_size
            mark_deferred(deferred, key, "source-appended-after-scan")

    files = sorted((dict(record) for record in records_by_key.values()), key=file_key)
    metrics["removed_files"] = len(set(existing_files) - final_keys)

    deferred_reasons: Dict[str, int] = {}
    for reasons in deferred.values():
        for reason in reasons:
            deferred_reasons[reason] = deferred_reasons.get(reason, 0) + 1
    deferred_ids = [issue_id(*key) for key in sorted(deferred)]

    digest = state_digest(device_id, chunk_bytes, files)
    existing_digest = existing.get("state_sha256") if existing else None
    result: Dict[str, Any] = {
        "files": files,
        "source_bytes": sum(int(record["size"]) for record in files),
        "state_sha256": digest,
        "state_changed": digest != existing_digest,
        "current_source_files": len(final_keys),
        "deferred_files": len(deferred),
        "deferred_tail_bytes": deferred_tail_bytes,
        "deferred_source_ids": deferred_ids,
        "deferred_reasons": dict(sorted(deferred_reasons.items())),
        "publication_complete": not deferred,
        "source_exact_manifest": bool(not deferred and not metrics["removed_files"]),
        "pending": bool(deferred),
    }
    result.update(metrics)
    return result


def apply_archive(
    archive_root: Path,
    device_id: str,
    home: Path,
    source: str,
    chunk_bytes: int,
) -> Dict[str, Any]:
    archive_root.mkdir(parents=True, exist_ok=True)
    lock_path = archive_root / ".archive.lock"
    with lock_path.open("a+") as lock:
        try:
            file_lock.acquire(lock, exclusive=True, blocking=False)
        except BlockingIOError:
            raise ArchiveError("archive-already-running")

        prior, prior_raw = load_manifest(archive_root, device_id)
        plan = build_plan(archive_root, device_id, home, source, chunk_bytes, True)
        if prior is not None and not plan["state_changed"]:
            prior_payload = prior_raw or canonical_bytes(prior)
            prior_digest = sha256_bytes(prior_payload)
            retained_full = matching_full_verification(
                archive_root, device_id, prior_digest, prior
            )
            specs = object_specs(prior.get("files", []))
            sample_round = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
            selected = verification_sample(list(specs), sample_round)
            # An unchanged apply remains a cheap rotating corruption check, but
            # must not destroy a still-useful full proof for the same manifest.
            verify_selected_objects(archive_root, specs, selected)
            if retained_full is not None:
                verification_record = retained_full
                verification_preserved = True
            else:
                verification_record = save_verification_record(
                    archive_root, prior, prior_payload, False
                )
                verification_preserved = False
            result = public_plan(plan, archive_root, device_id)
            result.update({
                "action": "unchanged",
                "manifest": str(manifest_path(archive_root)),
                "manifest_sha256": prior_digest,
                "objects_written": int(plan["objects_written"]),
                "bytes_written": int(plan["bytes_written"]),
                "verification_record": str(verification_path(archive_root)),
                "objects_sampled": len(selected),
                "verification_mode": str(verification_record.get("mode") or ""),
                "full_verification_preserved": verification_preserved,
            })
            return result

        manifest: Dict[str, Any] = state_material(device_id, chunk_bytes, plan["files"])
        manifest.update({
            "created_at": utc_now(),
            "state_sha256": plan["state_sha256"],
            "previous_manifest_sha256": sha256_bytes(prior_raw) if prior_raw else None,
        })
        payload = canonical_bytes(manifest)
        manifest_sha256 = sha256_bytes(payload)
        snapshot = archive_root / "manifests" / (manifest_sha256 + ".json")
        atomic_create(snapshot, payload, manifest_sha256)
        atomic_replace(manifest_path(archive_root), payload)
        verification_record = save_verification_record(archive_root, manifest, payload, False)

        result = public_plan(plan, archive_root, device_id)
        result.update({
            "action": "archived",
            "manifest": str(manifest_path(archive_root)),
            "snapshot": str(snapshot),
            "manifest_sha256": manifest_sha256,
            "objects_written": int(plan["objects_written"]),
            "bytes_written": int(plan["bytes_written"]),
            "verification_record": str(verification_path(archive_root)),
            "objects_sampled": verification_record["objects_verified"],
        })
        return result


def issue_id(source: str, reference: str) -> str:
    return hashlib.sha256((source + "\0" + reference).encode("utf-8")).hexdigest()[:16]


def verify_archive(
    archive_root: Path,
    device_id: str,
    home: Path,
    source: str,
) -> Dict[str, Any]:
    manifest, raw = load_manifest(archive_root, device_id)
    if manifest is None or raw is None:
        return {"ok": False, "action": "verify", "device_id": device_id, "issues": ["manifest-missing"]}

    issues: List[str] = []
    files = manifest.get("files", [])
    try:
        chunk_bytes = int(manifest.get("chunk_bytes"))
    except (TypeError, ValueError):
        chunk_bytes = 0
    if chunk_bytes <= 0:
        issues.append("invalid-chunk-size")
    else:
        expected_state = state_digest(device_id, chunk_bytes, files)
        if manifest.get("state_sha256") != expected_state:
            issues.append("state-digest-mismatch")

    raw_digest = sha256_bytes(raw)
    snapshot = archive_root / "manifests" / (raw_digest + ".json")
    if not snapshot.is_file() or snapshot.is_symlink() or snapshot.read_bytes() != raw:
        issues.append("immutable-manifest-missing-or-different")

    verified_objects: Dict[str, int] = {}
    total_bytes = 0
    manifest_by_key: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    for record in files:
        try:
            key = file_key(record)
            if key in manifest_by_key:
                raise ArchiveError("duplicate-manifest-file")
            manifest_by_key[key] = record
            chunks = record.get("chunks")
            if not isinstance(chunks, list):
                raise ArchiveError("invalid-manifest-file")
            file_digest = hashlib.sha256()
            file_bytes = 0
            for chunk in chunks:
                digest = str(chunk.get("sha256"))
                size = int(chunk.get("size"))
                path = object_path(archive_root, digest)
                if not path.is_file() or path.is_symlink():
                    issues.append("object-missing:%s" % digest[:16])
                    continue
                if path.stat().st_size != size:
                    issues.append("object-size-mismatch:%s" % digest[:16])
                    continue
                chunk_digest = hashlib.sha256()
                with path.open("rb") as handle:
                    while True:
                        block = handle.read(1024 * 1024)
                        if not block:
                            break
                        if digest not in verified_objects:
                            chunk_digest.update(block)
                        file_digest.update(block)
                        file_bytes += len(block)
                if digest not in verified_objects:
                    if chunk_digest.hexdigest() != digest:
                        issues.append("object-digest-mismatch:%s" % digest[:16])
                        continue
                    verified_objects[digest] = size
            if file_bytes != int(record.get("size")):
                issues.append("file-size-mismatch:%s" % issue_id(*key))
            elif file_digest.hexdigest() != record.get("sha256"):
                issues.append("file-digest-mismatch:%s" % issue_id(*key))
            total_bytes += int(record.get("size"))
        except (ArchiveError, KeyError, TypeError, ValueError):
            issues.append("invalid-manifest-file")

    object_verification_ok = not issues
    coverage = assess_source_high_water_coverage(files, home, source)
    issues.extend(str(item) for item in coverage["issues"])

    result = {
        "ok": not issues,
        "action": "verify",
        "device_id": device_id,
        "manifest": str(manifest_path(archive_root)),
        "manifest_sha256": raw_digest,
        "source_files": len(files),
        "source_bytes": total_bytes,
        "objects_verified": len(verified_objects),
        "object_verification_ok": object_verification_ok,
        "source_coverage_ok": bool(coverage["ok"]),
        "source_matches_manifest": bool(coverage["source_exact_manifest"]),
        "source_exact_manifest": bool(coverage["source_exact_manifest"]),
        "coverage_mode": coverage["coverage_mode"],
        "current_source_files": int(coverage["current_source_files"]),
        "unarchived_tail_files": int(coverage["unarchived_tail_files"]),
        "unarchived_tail_bytes": int(coverage["unarchived_tail_bytes"]),
        "unarchived_tail_source_ids": list(coverage["unarchived_tail_source_ids"]),
        "retired_source_files": int(coverage["retired_source_files"]),
        "retired_source_ids": list(coverage["retired_source_ids"]),
        "issues": sorted(set(issues)),
    }
    # mode="full" is publishable only when immutable objects and live source
    # coverage both pass.  Otherwise atomically downgrade any prior matching
    # full record so a peer (which cannot inspect this Mac's sources) cannot
    # mistake stale coverage for current readiness.
    if object_verification_ok and coverage["ok"]:
        record = save_verification_record(
            archive_root,
            manifest,
            raw,
            True,
            already_verified=sorted(verified_objects),
        )
    else:
        record = invalidate_verification_record(
            archive_root,
            manifest,
            raw,
            "coverage-failed" if object_verification_ok else "verification-failed",
        )
    result.update({
        "verification_record": str(verification_path(archive_root)),
        "verification_mode": record["mode"],
    })
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive complete Codex/Claude JSONL history without printing bodies")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--plan", action="store_true", help="scan and report without writing")
    actions.add_argument("--apply", action="store_true", help="write missing objects and publish a manifest")
    actions.add_argument("--verify", action="store_true", help="verify the latest immutable archive and source freshness")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", default=str(ROOT), help="knowledge-base root")
    parser.add_argument("--home", default=str(Path.home()), help=argparse.SUPPRESS)
    parser.add_argument("--device-id")
    parser.add_argument("--source", choices=("codex", "claude", "all"), default="all")
    parser.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES, help=argparse.SUPPRESS)
    return parser


def emit(result: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    if result.get("action") == "plan":
        print(
            "plan: files=%d bytes=%d missing_objects=%d bytes_to_write=%d"
            % (
                result["source_files"], result["source_bytes"],
                result["objects_missing"], result["bytes_to_write"],
            )
        )
    elif result.get("action") == "verify":
        print(
            "verify: %s files=%d objects=%d"
            % ("ok" if result.get("ok") else "failed", result.get("source_files", 0), result.get("objects_verified", 0))
        )
    else:
        print(
            "%s: files=%d objects_written=%d bytes_written=%d"
            % (
                result.get("action"), result.get("source_files", 0),
                result.get("objects_written", 0), result.get("bytes_written", 0),
            )
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.chunk_bytes < 1024 or args.chunk_bytes > 64 * 1024 * 1024:
        raise ArchiveError("invalid-chunk-size")
    root = Path(args.root).expanduser().resolve()
    home = Path(args.home).expanduser().resolve()
    device_id = resolve_device_id(args.device_id, root, home)
    archive_root = root / ".kb-data" / "raw-history" / device_id
    data_root = root / ".kb-data"
    try:
        cold_status = device_identity.validate_cold_data_root(
            root, home=home, require_bound_volume=True
        )
    except device_identity.DeviceIdentityError as error:
        raise ArchiveError("device-identity-invalid") from error
    if not cold_status.get("valid"):
        raise ArchiveError(str(cold_status.get("reason") or "cold-data-unavailable"))
    try:
        with storage_lock.acquire(root, exclusive=False):
            if args.plan:
                plan = build_plan(archive_root, device_id, home, args.source, args.chunk_bytes, False)
                result = public_plan(plan, archive_root, device_id)
            elif args.apply:
                result = apply_archive(archive_root, device_id, home, args.source, args.chunk_bytes)
            else:
                result = verify_archive(archive_root, device_id, home, args.source)
    except storage_lock.StorageBusy as error:
        raise ArchiveError(str(error)) from error
    emit(result, args.json)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ArchiveError as exc:
        action = next((name for name in ("plan", "apply", "verify") if "--" + name in sys.argv), "archive")
        as_json = "--json" in sys.argv
        result = {"ok": False, "action": action, "error": exc.code}
        if as_json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print("%s failed: %s" % (action, exc.code), file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        # Never let an unexpected filesystem/parser exception print a source
        # path or source body through a traceback in unattended task logs.
        action = next((name for name in ("plan", "apply", "verify") if "--" + name in sys.argv), "archive")
        as_json = "--json" in sys.argv
        result = {"ok": False, "action": action, "error": exc.__class__.__name__}
        if as_json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print("%s failed: %s" % (action, exc.__class__.__name__), file=sys.stderr)
        raise SystemExit(2)
