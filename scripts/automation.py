#!/usr/bin/env python3
"""Finite, capability-based jobs with separate scheduler and execution receipts.

A plan is never evidence of registration; a successful manual run is never
proof of a scheduler. All receipts are device-local, outside the hot Git layer.
"""
import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys
import time
import uuid

import file_lock

TOOL_ROOT = Path(__file__).resolve().parents[1]
JOBS = ("health", "sync", "capture", "curate", "review")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def directory(root):
    return root / ".ai-knowledge" / "automation"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read(root, job):
    path = directory(root) / (job + ".json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"job": job}


def update_state(root, job, changes, defaults=None):
    directory(root).mkdir(parents=True, exist_ok=True)
    with (directory(root) / (job + ".state.lock")).open("a+") as lock:
        file_lock.acquire(lock, exclusive=True, blocking=True)
        try:
            state = read(root, job)
            for key, value in (defaults or {}).items():
                state.setdefault(key, value)
            state.update(changes)
            write(directory(root) / (job + ".json"), state)
            return state
        finally:
            file_lock.release(lock)


def plan(root, job, interval_seconds=1800):
    try:
        with scheduler_operation(root):
            return _plan(root, job, interval_seconds)
    except BlockingIOError:
        return {"ok": False, "pending": True, "reason": "scheduler-operation-running"}


def _plan(root, job, interval_seconds=1800):
    """Called only under scheduler_operation; planning cannot rewrite registration."""
    if interval_seconds < 1:
        raise ValueError("interval must be positive")
    state = read(root, job)
    if state.get("registration") == "registered":
        if state.get("interval_seconds") != interval_seconds:
            return {"ok": False, "reason": "interval-change-requires-disable-and-register"}
        return state
    return update_state(root, job,
        {"format": "ai-knowledge-job-v1", "job": job, "interval_seconds": interval_seconds,
         "timezone": str(dt.datetime.now().astimezone().tzinfo),
         "requires_model": job == "curate", "requires_history": job == "capture"},
        {"planned_at": now(), "registration": "not-registered", "trial": "not-run", "last_success": None})


def command(root, job):
    scripts = TOOL_ROOT / "scripts"
    config_file = root / "config" / "kb.json"
    config = json.loads(config_file.read_text(encoding="utf-8")) if config_file.is_file() else {}
    if job == "sync":
        return [sys.executable, str(scripts / "git_sync.py"), "--pull-only"]
    if job == "capture":
        history = config.get("history_capture", {})
        if history.get("enabled") is not True:
            return None
        source = history.get("source")
        if source not in ("codex", "claude", "all"):
            raise ValueError("history source parser unavailable; use the adapter contract")
        return [sys.executable, str(scripts / "ingest_history.py"), "--scan", "--source", source,
                "--max-lines", str(history.get("maximum_lines", 2000))]
    if job == "curate":
        if config.get("semantic_maintenance", {}).get("enabled") is not True:
            return None
        return [sys.executable, str(scripts / "run_semantic_maintenance.py"), "--mode", "curate"]
    return None


def inspect_hot(root):
    """A deterministic health check, usable without cold storage or a model."""
    count = 0
    for zone in ("ledger", "profile", "projects", "resources", "playbooks"):
        for path in (root / zone).rglob("*") if (root / zone).exists() else ():
            if path.is_symlink():
                continue
            if path.is_file() and path.suffix == ".json":
                json.loads(path.read_text(encoding="utf-8"))
                count += 1
    if not (root / "POLICY.md").is_file():
        raise ValueError("policy missing")
    return {"ok": True, "validated_json_files": count, "coverage": "hot-json-syntax-and-policy",
            "semantic_review": "not-performed"}


def valid_job_receipt(job, payload):
    if not isinstance(payload, dict):
        return False
    if job == "capture":
        return (payload.get("action") == "scan"
                and isinstance(payload.get("source_device_id"), str)
                and all(isinstance(payload.get(key), int) and not isinstance(payload[key], bool)
                        and payload[key] >= 0 for key in ("lines", "accepted", "inserted"))
                and payload.get("coverage_status") in ("partial", "scanned-discovered-sources")
                and payload.get("ok") is not False)
    return payload.get("ok") is True and payload.get("action") == {"sync": "git-sync", "curate": "curate"}.get(job)


def run(root, job, timeout=60):
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    state = read(root, job)
    directory(root).mkdir(parents=True, exist_ok=True)
    with (directory(root) / (job + ".lock")).open("a+") as lock:
        try:
            file_lock.acquire(lock, exclusive=True, blocking=False)
        except BlockingIOError:
            return {"ok": True, "pending": True, "reason": "job-already-running"}
        started = time.monotonic()
        result = {"started_at": now(), "input_events": None, "model": None,
                  "usage": {"status": "unknown", "input_tokens": None, "output_tokens": None, "cached_tokens": None},
                  "retry_count": 0, "execution_id": uuid.uuid4().hex}
        try:
            cmd = command(root, job)
            if job == "health":
                result.update(inspect_hot(root))
                result["usage"] = {"status": "not-applicable", "model_calls": 0}
            elif job == "review":
                result.update({"ok": True, "pending": True, "reason": "foreground-knowledge-review-required"})
            elif cmd is None:
                result.update({"ok": True, "pending": True, "reason": "capability-or-user-scope-not-enabled"})
            else:
                env = dict(os.environ, AI_KNOWLEDGE_ROOT=str(root), PYTHONDONTWRITEBYTECODE="1")
                completed = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, timeout=timeout)
                try:
                    payload = json.loads(completed.stdout)
                except ValueError:
                    payload = {}
                valid = valid_job_receipt(job, payload)
                if not isinstance(payload, dict):
                    payload = {}
                result.update({"ok": completed.returncode == 0 and valid, "exit_code": completed.returncode,
                               "input_events": payload.get("accepted", payload.get("events_processed")),
                               "pending": bool(payload.get("pending") or payload.get("coverage_status") == "partial"),
                               "reason": (payload.get("stop_reason") or payload.get("error")) if valid else "invalid-job-receipt"})
                if isinstance(payload.get("usage"), dict):
                    result["usage"] = payload["usage"]
        except subprocess.TimeoutExpired:
            result.update({"ok": False, "pending": True, "reason": "time-budget-exhausted"})
        except (OSError, ValueError) as error:
            result.update({"ok": False, "reason": type(error).__name__})
        finally:
            result.update({"finished_at": now(), "elapsed_seconds": round(time.monotonic() - started, 3)})
            updates = {"last_run": result}
            updates["trial"] = "succeeded" if result.get("ok") and not result.get("pending") else "pending" if result.get("pending") else "failed"
            if updates["trial"] == "succeeded":
                updates["last_success"] = result["finished_at"]
            update_state(root, job, updates)
            file_lock.release(lock)
    return result


