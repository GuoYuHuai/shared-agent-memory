#!/usr/bin/env python3
"""Run bounded Codex semantic curation or a no-memory neutral audit."""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import automation_status
import device_identity
import file_lock
import git_sync
import hot_sync
import kb as knowledge_cli
import semantic_batches
import storage_lock
import workspace_lock


TOOL_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("AI_KNOWLEDGE_ROOT", str(TOOL_ROOT))).expanduser().resolve()
DATA = ROOT / ".kb-data"
REPORTS = DATA / "reports"
LOCK = DATA / "semantic-maintenance.lock"
CODEX = Path(os.environ.get("AI_KNOWLEDGE_CODEX", shutil.which("codex") or "codex"))
AUTOMATION_STATUS_HOME: Optional[Path] = None
TRANSACTION_FILE = "transaction.json"
TRANSACTION_FORMAT = "ai-knowledge-semantic-transaction-v1"
ALLOWED_TRANSACTION_PREFIXES = (
    "ledger/",
    "inbox/candidates/",
    "resources/catalog/",
    "resources/credentials/",
)
# Keep generated Git command lines comfortably below the Windows
# CreateProcess limit.  A semantic batch can legitimately create hundreds of
# managed paths, so passing every path to one ``git add`` is not portable.
GIT_ADD_COMMAND_LINE_LIMIT = 8000
MAX_EMPTY_SELECTION_WINDOWS = 64
CAPTURE_STATUS_MUTABLE_FIELDS = frozenset(
    {
        "history_events",
        "last_capture_completed_at",
        "source_files",
    }
)
LAST_CODEX_FAILURE_CLASS: Optional[str] = None


