#!/usr/bin/env python3
"""Commit canonical knowledge changes and synchronize them with the hot Git remote."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import device_identity
import file_lock
import git_sync
import storage_lock
import workspace_lock


TOOL_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("AI_KNOWLEDGE_ROOT", str(TOOL_ROOT))).expanduser().resolve()
KNOWLEDGE_PATHS = (
    "profile",
    "projects",
    "resources",
    "playbooks",
    "inbox",
    "ledger",
    "config/devices.json",
    "config/devices",
)


def run(arguments: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        arguments,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env=git_sync.git_environment(),
    )


def device_id() -> str:
    return device_identity.resolve_device_id(ROOT)


def commit_canonical() -> Dict[str, object]:
    # New empty templates may have no device registration files yet. Include
    # existing paths and tracked deletions, never fail on an absent optional zone.
    paths = [p for p in KNOWLEDGE_PATHS if (ROOT / p).exists()
             or run(["git", "ls-files", "--", p]).stdout.strip()]
    if not paths:
        return {"committed": False, "commit": None}
    added = run(["git", "add", "--", *paths])
    if added.returncode != 0:
        raise RuntimeError("canonical-stage-failed")
    staged = run(
        ["git", "diff", "--cached", "--quiet", "--", *paths]
    )
    if staged.returncode == 0:
        return {"committed": False, "commit": None}
    if staged.returncode != 1:
        raise RuntimeError("cannot-inspect-staged-changes")
    paths = [p for p in paths if run(["git", "diff", "--cached", "--quiet", "--", p]).returncode == 1]
    message = "knowledge: sync %s %s" % (device_id(), dt.datetime.now().strftime("%Y-%m-%dT%H:%M"))
    # --only makes the automated commit path-scoped. Any unrelated entries
    # already staged by the user remain staged and are never swept into this
    # commit or reset as a side effect.
    commit_command = ["git", "-c", "commit.gpgsign=false"]
    fallback_fields = []
    for field, fallback in (("user.name", "AI Knowledge"), ("user.email", "knowledge@localhost")):
        identity = run(["git", "config", "--get", field])
        if identity.returncode not in (0, 1):
            raise RuntimeError("canonical-identity-unavailable")
        if identity.returncode == 1 or not identity.stdout.strip():
            commit_command.extend(["-c", "%s=%s" % (field, fallback)])
            fallback_fields.append(field)
    # Effective local/global identity takes precedence. Missing values and
    # unattended signing are configured for this invocation only, never saved.
    committed = run(
        [*commit_command, "commit", "--only", "-m", message, "--", *paths]
    )
    if committed.returncode != 0:
        raise RuntimeError("canonical-commit-failed")
    return {"committed": True, "commit": run(["git", "rev-parse", "HEAD"]).stdout.strip(),
            "identity": {"scope": "this-commit", "fallback_fields": fallback_fields,
                         "configuration_changed": False}}


def refresh_index_after_sync(sync_result: Dict[str, object]) -> Dict[str, object]:
    """Idempotently reconcile the cold index after every successful sync.

    A pull may happen while the external cold layer is unavailable. Running
    the incremental updater whenever that layer is online closes the gap on
    the first later sync, even when Git correctly reports ``pulled=false``.
    """

    data_root = ROOT / ".kb-data"
    if not data_root.is_dir():
        if not sync_result.get("pulled"):
            return {
                "ok": True,
                "rebuilt": False,
                "status": "not-needed",
            }
        return {
            "ok": True,
            "rebuilt": False,
            "status": "deferred-cold-data-unavailable",
        }
    updated = run(
        [
            sys.executable,
            str(TOOL_ROOT / "scripts" / "kb.py"),
            "--root",
            str(ROOT),
            "update-index",
        ]
    )
    if updated.returncode != 0:
        return {
            "ok": False,
            "updated": False,
            "rebuilt": False,
            "status": "index-update-failed",
        }
    try:
        payload = json.loads(updated.stdout)
    except (TypeError, json.JSONDecodeError):
        return {
            "ok": False,
            "updated": False,
            "rebuilt": False,
            "status": "index-update-invalid",
        }
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return {
            "ok": False,
            "updated": False,
            "rebuilt": False,
            "status": "index-update-invalid",
        }
    mode = str(payload.get("mode") or "unknown")
    return {
        "ok": True,
        "updated": True,
        "rebuilt": mode == "full-rebuild",
        "mode": mode,
        "status": "index-updated-after-sync",
    }


def refresh_dashboard(root: Optional[Path] = None) -> Dict[str, object]:
    """Refresh the static view only when a caller explicitly opts in.

    The dashboard is a manual local snapshot, not part of Git durability.  A
    full build can be I/O-heavy enough to outlive a subprocess timeout on a
    mounted cold layer, so routine sync must never wait for it.
    """

    if os.name == "nt":
        # The static dashboard is the main Mac's readiness snapshot. Windows
        # reports installation and automation health from its local status
        # files and Task Scheduler, as documented in README.md.
        return {"ok": True, "status": "dashboard-main-mac-only"}
    if os.environ.get("AI_KNOWLEDGE_REFRESH_DASHBOARD") != "1":
        return {"ok": True, "status": "dashboard-refresh-manual"}
    work_root = root or ROOT
    script = work_root / "scripts" / "build_dashboard.py"
    if not script.is_file():
        return {"ok": False, "status": "dashboard-builder-missing"}
    try:
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(work_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
            env=git_sync.git_environment(),
        )
    except Exception:
        return {"ok": False, "status": "dashboard-refresh-failed"}
    if completed.returncode != 0:
        return {"ok": False, "status": "dashboard-refresh-failed"}
    return {"ok": True, "status": "dashboard-refreshed"}


def _synchronize(no_push: bool) -> Dict[str, object]:
    """Perform one sync while the caller owns the workspace mutation lock."""

    if not git_sync.LOCK.parent.is_dir():
        raise RuntimeError("missing-git-directory")
    with git_sync.LOCK.open("a+") as lock:
        try:
            file_lock.acquire(lock, exclusive=True, blocking=False)
        except BlockingIOError:
            return {
                "ok": True,
                "action": "hot-sync",
                "status": "pending",
                "pending": True,
                "retryable": True,
                "skipped": "sync-already-running",
            }
        try:
            config_path = ROOT / "config/kb.json"
            config = json.loads(config_path.read_text()) if config_path.is_file() else {}
            if config.get("hot_sync", {}).get("enabled") is False:
                commit = commit_canonical()
                return {"ok": True, "action": "hot-sync", "status": "local-only",
                        "remote_verified": False, "sync": {"skipped": "disabled-by-user", "pushed": False},
                        **commit}
            origin = git_sync.configured_origin_status("origin", ROOT)
            if not origin.get("ok"):
                return {
                    "ok": False,
                    "action": "hot-sync",
                    "status": "degraded",
                    "error": str(origin.get("status") or "origin-invalid"),
                    "origin_identity_valid": False,
                }
            commit = commit_canonical()
            sync_result = git_sync.sync(
                "origin", pull=True, push=not no_push, root=ROOT
            )
            index = (
                refresh_index_after_sync(sync_result)
                if sync_result.get("ok")
                else {"ok": False, "rebuilt": False, "status": "sync-incomplete"}
            )
        finally:
            file_lock.release(lock)
    return {
        "ok": bool(sync_result.get("ok") and index.get("ok")),
        "action": "hot-sync",
        "origin_identity_valid": True,
        **commit,
        "sync": sync_result,
        "index": index,
    }


def perform(
    no_push: bool = False, acquire_workspace_lock: bool = True
) -> Dict[str, object]:
    """Run a hot sync, optionally under the shared mutation lock.

    Semantic maintenance already owns this lock for its full Codex run and
    calls with ``acquire_workspace_lock=False``. The public CLI has no bypass.
    """

    if not acquire_workspace_lock:
        return _synchronize(no_push)
    try:
        with workspace_lock.acquire(ROOT, blocking=False):
            return _synchronize(no_push)
    except workspace_lock.WorkspaceBusy:
        return {
            "ok": True,
            "action": "hot-sync",
            "status": "pending",
            "pending": True,
            "retryable": True,
            "skipped": "workspace-mutation-running",
        }


def execute(
    no_push: bool = False, acquire_workspace_lock: bool = True
) -> Dict[str, object]:
    """Attempt sync; dashboard generation remains an explicit manual action."""

    try:
        with storage_lock.acquire(ROOT, exclusive=False):
            result = perform(
                no_push=no_push,
                acquire_workspace_lock=acquire_workspace_lock,
            )
    except storage_lock.StorageBusy:
        result = {
            "ok": True,
            "action": "hot-sync",
            "status": "pending",
            "pending": True,
            "retryable": True,
            "skipped": "storage-operation-running",
        }
    except subprocess.TimeoutExpired:
        # Network commands are separately contained; local commit/rebase/index
        # phases are allowed to finish. Do not echo command arguments or output.
        result = {
            "ok": False, "action": "hot-sync", "status": "pending",
            "error": "network-deadline", "pending": True,
        }
    except Exception as error:
        result = {
            "ok": False,
            "action": "hot-sync",
            "status": "degraded",
            "error": str(error),
        }
    # Dashboard generation is best-effort and cannot turn a successful sync
    # into a failure or conceal a sync failure.
    result["dashboard"] = refresh_dashboard(ROOT)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args(argv)
    result = execute(no_push=args.no_push, acquire_workspace_lock=True)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    if not os.environ.get("AI_KNOWLEDGE_ROOT") and not any(x in sys.argv for x in ("--help", "-h")):
        print(json.dumps({"ok": False, "error": "explicit-AI_KNOWLEDGE_ROOT-required"}))
        raise SystemExit(2)
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)
