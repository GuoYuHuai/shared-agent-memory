#!/usr/bin/env python3
"""Read-only integrity and freshness checks for content-addressed raw archives.

This standalone component reuses the archiver's coverage proof. It has no
scheduler, simulator, mount-management or personal workstation dependency.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Dict, Mapping, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import archive_raw_history

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RAW_VERIFICATION_FORMAT = archive_raw_history.VERIFICATION_FORMAT
RAW_VERIFICATION_MAX_AGE_HOURS = 36


def json_object(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None

def config(root: Path) -> Dict[str, Any]:
    return json_object(root / "config" / "kb.json") or {}

def configured_data_root(root: Path) -> Path:
    raw = str(config(root).get("data_root") or ".kb-data")
    path = Path(raw).expanduser()
    return path if path.is_absolute() else root / path

def canonical_digest(value: Mapping[str, Any]) -> str:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

def raw_source_high_water_coverage(
    files: Sequence[Mapping[str, Any]], home: Path
) -> Dict[str, Any]:
    """Use the archiver's race-safe content proof so readiness cannot drift."""
    return archive_raw_history.assess_source_high_water_coverage(files, home, "all")

def check_raw_archive_manifest(
    root: Path,
    local_device_id: Optional[str],
    home: Optional[Path] = None,
    require_source_freshness: bool = False,
) -> Dict[str, Any]:
    archive_root = configured_data_root(root) / "raw-history" / (local_device_id or "missing")
    manifest_path = archive_root / "manifest.json"
    report: Dict[str, Any] = {
        "ok": False,
        "status": "manifest-missing",
        "manifest_present": manifest_path.is_file() and not manifest_path.is_symlink(),
        "snapshot_present": False,
        "source_files": 0,
        "source_bytes": 0,
        "objects_declared": 0,
        "objects_present": 0,
        "verification_fresh": False,
        "verified_object_digests": 0,
        "full_baseline_object_digests": 0,
        "live_sampled_object_digests": 0,
        "source_matches_manifest": None if not require_source_freshness else False,
        "source_exact_manifest": None if not require_source_freshness else False,
        "source_coverage_ok": None if not require_source_freshness else False,
        "coverage_mode": "not-checked" if not require_source_freshness else "failed",
        "current_source_files": None if not require_source_freshness else 0,
        "unarchived_tail_files": 0,
        "unarchived_tail_bytes": 0,
        "retired_source_files": 0,
        "coverage_issues": [],
    }
    if not local_device_id or not report["manifest_present"]:
        return report
    try:
        raw = manifest_path.read_bytes()
        if len(raw) > 64 * 1024 * 1024:
            report["status"] = "manifest-too-large"
            return report
        manifest = json.loads(raw.decode("utf-8"))
        if not isinstance(manifest, dict):
            report["status"] = "manifest-invalid"
            return report
        files = manifest.get("files")
        try:
            chunk_bytes = int(manifest.get("chunk_bytes"))
        except (TypeError, ValueError):
            chunk_bytes = 0
        if (
            manifest.get("format") != "ai-knowledge-raw-history-v1"
            or manifest.get("device_id") != local_device_id
            or not isinstance(files, list)
            or not files
            or chunk_bytes <= 0
        ):
            report["status"] = "manifest-invalid"
            return report
        state = {
            "format": manifest.get("format"),
            "device_id": manifest.get("device_id"),
            "chunk_bytes": chunk_bytes,
            "files": files,
        }
        if manifest.get("state_sha256") != canonical_digest(state):
            report["status"] = "state-digest-mismatch"
            return report
        manifest_digest = hashlib.sha256(raw).hexdigest()
        snapshot = archive_root / "manifests" / (manifest_digest + ".json")
        report["snapshot_present"] = bool(
            snapshot.is_file() and not snapshot.is_symlink() and snapshot.read_bytes() == raw
        )
        seen_objects: set = set()
        valid = True
        for record in files:
            if not isinstance(record, dict) or record.get("source") not in {"codex", "claude"}:
                valid = False
                break
            try:
                size = int(record.get("size"))
            except (TypeError, ValueError):
                valid = False
                break
            chunks = record.get("chunks")
            if size < 0 or not SHA256_RE.fullmatch(str(record.get("sha256") or "")) or not isinstance(chunks, list):
                valid = False
                break
            chunk_total = 0
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    valid = False
                    break
                digest = str(chunk.get("sha256") or "")
                try:
                    chunk_size = int(chunk.get("size"))
                except (TypeError, ValueError):
                    valid = False
                    break
                if chunk_size < 0 or not SHA256_RE.fullmatch(digest):
                    valid = False
                    break
                chunk_total += chunk_size
                if digest not in seen_objects:
                    seen_objects.add(digest)
                    report["objects_declared"] += 1
                    object_path = archive_root / "objects" / "sha256" / digest[:2] / (digest[2:] + ".chunk")
                    if object_path.is_file() and not object_path.is_symlink() and object_path.stat().st_size == chunk_size:
                        report["objects_present"] += 1
            if not valid or chunk_total != size:
                valid = False
                break
            report["source_bytes"] += size
        report["source_files"] = len(files)
        content_structure_ok = bool(
            valid
            and report["snapshot_present"]
            and report["objects_declared"] == report["objects_present"]
        )
        verification_path = archive_root / "verification.json"
        verification = json_object(verification_path)
        verification_ok = False
        if content_structure_ok and verification:
            try:
                verified_at = dt.datetime.fromisoformat(
                    str(verification.get("verified_at") or "").replace("Z", "+00:00")
                )
                if verified_at.tzinfo is None:
                    verified_at = verified_at.replace(tzinfo=dt.timezone.utc)
                age = dt.datetime.now(dt.timezone.utc) - verified_at.astimezone(dt.timezone.utc)
                digests = verification.get("object_digests")
                mode = verification.get("mode")
                verification_ok = bool(
                    verification.get("format") == RAW_VERIFICATION_FORMAT
                    and verification.get("device_id") == local_device_id
                    and verification.get("manifest_sha256") == manifest_digest
                    and mode == "full"
                    and isinstance(digests, list)
                    and dt.timedelta(0) <= age <= dt.timedelta(hours=RAW_VERIFICATION_MAX_AGE_HOURS)
                )
                if verification_ok:
                    declared = {
                        str(chunk.get("sha256"))
                        for record in files
                        for chunk in record.get("chunks", [])
                        if isinstance(chunk, dict)
                    }
                    if any(not isinstance(item, str) or item not in declared for item in digests):
                        verification_ok = False
                    else:
                        # The recent full record already certifies every object
                        # for this immutable manifest. Readiness rechecks a
                        # deterministic daily sample plus existence/size for
                        # all objects; the capture cadence refreshes the full
                        # proof at least every 24h. Rehashing the entire
                        # archive on every dashboard build would turn a small
                        # status check into repeated full-history scanning.
                        sample_round = dt.datetime.now(dt.timezone.utc).strftime(
                            "%Y-%m-%d"
                        )
                        sampled = archive_raw_history.verification_sample(
                            sorted(declared), sample_round
                        )
                        for digest in sampled:
                            object_path = archive_root / "objects" / "sha256" / digest[:2] / (digest[2:] + ".chunk")
                            if not object_path.is_file() or object_path.is_symlink():
                                verification_ok = False
                                break
                            if hashlib.sha256(object_path.read_bytes()).hexdigest() != digest:
                                verification_ok = False
                                break
                    if mode == "full" and set(digests) != declared:
                        verification_ok = False
                    try:
                        if (
                            int(verification.get("objects_total")) != len(declared)
                            or int(verification.get("objects_verified")) != len(declared)
                        ):
                            verification_ok = False
                    except (TypeError, ValueError):
                        verification_ok = False
                    if verification_ok:
                        report["full_baseline_object_digests"] = len(digests)
                        report["live_sampled_object_digests"] = len(sampled)
                        report["verified_object_digests"] = len(sampled)
            except (OSError, TypeError, ValueError):
                verification_ok = False
        source_coverage_ok = True
        if require_source_freshness:
            coverage = raw_source_high_water_coverage(
                files, (home or Path.home()).expanduser()
            )
            source_coverage_ok = bool(coverage["ok"])
            report.update({
                "source_matches_manifest": bool(coverage["source_exact_manifest"]),
                "source_exact_manifest": bool(coverage["source_exact_manifest"]),
                "source_coverage_ok": source_coverage_ok,
                "coverage_mode": str(coverage["coverage_mode"]),
                "current_source_files": int(coverage["current_source_files"]),
                "unarchived_tail_files": int(coverage["unarchived_tail_files"]),
                "unarchived_tail_bytes": int(coverage["unarchived_tail_bytes"]),
                "retired_source_files": int(coverage["retired_source_files"]),
                "coverage_issues": list(coverage["issues"]),
            })
        report["verification_fresh"] = verification_ok
        report["ok"] = bool(content_structure_ok and verification_ok and source_coverage_ok)
        report["status"] = (
            "ready"
            if report["ok"]
            else "source-high-water-coverage-failed"
            if content_structure_ok and verification_ok and not source_coverage_ok
            else "full-verification-missing-stale-or-corrupt"
            if content_structure_ok
            else "objects-or-snapshot-incomplete"
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        report["status"] = "manifest-unreadable"
    return report