def load_semantic_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve maintenance settings from config/kb.json.

    config/kb.json is the single source of truth for the model and run
    budgets; the historical constants only remain as fallbacks for configs
    written before these keys existed.
    """
    section = config.get("semantic_maintenance") or {}
    return {
        "model": str(section.get("model", "")),
        "reasoning_effort": str(section.get("reasoning_effort", "")),
        "maximum_batches_per_run": int(section.get("maximum_batches_per_run", 8)),
        "maximum_run_seconds": float(section.get("maximum_run_seconds", 10800)),
    }


_config_file = ROOT / "config" / "kb.json"
CONFIG = json.loads(_config_file.read_text(encoding="utf-8")) if _config_file.is_file() else {}
DATA = ROOT / CONFIG.get("data_root", ".kb-data")
REPORTS = DATA / "reports"
LOCK = DATA / "semantic-maintenance.lock"
SEMANTIC_SETTINGS = load_semantic_settings(CONFIG)
CURATION_MODEL = SEMANTIC_SETTINGS["model"]
CURATION_REASONING = SEMANTIC_SETTINGS["reasoning_effort"]
AUDIT_MODEL = SEMANTIC_SETTINGS["model"]
AUDIT_REASONING = SEMANTIC_SETTINGS["reasoning_effort"]
MAXIMUM_BATCHES_PER_RUN = SEMANTIC_SETTINGS["maximum_batches_per_run"]
MAXIMUM_RUN_SECONDS = SEMANTIC_SETTINGS["maximum_run_seconds"]
RUNNER_TIMEOUT_SECONDS = min(3600, MAXIMUM_RUN_SECONDS)


def run_json(arguments: List[str]) -> Dict[str, Any]:
    arguments = list(arguments)
    if arguments and arguments[0].startswith("scripts/"):
        arguments[0] = str(TOOL_ROOT / arguments[0])
    result = subprocess.run(
        [sys.executable] + arguments,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("tool failed: %s" % Path(arguments[0]).name)
    return json.loads(result.stdout)


def run_health_report() -> Dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            str(TOOL_ROOT / "scripts" / "kb.py"),
            "--root",
            str(ROOT),
            "health",
            "--json",
        ],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode not in (knowledge_cli.EXIT_OK, knowledge_cli.EXIT_UNHEALTHY):
        raise RuntimeError("knowledge health command failed")
    report = json.loads(result.stdout)
    if not isinstance(report, dict):
        raise ValueError("knowledge health report must be an object")
    return report


def quarantine_pending_batch(batch: Path, reason: str) -> Path:
    """Preserve a stale derived batch while removing it from the live queue."""

    transaction_path = batch / TRANSACTION_FILE
    if transaction_path.is_file():
        try:
            transaction = json.loads(
                transaction_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            transaction = {}
        reference = transaction.get("prepared_ref")
        if isinstance(reference, str) and reference.startswith(
            "refs/ai-knowledge-semantic/"
        ):
            git(["update-ref", "-d", reference], ROOT, check=False)
    destination_root = DATA / "semantic" / "quarantined"
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / (
        "%s-%s-%s" % (batch.name, reason, uuid.uuid4().hex[:8])
    )
    os.replace(batch, destination)
    return destination


def retry_block_pending_batch(batch: Path, reason: str) -> Path:
    """Move one poison revision aside while preserving any transaction ref."""

    destination_root = DATA / "semantic" / "retry-blocked"
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / (
        "%s-%s-%s" % (batch.name, reason, uuid.uuid4().hex[:8])
    )
    os.replace(batch, destination)
    return destination


def recover_interrupted_bisections() -> Dict[str, Any]:
    """Finish prepared split handoffs before selecting any live batch."""

    published: List[str] = []
    quarantined_root = DATA / "semantic" / "quarantined"
    if quarantined_root.is_dir():
        for parent in sorted(
            path for path in quarantined_root.iterdir() if path.is_dir()
        ):
            plan_path = parent / semantic_batches.SPLIT_PLAN_FILE
            if not plan_path.is_file():
                continue
            try:
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise CurationTransactionError("batch-split-plan-unreadable")
            if plan.get("state") == "published":
                continue
            result = semantic_batches.publish_batch_bisection(
                parent, DATA / "semantic" / "pending"
            )
            published.extend(result["children"])
    pending_root = DATA / "semantic" / "pending"
    if pending_root.is_dir():
        prepared_parents = [
            path
            for path in sorted(pending_root.iterdir())
            if path.is_dir()
            and (path / semantic_batches.SPLIT_PLAN_FILE).is_file()
        ]
        for parent in prepared_parents:
            isolated = quarantine_pending_batch(
                parent, "poison-bisected-recovery"
            )
            result = semantic_batches.publish_batch_bisection(
                isolated, pending_root
            )
            published.extend(result["children"])
    return {
        "ok": True,
        "action": "recover-interrupted-bisections",
        "children_published": len(set(published)),
    }


def _transaction_mutation_crossed(batch: Path) -> bool:
    """Conservatively detect a live branch mutation before batch bisection."""

    def live_batch_commit_present() -> bool:
        try:
            manifest = semantic_batches.verify_batch_payload(batch)
            log = git(
                ["log", "-n", "256", "--format=%s", "HEAD"],
                ROOT,
                check=False,
            )
        except (CurationTransactionError, semantic_batches.DispositionError):
            return False
        expected = "knowledge: curate %s" % manifest.get("batch_id")
        return log.returncode == 0 and expected in log.stdout.splitlines()

    path = batch / TRANSACTION_FILE
    if not path.is_file():
        return False
    try:
        transaction = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return live_batch_commit_present()
    if transaction.get("state") == "applied":
        return True
    commit = transaction.get("commit")
    base = transaction.get("base_commit")
    if not isinstance(commit, str) or not isinstance(base, str):
        return live_batch_commit_present()
    if commit == base:
        return False
    try:
        return git_is_ancestor(commit, git_head(ROOT), ROOT)
    except CurationTransactionError:
        return live_batch_commit_present()


def isolate_failed_batch(
    batch: Path, failure_stage: str, failure_detail: str
) -> Dict[str, Any]:
    """Bisect ambiguous failures or back off one identified revision."""

    attempt = semantic_batches.record_batch_failure(
        batch, failure_stage, failure_detail
    )
    event_count = int(attempt["event_count"])
    if event_count > 1:
        if _transaction_mutation_crossed(batch):
            return {
                "ok": False,
                "strategy": "recovery-required",
                "attempt": attempt,
            }
        split = semantic_batches.prepare_batch_bisection(batch)
        quarantined = quarantine_pending_batch(batch, "poison-bisected")
        published = semantic_batches.publish_batch_bisection(
            quarantined, DATA / "semantic" / "pending"
        )
        return {
            "ok": True,
            "strategy": "bisected",
            "attempt": attempt,
            "split_id": split["split_id"],
            "children": published["children"],
            "quarantined": str(quarantined),
        }
    blocked = attempt.get("retry_blocked") or {}
    isolated = retry_block_pending_batch(batch, "poison-retry")
    return {
        "ok": True,
        "strategy": "retry-blocked",
        "attempt": attempt,
        "isolated": str(isolated),
        "attempts": blocked.get("attempts"),
        "next_retry_at": blocked.get("next_retry_at"),
    }


def validate_batch_contract_current(
    manifest: Dict[str, Any],
    expected_review_fingerprint: Optional[str] = None,
    expected_policy_sha256: Optional[str] = None,
) -> None:
    policy_sha256 = hashlib.sha256(
        (semantic_batches.ROOT / "POLICY.md").read_bytes()
    ).hexdigest()
    if manifest.get("policy_sha256") != policy_sha256:
        raise CurationTransactionError("pending-batch-contract-stale")
    if expected_policy_sha256 is not None and (
        policy_sha256 != expected_policy_sha256
    ):
        raise CurationTransactionError("pending-batch-contract-stale")
    if expected_review_fingerprint is not None:
        prompt_sha256 = hashlib.sha256(
            (
                semantic_batches.ROOT
                / "automation"
                / "semantic-curator-prompt.md"
            ).read_bytes()
        ).hexdigest()
        current_review = semantic_batches.semantic_review_fingerprint(
            prompt_sha256,
            CURATION_MODEL,
            CURATION_REASONING,
            policy_sha256,
        )
        if (
            manifest.get("review_fingerprint")
            != expected_review_fingerprint
            or current_review != expected_review_fingerprint
        ):
            raise CurationTransactionError("pending-batch-contract-stale")


def next_batch(
    review_fingerprint: str = "",
    policy_sha256: str = "",
    modes: Sequence[str] = (
        "incremental",
        "evidence-revisions",
        "priority-backfill",
    ),
) -> Optional[Path]:
    recover_interrupted_bisections()
    retry_root = DATA / "semantic" / "retry-blocked"
    if retry_root.is_dir():
        for existing in sorted(
            path for path in retry_root.iterdir() if path.is_dir()
        ):
            if not (existing / "manifest.json").is_file() or (
                existing / ".completed"
            ).exists():
                continue
            manifest = semantic_batches.verify_batch_payload(existing)
            transaction_path = existing / TRANSACTION_FILE
            transaction_state = None
            if transaction_path.is_file():
                try:
                    transaction_state = json.loads(
                        transaction_path.read_text(encoding="utf-8")
                    ).get("state")
                except (OSError, ValueError, AttributeError):
                    transaction_state = None
            transaction_crossed = bool(
                transaction_path.is_file()
                and _transaction_mutation_crossed(existing)
            )
            retry_state = semantic_batches.retry_batch_state(existing)
            if retry_state is not None and not retry_state.get("due"):
                # Context/contract drift is an explicit wake signal even
                # before the timer.  Check it without exposing event bodies.
                if transaction_state in ("evidence-validated", "applied") or transaction_crossed:
                    continue
                if (
                    manifest.get("review_fingerprint") != review_fingerprint
                    or manifest.get("policy_sha256") != policy_sha256
                ):
                    semantic_batches.clear_retry_block(existing)
                    quarantine_pending_batch(existing, "stale-contract")
                    continue
                try:
                    semantic_batches.validate_batch_context_current(existing)
                except semantic_batches.DispositionError as error:
                    if str(error) != "batch-context-stale":
                        raise
                    semantic_batches.clear_retry_block(existing)
                    quarantine_pending_batch(existing, "stale-context")
                continue
            if transaction_state in ("evidence-validated", "applied") or transaction_crossed:
                return existing
            if (
                manifest.get("review_fingerprint") != review_fingerprint
                or manifest.get("policy_sha256") != policy_sha256
            ):
                semantic_batches.clear_retry_block(existing)
                quarantine_pending_batch(existing, "stale-contract")
                continue
            try:
                semantic_batches.validate_batch_context_current(existing)
            except semantic_batches.DispositionError as error:
                if str(error) != "batch-context-stale":
                    raise
                semantic_batches.clear_retry_block(existing)
                quarantine_pending_batch(existing, "stale-context")
                continue
            return existing
    pending_root = DATA / "semantic" / "pending"
    if pending_root.is_dir():
        for existing in sorted(path for path in pending_root.iterdir() if path.is_dir()):
            if not (existing / "manifest.json").is_file() or (
                existing / ".completed"
            ).exists():
                continue
            manifest = semantic_batches.verify_batch_payload(existing)
            transaction_path = existing / TRANSACTION_FILE
            transaction_state = None
            if transaction_path.is_file():
                try:
                    transaction_state = json.loads(
                        transaction_path.read_text(encoding="utf-8")
                    ).get("state")
                except (OSError, ValueError, AttributeError):
                    transaction_state = None
            if transaction_state in ("evidence-validated", "applied"):
                return existing
            if (
                manifest.get("review_fingerprint") != review_fingerprint
                or manifest.get("policy_sha256") != policy_sha256
            ):
                quarantine_pending_batch(existing, "stale-contract")
                continue
            try:
                semantic_batches.validate_batch_context_current(existing)
            except semantic_batches.DispositionError as error:
                if str(error) != "batch-context-stale":
                    raise
                quarantine_pending_batch(existing, "stale-context")
                continue
            return existing
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    for mode in modes:
        output = DATA / "semantic" / "pending" / (stamp + "-" + mode)
        for _ in range(MAX_EMPTY_SELECTION_WINDOWS):
            result = run_json(
                [
                    "scripts/semantic_batches.py",
                    "--prepare",
                    mode,
                    "--output",
                    str(output),
                    "--review-fingerprint",
                    review_fingerprint,
                    "--policy-sha256",
                    policy_sha256,
                ]
            )
            if int(result.get("event_count", 0)):
                return Path(result["path"])
            selection = result.get("selection") or {}
            if int(selection.get("candidate_rows_examined") or 0) == 0:
                break
    return None


def codex_binary() -> str:
    candidates = [
        str(CODEX) if CODEX.is_file() else None,
        shutil.which("codex"),
        shutil.which("codex.cmd"),
        shutil.which("codex.exe"),
    ]
    seen = set()
    for candidate in candidates:
        if not candidate or candidate.casefold() in seen:
            continue
        seen.add(candidate.casefold())
        try:
            probe = subprocess.run(
                [candidate, "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return str(candidate)
    raise RuntimeError("codex executable not runnable")


def run_codex(
    prompt_file: Path,
    suffix: str,
    report_path: Path,
    workspace_write: bool,
    model: str,
    reasoning_effort: str,
) -> int:
    global LAST_CODEX_FAILURE_CLASS
    LAST_CODEX_FAILURE_CLASS = None
    prompt = prompt_file.read_text(encoding="utf-8") + "\n\n" + suffix
    command = [
        codex_binary(), "exec", "-C", str(ROOT),
        "-s", "workspace-write" if workspace_write else "read-only",
        "--ephemeral", "--ignore-user-config", "--disable", "memories",
        "--output-last-message", str(report_path), "-",
    ]
    if model:
        command[-1:-1] = ["--model", model]
    if reasoning_effort:
        command[-1:-1] = ["--config", 'model_reasoning_effort="%s"' % reasoning_effort]
    try:
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            input=prompt,
            text=True,
            encoding="utf-8",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=max(1, RUNNER_TIMEOUT_SECONDS),
            check=False,
        )
    except subprocess.TimeoutExpired:
        LAST_CODEX_FAILURE_CLASS = "timeout"
        return 124
    if result.returncode != 0:
        folded = str(result.stderr or "").casefold()
        if any(
            marker in folded
            for marker in (
                "unauthorized",
                "authentication",
                "not logged in",
                "login required",
                "status 401",
                "status 403",
            )
        ):
            LAST_CODEX_FAILURE_CLASS = "authentication"
        elif any(
            marker in folded
            for marker in (
                "rate limit",
                "too many requests",
                "service unavailable",
                "bad gateway",
                "gateway timeout",
                "connection refused",
                "connection reset",
                "network is unreachable",
                "status 429",
                "status 500",
                "status 502",
                "status 503",
                "status 504",
            )
        ):
            LAST_CODEX_FAILURE_CLASS = "service"
        else:
            # A nonzero CLI exit produced no proposal body to classify.  It is
            # an operational failure, never evidence that a source event is
            # poisonous.
            LAST_CODEX_FAILURE_CLASS = "runtime"
    return result.returncode


class CurationTransactionError(RuntimeError):
    """A proposal could not be safely turned into one isolated Git transaction."""


def stable_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def git(
    arguments: List[str],
    cwd: Path = ROOT,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git"] + arguments,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise CurationTransactionError(
            "git-%s-failed" % (arguments[0] if arguments else "command")
        )
    return result


def git_add_paths(paths: List[str], cwd: Path = ROOT) -> None:
    """Stage an allow-listed path set without exceeding Windows argv limits."""

    chunk: List[str] = []
    for path in paths:
        candidate = chunk + [path]
        serialized = subprocess.list2cmdline(["git", "add", "--"] + candidate)
        if chunk and len(serialized) > GIT_ADD_COMMAND_LINE_LIMIT:
            git(["add", "--"] + chunk, cwd)
            chunk = [path]
        else:
            chunk = candidate
    if chunk:
        git(["add", "--"] + chunk, cwd)


def git_head(root: Path = ROOT) -> str:
    return git(["rev-parse", "HEAD"], root).stdout.strip()


def git_is_ancestor(commit: str, head: str, root: Path = ROOT) -> bool:
    return (
        git(["merge-base", "--is-ancestor", commit, head], root, check=False).returncode
        == 0
    )


def ensure_clean_checkout(root: Path = ROOT) -> None:
    status = git(["status", "--porcelain=v1", "--untracked-files=all"], root).stdout
    if status.strip():
        raise CurationTransactionError("curation-root-dirty")


def parse_proposal(report_path: Path, batch: Path) -> Dict[str, Any]:
    """Parse a strict data-only model response.

    Markdown fences and explanatory prose are deliberately rejected.  The
    model has read-only access; this object is its only possible influence on
    the repository and is therefore treated like an untrusted API request.
    """
    try:
        document = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise semantic_batches.ProposalError("proposal-unreadable") from error
    if not isinstance(document, dict) or document.get("format") != semantic_batches.PROPOSAL_FORMAT:
        raise semantic_batches.ProposalError("proposal-format-unsupported")
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    if document.get("batch_id") != manifest["batch_id"]:
        raise semantic_batches.ProposalError("proposal-batch-id-mismatch")
    entries = document.get("events")
    if not isinstance(entries, list):
        raise semantic_batches.ProposalError("proposal-events-invalid")
    targets = {
        event["event_hash"]: event
        for event in semantic_batches.batch_events(batch, manifest)
    }
    by_hash: Dict[str, Dict[str, Any]] = {}
    allowed_capture_fields = {
        "kind",
        "scope",
        "statement",
        "value",
        "authority",
        "status",
    }
    allowed_entry_fields = {
        "event_hash",
        "outcome",
        "captures",
        "existing_ids",
        "reason_code",
        "evidence",
    }
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - allowed_entry_fields:
            raise semantic_batches.ProposalError("proposal-event-fields-invalid")
        event_hash = entry.get("event_hash")
        outcome = entry.get("outcome")
        if (
            not isinstance(event_hash, str)
            or not event_hash
            or event_hash in by_hash
            or event_hash not in targets
            or outcome not in semantic_batches.DISPOSITION_OUTCOMES
        ):
            raise semantic_batches.ProposalError("proposal-event-invalid")
        captures = entry.get("captures") or []
        existing_ids = entry.get("existing_ids") or []
        if (
            not isinstance(captures, list)
            or len(captures) > 32
            or not isinstance(existing_ids, list)
            or not all(isinstance(item, str) and item for item in existing_ids)
        ):
            raise semantic_batches.ProposalError("proposal-knowledge-plan-invalid")
        clean_captures: List[Dict[str, Any]] = []
        for capture in captures:
            if not isinstance(capture, dict) or set(capture) - allowed_capture_fields:
                raise semantic_batches.ProposalError("proposal-capture-fields-invalid")
            normalized = {
                "kind": capture.get("kind"),
                "scope": capture.get("scope"),
                "statement": capture.get("statement"),
                "authority": capture.get("authority", "user-explicit"),
                "status": capture.get("status", "active"),
            }
            if "value" in capture:
                normalized["value"] = capture["value"]
            if not all(
                isinstance(normalized[field], str) and normalized[field].strip()
                for field in ("kind", "scope", "statement", "authority", "status")
            ):
                raise semantic_batches.ProposalError("proposal-capture-value-invalid")
            if "value" in normalized and not isinstance(normalized["value"], str):
                raise semantic_batches.ProposalError("proposal-capture-value-invalid")
            if normalized["authority"] not in (
                "user-explicit",
                "agent-inference",
                "verified-system",
            ):
                raise semantic_batches.ProposalError("proposal-authority-invalid")
            if normalized["status"] not in ("active", "candidate"):
                raise semantic_batches.ProposalError("proposal-status-invalid")
            if (
                normalized["authority"] == "agent-inference"
                and normalized["status"] != "candidate"
            ):
                raise semantic_batches.ProposalError("proposal-inference-must-be-candidate")
            if knowledge_cli.event_requires_credential_value(normalized):
                if not knowledge_cli.credential_value_present(normalized):
                    raise semantic_batches.ProposalError(
                        "proposal-credential-value-missing"
                    )
                if not semantic_batches.credential_value_supported_by_event(
                    targets[event_hash], normalized.get("value")
                ):
                    raise semantic_batches.ProposalError(
                        "proposal-credential-value-unsubstantiated"
                    )
                if not semantic_batches.credential_identity_supported_by_event(
                    targets[event_hash], normalized
                ):
                    raise semantic_batches.ProposalError(
                        "proposal-credential-identity-mismatch"
                    )
            clean_captures.append(normalized)
        if outcome == "captured" and (not clean_captures or existing_ids):
            raise semantic_batches.ProposalError("captured-plan-invalid")
        if outcome == "deduplicated" and (clean_captures or not existing_ids):
            raise semantic_batches.ProposalError("deduplicated-plan-invalid")
        if outcome == "conflict":
            if not clean_captures or existing_ids:
                raise semantic_batches.ProposalError("conflict-plan-invalid")
            if any(item["status"] != "candidate" for item in clean_captures):
                raise semantic_batches.ProposalError("conflict-must-be-candidate")
        if outcome in ("temporary", "non-knowledge", "deferred") and (
            clean_captures or existing_ids
        ):
            raise semantic_batches.ProposalError("non-capture-plan-invalid")
        if outcome == "deferred":
            reason_code = entry.get("reason_code")
            evidence = entry.get("evidence")
            if reason_code not in semantic_batches.DEFERRED_REASON_CODES:
                raise semantic_batches.ProposalError(
                    "proposal-deferred-reason-invalid"
                )
            if (
                not isinstance(evidence, str)
                or not evidence
                or evidence not in targets[event_hash]["content"]
            ):
                raise semantic_batches.ProposalError(
                    "proposal-deferred-evidence-invalid"
                )
        if (
            targets[event_hash].get("linked_evidence_incomplete") is True
            and semantic_batches.event_requires_credential_resolution(
                targets[event_hash]
            )
            and outcome not in ("deferred", "temporary")
        ):
            raise semantic_batches.ProposalError(
                "proposal-credential-evidence-incomplete"
            )
        disclosures = semantic_batches.credential_disclosure_units(
            targets[event_hash]
        )
        disclosure_groups = (
            semantic_batches.credential_disclosures_by_identity(
                targets[event_hash]
            )
        )
        conflicting_identity = any(
            len(set(values)) > 1 for values in disclosure_groups.values()
        )
        if disclosures and outcome not in ("deferred", "temporary"):
            if conflicting_identity and outcome != "conflict":
                raise semantic_batches.ProposalError(
                    "proposal-credential-values-conflict"
                )
            if outcome in ("captured", "conflict") and (
                any(
                    not any(
                        semantic_batches.credential_disclosure_covered_by_record(
                            disclosure, capture
                        )
                        for capture in clean_captures
                    )
                    for disclosure in disclosures
                )
            ):
                raise semantic_batches.ProposalError(
                    "proposal-credential-values-incomplete"
                )
            if len(disclosures) > 1 and outcome == "deduplicated":
                raise semantic_batches.ProposalError(
                    "proposal-multi-credential-dedup-unsupported"
                )
        if (
            semantic_batches.event_requires_credential_resolution(
                targets[event_hash]
            )
            and outcome in ("captured", "conflict")
            and not any(
                knowledge_cli.event_requires_credential_value(capture)
                for capture in clean_captures
            )
        ):
            raise semantic_batches.ProposalError(
                "proposal-credential-target-unresolved"
            )
        if (
            semantic_batches.event_requires_credential_resolution(
                targets[event_hash]
            )
            and outcome == "non-knowledge"
        ):
            raise semantic_batches.ProposalError(
                "proposal-credential-target-unresolved"
            )
        by_hash[event_hash] = {
            "event_hash": event_hash,
            "outcome": outcome,
            "captures": clean_captures,
            "existing_ids": existing_ids,
            "reason_code": entry.get("reason_code"),
            "evidence": entry.get("evidence"),
        }
    if set(by_hash) != set(targets):
        raise semantic_batches.ProposalError("proposal-event-hashes-mismatch")
    return {
        "format": semantic_batches.PROPOSAL_FORMAT,
        "batch_id": manifest["batch_id"],
        "events": [by_hash[event_hash] for event_hash in targets],
    }


def deterministic_capture_id(
    batch_id: str,
    event_hash: str,
    capture: Dict[str, Any],
    index: int,
) -> str:
    digest = hashlib.sha256(
        stable_json(
            {
                "batch_id": batch_id,
                "event_hash": event_hash,
                "index": index,
                "capture": capture,
            }
        ).encode("utf-8")
    ).hexdigest()
    return "sem-%s" % digest[:40]


def run_staging_kb_json(
    staging_root: Path, arguments: List[str], error_code: str
) -> Dict[str, Any]:
    """Run a staging kb.py check without ever surfacing sensitive stdout."""

    result = subprocess.run(
        [
            sys.executable,
            str(TOOL_ROOT / "scripts" / "kb.py"),
            "--root",
            str(staging_root),
        ]
        + arguments,
        cwd=str(staging_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != knowledge_cli.EXIT_OK:
        raise CurationTransactionError(error_code)
    try:
        output = json.loads(result.stdout)
    except ValueError as error:
        raise CurationTransactionError(error_code) from error
    if not isinstance(output, dict) or not output.get("ok"):
        raise CurationTransactionError(error_code)
    return output


def verify_credential_capture_readback(
    staging_root: Path,
    *,
    event_id: str,
    capture: Dict[str, Any],
    capture_output: Dict[str, Any],
) -> None:
    """Prove a captured credential remains complete after materialization."""

    if not knowledge_cli.event_requires_credential_value(capture):
        return
    if not knowledge_cli.credential_value_present(capture):
        raise CurationTransactionError("credential-capture-value-missing")
    expected = capture["value"]
    # Explicitly exercise the same idempotent materialization command used to
    # repair older ledgers.  Capture already attempts eager materialization,
    # but success is not trusted until the public retrieval contract verifies
    # the exact value.
    run_staging_kb_json(
        staging_root,
        ["materialize-resources", "--json"],
        "credential-materialization-failed",
    )
    if capture["status"] == "active":
        retrieved = run_staging_kb_json(
            staging_root,
            [
                "get-resource",
                event_id,
                "--include-values",
                "--json",
            ],
            "credential-readback-failed",
        )
        record = retrieved.get("record")
        if (
            retrieved.get("credential") is not True
            or not isinstance(record, dict)
            or semantic_batches.normalize_knowledge_id(
                str(record.get("id") or "")
            )
            != semantic_batches.normalize_knowledge_id(event_id)
            or record.get("value") != expected
            or retrieved.get("superseded_by")
        ):
            raise CurationTransactionError("credential-readback-mismatch")
        return

    # Candidate credentials intentionally do not materialize as canonical
    # resources until accepted.  They must still preserve the exact value in
    # their immutable ledger event so a conflict cannot consume a locator-only
    # statement.
    ledger_relative = capture_output.get("ledger")
    if not isinstance(ledger_relative, str) or not ledger_relative:
        raise CurationTransactionError("credential-candidate-readback-failed")
    try:
        ledger_path = (staging_root / ledger_relative).resolve()
        relative = ledger_path.relative_to(staging_root.resolve())
    except (OSError, ValueError) as error:
        raise CurationTransactionError(
            "credential-candidate-readback-failed"
        ) from error
    if not relative.parts or relative.parts[0] != "ledger":
        raise CurationTransactionError("credential-candidate-readback-failed")
    try:
        ledger_record = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CurationTransactionError(
            "credential-candidate-readback-failed"
        ) from error
    if (
        not isinstance(ledger_record, dict)
        or ledger_record.get("id") != event_id
        or ledger_record.get("value") != expected
        or not knowledge_cli.credential_value_present(ledger_record)
    ):
        raise CurationTransactionError("credential-candidate-readback-mismatch")


def run_capture(
    staging_root: Path,
    *,
    event_id: str,
    event: Dict[str, Any],
    capture: Dict[str, Any],
    source: str,
    value_root: Path,
) -> Dict[str, Any]:
    command = [
        sys.executable,
        str(TOOL_ROOT / "scripts" / "kb.py"),
        "--root",
        str(staging_root),
        "capture",
        "--id",
        event_id,
        "--kind",
        capture["kind"],
        "--scope",
        capture["scope"],
        "--statement",
        capture["statement"],
        "--source",
        source,
        "--authority",
        capture["authority"],
        "--status",
        capture["status"],
        "--source-device-id",
        event["source_device_id"],
    ]
    value_path: Optional[Path] = None
    if "value" in capture:
        value_root.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix="semantic-value-", dir=str(value_root)
        )
        os.close(descriptor)
        value_path = Path(raw_path)
        with value_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(capture["value"])
        command.extend(("--value-file", str(value_path)))
    try:
        result = subprocess.run(
            command,
            cwd=str(staging_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    finally:
        if value_path is not None:
            value_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise CurationTransactionError("capture-application-failed")
    try:
        output = json.loads(result.stdout)
    except ValueError as error:
        raise CurationTransactionError("capture-output-invalid") from error
    if not output.get("ok") or output.get("id") != event_id:
        raise CurationTransactionError("capture-output-invalid")
    verify_credential_capture_readback(
        staging_root,
        event_id=event_id,
        capture=capture,
        capture_output=output,
    )
    return output


def proposal_disposition_in_worktree(
    staging_root: Path,
    batch: Path,
    proposal: Dict[str, Any],
    value_root: Path,
) -> Dict[str, Any]:
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    source_events = {
        event["event_hash"]: event
        for event in semantic_batches.batch_events(batch, manifest)
    }
    dispositions: List[Dict[str, Any]] = []
    for plan in proposal["events"]:
        event_hash = plan["event_hash"]
        source_event = source_events[event_hash]
        marker = semantic_batches.source_marker(manifest["batch_id"], event_hash)
        knowledge_ids: List[str] = []
        if plan["outcome"] in ("captured", "conflict"):
            for index, capture in enumerate(plan["captures"]):
                event_id = deterministic_capture_id(
                    manifest["batch_id"], event_hash, capture, index
                )
                run_capture(
                    staging_root,
                    event_id=event_id,
                    event=source_event,
                    capture=capture,
                    source=marker,
                    value_root=value_root,
                )
                knowledge_ids.append(event_id)
        elif plan["outcome"] == "deduplicated":
            available = semantic_batches.knowledge_records(staging_root)
            for existing_id in plan["existing_ids"]:
                if semantic_batches.normalize_knowledge_id(existing_id) not in available:
                    raise CurationTransactionError("deduplicated-id-unresolved")
            link_capture = {
                "kind": "source-link",
                "scope": "global",
                "statement": "Historical source event confirms existing knowledge: %s"
                % ", ".join(plan["existing_ids"]),
                "authority": "user-explicit",
                "status": "active",
            }
            link_id = deterministic_capture_id(
                manifest["batch_id"], event_hash, link_capture, 0
            )
            run_capture(
                staging_root,
                event_id=link_id,
                event=source_event,
                capture=link_capture,
                source=marker,
                value_root=value_root,
            )
            knowledge_ids = list(plan["existing_ids"]) + [link_id]
        entry: Dict[str, Any] = {
            "event_hash": event_hash,
            "outcome": plan["outcome"],
            "knowledge_ids": knowledge_ids,
        }
        if plan.get("reason_code") is not None:
            entry["reason_code"] = plan["reason_code"]
        if plan.get("evidence") is not None:
            entry["evidence"] = plan["evidence"]
        dispositions.append(entry)
    document = {
        "format": semantic_batches.DISPOSITION_FORMAT,
        "batch_id": manifest["batch_id"],
        "dispositions": dispositions,
    }
    semantic_batches.validate_disposition_document(
        batch, document, knowledge_root=staging_root
    )
    return document


def _changed_paths(root: Path) -> List[str]:
    changed = git(["diff", "--name-only", "HEAD"], root).stdout.splitlines()
    untracked = git(
        ["ls-files", "--others", "--exclude-standard"], root
    ).stdout.splitlines()
    return sorted(set(changed + untracked))


def _write_json_atomic(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-%s" % uuid.uuid4().hex[:8])
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # Windows does not allow opening a directory with os.open(). The file
        # itself is already flushed and atomically replaced; directory fsync
        # is an additional POSIX durability guarantee.
        if os.name != "nt":
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def stage_proposal_transaction(
    batch: Path,
    proposal: Dict[str, Any],
    expected_review_fingerprint: Optional[str] = None,
    expected_policy_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply the untrusted proposal in a disposable detached worktree.

    Only kb.py-created ledger/candidate/resource files may survive.  POLICY,
    scripts, prompts, batch input and existing canonical files never become
    writable by the model and cannot enter the transaction.
    """
    ensure_clean_checkout(ROOT)
    manifest = semantic_batches.verify_batch_payload(batch)
    validate_batch_contract_current(
        manifest,
        expected_review_fingerprint,
        expected_policy_sha256,
    )
    semantic_batches.validate_batch_context_current(batch)
    base_commit = git_head(ROOT)
    workspace_parent = DATA / "semantic" / "workspaces"
    workspace_parent.mkdir(parents=True, exist_ok=True)
    staging_root = workspace_parent / (
        "curate-%s-%s"
        % (json.loads((batch / "manifest.json").read_text())["batch_id"], uuid.uuid4().hex[:8])
    )
    value_root = DATA / "semantic" / "values"
    created = False
    try:
        git(["worktree", "add", "--detach", str(staging_root), base_commit], ROOT)
        created = True
        disposition = proposal_disposition_in_worktree(
            staging_root, batch, proposal, value_root
        )
        paths = _changed_paths(staging_root)
        invalid = [
            path
            for path in paths
            if not any(path.startswith(prefix) for prefix in ALLOWED_TRANSACTION_PREFIXES)
        ]
        if invalid:
            raise CurationTransactionError("transaction-path-outside-knowledge-zones")
        if paths:
            git_add_paths(paths, staging_root)
            git(
                [
                    "-c",
                    "user.name=AI Knowledge Curator",
                    "-c",
                    "user.email=knowledge-curator@local.invalid",
                    "commit",
                    "-m",
                    "knowledge: curate %s" % proposal["batch_id"],
                ],
                staging_root,
            )
            commit = git_head(staging_root)
        else:
            commit = base_commit
        transaction_ref = "refs/ai-knowledge-semantic/%s" % proposal[
            "batch_id"
        ].replace(":", "-")
        git(["update-ref", transaction_ref, commit], ROOT)
        transaction = {
            "format": TRANSACTION_FORMAT,
            "batch_id": proposal["batch_id"],
            "base_commit": base_commit,
            "commit": commit,
            "prepared_ref": transaction_ref,
            "events_sha256": manifest["events_sha256"],
            "proposal_sha256": hashlib.sha256(
                stable_json(proposal).encode("utf-8")
            ).hexdigest(),
            "disposition": disposition,
            "state": "prepared",
            "prepared_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        _write_json_atomic(batch / TRANSACTION_FILE, transaction)
        return transaction
    finally:
        if created:
            git(
                ["worktree", "remove", "--force", str(staging_root)],
                ROOT,
                check=False,
            )
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)