def label(root, job):
    return "org.ai-knowledge." + hashlib.sha256(str(root).encode()).hexdigest()[:16] + "." + job


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=15)


@contextmanager
def scheduler_operation(root):
    directory(root).mkdir(parents=True, exist_ok=True)
    with (directory(root) / "scheduler-operation.lock").open("a+") as lock:
        file_lock.acquire(lock, exclusive=True, blocking=False)
        try:
            yield
        finally:
            file_lock.release(lock)


def register(root, job, interval_seconds=1800):
    try:
        with scheduler_operation(root):
            return _register(root, job, interval_seconds)
    except BlockingIOError:
        return {"ok": False, "pending": True, "reason": "scheduler-operation-running"}


def _register(root, job, interval_seconds=1800):
    if platform.system() != "Darwin":
        return {"ok": False, "reason": "launchd-unavailable", "registration": "not-registered"}
    if interval_seconds < 1:
        raise ValueError("interval must be positive")
    state = read(root, job)
    identity = label(root, job)
    domain = "gui/" + str(os.getuid())
    target = domain + "/" + identity
    path = directory(root) / (job + ".plist")
    if launchctl("print", target).returncode == 0:
        if state.get("registration") != "registered" or not path.is_file():
            raise ValueError("scheduler identity already exists without owned receipt")
        installed = plistlib.loads(path.read_bytes())
        expected_args = [sys.executable, str(Path(__file__).resolve()), "--root", str(root), "run", job]
        if installed.get("ProgramArguments") != expected_args or installed.get("Label") != identity:
            return {"ok": False, "reason": "scheduler-owned-config-changed"}
        if installed.get("StartInterval") != interval_seconds or state.get("interval_seconds") != interval_seconds:
            return {"ok": False, "reason": "interval-change-requires-disable-and-register"}
        return state
    state = _plan(root, job, interval_seconds)
    if state.get("ok") is False:
        return state
    payload = {"Label": identity, "ProgramArguments": [sys.executable, str(Path(__file__).resolve()),
               "--root", str(root), "run", job], "StartInterval": interval_seconds, "RunAtLoad": True,
               "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"}}
    path.write_bytes(plistlib.dumps(payload))
    boot = launchctl("bootstrap", domain, str(path))
    verified = boot.returncode == 0 and launchctl("print", target).returncode == 0
    return update_state(root, job, {"registration": "registered" if verified else "failed", "scheduler": "launchd", "scheduler_id": target,
                  "registered_at": now() if verified else None, "registration_exit_code": boot.returncode})


def disable(root, job):
    try:
        with scheduler_operation(root):
            return _disable(root, job)
    except BlockingIOError:
        return {"ok": False, "pending": True, "reason": "scheduler-operation-running"}


def _disable(root, job):
    state = read(root, job)
    if state.get("registration") == "registered" and state.get("scheduler") != "launchd":
        return {"ok": False, "reason": "scheduler-disable-adapter-unavailable"}
    target = "gui/" + str(os.getuid()) + "/" + label(root, job) if platform.system() == "Darwin" else None
    if state.get("scheduler") == "launchd":
        if target != state.get("scheduler_id"):
            raise ValueError("scheduler ownership mismatch")
        launchctl("bootout", target)
        if launchctl("print", target).returncode == 0:
            return {"ok": False, "reason": "scheduler-still-registered"}
    state = update_state(root, job, {"registration": "disabled", "disabled_at": now()})
    (directory(root) / (job + ".plist")).unlink(missing_ok=True)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("action", choices=("plan", "run", "status", "register-launchd", "disable"))
    parser.add_argument("job", choices=JOBS)
    parser.add_argument("--interval-seconds", type=int, default=1800)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_dir() or not (root / "POLICY.md").is_file():
        parser.error("existing knowledge root with POLICY.md required")
    if args.action == "plan":
        result = plan(root, args.job, args.interval_seconds)
    elif args.action == "status":
        result = read(root, args.job)
    elif args.action == "run":
        result = run(root, args.job, args.timeout)
    elif args.action == "register-launchd":
        result = register(root, args.job, args.interval_seconds)
    else:
        result = disable(root, args.job)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
