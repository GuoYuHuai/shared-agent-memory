#!/usr/bin/env python3
"""Atomic, body-safe outcome records for finite AI-Knowledge automations.

The record lives beside the local device identity instead of below
``.kb-data``.  A failed SSD mount must therefore be observable as a failed
attempt; it cannot make the evidence file itself disappear.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
from typing import Any, Dict, Mapping, Optional
import uuid

import device_identity


ROOT = Path(__file__).resolve().parents[1]
FORMAT = "ai-knowledge-automation-status-v1"
TASKS = ("capture", "hot_sync", "curate", "audit")
FINAL_RESULTS = ("success", "no-op", "pending", "failure")
SAFE_REASON = re.compile(r"[^A-Za-z0-9_.:/-]+")


class AutomationStatusError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat()


def normalize_task(task: str) -> str:
    normalized = str(task).strip().lower().replace("-", "_")
    if normalized not in TASKS:
        raise AutomationStatusError("unsupported-automation-task")
    return normalized


def status_path(
    task: str,
    root: Path = ROOT,
    home: Optional[Path] = None,
) -> Path:
    normalized = normalize_task(task)
    local_root = device_identity.local_state_path(root, home).parent
    return local_root / "automation-status" / (normalized + ".json")


def read_status(
    task: str,
    root: Path = ROOT,
    home: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    path = status_path(task, root=root, home=home)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def safe_reason(value: object, fallback: str = "automation-failed") -> str:
    first_line = str(value or "").splitlines()[0].strip() if value else ""
    normalized = SAFE_REASON.sub("-", first_line).strip("-._:/")
    return (normalized[:160] or fallback)[:160]


def _processed(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _write(
    task: str,
    payload: Mapping[str, Any],
    root: Path,
    home: Optional[Path],
) -> Dict[str, Any]:
    path = status_path(task, root=root, home=home)
    value = dict(payload)
    device_identity.atomic_write(path, value)
    return value


def record_attempt(
    task: str,
    root: Path = ROOT,
    home: Optional[Path] = None,
    started_at: Optional[str] = None,
) -> str:
    """Start an attempt and preserve the last proven success, if any."""

    normalized = normalize_task(task)
    previous = read_status(normalized, root=root, home=home) or {}
    attempt_id = uuid.uuid4().hex
    start = str(started_at or utc_now())
    payload = {
        "format": FORMAT,
        "task": normalized,
        "attempt_id": attempt_id,
        "last_attempt_started_at": start,
        "last_attempt_at": start,
        "last_success_at": previous.get("last_success_at"),
        "result": "running",
        "processed": 0,
        "error": None,
        "exit_code": None,
        "updated_at": start,
    }
    _write(normalized, payload, root, home)
    return attempt_id


def _finish(
    task: str,
    attempt_id: str,
    *,
    result: str,
    processed: object,
    error: Optional[str],
    exit_code: Optional[int],
    root: Path,
    home: Optional[Path],
    finished_at: Optional[str],
) -> Dict[str, Any]:
    normalized = normalize_task(task)
    if result not in FINAL_RESULTS:
        raise AutomationStatusError("unsupported-automation-result")
    previous = read_status(normalized, root=root, home=home) or {}
    # A later attempt owns the current record. An older process finishing late
    # must never overwrite newer evidence.
    if previous.get("attempt_id") not in (None, attempt_id):
        return {**previous, "recorded": False, "write_skipped": "newer-attempt-exists"}
    finished = str(finished_at or utc_now())
    success = result in ("success", "no-op")
    payload = {
        "format": FORMAT,
        "task": normalized,
        "attempt_id": attempt_id,
        "last_attempt_started_at": previous.get("last_attempt_started_at")
        or finished,
        "last_attempt_at": finished,
        # This is the central no-fake-green rule: only a proven successful
        # execution, including a successful no-op, advances last_success_at.
        "last_success_at": finished
        if success
        else previous.get("last_success_at"),
        "result": result,
        "processed": _processed(processed),
        "error": safe_reason(error) if error else None,
        "exit_code": exit_code,
        "updated_at": finished,
    }
    value = _write(normalized, payload, root, home)
    value["recorded"] = True
    return value


def record_success(
    task: str,
    attempt_id: str,
    processed: object = 0,
    result: str = "success",
    root: Path = ROOT,
    home: Optional[Path] = None,
    finished_at: Optional[str] = None,
) -> Dict[str, Any]:
    if result not in ("success", "no-op"):
        raise AutomationStatusError("successful-result-must-be-success-or-no-op")
    return _finish(
        task,
        attempt_id,
        result=result,
        processed=processed,
        error=None,
        exit_code=0,
        root=root,
        home=home,
        finished_at=finished_at,
    )


def record_pending(
    task: str,
    attempt_id: str,
    reason: str,
    processed: object = 0,
    root: Path = ROOT,
    home: Optional[Path] = None,
    finished_at: Optional[str] = None,
) -> Dict[str, Any]:
    return _finish(
        task,
        attempt_id,
        result="pending",
        processed=processed,
        error=reason,
        exit_code=0,
        root=root,
        home=home,
        finished_at=finished_at,
    )


def record_failure(
    task: str,
    attempt_id: str,
    error: str,
    processed: object = 0,
    exit_code: int = 2,
    root: Path = ROOT,
    home: Optional[Path] = None,
    finished_at: Optional[str] = None,
) -> Dict[str, Any]:
    return _finish(
        task,
        attempt_id,
        result="failure",
        processed=processed,
        error=error,
        exit_code=int(exit_code),
        root=root,
        home=home,
        finished_at=finished_at,
    )