def load_transaction(batch: Path) -> Optional[Dict[str, Any]]:
    path = batch / TRANSACTION_FILE
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CurationTransactionError("transaction-unreadable") from error
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("format") != TRANSACTION_FORMAT
        or value.get("batch_id") != manifest["batch_id"]
        or not isinstance(value.get("commit"), str)
        or not isinstance(value.get("base_commit"), str)
        or not isinstance(value.get("prepared_ref"), str)
        or not isinstance(value.get("events_sha256"), str)
        or not isinstance(value.get("disposition"), dict)
    ):
        raise CurationTransactionError("transaction-invalid")
    return value


def apply_prepared_transaction(
    batch: Path,
    transaction: Dict[str, Any],
    expected_review_fingerprint: Optional[str] = None,
    expected_policy_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Fast-forward one verified commit, then publish its durable checkpoint.

    transaction.json is written before the branch move.  A crash immediately
    after the fast-forward is therefore recoverable: the next run sees that
    the commit is already an ancestor and finishes the same disposition
    without asking the model again.
    """
    manifest = semantic_batches.verify_batch_payload(batch)
    if transaction["events_sha256"] != manifest["events_sha256"]:
        raise CurationTransactionError("transaction-payload-mismatch")
    if transaction.get("state") != "applied":
        validate_batch_contract_current(
            manifest,
            expected_review_fingerprint,
            expected_policy_sha256,
        )
    ensure_clean_checkout(ROOT)
    current = git_head(ROOT)
    commit = transaction["commit"]
    base = transaction["base_commit"]
    anchored = git(["rev-parse", transaction["prepared_ref"]], ROOT, check=False)
    if anchored.returncode != 0 or anchored.stdout.strip() != commit:
        raise CurationTransactionError("transaction-ref-mismatch")
    if commit != base:
        if not git_is_ancestor(base, commit, ROOT):
            raise CurationTransactionError("transaction-base-not-ancestor")
        commits = git(["rev-list", "--count", "%s..%s" % (base, commit)], ROOT)
        if commits.stdout.strip() != "1":
            raise CurationTransactionError("transaction-commit-count-invalid")
        changed = git(
            ["diff", "--name-only", base, commit], ROOT
        ).stdout.splitlines()
        if any(
            not any(path.startswith(prefix) for prefix in ALLOWED_TRANSACTION_PREFIXES)
            for path in changed
        ):
            raise CurationTransactionError("transaction-path-outside-knowledge-zones")
    commit_is_ancestor = commit == base or git_is_ancestor(commit, current, ROOT)
    rebased_applied = False
    if (
        commit != base
        and not commit_is_ancestor
        and transaction.get("state") == "applied"
        and git_is_ancestor(base, current, ROOT)
    ):
        # git_sync may rebase an already-applied local semantic commit while
        # integrating a peer device's hot-layer update. The immutable
        # transaction ref still points to the original commit, so prove the
        # rebased live tree contains every batch-linked knowledge id before
        # accepting the new commit identity.
        try:
            semantic_batches.validate_disposition_document(
                batch, transaction["disposition"], knowledge_root=ROOT
            )
        except semantic_batches.DispositionError:
            rebased_applied = False
        else:
            rebased_applied = True
    history: Optional[sqlite3.Connection] = None
    mutation_already_crossed = bool(
        transaction.get("state") == "applied"
        or rebased_applied
        or (commit != base and commit_is_ancestor)
    )
    try:
        if not mutation_already_crossed:
            semantic_batches.ensure_history_semantic_indexes()
            history = sqlite3.connect(
                str(semantic_batches.HISTORY), timeout=30
            )
            history.execute("BEGIN IMMEDIATE")
            semantic_batches.validate_batch_context_current(
                batch, history_connection=history
            )
            transaction = dict(transaction)
            transaction["state"] = "evidence-validated"
            transaction["evidence_validated_at"] = (
                dt.datetime.now(dt.timezone.utc).isoformat()
            )
            _write_json_atomic(batch / TRANSACTION_FILE, transaction)
        if commit != base and not commit_is_ancestor and not rebased_applied:
            if current != base:
                raise CurationTransactionError("transaction-base-advanced")
            git(["merge", "--ff-only", commit], ROOT)
            current = git_head(ROOT)
            commit_is_ancestor = git_is_ancestor(commit, current, ROOT)
        if commit != base and not commit_is_ancestor and not rebased_applied:
            raise CurationTransactionError("transaction-commit-not-applied")
        disposition = transaction["disposition"]
        semantic_batches.validate_disposition_document(
            batch, disposition, knowledge_root=ROOT
        )
        semantic_batches.write_disposition(batch, disposition)
        transaction = dict(transaction)
        transaction["state"] = "applied"
        transaction["applied_at"] = (
            dt.datetime.now(dt.timezone.utc).isoformat()
        )
        if rebased_applied:
            transaction["rebased_commit"] = current
        _write_json_atomic(batch / TRANSACTION_FILE, transaction)
        if history is not None:
            history.commit()
        return transaction
    except Exception:
        if history is not None and history.in_transaction:
            history.rollback()
        raise
    finally:
        if history is not None:
            history.close()


def release_transaction_ref(transaction: Dict[str, Any]) -> None:
    reference = transaction.get("prepared_ref")
    if isinstance(reference, str) and reference.startswith(
        "refs/ai-knowledge-semantic/"
    ):
        git(["update-ref", "-d", reference], ROOT, check=False)


def _nul_paths(arguments: List[str], root: Path = ROOT) -> Optional[List[str]]:
    result = git(arguments, root, check=False)
    if result.returncode != 0:
        return None
    return [path for path in result.stdout.split("\0") if path]


def _capture_status_documents_are_safe(
    previous: Dict[str, Any], current: Dict[str, Any], source_device_id: str
) -> bool:
    changed = {
        key
        for key in set(previous).union(current)
        if previous.get(key) != current.get(key)
    }
    if not changed or not changed.issubset(CAPTURE_STATUS_MUTABLE_FIELDS):
        return False
    if previous.get("id") != source_device_id or current.get("id") != source_device_id:
        return False
    for key in ("history_events", "source_files"):
        old_value = previous.get(key)
        new_value = current.get(key)
        if (
            isinstance(old_value, bool)
            or isinstance(new_value, bool)
            or not isinstance(old_value, int)
            or not isinstance(new_value, int)
            or new_value < old_value
        ):
            return False
    try:
        old_capture = dt.datetime.fromisoformat(
            str(previous["last_capture_completed_at"]).replace("Z", "+00:00")
        )
        new_capture = dt.datetime.fromisoformat(
            str(current["last_capture_completed_at"]).replace("Z", "+00:00")
        )
    except (KeyError, TypeError, ValueError):
        return False
    if (
        old_capture.tzinfo is None
        or new_capture.tzinfo is None
        or new_capture <= old_capture
    ):
        return False
    return True


def _capture_status_matches_latest_report(
    current: Dict[str, Any], source_device_id: str, root: Path = ROOT
) -> bool:
    reports = root / ".kb-data" / "reports"
    try:
        report_path = max(
            reports.glob("windows-capture-*.json"),
            key=lambda path: path.stat().st_mtime_ns,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    coverage = report.get("checkpoint_coverage") or {}
    return bool(
        report.get("ok") is True
        and report.get("device_id") == source_device_id
        and report.get("history_events") == current.get("history_events")
        and report.get("source_files") == current.get("source_files")
        and report.get("last_capture_completed_at")
        == current.get("last_capture_completed_at")
        and coverage.get("coverage_complete") is True
        and report.get("raw_exported") is False
        and report.get("bundle_exported") is False
    )


def commit_local_capture_status_if_safe(
    root: Path = ROOT,
) -> Optional[Dict[str, Any]]:
    """Commit only a verified local capture-status update before pre-sync.

    Daily Windows capture intentionally updates one tracked device status file.
    Semantic maintenance must preserve fail-closed dirty-worktree behavior for
    every other path, including user edits and staged or untracked files.
    """

    staged = _nul_paths(["diff", "--cached", "--name-only", "-z"], root)
    untracked = _nul_paths(
        ["ls-files", "--others", "--exclude-standard", "-z"], root
    )
    unmerged = _nul_paths(
        ["diff", "--name-only", "--diff-filter=U", "-z"], root
    )
    unstaged = _nul_paths(["diff", "--name-only", "-z"], root)
    if None in (staged, untracked, unmerged, unstaged):
        return None
    if staged or untracked or unmerged:
        return None
    try:
        source_device_id = device_identity.resolve_device_id(root)
    except (device_identity.DeviceIdentityError, OSError, ValueError):
        return None
    relative = "config/devices/%s.json" % source_device_id
    if unstaged != [relative]:
        return None
    previous_result = subprocess.run(
        ["git", "show", "HEAD:%s" % relative],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
        env=git_sync.git_environment(),
    )
    current_path = root / Path(relative)
    try:
        previous = json.loads(previous_result.stdout)
        current = json.loads(current_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if previous_result.returncode != 0:
        return None
    if not _capture_status_documents_are_safe(previous, current, source_device_id):
        return None
    if not _capture_status_matches_latest_report(current, source_device_id, root):
        return None
    try:
        git_sync.ensure_local_bot_identity(root)
    except (git_sync.SyncError, OSError, subprocess.SubprocessError):
        raise CurationTransactionError("capture-status-identity-failed")
    message = "knowledge: capture %s %s" % (
        source_device_id,
        dt.datetime.now().strftime("%Y-%m-%dT%H:%M"),
    )
    committed = git(
        ["commit", "--only", "-m", message, "--", relative],
        root,
        check=False,
    )
    if committed.returncode != 0:
        raise CurationTransactionError("capture-status-commit-failed")
    return {
        "committed": True,
        "commit": git(["rev-parse", "HEAD"], root).stdout.strip(),
        "path": relative,
        "source_device_id": source_device_id,
    }


def pre_sync_status() -> Dict[str, Any]:
    """Pull current hot knowledge and fail closed on repository-integrity errors.

    Only two explicitly classified network conditions may continue offline.
    Dirty/wrong/diverged/public/identity-invalid repositories are not network
    outages and must never become the base for an automated knowledge commit.
    """
    if not (ROOT / ".git").exists():
        raise RuntimeError("git-repository-missing")
    def run_sync() -> tuple[subprocess.CompletedProcess, Optional[Dict[str, Any]]]:
        result = subprocess.run(
            [sys.executable, str(TOOL_ROOT / "scripts" / "git_sync.py"), "--pull-only"],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        document: Optional[Dict[str, Any]] = None
        for raw in (result.stdout, result.stderr):
            try:
                candidate = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(candidate, dict):
                document = candidate
                break
        return result, document

    result, document = run_sync()
    capture_commit: Optional[Dict[str, Any]] = None
    if str((document or {}).get("error") or "") == "dirty-worktree":
        capture_commit = commit_local_capture_status_if_safe(ROOT)
        if capture_commit:
            result, document = run_sync()
    if capture_commit and document:
        document = {**document, "local_capture_commit": capture_commit}
    if result.returncode == 0 and document and document.get("ok"):
        if document.get("skipped"):
            raise CurationTransactionError(
                "pre-sync-%s" % document["skipped"]
            )
        return document
    error = str((document or {}).get("error") or "git-sync-failed")
    if error in ("repository-privacy-unknown", "GitTimeout"):
        return {
            **(document or {}),
            "ok": False,
            "action": "git-sync",
            "offline": True,
            "error": error,
        }
    raise CurationTransactionError("pre-sync-%s" % error)


def curate() -> Dict[str, Any]:
    global LAST_CODEX_FAILURE_CLASS, RUNNER_TIMEOUT_SECONDS
    pre_sync = pre_sync_status()
    prompt_file = ROOT / "automation" / "semantic-curator-prompt.md"
    if not prompt_file.is_file():
        prompt_file = TOOL_ROOT / "automation" / "semantic-curator-prompt.md"
    prompt_sha256 = hashlib.sha256(prompt_file.read_bytes()).hexdigest()
    policy_file = ROOT / "POLICY.md"
    policy_sha256 = hashlib.sha256(policy_file.read_bytes()).hexdigest()
    review_fingerprint = semantic_batches.semantic_review_fingerprint(
        prompt_sha256,
        CURATION_MODEL,
        CURATION_REASONING,
        policy_sha256,
    )
    started = time.monotonic()
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    model_calls = 0
    completed: List[Dict[str, Any]] = []
    events_processed = 0
    events_deferred = 0
    batch_attempts = 0
    failure_attempts: List[Dict[str, Any]] = []
    batches_bisected = 0
    retry_blocked_this_run = 0
    infrastructure_retry: Optional[Dict[str, Any]] = None

    def finish(extra: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "action": "curate",
            "started_at": started_at,
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "model": CURATION_MODEL or None,
            "reasoning_effort": CURATION_REASONING or None,
            "model_calls": model_calls,
            "usage": {"status": "unknown", "input_tokens": None, "output_tokens": None, "cached_tokens": None},
            "pre_sync": pre_sync,
            "batches_completed": len(completed),
            "events_processed": events_processed,
            "events_deferred": events_deferred,
            "batch_attempts": batch_attempts,
            "failure_attempts": failure_attempts,
            "batches_bisected": batches_bisected,
            "retry_blocked_this_run": retry_blocked_this_run,
            "batches": completed,
        }
        result.update(extra)
        if infrastructure_retry is not None:
            result["infrastructure_retry"] = infrastructure_retry
        return result

    try:
        preflight_health = run_health_report()
    except (RuntimeError, ValueError, json.JSONDecodeError):
        return finish(
            {
                "ok": False,
                "error": "preflight-health-check-failed",
                "stop_reason": "unhealthy-before-curation",
            }
        )
    preflight_health_issues = {
        str(issue) for issue in preflight_health.get("issues", [])
    }
    if preflight_health_issues or not preflight_health.get("ok"):
        return finish(
            {
                "ok": False,
                "error": "preflight-health-check-failed",
                "stop_reason": "unhealthy-before-curation",
                "health_issues": sorted(preflight_health_issues),
            }
        )

    stop_reason = "no-pending"
    while True:
        if batch_attempts >= MAXIMUM_BATCHES_PER_RUN:
            stop_reason = "batch-limit"
            break
        if time.monotonic() - started >= MAXIMUM_RUN_SECONDS:
            stop_reason = "time-budget"
            break
        rotations = (
            ("incremental", "evidence-revisions", "priority-backfill"),
            ("evidence-revisions", "priority-backfill", "incremental"),
            ("priority-backfill", "incremental", "evidence-revisions"),
        )
        modes = rotations[batch_attempts % len(rotations)]
        batch = next_batch(review_fingerprint, policy_sha256, modes)
        if batch is None:
            break
        manifest = semantic_batches.verify_batch_payload(batch)
        if manifest.get("requires_explicit_large_event_budget") and not CONFIG.get("semantic_maintenance", {}).get("allow_oversized_events", False):
            return finish({"ok": True, "pending": True, "stop_reason": "oversized-event-needs-budget", "batch": str(batch), "required_characters": manifest.get("content_characters")})
        batch_attempts += 1
        report: Optional[Path] = None
        try:
            transaction = load_transaction(batch)
        except CurationTransactionError as error:
            isolation = isolate_failed_batch(
                batch, "transaction-load", str(error)
            )
            failure_attempts.append(
                {
                    "batch": str(batch),
                    "stage": "transaction-load",
                    "strategy": isolation.get("strategy"),
                    "failure_fingerprint": (
                        isolation.get("attempt") or {}
                    ).get("failure_fingerprint"),
                    "next_retry_at": isolation.get("next_retry_at"),
                }
            )
            if not isolation.get("ok"):
                return finish(
                    {
                        "ok": False,
                        "batch": str(batch),
                        "error": "transaction-load-failed",
                        "detail": str(error),
                        "isolation": isolation,
                    }
                )
            if isolation.get("strategy") == "bisected":
                batches_bisected += 1
            else:
                retry_blocked_this_run += 1
            continue
        resumed_transaction = transaction is not None
        if transaction is None:
            REPORTS.mkdir(parents=True, exist_ok=True)
            report = REPORTS / (
                "semantic-%s-%s.json"
                % (
                    dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
                    uuid.uuid4().hex[:6],
                )
            )
            maximum_calls = int(CONFIG.get("semantic_maintenance", {}).get("maximum_model_calls", MAXIMUM_BATCHES_PER_RUN))
            if model_calls >= maximum_calls:
                return finish({"ok": True, "pending": True, "stop_reason": "model-call-budget"})
            RUNNER_TIMEOUT_SECONDS = max(1, min(3600, MAXIMUM_RUN_SECONDS - (time.monotonic() - started)))
            model_calls += 1
            LAST_CODEX_FAILURE_CLASS = None
            exit_code = run_codex(
                prompt_file,
                "Immutable batch directory: `%s`. Return only the required JSON proposal."
                % batch,
                report,
                False,
                CURATION_MODEL,
                CURATION_REASONING,
            )
            if exit_code != 0:
                failure_class = LAST_CODEX_FAILURE_CLASS or "runtime"
                attempt = semantic_batches.record_batch_failure(
                    batch,
                    "codex-infrastructure-%s" % failure_class,
                    "exit-%d" % exit_code,
                    block_single_revision=False,
                )
                failure_attempts.append(
                    {
                        "batch": str(batch),
                        "stage": "codex-infrastructure",
                        "failure_class": failure_class,
                        "strategy": "retry-pending",
                        "failure_fingerprint": attempt.get(
                            "failure_fingerprint"
                        ),
                        "next_retry_at": None,
                    }
                )
                infrastructure_retry = {
                    "failure_class": failure_class,
                    "codex_exit_code": exit_code,
                    "batch": str(batch),
                    "failure_fingerprint": attempt.get(
                        "failure_fingerprint"
                    ),
                }
                stop_reason = "codex-infrastructure-%s" % failure_class
                break
            try:
                proposal = parse_proposal(report, batch)
            except (semantic_batches.ProposalError, semantic_batches.DispositionError) as error:
                if str(error) in (
                    "batch-context-stale",
                    "pending-batch-contract-stale",
                ):
                    reason = (
                        "stale-context"
                        if str(error) == "batch-context-stale"
                        else "stale-contract"
                    )
                    semantic_batches.clear_retry_block(batch)
                    quarantine_pending_batch(batch, reason)
                    continue
                isolation = isolate_failed_batch(
                    batch, "proposal", str(error)
                )
                failure_attempts.append(
                    {
                        "batch": str(batch),
                        "stage": "proposal",
                        "strategy": isolation.get("strategy"),
                        "failure_fingerprint": (
                            isolation.get("attempt") or {}
                        ).get("failure_fingerprint"),
                        "next_retry_at": isolation.get("next_retry_at"),
                    }
                )
                if not isolation.get("ok"):
                    return finish({
                        "ok": False,
                        "batch": str(batch),
                        "report": str(report),
                        "error": "proposal-invalid",
                        "detail": str(error),
                        "isolation": isolation,
                    })
                if isolation.get("strategy") == "bisected":
                    batches_bisected += 1
                else:
                    retry_blocked_this_run += 1
                continue
            try:
                transaction = stage_proposal_transaction(
                    batch,
                    proposal,
                    review_fingerprint,
                    policy_sha256,
                )
            except (semantic_batches.DispositionError, CurationTransactionError) as error:
                if str(error) in (
                    "batch-context-stale",
                    "pending-batch-contract-stale",
                ):
                    reason = (
                        "stale-context"
                        if str(error) == "batch-context-stale"
                        else "stale-contract"
                    )
                    semantic_batches.clear_retry_block(batch)
                    quarantine_pending_batch(batch, reason)
                    continue
                isolation = isolate_failed_batch(
                    batch, "transaction-prepare", str(error)
                )
                failure_attempts.append(
                    {
                        "batch": str(batch),
                        "stage": "transaction-prepare",
                        "strategy": isolation.get("strategy"),
                        "failure_fingerprint": (
                            isolation.get("attempt") or {}
                        ).get("failure_fingerprint"),
                        "next_retry_at": isolation.get("next_retry_at"),
                    }
                )
                if not isolation.get("ok"):
                    return finish({
                        "ok": False,
                        "batch": str(batch),
                        "report": str(report),
                        "error": "transaction-prepare-failed",
                        "detail": str(error),
                        "isolation": isolation,
                    })
                if isolation.get("strategy") == "bisected":
                    batches_bisected += 1
                else:
                    retry_blocked_this_run += 1
                continue
        try:
            transaction = apply_prepared_transaction(
                batch,
                transaction,
                review_fingerprint,
                policy_sha256,
            )
        except (semantic_batches.DispositionError, CurationTransactionError) as error:
            if str(error) in (
                "batch-context-stale",
                "pending-batch-contract-stale",
            ):
                release_transaction_ref(transaction)
                reason = (
                    "stale-context"
                    if str(error) == "batch-context-stale"
                    else "stale-contract"
                )
                semantic_batches.clear_retry_block(batch)
                quarantine_pending_batch(batch, reason)
                continue
            isolation = isolate_failed_batch(
                batch, "transaction-apply", str(error)
            )
            failure_attempts.append(
                {
                    "batch": str(batch),
                    "stage": "transaction-apply",
                    "strategy": isolation.get("strategy"),
                    "failure_fingerprint": (
                        isolation.get("attempt") or {}
                    ).get("failure_fingerprint"),
                    "next_retry_at": isolation.get("next_retry_at"),
                }
            )
            if not isolation.get("ok"):
                return finish({
                    "ok": False,
                    "batch": str(batch),
                    "report": str(report) if report else None,
                    "error": "transaction-apply-failed",
                    "detail": str(error),
                    "isolation": isolation,
                })
            if isolation.get("strategy") == "bisected":
                batches_bisected += 1
            else:
                retry_blocked_this_run += 1
            continue
        # The disposition gate runs against the live, fast-forwarded knowledge
        # tree.  A syntactically valid but nonexistent or source-less knowledge
        # id cannot consume the event.
        try:
            semantic_batches.validate_disposition(batch, ROOT)
        except semantic_batches.DispositionError as error:
            return finish({
                "ok": False,
                "batch": str(batch),
                "report": str(report),
                "error": "disposition-invalid",
                "detail": str(error),
            })
        # The transaction was built in an isolated worktree with its own
        # disposable index. After the verified commit fast-forwards the live
        # branch, refresh this checkout's index before asking health() whether
        # canonical and ledger sources are fully represented.
        try:
            index_update = run_json(
                ["scripts/kb.py", "--root", str(ROOT), "update-index"]
            )
        except (RuntimeError, ValueError):
            index_update = {"ok": False}
        if not index_update.get("ok"):
            return finish({
                "ok": False,
                "batch": str(batch),
                "report": str(report) if report else None,
                "error": "index-update-failed",
            })
        try:
            health = run_health_report()
        except (RuntimeError, ValueError, json.JSONDecodeError):
            health = {"ok": False, "issues": ["health-report-unavailable"]}
        post_health_issues = {
            str(issue) for issue in health.get("issues", [])
        }
        if post_health_issues or not health.get("ok"):
            return finish({
                "ok": False,
                "batch": str(batch),
                "report": str(report),
                "error": "health-check-failed",
                "health_issues": sorted(post_health_issues),
            })
        try:
            completion = run_json([
                "scripts/semantic_batches.py",
                "--mark-complete", str(batch),
                "--model", CURATION_MODEL,
                "--reasoning-effort", CURATION_REASONING,
                "--prompt-sha256", prompt_sha256,
            ])
        except (RuntimeError, ValueError):
            return finish({
                "ok": False,
                "batch": str(batch),
                "report": str(report),
                "error": "mark-complete-failed",
            })
        release_transaction_ref(transaction)
        events_processed += int(completion.get("processed") or 0)
        events_deferred += int(completion.get("deferred") or 0)
        # main() owns the workspace mutation lock across this complete
        # workflow; invoke the inner sync directly so a subprocess cannot
        # deadlock trying to reacquire the same flock.
        hot = hot_sync.execute(acquire_workspace_lock=False)
        completed.append({
            "batch": str(batch),
            "batch_id": completion.get("batch_id"),
            "report": str(report) if report else None,
            "processed": completion.get("processed"),
            "deferred": completion.get("deferred", 0),
            "commit": hot.get("commit"),
        })
        if not hot.get("ok"):
            return finish({
                "ok": False,
                "batch": str(batch),
                "report": str(report),
                "error": "hot-sync-failed",
                "hot_sync": hot,
            })
        if resumed_transaction:
            # A recovered transaction has already crossed a prior failure or
            # process interruption. Finish and report that durable recovery as
            # one bounded success; the next scheduled run continues backlog
            # selection from the newly published checkpoint.
            stop_reason = "recovered-transaction"
            break
    backlog = None
    deferred_remaining = None
    evidence_revisions_remaining = None
    retry_blocked_remaining = None
    queue_pending_remaining = None
    statistics_available = True
    try:
        statistics = run_json(["scripts/semantic_batches.py", "--stats"])
        backlog = statistics.get("backlog_user_events")
        deferred_remaining = statistics.get("deferred_user_events")
        evidence_revisions_remaining = statistics.get(
            "evidence_revision_backlog"
        )
        retry_blocked_remaining = statistics.get(
            "retry_blocked_user_events"
        )
        queue_pending_remaining = statistics.get(
            "queue_pending_user_events"
        )
    except (RuntimeError, ValueError):
        statistics_available = False
        backlog = None
        deferred_remaining = None
        evidence_revisions_remaining = None
        retry_blocked_remaining = None
        queue_pending_remaining = None
    if not statistics_available:
        return finish(
            {
                "ok": False,
                "error": "semantic-stats-unavailable",
                "stop_reason": "stats-unavailable",
                "queue_state": "unknown",
                "backlog_remaining": None,
                "deferred_remaining": None,
                "evidence_revisions_remaining": None,
                "retry_blocked_remaining": None,
                "queue_pending_remaining": None,
            }
        )
    if stop_reason == "no-pending":
        if int(queue_pending_remaining or 0) or int(
            evidence_revisions_remaining or 0
        ):
            stop_reason = "scan-window-pending"
        elif int(retry_blocked_remaining or 0):
            stop_reason = "retry-blocked"
        elif int(deferred_remaining or 0):
            stop_reason = "deferred-quarantined"
    outcome: Dict[str, Any] = {
        "ok": True,
        "stop_reason": stop_reason,
        "backlog_remaining": backlog,
        "deferred_remaining": deferred_remaining,
        "evidence_revisions_remaining": evidence_revisions_remaining,
        "retry_blocked_remaining": retry_blocked_remaining,
        "queue_pending_remaining": queue_pending_remaining,
    }
    if (
        int(backlog or 0)
        or int(deferred_remaining or 0)
        or int(evidence_revisions_remaining or 0)
        or int(retry_blocked_remaining or 0)
        or int(queue_pending_remaining or 0)
        or infrastructure_retry is not None
    ):
        outcome["status"] = "pending"
        outcome["pending"] = True
    if not completed:
        outcome["batch"] = None
        outcome["message"] = (
            "semantic model infrastructure retry is pending"
            if infrastructure_retry is not None
            else (
                "semantic retry is backoff-blocked"
                if int(retry_blocked_remaining or 0)
                else "no pending semantic events"
            )
        )
    return finish(outcome)


def audit() -> Dict[str, Any]:
    pre_sync = pre_sync_status()
    REPORTS.mkdir(parents=True, exist_ok=True)
    report = REPORTS / ("neutral-audit-%s.md" % dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    exit_code = run_codex(
        ROOT / "automation" / "neutral-audit-prompt.md",
        (
            "Audit this repository now. Do not use external memories. "
            "This is a report-only run: do not modify any file."
        ),
        report,
        False,
        AUDIT_MODEL,
        AUDIT_REASONING,
    )
    if exit_code != 0:
        return {"ok": False, "action": "audit", "codex_exit_code": exit_code, "pre_sync": pre_sync}
    test_environment = os.environ.copy()
    # The local scheduler binds this process to the real device. Unit tests
    # create independent temporary identities, so leaking the host override
    # into them makes valid isolation checks fail spuriously.
    test_environment.pop("KB_SOURCE_DEVICE_ID", None)
    tests = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-v", "tests"],
        cwd=str(ROOT),
        env=test_environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if tests.returncode != 0:
        return {"ok": False, "action": "audit", "error": "tests-failed", "report": str(report), "pre_sync": pre_sync}
    return {
        "ok": True,
        "action": "audit",
        "report": str(report),
        "commit": None,
        "report_only": True,
        "pre_sync": pre_sync,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("curate", "audit"), required=True)
    args = parser.parse_args(argv)
    attempt_id = automation_status.record_attempt(
        args.mode, root=ROOT, home=AUTOMATION_STATUS_HOME
    )

    def emit(result: Dict[str, Any], exit_code: int) -> int:
        processed = int(result.get("events_processed") or 0)
        if result.get("pending") or result.get("status") == "pending" or result.get("skipped"):
            automation_status.record_pending(
                args.mode,
                attempt_id,
                str(result.get("skipped") or result.get("stop_reason") or "pending"),
                processed=processed,
                root=ROOT,
                home=AUTOMATION_STATUS_HOME,
            )
        elif result.get("ok"):
            no_op = args.mode == "curate" and processed == 0
            automation_status.record_success(
                args.mode,
                attempt_id,
                processed=processed,
                result="no-op" if no_op else "success",
                root=ROOT,
                home=AUTOMATION_STATUS_HOME,
            )
        else:
            automation_status.record_failure(
                args.mode,
                attempt_id,
                str(result.get("error") or "automation-failed"),
                processed=processed,
                exit_code=exit_code,
                root=ROOT,
                home=AUTOMATION_STATUS_HOME,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return exit_code

    try:
        try:
            cold = device_identity.validate_cold_data_root(
                ROOT,
                require_bound_volume=True,
            )
        except (device_identity.DeviceIdentityError, OSError):
            cold = {
                "valid": False,
                "mode": "unknown",
                "reason": "cold-data-validation-error",
            }
        if not cold.get("valid"):
            return emit(
                {
                    "ok": False,
                    "error": cold.get("reason") or "cold-data-validation-failed",
                    "cold_storage_mode": cold.get("mode"),
                    "action": args.mode,
                },
                2,
            )
        try:
            with storage_lock.acquire(ROOT, exclusive=False):
                with LOCK.open("a+") as lock:
                    try:
                        file_lock.acquire(lock, exclusive=True, blocking=False)
                    except BlockingIOError:
                        return emit(
                            {
                                "ok": True,
                                "action": args.mode,
                                "status": "pending",
                                "pending": True,
                                "skipped": "maintenance-already-running",
                            },
                            0,
                        )
                    try:
                        try:
                            with workspace_lock.acquire(ROOT, blocking=False):
                                result = (
                                    curate() if args.mode == "curate" else audit()
                                )
                        except workspace_lock.WorkspaceBusy:
                            return emit(
                                {
                                    "ok": True,
                                    "action": args.mode,
                                    "status": "pending",
                                    "pending": True,
                                    "skipped": "workspace-mutation-running",
                                },
                                0,
                            )
                    finally:
                        file_lock.release(lock)
        except storage_lock.StorageBusy:
            return emit(
                {
                    "ok": True,
                    "action": args.mode,
                    "status": "pending",
                    "pending": True,
                    "skipped": "storage-operation-running",
                },
                0,
            )
        return emit(result, 0 if result.get("ok") else 2)
    except Exception as error:
        automation_status.record_failure(
            args.mode,
            attempt_id,
            error.__class__.__name__,
            root=ROOT,
            home=AUTOMATION_STATUS_HOME,
        )
        raise


if __name__ == "__main__":
    if not os.environ.get("AI_KNOWLEDGE_ROOT") and not any(x in sys.argv for x in ("--help", "-h")):
        print(json.dumps({"ok": False, "error": "explicit-AI_KNOWLEDGE_ROOT-required"}))
        raise SystemExit(2)
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired:
        print(json.dumps({"ok": False, "error": "CodexTimeout"}), file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": exc.__class__.__name__}), file=sys.stderr)
        raise SystemExit(2)
