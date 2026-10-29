#!/usr/bin/env python3
"""Safely synchronize the small canonical knowledge layer through Git.

Raw history, checkpoints, indexes and capture bundles remain outside Git.  This
tool only moves already-committed canonical knowledge.  It never resolves a
content conflict by choosing one side and it restores the pre-sync state when a
rebase cannot be completed automatically.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import file_lock
import sync_runner


TOOL_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("AI_KNOWLEDGE_ROOT", str(TOOL_ROOT))).expanduser().resolve()
LOCK = ROOT / ".git" / "ai-knowledge-sync.lock"


class SyncError(RuntimeError):
    pass


def git_environment() -> Dict[str, str]:
    # A host may launch us from a Git hook or with command-scoped configuration.
    # Those routing variables must not override the explicitly selected root.
    # Keep normal on-disk configuration (credentials, proxies, reviewed URL
    # aliases) and the small set of authentication/identity environment options.
    allowed = {
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
        "GIT_ASKPASS", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH",
        "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT",
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    }
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_") or key in allowed}
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            # Let Git resolve explicit SSH programs/commands and core.sshCommand.
            # Substituting OpenSSH here breaks identity flags and Windows Plink.
            "SSH_ASKPASS_REQUIRE": "never",
            "GIT_ALLOW_PROTOCOL": "https:ssh",
        }
    )
    return environment


def git(
    arguments: List[str], timeout: int = 180, root: Optional[Path] = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *arguments],
        cwd=str(root or ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # During foreground sync a soft network deadline must never kill a
        # local rebase/abort or leave the working tree midway through checkout.
        timeout=None if os.environ.get(sync_runner.DEADLINE_ENV) else timeout,
        check=False,
        env=git_environment(),
        # No controlling terminal for an SSH password/host-key prompt. Keep
        # configured command arguments untouched; Git owns their interpretation.
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def value(arguments: List[str], root: Optional[Path] = None) -> str:
    result = git(arguments, root=root)
    return result.stdout.strip() if result.returncode == 0 else ""


def sync_policy(root: Optional[Path] = None) -> Dict[str, Any]:
    policy_root = root or ROOT
    try:
        config = json.loads((policy_root / "config" / "kb.json").read_text(encoding="utf-8"))
        hot = config.get("hot_sync") if isinstance(config, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SyncError("git-sync-policy-invalid") from error
    if not isinstance(hot, dict):
        raise SyncError("git-sync-policy-invalid")
    repository = str(hot.get("expected_repository") or "").strip().strip("/")
    branch = str(hot.get("expected_branch") or "").strip()
    allowed = hot.get("allowed_ssh_hosts")
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
        or not isinstance(allowed, list)
    ):
        raise SyncError("git-sync-policy-invalid")
    ssh_hosts = {
        str(item).strip().lower()
        for item in allowed
        if isinstance(item, str) and str(item).strip()
    }
    if "github.com" not in ssh_hosts:
        raise SyncError("git-sync-policy-invalid")
    return {
        "expected_repository": repository,
        "expected_branch": branch,
        "allowed_ssh_hosts": sorted(ssh_hosts),
        "bot_user_name": str(hot.get("bot_user_name") or "AI Knowledge Bot"),
        "bot_user_email": str(hot.get("bot_user_email") or "ai-knowledge-bot@localhost"),
    }


def remote_matches_policy(remote_url: str, policy: Mapping[str, Any]) -> bool:
    """Recognize the one configured GitHub repository without echoing its URL."""
    raw = str(remote_url or "").strip()
    expected = str(policy.get("expected_repository") or "").strip().strip("/").casefold()
    allowed_ssh = {
        str(item).strip().lower() for item in policy.get("allowed_ssh_hosts", [])
    }
    if not raw or not expected:
        return False

    host = ""
    repository = ""
    if "://" in raw:
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return False
        if parsed.query or parsed.fragment:
            return False
        host = str(parsed.hostname or "").lower()
        if parsed.scheme.lower() == "https":
            if host != "github.com":
                return False
        elif parsed.scheme.lower() == "ssh":
            if host not in allowed_ssh or (parsed.username and parsed.username != "git"):
                return False
        else:
            return False
        repository = parsed.path.lstrip("/")
    else:
        match = re.fullmatch(r"git@([^:/]+):(.+)", raw)
        if not match:
            return False
        host = match.group(1).lower()
        if host not in allowed_ssh:
            return False
        repository = match.group(2)
    repository = repository.rstrip("/")
    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    return repository.strip("/").casefold() == expected


def configured_origin_status(
    remote: str = "origin", root: Optional[Path] = None
) -> Dict[str, Any]:
    policy = sync_policy(root)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
        raise SyncError("invalid-remote-name")
    result = git(["remote", "get-url", "--all", remote], root=root)
    if result.returncode != 0:
        return {
            "ok": False,
            "configured": False,
            "identity_valid": False,
            "status": "origin-missing",
            "expected_repository": policy["expected_repository"],
        }
    urls = result.stdout.strip().splitlines()
    valid = bool(urls) and all(remote_matches_policy(url, policy) for url in urls)
    pushed = git(["remote", "get-url", "--push", "--all", remote], root=root)
    push_urls = pushed.stdout.strip().splitlines()
    valid = valid and pushed.returncode == 0 and bool(push_urls) and all(
        remote_matches_policy(url, policy) for url in push_urls)
    return {
        "ok": valid,
        "configured": True,
        "identity_valid": valid,
        "status": "ready" if valid else "origin-identity-mismatch",
        "expected_repository": policy["expected_repository"],
    }


def network_git(arguments: List[str], remote: str, root: Path) -> subprocess.CompletedProcess:
    """Recheck both effective routes immediately before each network operation."""
    status = configured_origin_status(remote, root)
    if not status["ok"]:
        raise SyncError(str(status["status"]))
    if os.environ.get(sync_runner.DEADLINE_ENV):
        return sync_runner.run_network(["git", *arguments], timeout=180,
                                       cwd=str(root), env=git_environment())
    return git(arguments, root=root)


def literal_fetch_status(url: str, policy: Mapping[str, Any], root: Path) -> bool:
    """Resolve a literal fetch URL without creating a remote or contacting it.

    A new connection checks the effective push URLs after adding its origin,
    before saving the connection. Existing sync checks both before any network.
    """
    result = git(["ls-remote", "--get-url", url], root=root)
    return result.returncode == 0 and remote_matches_policy(result.stdout.strip(), policy)


def fetch_local_history(root: Path, source: Path, revision: str) -> subprocess.CompletedProcess:
    """The only file transport: import a known local commit into a temp probe."""
    source = source.resolve()
    result = git(["ls-remote", "--get-url", str(source)], root=root)
    if result.returncode or result.stdout.strip() != str(source):
        raise SyncError("local-history-url-rewritten")
    environment = git_environment()
    environment["GIT_ALLOW_PROTOCOL"] = "file"
    return subprocess.run(
        ["git", "fetch", "--quiet", "--no-tags", str(source), revision + ":refs/heads/local"],
        cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=45, check=False, env=environment,
    )


def anonymous_repository_probe_arguments(policy: Mapping[str, Any]) -> List[str]:
    repository = str(policy.get("expected_repository") or "").strip().strip("/")
    branch = str(policy.get("expected_branch") or "").strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
    ):
        raise SyncError("git-sync-policy-invalid")
    canonical_api = "https://api.github.com/repos/%s" % repository
    curl = shutil.which("curl")
    if not curl and Path("/usr/bin/curl").is_file():
        curl = "/usr/bin/curl"
    # Let subprocess produce a normal FileNotFoundError when curl is genuinely
    # unavailable; repository_privacy_status already maps that to unknown.
    curl = curl or "curl"
    return [
        curl,
        # --disable must be first so ~/.curlrc cannot inject credentials,
        # headers, alternate URLs, or an output destination.
        "--disable",
        "--silent",
        "--show-error",
        "--output",
        os.devnull,
        "--write-out",
        "%{http_code}",
        "--connect-timeout",
        # Local HTTPS proxies may need several seconds to establish and
        # renegotiate the upstream TLS tunnel before GitHub responds.
        "12",
        "--max-time",
        "30",
        "--proto",
        "=https",
        "--user-agent",
        "AI-Knowledge-Privacy-Probe",
        canonical_api,
    ]


def classify_anonymous_probe(result: subprocess.CompletedProcess) -> Dict[str, Any]:
    status_code = str(result.stdout or "").strip()
    if result.returncode == 0 and status_code == "200":
        return {
            "ok": False,
            "private": False,
            "status": "repository-public",
            "retryable": False,
        }
    if result.returncode == 0 and status_code == "404":
        return {
            "ok": True,
            "private": True,
            "status": "repository-private",
            "retryable": False,
        }
    return {
        "ok": False,
        "private": None,
        "status": "repository-privacy-unknown",
        "retryable": True,
    }


def anonymous_git_probe_arguments(policy: Mapping[str, Any]) -> List[str]:
    """Build a credential-free smart-HTTP probe for the expected repository."""
    repository = str(policy.get("expected_repository") or "").strip().strip("/")
    branch = str(policy.get("expected_branch") or "").strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
    ):
        raise SyncError("git-sync-policy-invalid")
    git_executable = shutil.which("git") or "git"
    return [
        git_executable,
        "-c",
        "credential.helper=",
        "-c",
        "credential.interactive=never",
        "-c",
        "core.askPass=",
        "ls-remote",
        "https://github.com/%s.git" % repository,
        "refs/heads/%s" % branch,
    ]


def anonymous_git_probe_environment() -> Dict[str, str]:
    """Prevent the fallback probe from loading credentials or trace settings."""
    environment = git_environment()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_ALLOW_PROTOCOL": "https",
        }
    )
    for key in (
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_TRACE",
        "GIT_TRACE_CURL",
        "GIT_CURL_VERBOSE",
    ):
        environment.pop(key, None)
    return environment


def classify_anonymous_git_probe(
    result: subprocess.CompletedProcess,
) -> Dict[str, Any]:
    if result.returncode == 0:
        return {
            "ok": False,
            "private": False,
            "status": "repository-public",
            "retryable": False,
        }
    error = str(result.stderr or "").casefold()
    authentication_markers = (
        "authentication failed",
        "could not read username",
        "terminal prompts disabled",
        "unable to get password from user",
    )
    if any(marker in error for marker in authentication_markers):
        return {
            "ok": True,
            "private": True,
            "status": "repository-private",
            "retryable": False,
        }
    return {
        "ok": False,
        "private": None,
        "status": "repository-privacy-unknown",
        "retryable": True,
    }


def repository_privacy_status(
    root: Optional[Path] = None, policy: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    effective_policy = dict(policy or sync_policy(root))
    arguments = anonymous_repository_probe_arguments(effective_policy)
    def probe(command, timeout, environment):
        if os.environ.get(sync_runner.DEADLINE_ENV):
            return sync_runner.run_network(command, timeout,
                cwd=os.path.abspath(os.sep), env=environment)
        return subprocess.run(command, cwd=os.path.abspath(os.sep),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=timeout, check=False, env=environment)
    try:
        # curl --disable ignores user config, and no response body is read.
        result = probe(arguments, 35, os.environ.copy())
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        privacy = {
            "ok": False,
            "private": None,
            "status": "repository-privacy-unknown",
            "retryable": True,
        }
    else:
        privacy = classify_anonymous_probe(result)
    if privacy.get("status") != "repository-privacy-unknown":
        return privacy
    for _attempt in range(3):
        try:
            fallback = probe(anonymous_git_probe_arguments(effective_policy), 45,
                             anonymous_git_probe_environment())
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        fallback_privacy = classify_anonymous_git_probe(fallback)
        if fallback_privacy.get("status") != "repository-privacy-unknown":
            return fallback_privacy
    return privacy


def ensure_local_bot_identity(root: Optional[Path] = None, identity_policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    policy = sync_policy(root) if identity_policy is None else identity_policy
    configured = []
    for key, policy_key in (
        ("user.name", "bot_user_name"),
        ("user.email", "bot_user_email"),
    ):
        if not value(["config", "--local", "--get", key], root=root):
            result = git(
                ["config", "--local", key, str(policy[policy_key])], root=root
            )
            if result.returncode != 0:
                raise SyncError("git-local-identity-configuration-failed")
            configured.append(key)
    # Background knowledge commits must not block on an unrelated global
    # signing policy or a missing hardware signing token.
    signing = git(
        ["config", "--local", "commit.gpgsign", "false"], root=root
    )
    if signing.returncode != 0:
        raise SyncError("git-local-identity-configuration-failed")
    return {"ok": True, "configured": configured, "signing_disabled_locally": True}


def clean_worktree(root: Optional[Path] = None) -> bool:
    result = git(
        ["status", "--porcelain", "--untracked-files=normal"], root=root
    )
    return result.returncode == 0 and not result.stdout.strip()


def rebase_state_present(root: Optional[Path] = None) -> bool:
    work_root = root or ROOT
    git_dir_raw = value(["rev-parse", "--git-dir"], root=work_root)
    if not git_dir_raw:
        return True
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = work_root / git_dir
    return any(
        (git_dir / name).exists()
        for name in ("rebase-merge", "rebase-apply")
    )


def remote_branch_exists(
    remote: str, branch: str, root: Optional[Path] = None
) -> bool:
    return git(
        ["show-ref", "--verify", "--quiet", "refs/remotes/%s/%s" % (remote, branch)],
        root=root,
    ).returncode == 0


def rebase_onto(
    remote: str, branch: str, root: Optional[Path] = None
) -> Dict[str, Any]:
    work_root = root or ROOT
    original_head = value(["rev-parse", "HEAD"], root=work_root)
    result = git(["rebase", "%s/%s" % (remote, branch)], root=work_root)
    if result.returncode == 0:
        return {"ok": True, "conflicts": [], "cleanup_verified": True}
    conflicts: Dict[str, Dict[str, str]] = {}
    unmerged = git(["ls-files", "-u", "-z"], root=work_root)
    if unmerged.returncode == 0:
        for record in unmerged.stdout.split("\0"):
            if not record or "\t" not in record:
                continue
            metadata, path = record.split("\t", 1)
            parts = metadata.split()
            if len(parts) != 3:
                continue
            _mode, blob, stage = parts
            conflicts.setdefault(path, {})[stage] = blob
    had_conflicts = bool(conflicts)
    # A failed unattended sync must never leave the repository mid-rebase.
    abort_required = rebase_state_present(work_root)
    abort = git(["rebase", "--abort"], root=work_root)
    current_head = value(["rev-parse", "HEAD"], root=work_root)
    remaining_unmerged = git(["ls-files", "-u", "-z"], root=work_root)
    cached_clean = git(["diff", "--cached", "--quiet"], root=work_root)
    worktree_clean = clean_worktree(work_root)
    state_present = rebase_state_present(work_root)
    cleanup_verified = bool(
        (abort.returncode == 0 or not abort_required)
        and original_head
        and current_head == original_head
        and remaining_unmerged.returncode == 0
        and not remaining_unmerged.stdout
        and cached_clean.returncode == 0
        and worktree_clean
        and not state_present
    )
    return {
        "ok": False,
        "failure_kind": "conflict" if had_conflicts else "rebase-failed",
        "conflicts": [
            {"path": path, "stages": stages}
            for path, stages in sorted(conflicts.items())
        ],
        "cleanup_verified": cleanup_verified,
        "cleanup": {
            "abort_required": abort_required,
            "abort_succeeded": abort.returncode == 0,
            "head_restored": bool(original_head and current_head == original_head),
            "index_clean": bool(
                remaining_unmerged.returncode == 0
                and not remaining_unmerged.stdout
                and cached_clean.returncode == 0
            ),
            "worktree_clean": worktree_clean,
            "rebase_state_absent": not state_present,
        },
    }


def conflict_incident_key(
    error: str,
    branch: str,
    local_head: str,
    remote_head: str,
) -> str:
    # The same pair of heads is one unresolved merge fact whether discovered
    # during pull or after a push race.  Phase-specific labels must not bypass
    # unchanged-head suppression.
    del error
    payload = "\0".join((branch, local_head, remote_head))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def conflict_incident_path(
    error: str,
    branch: str,
    local_head: str,
    remote_head: str,
    root: Optional[Path] = None,
) -> Path:
    work_root = root or ROOT
    key = conflict_incident_key(error, branch, local_head, remote_head)
    return work_root / ".git" / "ai-knowledge-sync-incidents" / (key + ".json")


def existing_conflict_incident(
    error: str,
    branch: str,
    local_head: str,
    remote_head: str,
    root: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    path = conflict_incident_path(
        error, branch, local_head, remote_head, root
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def record_conflict_incident(
    *,
    error: str,
    branch: str,
    local_head: str,
    remote_head: str,
    merge_base: str,
    conflicts: List[Dict[str, Any]],
    cleanup: Optional[Mapping[str, Any]] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist a body-free conflict fingerprint without choosing either side."""

    path = conflict_incident_path(
        error, branch, local_head, remote_head, root
    )
    previous = existing_conflict_incident(
        error, branch, local_head, remote_head, root
    ) or {}
    now = dt.datetime.now(dt.timezone.utc).astimezone().isoformat()
    value: Dict[str, Any] = {
        "format": "ai-knowledge-sync-conflict-v1",
        "error": error,
        "branch": branch,
        "local_head": local_head,
        "remote_head": remote_head,
        "merge_base": merge_base,
        "conflicts": conflicts,
        "conflict_files": len(conflicts),
        "attempts": max(0, int(previous.get("attempts") or 0)) + 1,
        "first_seen_at": previous.get("first_seen_at") or now,
        "last_seen_at": now,
        "resolution": "unresolved",
        "cleanup": dict(cleanup or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.tmp-%s" % (path.name, os.getpid()))
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {
        "conflict_fingerprint": path.stem,
        "conflict_files": len(conflicts),
        "incident": path.relative_to(root or ROOT).as_posix(),
    }


def failure(
    error: str,
    branch: Optional[str] = None,
    retryable: bool = True,
    **details: object,
) -> Dict[str, object]:
    result: Dict[str, object] = {
        "ok": False,
        "action": "git-sync",
        "status": "pending" if retryable else "degraded",
        "error": error,
        "retryable": retryable,
    }
    if branch:
        result["branch"] = branch
    result.update(details)
    return result


def committed_knowledge_manifest(root: Path, revision: str, side: str) -> Dict[str, Any]:
    """Inspect a fetched commit before it can replace the working knowledge.

    Read a bounded regular Git blob; do not follow a symlink or import code from
    the peer. Tool releases may differ while sharing the same data protocol.
    """
    entry = value(["ls-tree", revision, "--", "MANIFEST.json"], root=root)
    if not re.fullmatch(r"100(?:644|755) blob [0-9a-f]{40,64}\tMANIFEST\.json", entry):
        raise SyncError(side + "-knowledge-manifest-missing-or-unsafe")
    object_name = revision + ":MANIFEST.json"
    size = value(["cat-file", "-s", object_name], root=root)
    if not size.isdigit() or not 0 < int(size) <= 65536:
        raise SyncError(side + "-knowledge-manifest-invalid")
    try:
        result = git(["show", object_name], root=root)
        manifest = json.loads(result.stdout) if result.returncode == 0 else None
    except (ValueError, UnicodeError):
        manifest = None
    if not isinstance(manifest, dict):
        raise SyncError(side + "-knowledge-manifest-invalid")
    if manifest.get("schema_version") != "1":
        raise SyncError(side + "-knowledge-schema-unsupported")
    identity = manifest.get("knowledge_base_id")
    if not isinstance(identity, str) or not identity.strip():
        raise SyncError(side + "-knowledge-identity-missing")
    return manifest


def knowledge_compatibility(root: Path, local_head: str, remote_head: Optional[str] = None) -> Dict[str, Any]:
    try:
        local = committed_knowledge_manifest(root, local_head, "local")
        if remote_head:
            peer = committed_knowledge_manifest(root, remote_head, "remote")
            if local["knowledge_base_id"] != peer["knowledge_base_id"]:
                raise SyncError("knowledge-base-mismatch")
    except SyncError as error:
        return {"ok": False, "error": str(error),
                "recovery": "Keep both copies. Check the selected knowledge repository and install a compatible tool version before retrying; do not rewrite its schema or identity."}
    return {"ok": True, "schema_version": "1", "knowledge_base_id": local["knowledge_base_id"]}


def sync(
    remote: str, pull: bool, push: bool, root: Optional[Path] = None
) -> Dict[str, object]:
    work_root = root or ROOT
    if git(["rev-parse", "--is-inside-work-tree"], root=work_root).returncode != 0:
        raise SyncError("not-a-git-repository")
    policy = sync_policy(work_root)
    branch = value(["branch", "--show-current"], root=work_root)
    if not branch:
        raise SyncError("detached-head")
    if branch != policy["expected_branch"]:
        return failure("unexpected-branch", branch, retryable=False)
    origin = configured_origin_status(remote, work_root)
    if not origin.get("ok"):
        return failure(str(origin.get("status") or "origin-invalid"), branch, retryable=False)
    if rebase_state_present(work_root):
        return failure("rebase-state-present", branch, retryable=False)
    if not clean_worktree(work_root):
        return failure("dirty-worktree", branch, retryable=True)
    privacy = repository_privacy_status(work_root, policy)
    if not privacy.get("ok"):
        return failure(
            str(privacy.get("status") or "repository-privacy-unknown"),
            branch,
            retryable=bool(privacy.get("retryable", True)),
        )

    fetched = network_git(["fetch", "--prune", remote], remote, work_root)
    if fetched.returncode != 0:
        return failure("fetch-failed", branch, retryable=True)

    remote_has_branch = remote_branch_exists(remote, branch, work_root)
    if not remote_has_branch:
        if not push:
            return failure("remote-branch-missing", branch, retryable=False)
        # A missing configured branch is not evidence of a new repository.
        # Inspect every advertised ref, including other branches and tags,
        # before allowing the narrowly scoped first push.
        refs = network_git(["ls-remote", "--refs", remote], remote, work_root)
        if refs.returncode != 0:
            return failure("remote-refs-unavailable", branch, retryable=True)
        if refs.stdout.strip():
            return failure("remote-not-empty", branch, retryable=False)
    local_head = value(["rev-parse", "HEAD"], root=work_root)
    remote_head = value(["rev-parse", "%s/%s" % (remote, branch)], root=work_root) if remote_has_branch else None
    if remote_head and not value(["merge-base", local_head, remote_head], root=work_root):
        return failure("unrelated-histories", branch, retryable=False)
    compatibility = knowledge_compatibility(work_root, local_head, remote_head)
    if not compatibility["ok"]:
        return failure(compatibility["error"], branch, retryable=False,
                       phase="compatibility", recovery=compatibility["recovery"])
    pulled = False
    pushed = False
    remote_branch_created = False
    if pull and remote_has_branch:
        local_head = value(["rev-parse", "HEAD"], root=work_root)
        remote_head = value(
            ["rev-parse", "%s/%s" % (remote, branch)], root=work_root
        )
        if local_head != remote_head:
            contains_remote = git(
                ["merge-base", "--is-ancestor", remote_head, local_head],
                root=work_root,
            )
            if contains_remote.returncode != 0:
                prior = existing_conflict_incident(
                    "rebase-conflict",
                    branch,
                    local_head,
                    remote_head,
                    work_root,
                )
                if prior is not None:
                    incident_path = conflict_incident_path(
                        "rebase-conflict",
                        branch,
                        local_head,
                        remote_head,
                        work_root,
                    )
                    return failure(
                        "rebase-conflict-unchanged",
                        branch,
                        retryable=False,
                        conflict_fingerprint=incident_path.stem,
                        conflict_files=int(prior.get("conflict_files") or 0),
                        incident=incident_path.relative_to(work_root).as_posix(),
                        repeated_without_head_change=True,
                    )
                merge_base = value(
                    ["merge-base", local_head, remote_head], root=work_root
                )
                if not merge_base:
                    return failure("unrelated-histories", branch, retryable=False)
                rebased = rebase_onto(remote, branch, work_root)
                if not rebased.get("ok"):
                    cleanup = dict(rebased.get("cleanup") or {})
                    if not rebased.get("cleanup_verified"):
                        return failure(
                            "rebase-cleanup-failed",
                            branch,
                            retryable=False,
                            cleanup=cleanup,
                        )
                    if rebased.get("failure_kind") != "conflict":
                        return failure(
                            "rebase-failed",
                            branch,
                            retryable=True,
                            cleanup=cleanup,
                        )
                    incident = record_conflict_incident(
                        error="rebase-conflict",
                        branch=branch,
                        local_head=local_head,
                        remote_head=remote_head,
                        merge_base=merge_base,
                        conflicts=list(rebased.get("conflicts") or []),
                        cleanup=cleanup,
                        root=work_root,
                    )
                    return failure(
                        "rebase-conflict",
                        branch,
                        retryable=False,
                        **incident,
                    )
                pulled = True

    if push:
        # Porcelain distinguishes an actual new branch from a concurrent
        # writer reaching the same commit before this ordinary, non-force push.
        push_arguments = ["push"] + (["--porcelain"] if not remote_has_branch else [])
        result = network_git(
            push_arguments + [remote, "HEAD:refs/heads/%s" % branch], remote, work_root
        )
        if result.returncode != 0 and remote_has_branch:
            # Another device may have pushed after our fetch. Retry exactly once
            # through a normal rebase; never force-push shared knowledge.
            fetched = network_git(["fetch", "--prune", remote], remote, work_root)
            if fetched.returncode != 0:
                return failure("push-race-fetch-failed", branch, retryable=True)
            if not remote_branch_exists(remote, branch, work_root):
                return failure("push-race-remote-branch-missing", branch, retryable=False)
            local_head = value(["rev-parse", "HEAD"], root=work_root)
            remote_head = value(
                ["rev-parse", "%s/%s" % (remote, branch)], root=work_root
            )
            prior = existing_conflict_incident(
                "push-race-conflict",
                branch,
                local_head,
                remote_head,
                work_root,
            )
            if prior is not None:
                incident_path = conflict_incident_path(
                    "push-race-conflict",
                    branch,
                    local_head,
                    remote_head,
                    work_root,
                )
                return failure(
                    "push-race-conflict-unchanged",
                    branch,
                    retryable=False,
                    conflict_fingerprint=incident_path.stem,
                    conflict_files=int(prior.get("conflict_files") or 0),
                    incident=incident_path.relative_to(work_root).as_posix(),
                    repeated_without_head_change=True,
                )
            merge_base = value(
                ["merge-base", local_head, remote_head], root=work_root
            )
            if not merge_base:
                return failure("unrelated-histories", branch, retryable=False,
                               phase="push-race")
            compatibility = knowledge_compatibility(work_root, local_head, remote_head)
            if not compatibility["ok"]:
                return failure(compatibility["error"], branch, retryable=False,
                               phase="push-race-compatibility", recovery=compatibility["recovery"])
            rebased = rebase_onto(remote, branch, work_root)
            if not rebased.get("ok"):
                cleanup = dict(rebased.get("cleanup") or {})
                if not rebased.get("cleanup_verified"):
                    return failure(
                        "push-race-rebase-cleanup-failed",
                        branch,
                        retryable=False,
                        cleanup=cleanup,
                    )
                if rebased.get("failure_kind") != "conflict":
                    return failure(
                        "push-race-rebase-failed",
                        branch,
                        retryable=True,
                        cleanup=cleanup,
                    )
                incident = record_conflict_incident(
                    error="push-race-conflict",
                    branch=branch,
                    local_head=local_head,
                    remote_head=remote_head,
                    merge_base=merge_base,
                    conflicts=list(rebased.get("conflicts") or []),
                    cleanup=cleanup,
                    root=work_root,
                )
                return failure(
                    "push-race-conflict",
                    branch,
                    retryable=False,
                    **incident,
                )
            result = network_git(
                ["push", remote, "HEAD:refs/heads/%s" % branch], remote, work_root
            )
        if result.returncode != 0:
            return failure("push-failed" if remote_has_branch else "first-push-failed",
                           branch, retryable=True)
        pushed = True
        if not remote_has_branch:
            remote_branch_created = any(
                line.startswith("*\t")
                and line.split("\t")[1].endswith(":refs/heads/%s" % branch)
                for line in result.stdout.splitlines()
            )

    return {
        "ok": True,
        "action": "git-sync",
        "status": "ready",
        "repository_private": True,
        "branch": branch,
        "pulled": pulled,
        "pushed": pushed,
        "remote_branch_created": remote_branch_created,
        "compatibility": compatibility,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pull-only", action="store_true")
    mode.add_argument("--push-only", action="store_true")
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args()

    if not LOCK.parent.is_dir():
        raise SyncError("missing-git-directory")
    with LOCK.open("a+") as lock:
        try:
            file_lock.acquire(lock, exclusive=True, blocking=False)
        except BlockingIOError:
            print(json.dumps({"ok": True, "action": "git-sync", "skipped": "sync-already-running"}))
            return 0
        try:
            result = sync(args.remote, pull=not args.push_only, push=not args.pull_only)
        finally:
            file_lock.release(lock)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    if not os.environ.get("AI_KNOWLEDGE_ROOT") and not any(x in sys.argv for x in ("--help", "-h")):
        print(json.dumps({"ok": False, "error": "explicit-AI_KNOWLEDGE_ROOT-required"}))
        raise SystemExit(2)
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired:
        print(json.dumps({"ok": False, "error": "GitTimeout"}), file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)
