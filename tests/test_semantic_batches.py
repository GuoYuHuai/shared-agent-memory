import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import tracemalloc
import unittest
import uuid
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    path = ROOT / "scripts" / (name + ".py")
    module_name = "%s_test_%s" % (name, uuid.uuid4().hex)
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SemanticBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "AI-Knowledge"
        self.status_environment = mock.patch.dict(
            os.environ,
            {
                "AI_KNOWLEDGE_DEVICE_STATE": str(
                    self.root / ".test-local-state" / "device.json"
                )
            },
        )
        self.status_environment.start()
        self.data = self.root / ".kb-data"
        self.pending = self.data / "semantic" / "pending"
        self.history = self.data / "history.sqlite"
        self.semantic = self.data / "semantic.sqlite"
        self.data.mkdir(parents=True)
        self.root.joinpath("POLICY.md").write_text("# Test policy\n", encoding="utf-8")
        # pre_sync_status refuses to run outside a git repository, and curate()
        # hashes the curator prompt before contacting the model.
        self.root.joinpath(".git").mkdir()
        self.root.joinpath("automation").mkdir()
        self.prompt = self.root / "automation" / "semantic-curator-prompt.md"
        self.prompt.write_text("# Curator prompt for tests\n", encoding="utf-8")

        self.batches = load_script("semantic_batches")
        self.batches.ROOT = self.root
        self.batches.DATA = self.data
        self.batches.HISTORY = self.history
        self.batches.SEMANTIC = self.semantic
        self.batches.PENDING = self.pending
        self._initialize_history()

    def tearDown(self):
        self.status_environment.stop()
        self.temporary.cleanup()

    def _initialize_history(self):
        connection = sqlite3.connect(str(self.history))
        connection.execute(
            """
            CREATE TABLE source_events (
                id INTEGER PRIMARY KEY,
                event_hash TEXT NOT NULL UNIQUE,
                source_device_id TEXT NOT NULL,
                source TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                event_id TEXT,
                role TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_timestamp TEXT,
                ingested_at TEXT,
                cwd TEXT,
                content TEXT NOT NULL,
                related_user_event_hash TEXT,
                evidence_content_complete INTEGER NOT NULL DEFAULT 1,
                source_path TEXT NOT NULL,
                source_offset INTEGER NOT NULL,
                source_line INTEGER NOT NULL
            )
            """
        )
        connection.commit()
        connection.close()

    def test_git_add_paths_chunks_large_windows_command_lines(self):
        runner = load_script("run_semantic_maintenance")
        paths = [
            "ledger/2026/07/%04d-%s.json" % (index, "x" * 120)
            for index in range(200)
        ]
        calls = []

        def fake_git(arguments, cwd=runner.ROOT, check=True):
            calls.append((arguments, cwd, check))
            return subprocess.CompletedProcess(arguments, 0, "", "")

        runner.git = fake_git
        runner.git_add_paths(paths, self.root)

        self.assertGreater(len(calls), 1)
        flattened = []
        for arguments, cwd, check in calls:
            self.assertEqual(arguments[:2], ["add", "--"])
            self.assertEqual(cwd, self.root)
            self.assertTrue(check)
            self.assertLessEqual(
                len(subprocess.list2cmdline(["git"] + arguments)),
                runner.GIT_ADD_COMMAND_LINE_LIMIT,
            )
            flattened.extend(arguments[2:])
        self.assertEqual(flattened, paths)

    def add_event(
        self,
        event_id,
        content,
        role="user",
        source="codex",
        device="device-test",
        event_type="message",
        related_user_event_hash=None,
        evidence_content_complete=True,
        thread=None,
        source_path=None,
        source_offset=None,
        source_line=None,
        event_hash=None,
        source_event_id=None,
    ):
        connection = sqlite3.connect(str(self.history))
        connection.execute(
            """
            INSERT INTO source_events(
                id, event_hash, source_device_id, source, thread_id,
                event_id, role, event_type, event_timestamp, ingested_at, cwd,
                content, related_user_event_hash, evidence_content_complete,
                source_path, source_offset, source_line
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_hash or "hash-%03d" % event_id,
                device,
                source,
                thread or "thread-%s" % source,
                source_event_id or "event-%03d" % event_id,
                role,
                event_type,
                "2026-07-22T%02d:00:00Z" % event_id,
                "2026-07-22T%02d:00:00Z" % event_id,
                "/projects/test",
                content,
                related_user_event_hash,
                int(bool(evidence_content_complete)),
                source_path or "/history/%s.jsonl" % source,
                event_id * 100 if source_offset is None else source_offset,
                event_id if source_line is None else source_line,
            ),
        )
        connection.commit()
        connection.close()

    def disposable_repository(self, name="transaction-repo"):
        destination = Path(self.temporary.name) / name
        shutil.copytree(
            ROOT,
            destination,
            ignore=shutil.ignore_patterns(
                ".git",
                ".kb-data",
                ".work",
                "generated",
                "__pycache__",
                "*.pyc",
            ),
        )
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(destination),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        # Subsequent fixture commits and commit-tree calls must not depend on
        # the developer's global Git identity (absent on clean CI workers).
        for key, value in (
            ("user.name", "Semantic Test"),
            ("user.email", "semantic@example.test"),
        ):
            subprocess.run(
                ["git", "config", "--local", key, value],
                cwd=str(destination),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        subprocess.run(
            ["git", "add", "."],
            cwd=str(destination),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@local.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            cwd=str(destination),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return destination

    @staticmethod
    def events_in(batch):
        return [
            json.loads(line)
            for line in (batch / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_disposition(
        self,
        batch,
        outcome="captured",
        entries=None,
        overrides=None,
        seed_knowledge=True,
    ):
        manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        if entries is None:
            entries = []
            for event in self.events_in(batch):
                entry = {
                    "event_hash": event["event_hash"],
                    "outcome": outcome,
                    "knowledge_ids": (
                        ["test-knowledge-%s" % event["event_hash"]]
                        if outcome in self.batches.KNOWLEDGE_OUTCOMES
                        else []
                    ),
                }
                if outcome == "deferred":
                    entry.update(
                        {
                            "reason_code": "insufficient-context",
                            "evidence": event["content"],
                        }
                    )
                entries.append(entry)
        if seed_knowledge:
            ledger = self.root / "ledger" / "test"
            ledger.mkdir(parents=True, exist_ok=True)
            for entry in entries:
                if entry.get("outcome") not in self.batches.KNOWLEDGE_OUTCOMES:
                    continue
                for raw_id in entry.get("knowledge_ids") or []:
                    event_id = self.batches.normalize_knowledge_id(raw_id)
                    path = ledger / (event_id + ".json")
                    if path.exists():
                        continue
                    path.write_text(
                        json.dumps(
                            {
                                "id": event_id,
                                "source": self.batches.source_marker(
                                    manifest["batch_id"], entry["event_hash"]
                                ),
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
        document = {
            "format": "ai-knowledge-semantic-disposition-v1",
            "batch_id": manifest["batch_id"],
            "dispositions": entries,
        }
        if overrides:
            document.update(overrides)
        (batch / "disposition.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return document

    def semantic_counts(self):
        connection = sqlite3.connect(str(self.semantic))
        try:
            processed = connection.execute("SELECT count(*) FROM processed_events").fetchone()[0]
            batches = connection.execute("SELECT count(*) FROM completed_batches").fetchone()[0]
        finally:
            connection.close()
        return processed, batches

    def load_runner(self):
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = self.root
        runner.DATA = self.data
        runner.REPORTS = self.data / "reports"
        runner.LOCK = self.data / "semantic-maintenance.lock"
        runner.AUTOMATION_STATUS_HOME = self.root / "test-home"
        runner.semantic_batches = self.batches
        runner.pre_sync_status = lambda: {
            "ok": True,
            "action": "git-sync",
            "pulled": False,
        }
        runner.run_health_report = lambda: {"ok": True, "issues": []}

        def stage(batch, proposal, *_contract):
            manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
            dispositions = []
            for plan in proposal["events"]:
                knowledge_ids = []
                if plan["outcome"] in self.batches.KNOWLEDGE_OUTCOMES:
                    knowledge_ids = [
                        "test-knowledge-%s-%d" % (plan["event_hash"], index)
                        for index, _ in enumerate(plan.get("captures") or [None])
                    ]
                    self.write_disposition(
                        batch,
                        entries=[
                            {
                                "event_hash": plan["event_hash"],
                                "outcome": plan["outcome"],
                                "knowledge_ids": knowledge_ids,
                            }
                        ],
                    )
                    # write_disposition above is only a convenient knowledge
                    # seeder; the transaction publishes the complete document.
                    (batch / "disposition.json").unlink()
                dispositions.append(
                    {
                        "event_hash": plan["event_hash"],
                        "outcome": plan["outcome"],
                        "knowledge_ids": knowledge_ids,
                        **(
                            {"reason_code": plan["reason_code"]}
                            if plan.get("reason_code") is not None
                            else {}
                        ),
                        **(
                            {"evidence": plan["evidence"]}
                            if plan.get("evidence") is not None
                            else {}
                        ),
                    }
                )
            transaction = {
                "format": runner.TRANSACTION_FORMAT,
                "batch_id": manifest["batch_id"],
                "base_commit": "a" * 40,
                "commit": "a" * 40,
                "prepared_ref": "refs/ai-knowledge-semantic/" + manifest["batch_id"],
                "events_sha256": manifest["events_sha256"],
                "proposal_sha256": "b" * 64,
                "disposition": {
                    "format": self.batches.DISPOSITION_FORMAT,
                    "batch_id": manifest["batch_id"],
                    "dispositions": dispositions,
                },
                "state": "prepared",
            }
            (batch / runner.TRANSACTION_FILE).write_text(
                json.dumps(transaction), encoding="utf-8"
            )
            return transaction

        def apply(batch, transaction, *_contract):
            self.batches.write_disposition(batch, transaction["disposition"])
            self.batches.validate_disposition(batch, self.root)
            return {**transaction, "state": "applied"}

        runner.stage_proposal_transaction = stage
        runner.apply_prepared_transaction = apply
        runner.release_transaction_ref = lambda transaction: None
        return runner

    def runner_run_json(self, mark_calls=None, prepare_limit=1):
        # Emulates the semantic_batches/kb/git_sync subprocesses against the
        # real batch module so runner tests never spawn Codex or a shell.
        def fake(arguments):
            if arguments[0].endswith("git_sync.py"):
                return {"ok": True, "action": "git-sync", "pulled": True}
            if "--prepare" in arguments:
                mode = arguments[arguments.index("--prepare") + 1]
                output = Path(arguments[arguments.index("--output") + 1])
                review_fingerprint = (
                    arguments[arguments.index("--review-fingerprint") + 1]
                    if "--review-fingerprint" in arguments
                    else None
                )
                return self.batches.prepare(
                    mode,
                    output,
                    limit=prepare_limit,
                    maximum_characters=10000,
                    review_fingerprint=review_fingerprint,
                )
            if "--mark-complete" in arguments:
                batch = Path(arguments[arguments.index("--mark-complete") + 1])
                call = {
                    "model": arguments[arguments.index("--model") + 1],
                    "reasoning_effort": arguments[arguments.index("--reasoning-effort") + 1],
                    "prompt_sha256": arguments[arguments.index("--prompt-sha256") + 1],
                }
                if mark_calls is not None:
                    mark_calls.append(call)
                return self.batches.mark_complete(batch, **call)
            if "--stats" in arguments:
                return self.batches.stats()
            if arguments[0].endswith("kb.py"):
                return {"ok": True}
            raise AssertionError("unexpected run_json call: %r" % (arguments,))

        return fake

    def codex_writing_disposition(self, outcome="captured"):
        def fake_codex(prompt_file, suffix, report, workspace_write, model, reasoning_effort):
            batch = Path(suffix.split("`")[1])
            manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
            events = []
            for event in self.events_in(batch):
                plan = {
                    "event_hash": event["event_hash"],
                    "outcome": outcome,
                    "captures": [],
                    "existing_ids": [],
                }
                if outcome == "captured":
                    plan["captures"] = [
                        {
                            "kind": "fact",
                            "scope": "test",
                            "statement": event["content"],
                            "authority": "user-explicit",
                            "status": "active",
                        }
                    ]
                elif outcome == "deferred":
                    plan.update(
                        {
                            "reason_code": "insufficient-context",
                            "evidence": event["content"],
                        }
                    )
                events.append(plan)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps(
                    {
                        "format": self.batches.PROPOSAL_FORMAT,
                        "batch_id": manifest["batch_id"],
                        "events": events,
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(workspace_write)
            return 0

        return fake_codex

    def test_priority_backfill_selects_only_matching_user_events_newest_first(self):
        self.add_event(1, "普通闲聊，不需要进入优先回溯")
        self.add_event(2, "服务器密码是 assistant 提到的", role="assistant")
        self.add_event(3, "我希望所有结果都先给结论")
        self.add_event(4, "这个项目的服务器域名以后默认使用测试环境")
        self.add_event(5, "Remember this API key belongs to the simulator account")

        output = self.pending / "priority"
        result = self.batches.prepare("priority-backfill", output, limit=10, maximum_characters=10000)

        self.assertTrue(result["ok"])
        self.assertEqual(result["event_count"], 3)
        events = self.events_in(output)
        self.assertEqual([event["source_event_rowid"] for event in events], [5, 4, 3])
        self.assertTrue(all(event["role"] == "user" for event in events))
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["mode"], "priority-backfill")
        self.assertEqual(manifest["minimum_source_event_rowid"], 3)
        self.assertEqual(manifest["maximum_source_event_rowid"], 5)
        self.assertEqual(
            manifest["events_sha256"],
            hashlib.sha256((output / "events.jsonl").read_bytes()).hexdigest(),
        )

    def test_forked_observations_share_one_target_and_keep_best_evidence(self):
        content = "我希望所有结果都先给结论"
        self.add_event(
            1,
            content,
            thread="fork-a",
            source_path="/history/fork-a.jsonl",
            event_hash="fork-hash-a",
            source_event_id="msg-shared",
        )
        self.add_event(
            2,
            content,
            thread="fork-b",
            source_path="/history/fork-b.jsonl",
            event_hash="fork-hash-b",
            source_event_id="msg-shared",
        )
        self.add_event(
            3,
            "short linked evidence",
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="fork-hash-a",
            thread="fork-a",
            source_path="/history/fork-a.jsonl",
        )
        self.add_event(
            4,
            "longer linked evidence that preserves the richer fork context",
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="fork-hash-b",
            thread="fork-b",
            source_path="/history/fork-b.jsonl",
        )

        batch = self.pending / "forked-logical-event"
        prepared = self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )

        self.assertEqual(prepared["event_count"], 1)
        event = self.events_in(batch)[0]
        self.assertEqual(event["event_hash"], "fork-hash-b")
        self.assertEqual(len(event["equivalent_observations"]), 2)
        self.assertIn(
            "longer linked evidence",
            "\n".join(item["content"] for item in event["context"]),
        )
        self.assertIn(
            "short linked evidence",
            "\n".join(item["content"] for item in event["context"]),
        )
        self.write_disposition(batch)
        self.batches.mark_complete(batch)
        following = self.pending / "forked-logical-event-following"
        self.assertEqual(
            self.batches.prepare(
                "incremental", following, limit=10, maximum_characters=10000
            )["event_count"],
            0,
        )
        revision_scan = self.pending / "forked-logical-event-revisions"
        self.assertEqual(
            self.batches.prepare(
                "evidence-revisions",
                revision_scan,
                limit=10,
                maximum_characters=10000,
            )["event_count"],
            0,
        )
        self.assertEqual(self.batches.stats()["backlog_user_events"], 0)
        connection = sqlite3.connect(str(self.history))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM source_events WHERE role='user'"
                ).fetchone()[0],
                2,
            )
        finally:
            connection.close()

    def test_fork_merge_authorizes_exact_evidence_from_nonrepresentative_alias(self):
        content = "请读取 Store API key，并保留关联证据里的完整原值"
        value = "alpha-credential-7429"
        self.add_event(
            1,
            content,
            thread="fork-a",
            source_path="/history/fork-a.jsonl",
            event_hash="credential-fork-a",
            source_event_id="credential-msg-shared",
        )
        self.add_event(
            2,
            content,
            thread="fork-b",
            source_path="/history/fork-b.jsonl",
            event_hash="credential-fork-b",
            source_event_id="credential-msg-shared",
        )
        self.add_event(
            3,
            "generic diagnostic context that is deliberately much longer " * 8,
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="credential-fork-a",
            thread="fork-a",
            source_path="/history/fork-a.jsonl",
        )
        self.add_event(
            4,
            "Store API key value is %s" % value,
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="credential-fork-b",
            thread="fork-b",
            source_path="/history/fork-b.jsonl",
        )

        batch = self.pending / "forked-credential-evidence"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        event = self.events_in(batch)[0]

        self.assertEqual(event["event_hash"], "credential-fork-a")
        self.assertEqual(len(event["equivalent_observations"]), 2)
        self.assertIn(
            value,
            "\n".join(item["content"] for item in event["context"]),
        )
        self.assertTrue(event["credential_resolution_required"])
        self.assertTrue(
            self.batches.credential_value_supported_by_event(event, value)
        )

    def test_evidence_revision_wakes_deferred_target_after_user_cursor(self):
        self.add_event(1, "那就用上一个，需要更多上下文才能知道指什么")
        review = self.batches.semantic_review_fingerprint(
            "a" * 64, "model-a", "high", "b" * 64
        )
        first = self.pending / "deferred-before-new-evidence"
        self.batches.prepare(
            "incremental",
            first,
            limit=10,
            maximum_characters=10000,
            review_fingerprint=review,
        )
        self.write_disposition(first, outcome="deferred")
        first_completion = self.batches.mark_complete(first)
        self.assertEqual(first_completion["selection_cursor"], 1)

        self.add_event(
            2,
            "new complete linked evidence",
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="hash-001",
        )
        second = self.pending / "deferred-after-new-evidence"
        prepared = self.batches.prepare(
            "evidence-revisions",
            second,
            limit=10,
            maximum_characters=10000,
            review_fingerprint=review,
        )

        self.assertEqual(prepared["event_count"], 1)
        event = self.events_in(second)[0]
        self.assertEqual(event["source_event_rowid"], 1)
        self.assertIn(
            "new complete linked evidence",
            [item["content"] for item in event["context"]],
        )
        manifest = json.loads(
            (second / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["selection"]["cursor_boundary_rowid"], 1)
        self.write_disposition(second, outcome="deferred")
        second_completion = self.batches.mark_complete(second)
        self.assertEqual(second_completion["selection_cursor"], 1)
        self.assertEqual(self.batches.selection_cursor("incremental"), 1)

    def test_later_user_context_wakes_deferred_target_without_skipping_trigger(self):
        self.add_event(
            1,
            "Retrieve the Acme API key; the account is still unclear",
        )
        first = self.pending / "deferred-before-user-clarification"
        self.batches.prepare(
            "incremental", first, limit=10, maximum_characters=10000
        )
        first_event = self.events_in(first)[0]
        first_fingerprint = first_event["context_fingerprint"]
        self.write_disposition(first, outcome="deferred")
        first_completion = self.batches.mark_complete(first)
        self.assertEqual(first_completion["selection_cursor"], 1)

        self.add_event(
            2,
            "Please clarify which Acme account this belongs to.",
            role="assistant",
        )
        self.add_event(3, "The account is primary and the environment is test")
        wake = self.pending / "deferred-after-user-clarification"
        prepared = self.batches.prepare(
            "incremental", wake, limit=1, maximum_characters=10000
        )

        self.assertEqual(prepared["event_count"], 1)
        event = self.events_in(wake)[0]
        self.assertEqual(event["event_hash"], "hash-001")
        self.assertEqual(event["selection_trigger_context_rowids"], [3])
        self.assertNotEqual(event["context_fingerprint"], first_fingerprint)
        manifest = json.loads(
            (wake / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["selection"]["cursor_boundary_rowid"], 2)
        self.assertTrue(
            self.batches.validate_batch_context_current(wake)["ok"]
        )

        self.write_disposition(wake, outcome="deferred")
        wake_completion = self.batches.mark_complete(wake)
        self.assertEqual(wake_completion["selection_cursor"], 2)
        trigger_batch = self.pending / "clarification-remains-a-target"
        trigger_prepared = self.batches.prepare(
            "incremental",
            trigger_batch,
            limit=1,
            maximum_characters=10000,
        )
        self.assertEqual(trigger_prepared["event_count"], 1)
        self.assertEqual(
            self.events_in(trigger_batch)[0]["event_hash"], "hash-003"
        )

    def test_later_user_context_wakes_processed_target(self):
        self.add_event(1, "The deployment account is the one we discussed")
        first = self.pending / "processed-before-user-clarification"
        self.batches.prepare(
            "incremental", first, limit=10, maximum_characters=10000
        )
        first_fingerprint = self.events_in(first)[0]["context_fingerprint"]
        self.write_disposition(first)
        self.batches.mark_complete(first)

        self.add_event(2, "By that I mean the primary test account")
        wake = self.pending / "processed-after-user-clarification"
        prepared = self.batches.prepare(
            "incremental", wake, limit=1, maximum_characters=10000
        )

        self.assertEqual(prepared["event_count"], 1)
        event = self.events_in(wake)[0]
        self.assertEqual(event["event_hash"], "hash-001")
        self.assertEqual(event["selection_trigger_context_rowids"], [2])
        self.assertNotEqual(event["context_fingerprint"], first_fingerprint)

    def test_user_context_wake_is_same_thread_and_bounded(self):
        self.add_event(1, "Use the account discussed earlier")
        first = self.pending / "context-boundary-first"
        self.batches.prepare(
            "incremental", first, limit=10, maximum_characters=10000
        )
        self.write_disposition(first, outcome="deferred")
        self.batches.mark_complete(first)

        self.add_event(2, "Primary means the test account", thread="other-thread")
        cross_thread = self.pending / "cross-thread-does-not-wake"
        self.batches.prepare(
            "incremental", cross_thread, limit=10, maximum_characters=10000
        )
        self.assertNotIn(
            "hash-001",
            [event["event_hash"] for event in self.events_in(cross_thread)],
        )
        self.write_disposition(cross_thread)
        self.batches.mark_complete(cross_thread)

        self.add_event(3, "first intervening response", role="assistant")
        self.add_event(4, "second intervening response", role="assistant")
        self.add_event(5, "Primary means the test account")
        out_of_window = self.pending / "out-of-window-does-not-wake"
        self.batches.prepare(
            "incremental", out_of_window, limit=10, maximum_characters=10000
        )
        events = self.events_in(out_of_window)
        self.assertEqual([event["event_hash"] for event in events], ["hash-005"])

    def test_cardinal_one_does_not_create_context_revision(self):
        self.add_event(1, "I prefer one reminder per day.")
        first = self.pending / "cardinal-one-first"
        self.batches.prepare(
            "incremental", first, limit=10, maximum_characters=10000
        )
        self.write_disposition(first)
        self.batches.mark_complete(first)
        self.add_event(2, "The account is primary.")

        following = self.pending / "cardinal-one-following"
        self.batches.prepare(
            "incremental", following, limit=10, maximum_characters=10000
        )

        self.assertEqual(
            [event["event_hash"] for event in self.events_in(following)],
            ["hash-002"],
        )

    def test_ordinary_user_context_never_authorizes_credential_value(self):
        value = "ordinary-user-context-secret-7429"
        self.add_event(1, "Retrieve the Acme API key from the inspected file")
        self.add_event(2, "The Acme API key is %s" % value)
        batch = self.pending / "ordinary-context-is-not-credential-evidence"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        target = next(
            event
            for event in self.events_in(batch)
            if event["event_hash"] == "hash-001"
        )

        self.assertIn(value, "\n".join(item["content"] for item in target["context"]))
        self.assertFalse(
            self.batches.credential_value_supported_by_event(target, value)
        )

    def test_interleaved_evidence_trigger_never_skips_unreviewed_user(self):
        self.add_event(1, "ordinary event A")
        self.add_event(2, "ordinary event B")
        self.add_event(
            3,
            "late evidence for A",
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="hash-001",
        )
        first = self.pending / "interleaved-first"
        self.batches.prepare(
            "incremental", first, limit=1, maximum_characters=10000
        )
        self.assertEqual(self.events_in(first)[0]["event_hash"], "hash-001")
        self.write_disposition(first)
        first_completion = self.batches.mark_complete(first)
        self.assertEqual(first_completion["selection_cursor"], 1)

        second = self.pending / "interleaved-second"
        self.batches.prepare(
            "incremental", second, limit=1, maximum_characters=10000
        )
        self.assertEqual(self.events_in(second)[0]["event_hash"], "hash-002")

    def test_logical_key_does_not_merge_conflicting_or_idless_events(self):
        self.add_event(
            1,
            "first meaning",
            event_hash="same-id-a",
            source_event_id="msg-collision",
        )
        self.add_event(
            2,
            "different meaning",
            event_hash="same-id-b",
            source_event_id="msg-collision",
        )
        self.add_event(3, "same idless prose", event_hash="idless-a")
        self.add_event(4, "same idless prose", event_hash="idless-b")
        connection = sqlite3.connect(str(self.history))
        with connection:
            connection.execute(
                "UPDATE source_events SET event_id=NULL WHERE id IN (3,4)"
            )
        connection.close()

        batch = self.pending / "logical-key-boundaries"
        prepared = self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )

        self.assertEqual(prepared["event_count"], 4)

    def test_transport_context_is_not_curated_or_counted_as_backlog(self):
        agents_wrapper = (
            "<recommended_plugins>\n"
            "- Example plugin\n"
            "</recommended_plugins>"
            "# AGENTS.md instructions\n\n"
            "<INSTRUCTIONS>\n"
            "账号、密码、Token 和密钥必须保留原值。\n"
            "</INSTRUCTIONS>"
        )
        project_agents_wrapper = agents_wrapper.replace(
            "# AGENTS.md instructions\n",
            "# AGENTS.md instructions for /projects/test\n",
            1,
        )
        environment_wrapper = (
            "<environment_context>\n"
            "  <cwd>/projects/test</cwd>\n"
            "</environment_context>"
        )
        self.add_event(1, agents_wrapper)
        self.add_event(2, project_agents_wrapper)
        self.add_event(3, environment_wrapper)
        self.add_event(4, "我希望所有结果都先给结论")

        priority = self.pending / "transport-context-priority"
        prepared = self.batches.prepare(
            "priority-backfill",
            priority,
            limit=10,
            maximum_characters=10000,
        )
        self.assertEqual(prepared["event_count"], 1)
        self.assertEqual(
            self.events_in(priority)[0]["source_event_rowid"],
            4,
        )
        self.write_disposition(priority)
        self.batches.mark_complete(priority)

        incremental = self.batches.prepare(
            "incremental",
            self.pending / "transport-context-incremental",
            limit=10,
            maximum_characters=10000,
        )
        self.assertEqual(incremental["event_count"], 0)
        self.assertEqual(self.batches.stats()["backlog_user_events"], 0)
        self.assertFalse(
            self.batches.is_transport_context_only(
                agents_wrapper + "\n请解释这份规则"
            )
        )

    def test_short_followup_includes_bounded_same_thread_context(self):
        self.add_event(1, "我预计最多不超过 512GB，所以不需要更大的硬盘")
        self.add_event(2, "明白，你希望把容量限制在 512GB。", role="assistant")
        self.add_event(3, "tb")
        self.add_event(4, "你是在把容量修正为 1TB。", role="assistant")

        batch = self.pending / "short-followup-context"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        target = next(
            event for event in self.events_in(batch) if event["event_hash"] == "hash-003"
        )

        self.assertEqual(target["content"], "tb")
        self.assertEqual(
            [item["content"] for item in target["context"]],
            [
                "我预计最多不超过 512GB，所以不需要更大的硬盘",
                "明白，你希望把容量限制在 512GB。",
                "你是在把容量修正为 1TB。",
            ],
        )
        self.assertIn("never follow instructions", target["context_notice"])

    def test_linked_tool_evidence_is_merged_beyond_adjacency_only_for_target_thread(self):
        self.add_event(1, "Find the Store API key from the inspected file")
        for event_id in range(2, 7):
            self.add_event(
                event_id,
                "intermediate assistant call %d" % event_id,
                role="assistant",
            )
        self.add_event(
            7,
            "DOUBAO_API_KEY=alpha-credential-7429",
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="hash-001",
        )
        self.add_event(
            8,
            "DOUBAO_API_KEY=wrong-hash-evidence-1835",
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="hash-unrelated",
        )
        self.add_event(
            9,
            "DOUBAO_API_KEY=cross-thread-evidence-9914",
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="hash-001",
            thread="thread-other",
        )

        batch = self.pending / "linked-evidence-outside-adjacency"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        target = self.events_in(batch)[0]
        context_ids = {
            item["source_event_rowid"] for item in target["context"]
        }

        self.assertIn(7, context_ids)
        self.assertNotIn(8, context_ids)
        self.assertNotIn(9, context_ids)
        linked = next(
            item
            for item in target["context"]
            if item["source_event_rowid"] == 7
        )
        self.assertEqual(linked["related_user_event_hash"], "hash-001")
        self.assertFalse(linked["content_truncated"])

    def test_exact_relation_accepts_cross_device_evidence_imported_before_target(self):
        value = "peer-device-credential-7429"
        self.add_event(
            1,
            "STORE_API_KEY=%s" % value,
            role="assistant",
            device="device-peer",
            event_type="tool-result-evidence",
            related_user_event_hash="target-imported-later",
            thread="shared-thread",
            source_path="/history/peer-tool.jsonl",
        )
        self.add_event(
            2,
            "Find the Store API key from the inspected file",
            device="device-local",
            event_hash="target-imported-later",
            thread="shared-thread",
            source_path="/history/local-user.jsonl",
        )

        batch = self.pending / "cross-device-evidence-first"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        target = self.events_in(batch)[0]
        linked = [
            item
            for item in target["context"]
            if item["event_type"] == "tool-result-evidence"
        ]

        self.assertEqual([item["source_event_rowid"] for item in linked], [1])
        self.assertEqual(linked[0]["source_device_id"], "device-peer")
        self.assertTrue(
            self.batches.credential_value_supported_by_event(target, value)
        )

    def test_relation_conflict_revision_wakes_every_target_fail_closed(self):
        self.add_event(1, "Find the shared credential", event_hash="target-one")
        self.add_event(2, "Use the other account", event_hash="target-two")
        self.add_event(
            3,
            "SHARED_API_KEY=conflicted-value-7429",
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="target-one",
            event_hash="evidence-shared",
        )
        self.batches.ensure_history_semantic_indexes()
        connection = sqlite3.connect(str(self.history))
        with connection:
            connection.execute(
                "INSERT INTO source_event_user_relations("
                "event_hash,related_user_event_hash,first_seen_at) VALUES (?,?,?)",
                ("evidence-shared", "target-two", "2026-07-22T04:00:00Z"),
            )
            connection.execute(
                "UPDATE source_events SET related_user_event_hash=NULL, "
                "evidence_content_complete=0, semantic_revision=2 "
                "WHERE event_hash='evidence-shared'"
            )
            connection.execute(
                "INSERT INTO semantic_evidence_changes("
                "event_hash,semantic_revision,changed_at) VALUES (?,?,?)",
                ("evidence-shared", 2, "2026-07-22T04:00:00Z"),
            )
        connection.close()

        batch = self.pending / "relation-conflict-revision"
        prepared = self.batches.prepare(
            "evidence-revisions", batch, limit=10, maximum_characters=10000
        )
        events = self.events_in(batch)

        self.assertEqual(prepared["event_count"], 2)
        self.assertEqual(
            {event["event_hash"] for event in events},
            {"target-one", "target-two"},
        )
        self.assertTrue(
            all(event["linked_evidence_incomplete"] for event in events)
        )

    def test_batch_context_rejects_in_place_evidence_completeness_revision(self):
        self.add_event(1, "Find the Store API key from the inspected file")
        self.add_event(
            2,
            "STORE_API_KEY=complete-value-7429",
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="hash-001",
        )
        batch = self.pending / "stale-evidence-completeness"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        self.assertTrue(
            self.batches.validate_batch_context_current(batch)["ok"]
        )

        connection = sqlite3.connect(str(self.history))
        with connection:
            connection.execute(
                "UPDATE source_events SET evidence_content_complete=0, "
                "semantic_revision=semantic_revision+1 WHERE id=2"
            )
            revision = connection.execute(
                "SELECT semantic_revision FROM source_events WHERE id=2"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO semantic_evidence_changes("
                "event_hash,semantic_revision,changed_at) VALUES (?,?,?)",
                ("hash-002", revision, "2026-07-22T03:00:00Z"),
            )
        connection.close()

        with self.assertRaisesRegex(
            self.batches.DispositionError, "batch-context-stale"
        ):
            self.batches.validate_batch_context_current(batch)

    def test_linked_tool_evidence_has_hard_row_and_character_bounds(self):
        self.add_event(1, "Find the Store API key from the inspected files")
        for event_id in range(2, 14):
            self.add_event(
                event_id,
                "STORE_API_KEY=value-%02d-%s"
                % (event_id, "x" * 30000),
                role="assistant",
                event_type="tool-result-evidence",
                related_user_event_hash="hash-001",
            )

        batch = self.pending / "linked-evidence-bounds"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=200000
        )
        target = self.events_in(batch)[0]
        evidence = [
            item
            for item in target["context"]
            if item["event_type"] == "tool-result-evidence"
        ]

        self.assertLessEqual(
            len(evidence), self.batches.LINKED_EVIDENCE_MAX_ROWS
        )
        self.assertLessEqual(
            sum(len(item["content"]) for item in evidence),
            self.batches.LINKED_EVIDENCE_MAX_CHARACTERS,
        )
        self.assertEqual(len(evidence), 4)

    def test_late_thirty_third_evidence_wakes_target_and_fails_closed_on_cap(self):
        value = "alpha-credential-7429"
        self.add_event(1, "Find the Store API key from the inspected files")
        for event_id in range(2, 35):
            content = (
                "Store API key value is %s" % value
                if event_id == 34
                else "generic linked diagnostic %02d" % event_id
            )
            self.add_event(
                event_id,
                content,
                role="assistant",
                event_type="tool-result-evidence",
                related_user_event_hash="hash-001",
            )

        batch = self.pending / "linked-evidence-thirty-third"
        self.batches.prepare(
            "evidence-revisions",
            batch,
            limit=10,
            maximum_characters=200000,
        )
        target = self.events_in(batch)[0]
        evidence = [
            item
            for item in target["context"]
            if item["event_type"] == "tool-result-evidence"
        ]

        self.assertTrue(target["linked_evidence_incomplete"])
        self.assertLessEqual(
            len(evidence), self.batches.LINKED_EVIDENCE_MAX_ROWS
        )
        self.assertIn(value, "\n".join(item["content"] for item in evidence))
        self.write_disposition(batch)
        with self.assertRaisesRegex(
            self.batches.DispositionError,
            "disposition-credential-evidence-incomplete",
        ):
            self.batches.validate_disposition(batch, self.root)

    def test_context_uses_source_offsets_when_backfill_rowids_are_newer(self):
        target_path = r"C:\users\admin\.codex\sessions\thread.jsonl"
        evidence_path = r"C:\USERS\ADMIN\.CODEX\SESSIONS\THREAD.JSONL"
        self.add_event(
            1,
            "Find the Store API key from the inspected file",
            source_offset=100,
            source_line=10,
            source_path=target_path,
        )
        self.add_event(
            2,
            "ordinary assistant response",
            role="assistant",
            source_offset=110,
            source_line=11,
            source_path=target_path,
        )
        self.add_event(
            100,
            "DOUBAO_API_KEY=alpha-credential-7429",
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="hash-001",
            source_offset=120,
            source_line=12,
            source_path=evidence_path,
        )
        self.add_event(
            101,
            "DOUBAO_API_KEY=stale-before-target-1835",
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="hash-001",
            source_offset=90,
            source_line=9,
            source_path=evidence_path,
        )

        batch = self.pending / "source-offset-context-order"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        target = self.events_in(batch)[0]
        context_ids = [
            item["source_event_rowid"] for item in target["context"]
        ]

        self.assertIn(100, context_ids)
        self.assertNotIn(101, context_ids)
        self.assertEqual(context_ids, [2, 100])
        linked = target["context"][1]
        self.assertEqual(linked["source_offset"], 120)
        self.assertEqual(linked["source_line"], 12)

    def test_context_lookup_uses_composite_source_position_indexes(self):
        self.batches.ensure_history_semantic_indexes()
        connection = sqlite3.connect(str(self.history))
        try:
            for path_predicate, expected_index in (
                (
                    "h.source_path=?",
                    "source_events_context_position_idx",
                ),
                (
                    "h.source_path=? COLLATE NOCASE",
                    "source_events_context_position_nocase_idx",
                ),
            ):
                with self.subTest(expected_index=expected_index):
                    plan = "\n".join(
                        str(row[3])
                        for row in connection.execute(
                            "EXPLAIN QUERY PLAN SELECT h.id "
                            "FROM source_events h "
                            "WHERE h.source_device_id=? AND h.source=? "
                            "AND h.thread_id=? AND h.event_type<>? AND "
                            + path_predicate
                            + " AND (h.source_offset<? OR "
                            "(h.source_offset=? AND h.source_line<?) OR "
                            "(h.source_offset=? AND h.source_line=? "
                            "AND h.id<?)) "
                            "ORDER BY h.source_offset DESC,"
                            "h.source_line DESC,h.id DESC LIMIT 2",
                            (
                                "device-test",
                                "codex",
                                "thread-codex",
                                "tool-result-evidence",
                                "/history/codex.jsonl",
                                100,
                                100,
                                1,
                                100,
                                1,
                                1,
                            ),
                        )
                    )
                    self.assertIn(expected_index, plan)
        finally:
            connection.close()

    def test_linked_evidence_lookup_uses_relation_index(self):
        self.batches.ensure_history_semantic_indexes()
        connection = sqlite3.connect(str(self.history))
        try:
            plan = "\n".join(
                str(row[3])
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT content "
                    "FROM source_events WHERE source=? AND thread_id=? "
                    "AND role='assistant' AND event_type=? "
                    "AND related_user_event_hash=?",
                    (
                        "codex",
                        "thread-codex",
                        "tool-result-evidence",
                        "hash-001",
                    ),
                )
            )
        finally:
            connection.close()
        self.assertIn("source_events_linked_evidence_idx", plan)

    def test_pre_evidence_history_schema_remains_usable_but_fail_closed(self):
        self.history.unlink()
        connection = sqlite3.connect(str(self.history))
        connection.execute(
            """
            CREATE TABLE source_events (
                id INTEGER PRIMARY KEY,
                event_hash TEXT NOT NULL UNIQUE,
                source_device_id TEXT NOT NULL,
                source TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                event_id TEXT,
                role TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_timestamp TEXT,
                ingested_at TEXT,
                cwd TEXT,
                content TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO source_events(
                id,event_hash,source_device_id,source,thread_id,event_id,role,
                event_type,event_timestamp,ingested_at,cwd,content
            ) VALUES (1,'legacy-hash','legacy-device','codex','legacy-thread',
                      'legacy-event','user','message','2026-07-22T00:00:00Z',
                      '2026-07-22T00:00:00Z','/legacy','ordinary legacy fact')
            """
        )
        connection.commit()
        connection.close()

        batch = self.pending / "legacy-evidence-schema"
        result = self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )

        self.assertEqual(result["event_count"], 1)
        event = self.events_in(batch)[0]
        self.assertIsNone(event["related_user_event_hash"])
        self.assertFalse(event["evidence_content_complete"])

    def test_incremental_checkpoint_advances_only_after_batch_completion(self):
        self.add_event(1, "first ordinary event")
        self.add_event(2, "second ordinary event")
        self.add_event(3, "third ordinary event")
        self.add_event(4, "assistant output stays searchable raw but is not a primary semantic fact", role="assistant")

        first = self.pending / "incremental-first"
        first_result = self.batches.prepare("incremental", first, limit=2, maximum_characters=10000)
        self.assertEqual(first_result["event_count"], 2)
        self.assertEqual([item["source_event_rowid"] for item in self.events_in(first)], [1, 2])

        # Preparing without completing must not advance the durable checkpoint.
        retry = self.pending / "incremental-retry"
        retry_result = self.batches.prepare("incremental", retry, limit=2, maximum_characters=10000)
        self.assertEqual(retry_result["event_count"], 2)
        self.assertEqual([item["source_event_rowid"] for item in self.events_in(retry)], [1, 2])

        self.write_disposition(first)
        completed = self.batches.mark_complete(first)
        self.assertEqual(completed["processed"], 2)
        self.assertTrue((first / ".completed").is_file())

        following = self.pending / "incremental-following"
        following_result = self.batches.prepare("incremental", following, limit=2, maximum_characters=10000)
        self.assertEqual(following_result["event_count"], 1)
        self.assertEqual([item["source_event_rowid"] for item in self.events_in(following)], [3])

        connection = sqlite3.connect(str(self.semantic))
        try:
            processed = connection.execute(
                "SELECT event_hash FROM processed_events WHERE mode='incremental' ORDER BY event_hash"
            ).fetchall()
            completed_batches = connection.execute("SELECT count(*) FROM completed_batches").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual([row[0] for row in processed], ["hash-001", "hash-002"])
        self.assertEqual(completed_batches, 1)

        # Completion is idempotent: it never manufactures a second checkpoint.
        completed_again = self.batches.mark_complete(first)
        self.assertEqual(completed_again["processed"], 0)
        connection = sqlite3.connect(str(self.semantic))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM completed_batches"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_incremental_cursor_keeps_large_processed_history_memory_bounded(self):
        # The history tail is tiny while the durable processed table is large.
        # The selector must probe that table through SQLite, never materialize
        # all hashes into a Python set.
        self.add_event(100001, "new tail event from another device", device="device-peer")
        review = self.batches.semantic_review_fingerprint(
            "a" * 64, "model", "high", "b" * 64
        )
        semantic = self.batches.connect_semantic()
        with semantic:
            semantic.executemany(
                """
                INSERT INTO processed_events(
                    event_hash,mode,batch_id,processed_at,outcome,knowledge_ids,
                    review_fingerprint
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    (
                        "old-%06d" % index,
                        "incremental",
                        "legacy-batch",
                        "2026-01-01T00:00:00Z",
                        "captured",
                        "[]",
                        review,
                    )
                    for index in range(100000)
                ),
            )
            semantic.execute(
                "INSERT INTO semantic_meta(key,value) VALUES (?,?)",
                (
                    self.batches.CURSOR_KEYS["incremental"],
                    "100000",
                ),
            )
            semantic.execute(
                "INSERT INTO semantic_meta(key,value) VALUES (?,?)",
                ("active-processed-review-fingerprint", review),
            )
        semantic.close()

        batch = self.pending / "large-steady-state"
        tracemalloc.start()
        result = self.batches.prepare(
            "incremental",
            batch,
            limit=1,
            maximum_characters=10000,
            review_fingerprint=review,
        )
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual(result["event_count"], 1)
        self.assertEqual(self.events_in(batch)[0]["source_event_rowid"], 100001)
        manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["selection"]["cursor_start"], 100000)
        self.assertEqual(manifest["selection"]["candidate_rows_examined"], 1)
        self.assertLess(
            peak,
            3 * 1024 * 1024,
            "selection memory grew with all processed hashes",
        )

    def test_legacy_processed_hash_backfills_logical_key_for_fork_alias(self):
        content = "shared preference copied into a fork"
        self.add_event(
            1,
            content,
            event_hash="legacy-original",
            thread="original",
            source_event_id="msg-shared-legacy",
        )
        self.add_event(
            2,
            content,
            event_hash="legacy-fork",
            thread="fork",
            source_event_id="msg-shared-legacy",
        )
        semantic = self.batches.connect_semantic()
        with semantic:
            semantic.execute(
                "INSERT INTO processed_events("
                "event_hash,mode,batch_id,processed_at,outcome,knowledge_ids"
                ") VALUES (?,?,?,?,?,?)",
                (
                    "legacy-original",
                    "incremental",
                    "legacy-batch",
                    "2026-01-01T00:00:00Z",
                    "captured",
                    "[]",
                ),
            )
        semantic.close()

        batch = self.pending / "legacy-logical-backfill"
        prepared = self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )

        self.assertEqual(prepared["event_count"], 1)
        manifest = json.loads(
            (batch / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["selection"]["processed_reviews_reactivated"], 1
        )
        connection = sqlite3.connect(str(self.semantic))
        try:
            logical_key = connection.execute(
                "SELECT logical_key FROM processed_events "
                "WHERE event_hash='legacy-original'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertTrue(logical_key)

    def test_priority_backfill_advances_in_bounded_empty_windows(self):
        connection = sqlite3.connect(str(self.history))
        with connection:
            connection.executemany(
                """
                INSERT INTO source_events(
                    id,event_hash,source_device_id,source,thread_id,event_id,
                    role,event_type,event_timestamp,ingested_at,cwd,content,
                    source_path,source_offset,source_line
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    (
                        index,
                        "bulk-%04d" % index,
                        "device-%d" % (index % 3),
                        "codex",
                        "thread-%d" % (index % 11),
                        "event-%04d" % index,
                        "user",
                        "message",
                        None,
                        "2026-07-22T00:00:00Z",
                        "/projects/test",
                        "普通闲聊 %d" % index,
                        "/history/bulk.jsonl",
                        index * 100,
                        index,
                    )
                    for index in range(1, 601)
                ),
            )
        connection.close()

        first = self.batches.prepare(
            "priority-backfill",
            self.pending / "priority-empty-1",
            limit=1,
            maximum_characters=10000,
        )
        second = self.batches.prepare(
            "priority-backfill",
            self.pending / "priority-empty-2",
            limit=1,
            maximum_characters=10000,
        )

        self.assertEqual(first["event_count"], 0)
        self.assertEqual(first["selection"]["candidate_rows_examined"], 512)
        self.assertEqual(first["selection"]["cursor_start"], 601)
        self.assertEqual(first["selection"]["cursor_after_empty_scan"], 89)
        self.assertEqual(second["selection"]["candidate_rows_examined"], 88)
        self.assertEqual(second["selection"]["cursor_start"], 89)
        self.assertEqual(second["selection"]["cursor_after_empty_scan"], 1)
        third = self.batches.prepare(
            "priority-backfill",
            self.pending / "priority-empty-3",
            limit=1,
            maximum_characters=10000,
        )
        self.assertEqual(third["selection"]["candidate_rows_examined"], 0)
        self.assertEqual(third["selection"]["cursor_after_empty_scan"], 0)

    def test_incremental_cursor_discovers_later_peer_device_rows(self):
        self.add_event(1, "local fact", device="device-local")
        first = self.pending / "local-first"
        self.batches.prepare("incremental", first, limit=10, maximum_characters=10000)
        self.write_disposition(first)
        completed = self.batches.mark_complete(first)
        self.assertEqual(completed["selection_cursor"], 1)

        self.add_event(2, "peer fact imported later", device="device-windows")
        peer = self.pending / "peer-later"
        result = self.batches.prepare(
            "incremental", peer, limit=10, maximum_characters=10000
        )
        self.assertEqual(result["event_count"], 1)
        event = self.events_in(peer)[0]
        self.assertEqual(event["source_event_rowid"], 2)
        self.assertEqual(event["source_device_id"], "device-windows")
        manifest = json.loads((peer / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["selection"]["cursor_start"], 1)

    def test_pending_batch_is_reused_and_completed_batch_is_not_reused(self):
        self.add_event(1, "我希望这个 pending batch 被原样重用")
        batch = self.pending / "001-priority-backfill"
        prepared = self.batches.prepare("priority-backfill", batch, limit=10, maximum_characters=10000)
        self.assertEqual(prepared["event_count"], 1)

        runner = load_script("run_semantic_maintenance")
        runner.ROOT = self.root
        runner.DATA = self.data
        runner.semantic_batches = self.batches
        manifest = json.loads(
            (batch / "manifest.json").read_text(encoding="utf-8")
        )
        with mock.patch.object(
            runner,
            "run_json",
            side_effect=AssertionError("pending reuse must not prepare or call an external process"),
        ):
            self.assertEqual(
                runner.next_batch(
                    manifest["review_fingerprint"],
                    manifest["policy_sha256"],
                ),
                batch,
            )

        self.write_disposition(batch)
        completion = self.batches.mark_complete(batch)
        self.assertEqual(completion["processed"], 1)
        self.assertTrue((batch / ".completed").exists())

        calls = []

        def no_events(arguments):
            calls.append(arguments)
            return {"ok": True, "action": "prepare", "event_count": 0, "path": None}

        # Once marked complete, next_batch skips it and probes both modes.  The
        # fake is deliberately local, so this test can never invoke Codex/network.
        with mock.patch.object(runner, "run_json", side_effect=no_events):
            self.assertIsNone(runner.next_batch())
        self.assertEqual(
            [call[2] for call in calls],
            ["incremental", "evidence-revisions", "priority-backfill"],
        )

        calls.clear()
        with mock.patch.object(runner, "run_json", side_effect=no_events):
            self.assertIsNone(
                runner.next_batch(
                    "review-fingerprint",
                    "policy-sha256",
                    ("incremental", "priority-backfill"),
                )
            )
        self.assertEqual(
            [call[2] for call in calls],
            ["incremental", "priority-backfill"],
        )

    def test_event_reviewed_in_priority_is_not_paid_for_again_incrementally(self):
        self.add_event(1, "我希望这个事件只语义处理一次")
        self.add_event(2, "ordinary second event")
        priority = self.pending / "priority-once"
        self.batches.prepare("priority-backfill", priority, limit=10, maximum_characters=10000)
        self.write_disposition(priority)
        self.batches.mark_complete(priority)

        incremental = self.pending / "incremental-after-priority"
        result = self.batches.prepare("incremental", incremental, limit=10, maximum_characters=10000)
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(self.events_in(incremental)[0]["source_event_rowid"], 2)

    def test_semantic_maintenance_rejects_unvalidated_cold_root_before_writes(self):
        runner = load_script("run_semantic_maintenance")
        maintenance_root = Path(self.temporary.name) / "maintenance-no-cold"
        maintenance_root.mkdir()
        runner.ROOT = maintenance_root
        runner.DATA = maintenance_root / ".kb-data"
        runner.REPORTS = runner.DATA / "reports"
        runner.LOCK = runner.DATA / "semantic-maintenance.lock"
        validation = mock.Mock(
            return_value={
                "valid": False,
                "mode": "external",
                "reason": "cold-data-volume-not-bound-to-device",
            }
        )
        output = io.StringIO()
        with mock.patch.object(
            runner.device_identity, "validate_cold_data_root", validation
        ), mock.patch.object(
            runner,
            "curate",
            side_effect=AssertionError("curation must not run with invalid cold storage"),
        ), contextlib.redirect_stdout(output):
            exit_code = runner.main(["--mode", "curate"])

        self.assertEqual(exit_code, 2)
        self.assertFalse(runner.DATA.exists())
        validation.assert_called_once_with(
            maintenance_root,
            require_bound_volume=True,
        )
        report = json.loads(output.getvalue())
        self.assertEqual(report["error"], "cold-data-volume-not-bound-to-device")
        self.assertEqual(report["cold_storage_mode"], "external")

    def test_semantic_maintenance_allows_valid_local_staging(self):
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = self.root
        runner.DATA = self.data
        runner.REPORTS = self.data / "reports"
        runner.LOCK = self.data / "semantic-maintenance.lock"
        validation = mock.Mock(
            return_value={
                "valid": True,
                "mode": "local-staging",
                "path": str(self.data),
            }
        )
        curated = mock.Mock(
            return_value={"ok": True, "action": "curate", "batch": None}
        )
        output = io.StringIO()
        with mock.patch.object(
            runner.device_identity, "validate_cold_data_root", validation
        ), mock.patch.object(
            runner.storage_lock,
            "acquire",
            return_value=contextlib.nullcontext(),
        ), mock.patch.object(runner, "curate", curated), contextlib.redirect_stdout(output):
            exit_code = runner.main(["--mode", "curate"])

        self.assertEqual(exit_code, 0)
        validation.assert_called_once_with(self.root, require_bound_volume=True)
        curated.assert_called_once_with()
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_semantic_maintenance_holds_workspace_lock_around_full_action(self):
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = self.root
        runner.DATA = self.data
        runner.REPORTS = self.data / "reports"
        runner.LOCK = self.data / "semantic-maintenance.lock"
        lock_state = {"held": False}

        @contextlib.contextmanager
        def workspace_gate(root, blocking=False):
            self.assertEqual(root, self.root)
            self.assertFalse(blocking)
            lock_state["held"] = True
            try:
                yield object()
            finally:
                lock_state["held"] = False

        def curated():
            self.assertTrue(lock_state["held"])
            return {"ok": True, "action": "curate", "batch": None}

        output = io.StringIO()
        with mock.patch.object(
            runner.device_identity,
            "validate_cold_data_root",
            return_value={"valid": True, "mode": "local-staging"},
        ), mock.patch.object(
            runner.storage_lock, "acquire", return_value=contextlib.nullcontext()
        ), mock.patch.object(
            runner.workspace_lock, "acquire", side_effect=workspace_gate
        ), mock.patch.object(
            runner, "curate", side_effect=curated
        ), contextlib.redirect_stdout(output):
            exit_code = runner.main(["--mode", "curate"])

        self.assertEqual(exit_code, 0)
        self.assertFalse(lock_state["held"])
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_semantic_audit_is_read_only_and_does_not_hot_sync(self):
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = self.root
        runner.DATA = self.data
        runner.REPORTS = self.data / "reports"
        runner.pre_sync_status = lambda: {"ok": True, "action": "git-sync"}
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            runner, "run_json", return_value={"ok": True}
        ), mock.patch.object(
            runner, "run_codex", return_value=0
        ) as codex, mock.patch.object(
            runner.hot_sync,
            "execute",
            side_effect=AssertionError("report-only audit must not hot-sync"),
        ), mock.patch.object(
            runner.subprocess, "run", return_value=completed
        ):
            result = runner.audit()

        self.assertTrue(result["ok"])
        self.assertTrue(result["report_only"])
        self.assertFalse(codex.call_args.args[3])

    def test_semantic_workers_pin_model_and_reasoning_despite_ignored_user_config(self):
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = self.root
        prompt = self.root / "prompt.md"
        report = self.root / "report.md"
        prompt.write_text("test prompt", encoding="utf-8")
        completed = subprocess.CompletedProcess([], 0, "", "")

        with mock.patch.object(
            runner, "codex_binary", return_value="/safe/codex"
        ), mock.patch.object(
            runner.subprocess, "run", return_value=completed
        ) as execute:
            exit_code = runner.run_codex(
                prompt,
                "suffix",
                report,
                True,
                "configured-model",
                "medium",
            )

        self.assertEqual(exit_code, 0)
        command = execute.call_args.args[0]
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--model") + 1], "configured-model")
        self.assertEqual(
            command[command.index("--config") + 1],
            'model_reasoning_effort="medium"',
        )
        self.assertEqual(execute.call_args.kwargs["encoding"], "utf-8")

        with mock.patch.object(
            runner, "codex_binary", return_value="/safe/codex"
        ), mock.patch.object(
            runner.subprocess, "run", return_value=completed
        ) as execute:
            runner.run_codex(
                prompt,
                "suffix",
                report,
                True,
                "configured-audit-model",
                "low",
            )
        command = execute.call_args.args[0]
        self.assertEqual(command[command.index("--model") + 1], "configured-audit-model")
        self.assertEqual(
            command[command.index("--config") + 1],
            'model_reasoning_effort="low"',
        )

    def test_codex_exit_classification_is_body_free_and_timeout_safe(self):
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = self.root
        prompt = self.root / "classification-prompt.md"
        report = self.root / "classification-report.md"
        prompt.write_text("test prompt", encoding="utf-8")
        unauthorized = subprocess.CompletedProcess(
            [], 1, "", "request failed: status 401 unauthorized"
        )
        with mock.patch.object(
            runner, "codex_binary", return_value="/safe/codex"
        ), mock.patch.object(
            runner.subprocess, "run", return_value=unauthorized
        ):
            exit_code = runner.run_codex(
                prompt,
                "suffix",
                report,
                False,
                runner.CURATION_MODEL,
                runner.CURATION_REASONING,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(runner.LAST_CODEX_FAILURE_CLASS, "authentication")

        unavailable = subprocess.CompletedProcess(
            [], 1, "", "upstream service unavailable: status 503"
        )
        with mock.patch.object(
            runner, "codex_binary", return_value="/safe/codex"
        ), mock.patch.object(
            runner.subprocess, "run", return_value=unavailable
        ):
            exit_code = runner.run_codex(
                prompt,
                "suffix",
                report,
                False,
                runner.CURATION_MODEL,
                runner.CURATION_REASONING,
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(runner.LAST_CODEX_FAILURE_CLASS, "service")

        with mock.patch.object(
            runner, "codex_binary", return_value="/safe/codex"
        ), mock.patch.object(
            runner.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("codex", 3600),
        ):
            exit_code = runner.run_codex(
                prompt,
                "suffix",
                report,
                False,
                runner.CURATION_MODEL,
                runner.CURATION_REASONING,
            )
        self.assertEqual(exit_code, 124)
        self.assertEqual(runner.LAST_CODEX_FAILURE_CLASS, "timeout")

    def test_semantic_audit_tests_do_not_inherit_launchagent_device_override(self):
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = self.root
        runner.DATA = self.data
        runner.REPORTS = self.data / "reports"
        runner.pre_sync_status = lambda: {"ok": True, "action": "git-sync"}
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.dict(
            runner.os.environ,
            {"KB_SOURCE_DEVICE_ID": "real-launchagent-device"},
            clear=False,
        ), mock.patch.object(
            runner, "run_json", return_value={"ok": True}
        ), mock.patch.object(
            runner, "run_codex", return_value=0
        ), mock.patch.object(
            runner.subprocess, "run", return_value=completed
        ) as run:
            result = runner.audit()

        self.assertTrue(result["ok"])
        self.assertNotIn("KB_SOURCE_DEVICE_ID", run.call_args.kwargs["env"])

    def test_semantic_workspace_contention_is_nonblocking_pending(self):
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = self.root
        runner.DATA = self.data
        runner.REPORTS = self.data / "reports"
        runner.LOCK = self.data / "semantic-maintenance.lock"
        output = io.StringIO()
        with mock.patch.object(
            runner.device_identity,
            "validate_cold_data_root",
            return_value={"valid": True, "mode": "local-staging"},
        ), mock.patch.object(
            runner.storage_lock, "acquire", return_value=contextlib.nullcontext()
        ), mock.patch.object(
            runner.workspace_lock,
            "acquire",
            side_effect=runner.workspace_lock.WorkspaceBusy("busy"),
        ), mock.patch.object(
            runner,
            "curate",
            side_effect=AssertionError("busy workspace must not start Codex"),
        ), contextlib.redirect_stdout(output):
            exit_code = runner.main(["--mode", "curate"])

        self.assertEqual(exit_code, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["pending"])
        self.assertEqual(result["skipped"], "workspace-mutation-running")

    # ------------------------------------------------------------------
    # B1: disposition gate — a batch may only be consumed with a complete,
    # per-event accounting from the curator.
    # ------------------------------------------------------------------

    def test_mark_complete_requires_disposition_file(self):
        self.add_event(1, "first ordinary event")
        batch = self.pending / "no-disposition"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)

        with self.assertRaises(self.batches.DispositionError):
            self.batches.mark_complete(batch)

        self.assertFalse((batch / ".completed").exists())
        self.assertEqual(self.semantic_counts(), (0, 0))

    def test_mark_complete_rejects_event_hash_set_mismatch(self):
        self.add_event(1, "first ordinary event")
        self.add_event(2, "second ordinary event")
        batch = self.pending / "hash-mismatch"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        document = self.write_disposition(batch)

        # One event missing from the disposition.
        self.write_disposition(batch, entries=document["dispositions"][:1])
        with self.assertRaises(self.batches.DispositionError):
            self.batches.mark_complete(batch)

        # One extra hash that is not part of this batch.
        self.write_disposition(
            batch,
            entries=document["dispositions"]
            + [{"event_hash": "hash-999", "outcome": "captured"}],
        )
        with self.assertRaises(self.batches.DispositionError):
            self.batches.mark_complete(batch)

        self.assertFalse((batch / ".completed").exists())
        self.assertEqual(self.semantic_counts(), (0, 0))

    def test_mark_complete_rejects_illegal_outcome_and_foreign_batch_id(self):
        self.add_event(1, "first ordinary event")
        batch = self.pending / "bad-outcome"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)

        self.write_disposition(batch, outcome="vaporized")
        with self.assertRaises(self.batches.DispositionError):
            self.batches.mark_complete(batch)

        self.write_disposition(batch, overrides={"batch_id": "sem-some-other-batch"})
        with self.assertRaises(self.batches.DispositionError):
            self.batches.mark_complete(batch)

        self.assertFalse((batch / ".completed").exists())
        self.assertEqual(self.semantic_counts(), (0, 0))

    def test_mark_complete_records_disposition_and_provenance(self):
        self.add_event(1, "first ordinary event")
        self.add_event(2, "second ordinary event")
        batch = self.pending / "with-provenance"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        self.write_disposition(
            batch,
            entries=[
                {
                    "event_hash": "hash-001",
                    "outcome": "captured",
                    "knowledge_ids": ["ledger:evt-1"],
                    "note": "durable preference",
                },
                {
                    "event_hash": "hash-002",
                    "outcome": "captured",
                    "knowledge_ids": ["ledger:evt-2"],
                },
            ],
        )

        result = self.batches.mark_complete(
            batch, model="model-x", reasoning_effort="high", prompt_sha256="f" * 64
        )

        self.assertEqual(result["processed"], 2)
        expected_sha = hashlib.sha256((batch / "disposition.json").read_bytes()).hexdigest()
        self.assertEqual(result["disposition_sha256"], expected_sha)
        connection = sqlite3.connect(str(self.semantic))
        try:
            events = connection.execute(
                "SELECT event_hash,outcome,knowledge_ids FROM processed_events ORDER BY event_hash"
            ).fetchall()
            provenance = connection.execute(
                "SELECT model,reasoning_effort,prompt_sha256,disposition_sha256 FROM completed_batches"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(
            [tuple(row) for row in events],
            [
                ("hash-001", "captured", '["ledger:evt-1"]'),
                ("hash-002", "captured", '["ledger:evt-2"]'),
            ],
        )
        self.assertEqual(tuple(provenance), ("model-x", "high", "f" * 64, expected_sha))

    def test_deferred_is_quarantined_without_blocking_same_review(self):
        self.add_event(1, "那就用上一个，需要更多上下文才能知道指什么")
        first = self.pending / "deferred-first"
        self.batches.prepare("incremental", first, limit=10, maximum_characters=10000)
        self.write_disposition(first, outcome="deferred")

        completion = self.batches.mark_complete(first)

        self.assertEqual(completion["processed"], 0)
        self.assertEqual(completion["deferred"], 1)
        self.assertEqual(completion["quarantined"], 1)
        self.assertEqual(completion["selection_cursor"], 1)
        self.assertTrue((first / ".completed").exists())
        self.assertEqual(self.semantic_counts(), (0, 1))
        retry = self.pending / "deferred-next-run"
        prepared = self.batches.prepare(
            "incremental", retry, limit=10, maximum_characters=10000
        )
        self.assertEqual(prepared["event_count"], 0)
        statistics = self.batches.stats()
        self.assertEqual(statistics["backlog_user_events"], 0)
        self.assertEqual(statistics["deferred_user_events"], 1)

    def test_review_fingerprint_change_rewinds_deferred_once(self):
        self.add_event(1, "那就用上一个，需要更多上下文才能知道指什么")
        first_review = self.batches.semantic_review_fingerprint(
            "a" * 64, "model-a", "high"
        )
        first = self.pending / "deferred-first-review"
        self.batches.prepare(
            "incremental",
            first,
            limit=10,
            maximum_characters=10000,
            review_fingerprint=first_review,
        )
        self.write_disposition(first, outcome="deferred")
        self.batches.mark_complete(first)

        second_review = self.batches.semantic_review_fingerprint(
            "b" * 64, "model-a", "high"
        )
        retry = self.pending / "deferred-new-review"
        prepared = self.batches.prepare(
            "incremental",
            retry,
            limit=10,
            maximum_characters=10000,
            review_fingerprint=second_review,
        )

        self.assertEqual(prepared["event_count"], 1)
        self.assertEqual(self.events_in(retry)[0]["event_hash"], "hash-001")
        manifest = json.loads(
            (retry / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["selection"]["deferred_reviews_reactivated"], 1
        )

    def test_review_fingerprint_change_rewinds_processed_tail_once(self):
        self.add_event(1, "standalone processed fact")
        first_review = self.batches.semantic_review_fingerprint(
            "a" * 64, "model-a", "high"
        )
        first = self.pending / "processed-first-review"
        self.batches.prepare(
            "incremental",
            first,
            limit=10,
            maximum_characters=10000,
            review_fingerprint=first_review,
        )
        self.write_disposition(first)
        self.batches.mark_complete(first)
        self.assertEqual(self.batches.selection_cursor("incremental"), 1)

        second_review = self.batches.semantic_review_fingerprint(
            "b" * 64, "model-a", "high"
        )
        second = self.pending / "processed-second-review"
        prepared = self.batches.prepare(
            "incremental",
            second,
            limit=10,
            maximum_characters=10000,
            review_fingerprint=second_review,
        )
        self.assertEqual(prepared["event_count"], 1)
        manifest = json.loads(
            (second / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["selection"]["processed_reviews_reactivated"], 1
        )
        self.write_disposition(
            second,
            entries=[
                {
                    "event_hash": "hash-001",
                    "outcome": "captured",
                    "knowledge_ids": ["test-knowledge-second-review"],
                }
            ],
        )
        self.batches.mark_complete(second)

        unchanged = self.pending / "processed-same-review"
        result = self.batches.prepare(
            "incremental",
            unchanged,
            limit=10,
            maximum_characters=10000,
            review_fingerprint=second_review,
        )
        self.assertEqual(result["event_count"], 0)

    def test_policy_hash_change_rewinds_deferred_once_and_is_frozen(self):
        self.add_event(1, "那就用上一个，需要更多上下文才能知道指什么")
        first_policy = hashlib.sha256(
            (self.root / "POLICY.md").read_bytes()
        ).hexdigest()
        first_review = self.batches.semantic_review_fingerprint(
            "a" * 64, "model-a", "high", first_policy
        )
        first = self.pending / "deferred-first-policy"
        self.batches.prepare(
            "incremental",
            first,
            limit=10,
            maximum_characters=10000,
            review_fingerprint=first_review,
            policy_sha256=first_policy,
        )
        self.write_disposition(first, outcome="deferred")
        self.batches.mark_complete(first)

        (self.root / "POLICY.md").write_text(
            "# Changed test policy\n", encoding="utf-8"
        )
        second_policy = hashlib.sha256(
            (self.root / "POLICY.md").read_bytes()
        ).hexdigest()
        second_review = self.batches.semantic_review_fingerprint(
            "a" * 64, "model-a", "high", second_policy
        )
        retry = self.pending / "deferred-new-policy"
        prepared = self.batches.prepare(
            "incremental",
            retry,
            limit=10,
            maximum_characters=10000,
            review_fingerprint=second_review,
            policy_sha256=second_policy,
        )
        manifest = json.loads(
            (retry / "manifest.json").read_text(encoding="utf-8")
        )

        self.assertEqual(prepared["event_count"], 1)
        self.assertEqual(manifest["policy_sha256"], second_policy)
        self.assertEqual(
            manifest["selection"]["deferred_reviews_reactivated"], 1
        )
        self.write_disposition(retry, outcome="deferred")
        self.batches.mark_complete(retry)
        unchanged = self.pending / "deferred-same-policy"
        unchanged_result = self.batches.prepare(
            "incremental",
            unchanged,
            limit=10,
            maximum_characters=10000,
            review_fingerprint=second_review,
            policy_sha256=second_policy,
        )
        self.assertEqual(unchanged_result["event_count"], 0)

        with self.assertRaisesRegex(RuntimeError, "policy changed"):
            self.batches.prepare(
                "incremental",
                self.pending / "stale-policy-sha",
                limit=10,
                maximum_characters=10000,
                review_fingerprint=second_review,
                policy_sha256=first_policy,
            )

    def test_captured_requires_real_id_and_current_event_provenance(self):
        self.add_event(1, "服务器端口是 8080")
        batch = self.pending / "untraceable-capture"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        entries = [
            {
                "event_hash": "hash-001",
                "outcome": "captured",
                "knowledge_ids": ["does-not-exist"],
            }
        ]
        self.write_disposition(batch, entries=entries, seed_knowledge=False)
        with self.assertRaisesRegex(
            self.batches.DispositionError, "knowledge-id-unresolved"
        ):
            self.batches.mark_complete(batch)

        wrong = self.root / "ledger" / "test" / "does-not-exist.json"
        wrong.parent.mkdir(parents=True, exist_ok=True)
        wrong.write_text(
            json.dumps({"id": "does-not-exist", "source": "some-other-event"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            self.batches.DispositionError, "event-provenance-missing"
        ):
            self.batches.mark_complete(batch)
        self.assertEqual(self.semantic_counts(), (0, 0))

    def test_non_knowledge_is_limited_to_strict_acknowledgements(self):
        self.add_event(1, "继续，另外记住服务器端口是 8080")
        batch = self.pending / "not-a-pure-ack"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        self.write_disposition(
            batch,
            entries=[
                {
                    "event_hash": "hash-001",
                    "outcome": "non-knowledge",
                    "knowledge_ids": [],
                    "reason_code": "pure-acknowledgement-or-continuation",
                    "evidence": "继续，另外记住服务器端口是 8080",
                }
            ],
        )
        with self.assertRaisesRegex(
            self.batches.DispositionError,
            "non-knowledge-not-strict-acknowledgement",
        ):
            self.batches.mark_complete(batch)

        # Finish the first target with a real capture so the next incremental
        # batch isolates the exact acknowledgement.
        self.write_disposition(batch)
        self.batches.mark_complete(batch)
        self.add_event(2, "继续")
        pure = self.pending / "pure-ack"
        self.batches.prepare(
            "incremental", pure, limit=1, maximum_characters=10000
        )
        self.write_disposition(
            pure,
            entries=[
                {
                    "event_hash": "hash-002",
                    "outcome": "non-knowledge",
                    "knowledge_ids": [],
                    "reason_code": "pure-acknowledgement-or-continuation",
                    "evidence": "继续",
                }
            ],
        )
        self.assertEqual(self.batches.mark_complete(pure)["processed"], 1)

    def test_reopen_returns_events_for_precise_reaudit(self):
        self.add_event(1, "first ordinary event")
        batch = self.pending / "to-reopen"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        self.write_disposition(batch)
        self.batches.mark_complete(batch)
        consumed = self.pending / "after-complete"
        self.assertEqual(
            self.batches.prepare("incremental", consumed, limit=10, maximum_characters=10000)["event_count"],
            0,
        )

        result = self.batches.reopen(batch)

        self.assertEqual(result["removed_processed_events"], 1)
        self.assertEqual(result["removed_completed_batches"], 1)
        self.assertEqual(result["rewound_cursors"]["incremental"], 0)
        self.assertFalse((batch / ".completed").exists())
        # The stale disposition must go too, otherwise a re-run whose curator
        # silently does nothing would pass validation with the old file.
        self.assertFalse((batch / "disposition.json").exists())
        self.assertEqual(self.semantic_counts(), (0, 0))

        reaudit = self.pending / "reaudit"
        again = self.batches.prepare("incremental", reaudit, limit=10, maximum_characters=10000)
        self.assertEqual(again["event_count"], 1)
        self.assertEqual(self.events_in(reaudit)[0]["event_hash"], "hash-001")

    def test_legacy_semantic_schema_is_migrated_idempotently(self):
        connection = sqlite3.connect(str(self.semantic))
        connection.executescript(
            """
            CREATE TABLE processed_events (
                event_hash TEXT NOT NULL,
                mode TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY(event_hash, mode)
            );
            CREATE TABLE semantic_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE completed_batches (
                batch_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                event_count INTEGER NOT NULL,
                payload_sha256 TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO processed_events VALUES ('hash-000','incremental','sem-legacy','2026-01-01T00:00:00Z')"
        )
        connection.commit()
        connection.close()

        migrated = self.batches.connect_semantic()
        try:
            processed_columns = {row[1] for row in migrated.execute("PRAGMA table_info(processed_events)")}
            batch_columns = {row[1] for row in migrated.execute("PRAGMA table_info(completed_batches)")}
            legacy = migrated.execute(
                "SELECT event_hash,outcome FROM processed_events WHERE batch_id='sem-legacy'"
            ).fetchone()
        finally:
            migrated.close()
        self.assertLessEqual(
            {"outcome", "knowledge_ids", "logical_key"}, processed_columns
        )
        self.assertLessEqual(
            {"model", "reasoning_effort", "prompt_sha256", "disposition_sha256"}, batch_columns
        )
        self.assertEqual(tuple(legacy), ("hash-000", None))

        # A second connect is a no-op and the upgraded database accepts new
        # completions with full provenance.
        self.add_event(1, "first ordinary event")
        batch = self.pending / "after-migration"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        self.write_disposition(batch)
        completed = self.batches.mark_complete(
            batch, model="model-x", reasoning_effort="high", prompt_sha256="f" * 64
        )
        self.assertEqual(completed["processed"], 1)

    # ------------------------------------------------------------------
    # B2: throughput — stats backlog and the multi-batch curation loop.
    # ------------------------------------------------------------------

    def test_stats_reports_user_event_backlog(self):
        self.add_event(1, "first ordinary event")
        self.add_event(2, "second ordinary event")
        self.add_event(3, "third ordinary event")
        self.add_event(4, "assistant reply stays out of the backlog", role="assistant")
        batch = self.pending / "one-of-three"
        self.batches.prepare("incremental", batch, limit=1, maximum_characters=10000)
        self.write_disposition(batch)
        self.batches.mark_complete(batch)

        result = self.batches.stats()
        self.assertEqual(result["completed_batches"], 1)
        self.assertEqual(result["backlog_user_events"], 2)

        self.batches.HISTORY = self.data / "missing-history.sqlite"
        self.assertIsNone(self.batches.stats()["backlog_user_events"])

    def test_stats_union_does_not_double_count_deferred_evidence_revision(self):
        self.add_event(1, "Use the earlier account; it is still unclear")
        batch = self.pending / "deferred-before-stats-revision"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        self.write_disposition(batch, outcome="deferred")
        self.batches.mark_complete(batch)
        self.add_event(
            2,
            "new exact linked evidence",
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="hash-001",
        )

        result = self.batches.stats()

        self.assertEqual(result["backlog_user_events"], 1)
        self.assertEqual(result["deferred_user_events"], 1)
        self.assertEqual(result["deferred_only_user_events"], 0)
        self.assertEqual(result["total_pending_user_events"], 1)
        self.assertEqual(result["evidence_revision_backlog"], 1)

    def test_curate_loops_until_backlog_empty_with_disposition_chain(self):
        for index in (1, 2, 3):
            self.add_event(index, "ordinary conversational message %d" % index)
        runner = self.load_runner()
        mark_calls = []
        hot = mock.Mock(return_value={"ok": True, "commit": "abc", "sync": {"ok": True}})
        with mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json(mark_calls)
        ), mock.patch.object(
            runner, "run_codex", side_effect=self.codex_writing_disposition()
        ) as codex, mock.patch.object(
            runner.hot_sync, "execute", hot
        ):
            result = runner.curate()

        self.assertTrue(result["ok"])
        self.assertEqual(result["batches_completed"], 3)
        self.assertEqual(result["events_processed"], 3)
        self.assertEqual(result["backlog_remaining"], 0)
        self.assertEqual(result["stop_reason"], "no-pending")
        self.assertEqual(codex.call_count, 3)
        self.assertEqual(hot.call_count, 3)

        prompt_sha = hashlib.sha256(self.prompt.read_bytes()).hexdigest()
        self.assertEqual(mark_calls[0]["model"], runner.CURATION_MODEL)
        self.assertEqual(mark_calls[0]["reasoning_effort"], runner.CURATION_REASONING)
        self.assertEqual(mark_calls[0]["prompt_sha256"], prompt_sha)
        connection = sqlite3.connect(str(self.semantic))
        try:
            counts = connection.execute(
                "SELECT count(*), count(DISTINCT batch_id) FROM processed_events"
            ).fetchone()
            provenance = connection.execute(
                "SELECT count(*) FROM completed_batches WHERE model=? AND prompt_sha256=?",
                (runner.CURATION_MODEL, prompt_sha),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(tuple(counts), (3, 3))
        self.assertEqual(provenance, 3)

    def test_curate_quarantines_deferred_and_continues_other_events(self):
        self.add_event(1, "那就用上一个，需要更多上下文")
        self.add_event(2, "ordinary reusable fact")
        runner = self.load_runner()

        def classify(prompt_file, suffix, report, workspace_write, model, reasoning_effort):
            batch = Path(suffix.split("`")[1])
            manifest = json.loads(
                (batch / "manifest.json").read_text(encoding="utf-8")
            )
            event = self.events_in(batch)[0]
            if event["source_event_rowid"] == 1:
                plan = {
                    "event_hash": event["event_hash"],
                    "outcome": "deferred",
                    "captures": [],
                    "existing_ids": [],
                    "reason_code": "unresolved-reference",
                    "evidence": "上一个",
                }
            else:
                plan = {
                    "event_hash": event["event_hash"],
                    "outcome": "captured",
                    "captures": [
                        {
                            "kind": "fact",
                            "scope": "test",
                            "statement": event["content"],
                            "authority": "user-explicit",
                            "status": "active",
                        }
                    ],
                    "existing_ids": [],
                }
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps(
                    {
                        "format": self.batches.PROPOSAL_FORMAT,
                        "batch_id": manifest["batch_id"],
                        "events": [plan],
                    }
                ),
                encoding="utf-8",
            )
            return 0

        with mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json()
        ), mock.patch.object(
            runner, "run_codex", side_effect=classify
        ), mock.patch.object(
            runner.hot_sync,
            "execute",
            return_value={"ok": True, "commit": None, "sync": {"ok": True}},
        ):
            result = runner.curate()

        self.assertTrue(result["ok"])
        self.assertEqual(result["batches_completed"], 2)
        self.assertEqual(result["events_processed"], 1)
        self.assertEqual(result["events_deferred"], 1)
        self.assertEqual(result["backlog_remaining"], 0)
        self.assertEqual(result["deferred_remaining"], 1)
        self.assertEqual(result["stop_reason"], "deferred-quarantined")

    def test_curate_refreshes_live_index_before_health_gate(self):
        self.add_event(1, "ordinary conversational message")
        runner = self.load_runner()
        calls = []
        fake_run_json = self.runner_run_json()

        def recording_run_json(arguments):
            if arguments[0].endswith("kb.py"):
                calls.append(arguments[-1])
            return fake_run_json(arguments)

        with mock.patch.object(
            runner, "run_json", side_effect=recording_run_json
        ), mock.patch.object(
            runner, "run_codex", side_effect=self.codex_writing_disposition()
        ), mock.patch.object(
            runner.hot_sync,
            "execute",
            return_value={"ok": True, "commit": "abc", "sync": {"ok": True}},
        ):
            result = runner.curate()

        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["update-index"])

    def test_curate_respects_maximum_batches_per_run(self):
        for index in (1, 2, 3):
            self.add_event(index, "ordinary conversational message %d" % index)
        runner = self.load_runner()
        runner.MAXIMUM_BATCHES_PER_RUN = 2
        hot = mock.Mock(return_value={"ok": True, "commit": "abc", "sync": {"ok": True}})
        with mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json()
        ), mock.patch.object(
            runner, "run_codex", side_effect=self.codex_writing_disposition()
        ) as codex, mock.patch.object(
            runner.hot_sync, "execute", hot
        ):
            result = runner.curate()

        self.assertTrue(result["ok"])
        self.assertEqual(result["batches_completed"], 2)
        self.assertEqual(result["stop_reason"], "batch-limit")
        self.assertEqual(result["backlog_remaining"], 1)
        self.assertEqual(codex.call_count, 2)

    def test_curate_respects_time_budget(self):
        self.add_event(1, "ordinary conversational message 1")
        self.add_event(2, "ordinary conversational message 2")
        runner = self.load_runner()
        clock = {"calls": 0}

        def fake_monotonic():
            clock["calls"] += 1
            return 0.0 if clock["calls"] <= 2 else float(10 ** 9)

        hot = mock.Mock(return_value={"ok": True, "commit": "abc", "sync": {"ok": True}})
        with mock.patch.object(
            runner.time, "monotonic", side_effect=fake_monotonic
        ), mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json()
        ), mock.patch.object(
            runner, "run_codex", side_effect=self.codex_writing_disposition()
        ) as codex, mock.patch.object(
            runner.hot_sync, "execute", hot
        ):
            result = runner.curate()

        self.assertTrue(result["ok"])
        self.assertEqual(result["batches_completed"], 1)
        self.assertEqual(result["stop_reason"], "time-budget")
        self.assertEqual(result["backlog_remaining"], 1)
        self.assertEqual(codex.call_count, 1)

    def test_curate_makes_zero_model_calls_when_no_pending(self):
        runner = self.load_runner()
        with mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json()
        ), mock.patch.object(
            runner,
            "run_codex",
            side_effect=AssertionError("no pending batch must mean no model call"),
        ), mock.patch.object(
            runner.hot_sync,
            "execute",
            side_effect=AssertionError("no pending batch must mean no sync"),
        ):
            result = runner.curate()

        self.assertTrue(result["ok"])
        self.assertEqual(result["batches_completed"], 0)
        self.assertEqual(result["events_processed"], 0)
        self.assertEqual(result["message"], "no pending semantic events")
        self.assertEqual(result["backlog_remaining"], 0)

    def test_curate_stops_after_one_recovered_transaction(self):
        self.add_event(1, "first ordinary event")
        self.add_event(2, "second ordinary event")
        runner = self.load_runner()
        policy_sha256 = hashlib.sha256(
            (self.root / "POLICY.md").read_bytes()
        ).hexdigest()
        review_fingerprint = self.batches.semantic_review_fingerprint(
            hashlib.sha256(self.prompt.read_bytes()).hexdigest(),
            runner.CURATION_MODEL,
            runner.CURATION_REASONING,
            policy_sha256,
        )
        batch = self.pending / "recover-one"
        self.batches.prepare(
            "incremental",
            batch,
            limit=1,
            maximum_characters=10000,
            review_fingerprint=review_fingerprint,
            policy_sha256=policy_sha256,
        )
        disposition = self.write_disposition(batch)
        manifest = json.loads(
            (batch / "manifest.json").read_text(encoding="utf-8")
        )
        transaction = {
            "format": runner.TRANSACTION_FORMAT,
            "batch_id": manifest["batch_id"],
            "base_commit": "base",
            "commit": "commit",
            "prepared_ref": "refs/ai-knowledge-semantic/recover-one",
            "events_sha256": manifest["events_sha256"],
            "disposition": disposition,
            "state": "prepared",
        }
        (batch / runner.TRANSACTION_FILE).write_text(
            json.dumps(transaction), encoding="utf-8"
        )
        hot = mock.Mock(
            return_value={"ok": True, "commit": "abc", "sync": {"ok": True}}
        )

        with mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json()
        ), mock.patch.object(
            runner,
            "run_codex",
            side_effect=AssertionError(
                "a recovered transaction must not call the model again"
            ),
        ), mock.patch.object(runner.hot_sync, "execute", hot):
            result = runner.curate()

        self.assertTrue(result["ok"])
        self.assertEqual(result["batches_completed"], 1)
        self.assertEqual(result["events_processed"], 1)
        self.assertEqual(result["backlog_remaining"], 1)
        self.assertEqual(result["stop_reason"], "recovered-transaction")
        hot.assert_called_once()

    def test_curate_without_disposition_keeps_batch_pending(self):
        self.add_event(1, "first ordinary event")
        runner = self.load_runner()
        hot = mock.Mock(return_value={"ok": True, "commit": "abc", "sync": {"ok": True}})
        with mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json()
        ), mock.patch.object(
            runner, "run_codex", return_value=0
        ), mock.patch.object(
            runner.hot_sync, "execute", hot
        ):
            result = runner.curate()

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"])
        self.assertEqual(result["stop_reason"], "retry-blocked")
        self.assertEqual(result["retry_blocked_remaining"], 1)
        self.assertEqual(result["queue_pending_remaining"], 0)
        self.assertEqual(result["batches_completed"], 0)
        self.assertEqual(result["failure_attempts"][0]["stage"], "proposal")
        self.assertEqual(
            result["failure_attempts"][0]["strategy"], "retry-blocked"
        )
        hot.assert_not_called()
        blocked = list(
            (self.data / "semantic" / "retry-blocked").iterdir()
        )
        self.assertEqual(len(blocked), 1)
        batch = blocked[0]
        self.assertFalse((batch / ".completed").exists())
        self.assertEqual(self.semantic_counts(), (0, 0))
        retry = self.batches.retry_batch_state(batch)
        self.assertIsNotNone(retry)
        self.assertFalse(retry["due"])
        self.assertEqual(self.events_in(batch)[0]["content"], "first ordinary event")

    def test_failure_backoff_persists_and_increases_across_reload(self):
        self.add_event(1, "poison event body must stay out of retry metadata")
        batch = self.pending / "persistent-poison"
        self.batches.prepare(
            "incremental",
            batch,
            limit=1,
            maximum_characters=10000,
            review_fingerprint="review-a",
        )
        failed_at = self.batches._utc_datetime()
        first = self.batches.record_batch_failure(
            batch,
            "proposal",
            "proposal-unreadable",
            failed_at=failed_at,
        )
        self.assertEqual(first["retry_blocked"]["attempts"], 1)

        reloaded = load_script("semantic_batches")
        reloaded.ROOT = self.root
        reloaded.DATA = self.data
        reloaded.HISTORY = self.history
        reloaded.SEMANTIC = self.semantic
        reloaded.PENDING = self.pending
        state = reloaded.retry_batch_state(batch, now=failed_at)
        self.assertIsNotNone(state)
        self.assertEqual(state["attempts"], 1)
        self.assertFalse(state["due"])
        second = reloaded.record_batch_failure(
            batch,
            "proposal",
            "proposal-unreadable",
            failed_at=failed_at + reloaded.dt.timedelta(seconds=1),
        )
        self.assertEqual(second["retry_blocked"]["attempts"], 2)
        next_retry = reloaded.dt.datetime.fromisoformat(
            second["retry_blocked"]["next_retry_at"].replace("Z", "+00:00")
        )
        self.assertEqual(
            int(
                (
                    next_retry
                    - (failed_at + reloaded.dt.timedelta(seconds=1))
                ).total_seconds()
            ),
            reloaded.RETRY_BASE_SECONDS * 2,
        )
        connection = sqlite3.connect(str(self.semantic))
        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(semantic_failure_attempts)"
                )
            }
            attempts = connection.execute(
                "SELECT count(*) FROM semantic_failure_attempts"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotIn("failure_detail", columns)
        self.assertNotIn("content", columns)
        self.assertEqual(attempts, 2)

    def test_bisection_recovery_quarantines_parent_before_publishing_children(self):
        for index in range(1, 5):
            self.add_event(index, "split event %d" % index)
        parent = self.pending / "crash-before-parent-isolated"
        self.batches.prepare(
            "incremental",
            parent,
            limit=4,
            maximum_characters=10000,
            review_fingerprint="review-split",
        )
        first = self.batches.prepare_batch_bisection(parent)
        second = self.batches.prepare_batch_bisection(parent)
        self.assertEqual(first["split_id"], second["split_id"])
        self.assertEqual(first["children"], second["children"])
        # A crash here leaves only the parent in the live queue. Staged child
        # directories are outside pending and cannot race their parent.
        self.assertEqual(
            [path.name for path in self.pending.iterdir() if path.is_dir()],
            [parent.name],
        )

        runner = self.load_runner()
        recovery = runner.recover_interrupted_bisections()

        self.assertEqual(recovery["children_published"], 2)
        live = sorted(
            path
            for path in self.pending.iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        )
        self.assertEqual(len(live), 2)
        self.assertNotIn(parent, live)
        quarantined = list(
            (self.data / "semantic" / "quarantined").iterdir()
        )
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(
            sum(len(self.events_in(path)) for path in live), 4
        )
        boundaries = [
            json.loads((path / "manifest.json").read_text(encoding="utf-8"))[
                "selection"
            ]["cursor_boundary_rowid"]
            for path in live
        ]
        self.assertEqual(boundaries, [2, 4])

    def test_bisection_recovery_resumes_after_first_child_publish(self):
        self.add_event(1, "first split child")
        self.add_event(2, "second split child")
        parent = self.pending / "crash-mid-publish"
        self.batches.prepare(
            "incremental",
            parent,
            limit=2,
            maximum_characters=10000,
            review_fingerprint="review-split",
        )
        prepared = self.batches.prepare_batch_bisection(parent)
        runner = self.load_runner()
        isolated = runner.quarantine_pending_batch(
            parent, "poison-bisected-test"
        )
        first_child = prepared["children"][0]
        staging = (
            self.data
            / "semantic"
            / "split-staging"
            / prepared["split_id"]
            / first_child["name"]
        )
        os.replace(staging, self.pending / first_child["name"])

        recovery = runner.recover_interrupted_bisections()

        self.assertEqual(recovery["children_published"], 2)
        live = [
            path
            for path in self.pending.iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        ]
        self.assertEqual(len(live), 2)
        self.assertTrue((isolated / self.batches.SPLIT_PLAN_FILE).is_file())
        plan = json.loads(
            (isolated / self.batches.SPLIT_PLAN_FILE).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(plan["state"], "published")

    def test_codex_infrastructure_exit_keeps_batch_live_without_poisoning(self):
        self.add_event(1, "first infrastructure-delayed event")
        self.add_event(2, "second infrastructure-delayed event")
        runner = self.load_runner()
        with mock.patch.object(
            runner,
            "run_json",
            side_effect=self.runner_run_json(prepare_limit=2),
        ), mock.patch.object(
            runner, "run_codex", return_value=75
        ), mock.patch.object(
            runner.hot_sync,
            "execute",
            side_effect=AssertionError("infrastructure failure must not sync"),
        ):
            result = runner.curate()

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"])
        self.assertEqual(result["stop_reason"], "codex-infrastructure-runtime")
        self.assertEqual(result["infrastructure_retry"]["failure_class"], "runtime")
        self.assertEqual(result["batches_bisected"], 0)
        self.assertEqual(result["retry_blocked_remaining"], 0)
        self.assertEqual(result["queue_pending_remaining"], 2)
        live = [
            path
            for path in self.pending.iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        ]
        self.assertEqual(len(live), 1)
        self.assertEqual(len(self.events_in(live[0])), 2)
        self.assertFalse((self.data / "semantic" / "quarantined").exists())
        self.assertFalse((self.data / "semantic" / "retry-blocked").exists())

    def test_stats_failure_reports_unknown_and_cannot_return_green(self):
        self.add_event(1, "completed before stats became unavailable")
        runner = self.load_runner()
        normal = self.runner_run_json()

        def fail_stats(arguments):
            if "--stats" in arguments:
                raise RuntimeError("stats unavailable")
            return normal(arguments)

        with mock.patch.object(
            runner, "run_json", side_effect=fail_stats
        ), mock.patch.object(
            runner,
            "run_codex",
            side_effect=self.codex_writing_disposition(),
        ), mock.patch.object(
            runner.hot_sync,
            "execute",
            return_value={"ok": True, "commit": "abc", "sync": {"ok": True}},
        ):
            result = runner.curate()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "semantic-stats-unavailable")
        self.assertEqual(result["queue_state"], "unknown")
        self.assertIsNone(result["queue_pending_remaining"])
        self.assertEqual(result["events_processed"], 1)

    def test_poison_batch_bisects_blocks_one_revision_and_processes_sibling(self):
        self.add_event(1, "poison event")
        self.add_event(2, "healthy sibling")
        runner = self.load_runner()

        def classify(prompt_file, suffix, report, workspace_write, model, reasoning_effort):
            batch = Path(suffix.split("`")[1])
            events = self.events_in(batch)
            if len(events) > 1 or events[0]["content"] == "poison event":
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text("{}", encoding="utf-8")
                return 0
            return self.codex_writing_disposition()(prompt_file, suffix, report, workspace_write, model, reasoning_effort)

        hot = mock.Mock(
            return_value={"ok": True, "commit": "abc", "sync": {"ok": True}}
        )
        with mock.patch.object(
            runner,
            "run_json",
            side_effect=self.runner_run_json(prepare_limit=2),
        ), mock.patch.object(
            runner, "run_codex", side_effect=classify
        ), mock.patch.object(runner.hot_sync, "execute", hot):
            result = runner.curate()

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"])
        self.assertEqual(result["events_processed"], 1)
        self.assertEqual(result["batches_completed"], 1)
        self.assertEqual(result["batches_bisected"], 1)
        self.assertEqual(result["retry_blocked_remaining"], 1)
        self.assertEqual(result["queue_pending_remaining"], 0)
        self.assertEqual(result["stop_reason"], "retry-blocked")
        self.assertEqual(
            [item["strategy"] for item in result["failure_attempts"]],
            ["bisected", "retry-blocked"],
        )
        hot.assert_called_once()
        quarantined = list(
            (self.data / "semantic" / "quarantined").iterdir()
        )
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(
            [event["content"] for event in self.events_in(quarantined[0])],
            ["poison event", "healthy sibling"],
        )
        retry_batches = list(
            (self.data / "semantic" / "retry-blocked").iterdir()
        )
        self.assertEqual(len(retry_batches), 1)
        self.assertEqual(
            self.events_in(retry_batches[0])[0]["content"], "poison event"
        )

    def test_transaction_failure_is_bisected_without_blocking_sibling(self):
        self.add_event(1, "transaction poison")
        self.add_event(2, "transaction sibling")
        runner = self.load_runner()

        def apply(batch, transaction, *contract):
            events = self.events_in(batch)
            if len(events) > 1 or events[0]["content"] == "transaction poison":
                # load_runner's lightweight transaction stub seeds knowledge
                # directly in the fixture root.  Real staging uses a detached
                # worktree, so remove that test-only artifact on failure.
                for event in events:
                    for path in (self.root / "ledger" / "test").glob(
                        "test-knowledge-%s-*.json" % event["event_hash"]
                    ):
                        path.unlink()
                raise runner.CurationTransactionError(
                    "transaction-base-advanced"
                )
            self.batches.write_disposition(
                batch, transaction["disposition"]
            )
            self.batches.validate_disposition(batch, self.root)
            return {**transaction, "state": "applied"}

        with mock.patch.object(
            runner,
            "run_json",
            side_effect=self.runner_run_json(prepare_limit=2),
        ), mock.patch.object(
            runner,
            "run_codex",
            side_effect=self.codex_writing_disposition(),
        ), mock.patch.object(
            runner, "apply_prepared_transaction", side_effect=apply
        ), mock.patch.object(
            runner.hot_sync,
            "execute",
            return_value={"ok": True, "commit": "abc", "sync": {"ok": True}},
        ):
            result = runner.curate()

        self.assertTrue(result["ok"])
        self.assertEqual(result["events_processed"], 1)
        self.assertEqual(result["retry_blocked_remaining"], 1)
        self.assertEqual(result["queue_pending_remaining"], 0)
        self.assertEqual(result["batches_bisected"], 1)
        self.assertEqual(
            [item["stage"] for item in result["failure_attempts"]],
            ["transaction-apply", "transaction-apply"],
        )

    def test_review_contract_change_reactivates_retry_before_timer(self):
        self.add_event(1, "retry after review contract changes")
        runner = self.load_runner()
        with mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json()
        ), mock.patch.object(
            runner, "run_codex", return_value=0
        ), mock.patch.object(
            runner.hot_sync,
            "execute",
            side_effect=AssertionError("failed proposal must not sync"),
        ):
            first = runner.curate()
        self.assertEqual(first["retry_blocked_remaining"], 1)

        self.prompt.write_text(
            "# Updated curator contract for retry\n", encoding="utf-8"
        )
        runner = self.load_runner()
        with mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json()
        ), mock.patch.object(
            runner,
            "run_codex",
            side_effect=self.codex_writing_disposition(),
        ), mock.patch.object(
            runner.hot_sync,
            "execute",
            return_value={"ok": True, "commit": "abc", "sync": {"ok": True}},
        ):
            second = runner.curate()

        self.assertTrue(second["ok"])
        self.assertEqual(second["events_processed"], 1)
        self.assertEqual(second["retry_blocked_remaining"], 0)
        self.assertEqual(self.semantic_counts(), (1, 1))
        quarantined = list(
            (self.data / "semantic" / "quarantined").iterdir()
        )
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(
            self.events_in(quarantined[0])[0]["content"],
            "retry after review contract changes",
        )

    def test_context_change_reactivates_retry_before_timer(self):
        self.add_event(1, "Use that account; the reference is still unclear")
        runner = self.load_runner()
        with mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json()
        ), mock.patch.object(
            runner, "run_codex", return_value=0
        ), mock.patch.object(
            runner.hot_sync,
            "execute",
            side_effect=AssertionError("failed classifier must not sync"),
        ):
            first = runner.curate()
        self.assertEqual(first["retry_blocked_remaining"], 1)

        self.add_event(2, "That account means the primary test account")
        runner = self.load_runner()
        with mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json()
        ), mock.patch.object(
            runner,
            "run_codex",
            side_effect=self.codex_writing_disposition(),
        ), mock.patch.object(
            runner.hot_sync,
            "execute",
            return_value={"ok": True, "commit": "abc", "sync": {"ok": True}},
        ):
            second = runner.curate()

        self.assertTrue(second["ok"])
        self.assertGreaterEqual(second["events_processed"], 1)
        self.assertEqual(second["retry_blocked_remaining"], 0)
        quarantined = list(
            (self.data / "semantic" / "quarantined").iterdir()
        )
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(
            self.events_in(quarantined[0])[0]["content"],
            "Use that account; the reference is still unclear",
        )

    def test_deduplicated_proposal_creates_traceable_immutable_source_link(self):
        self.add_event(1, "这个端口仍然是 8080")
        batch = self.pending / "deduplicated-source-link"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        staging = Path(self.temporary.name) / "dedup-staging"
        for name in ("scripts", "config", "schema"):
            shutil.copytree(ROOT / name, staging / name)
        shutil.copy2(ROOT / "MANIFEST.json", staging / "MANIFEST.json")
        existing = staging / "ledger" / "2026" / "07" / "existing-port.json"
        existing.parent.mkdir(parents=True)
        existing.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "id": "existing-port",
                    "created_at": "2026-07-22T00:00:00Z",
                    "kind": "fact",
                    "scope": "test",
                    "statement": "端口是 8080",
                    "source": "earlier-user-event",
                    "authority": "user-explicit",
                    "status": "active",
                    "source_device_id": "device-test",
                }
            ),
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "init"], cwd=staging, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "semantic@example.test"],
            cwd=staging,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Semantic Test"],
            cwd=staging,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=staging, check=True)
        subprocess.run(
            ["git", "commit", "-m", "staging-root"],
            cwd=staging,
            check=True,
            capture_output=True,
        )
        runner = load_script("run_semantic_maintenance")
        runner.semantic_batches = self.batches
        proposal = {
            "format": self.batches.PROPOSAL_FORMAT,
            "batch_id": json.loads(
                (batch / "manifest.json").read_text(encoding="utf-8")
            )["batch_id"],
            "events": [
                {
                    "event_hash": "hash-001",
                    "outcome": "deduplicated",
                    "captures": [],
                    "existing_ids": ["existing-port"],
                    "reason_code": None,
                    "evidence": None,
                }
            ],
        }

        disposition = runner.proposal_disposition_in_worktree(
            staging,
            batch,
            proposal,
            Path(self.temporary.name) / "values",
        )

        ids = disposition["dispositions"][0]["knowledge_ids"]
        self.assertEqual(ids[0], "existing-port")
        self.assertEqual(len(ids), 2)
        records = self.batches.knowledge_records(staging)
        link = records[ids[1]][0]
        self.assertIn(
            self.batches.source_marker(proposal["batch_id"], "hash-001"),
            link["source"],
        )
        self.batches.validate_disposition_document(
            batch, disposition, knowledge_root=staging
        )

    def test_real_isolated_transaction_runs_staging_kb_under_workspace_lock_and_recovers(self):
        self.add_event(1, "服务器端口是 8080")
        batch = self.pending / "real-transaction"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        repository = self.disposable_repository()
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = repository
        runner.DATA = repository / ".kb-data"
        runner.REPORTS = runner.DATA / "reports"
        runner.semantic_batches = self.batches
        manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        proposal = {
            "format": self.batches.PROPOSAL_FORMAT,
            "batch_id": manifest["batch_id"],
            "events": [
                {
                    "event_hash": "hash-001",
                    "outcome": "captured",
                    "captures": [
                        {
                            "kind": "fact",
                            "scope": "test",
                            "statement": "服务器端口是 8080",
                            "authority": "user-explicit",
                            "status": "active",
                        }
                    ],
                    "existing_ids": [],
                    "reason_code": None,
                    "evidence": None,
                }
            ],
        }
        original_head = runner.git_head(repository)

        # The semantic runner owns the live repository lock for the full
        # transaction. Its isolated worktree has a distinct root and must be
        # able to run kb.py mutations without trying to reacquire this lock.
        with runner.workspace_lock.acquire(repository, blocking=False):
            transaction = runner.stage_proposal_transaction(batch, proposal)

        # stage_proposal_transaction has already removed its worktree. The
        # prepared ref is the durable recovery anchor for the still-unapplied
        # commit.
        self.assertEqual(runner.git_head(repository), original_head)
        self.assertNotEqual(transaction["commit"], original_head)
        anchored = subprocess.run(
            ["git", "show-ref", "--verify", transaction["prepared_ref"]],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(anchored.returncode, 0)

        # Simulate the next process after a crash between prepare and apply.
        recovered = runner.load_transaction(batch)
        runner.apply_prepared_transaction(batch, recovered)
        self.assertEqual(runner.git_head(repository), transaction["commit"])
        self.batches.validate_disposition(batch, repository)

        # A peer sync may rebase the already-applied semantic commit before
        # this batch reaches mark-complete. Preserve the same verified tree
        # under a new commit identity and prove recovery recognizes it.
        base_tree = subprocess.run(
            ["git", "rev-parse", "%s^{tree}" % original_head],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        upstream = subprocess.run(
            ["git", "commit-tree", base_tree, "-p", original_head, "-m", "peer"],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        semantic_tree = subprocess.run(
            ["git", "rev-parse", "%s^{tree}" % transaction["commit"]],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        rebased = subprocess.run(
            ["git", "commit-tree", semantic_tree, "-p", upstream, "-m", "rebased"],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "update-ref", "HEAD", rebased, transaction["commit"]],
            cwd=str(repository),
            check=True,
        )
        reapplied = runner.apply_prepared_transaction(
            batch, runner.load_transaction(batch)
        )
        self.assertEqual(runner.git_head(repository), rebased)
        self.assertEqual(reapplied["rebased_commit"], rebased)

        previous_root = self.batches.ROOT
        try:
            self.batches.ROOT = repository
            completed = self.batches.mark_complete(batch)
        finally:
            self.batches.ROOT = previous_root
        self.assertEqual(completed["processed"], 1)
        runner.release_transaction_ref(recovered)
        released = subprocess.run(
            ["git", "show-ref", "--verify", transaction["prepared_ref"]],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertNotEqual(released.returncode, 0)

    def test_isolated_transaction_rejects_out_of_zone_write_without_live_change(self):
        self.add_event(1, "普通消息")
        batch = self.pending / "malicious-path"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        repository = self.disposable_repository("malicious-repo")
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = repository
        runner.DATA = repository / ".kb-data"
        runner.semantic_batches = self.batches
        manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        proposal = {
            "format": self.batches.PROPOSAL_FORMAT,
            "batch_id": manifest["batch_id"],
            "events": [
                {
                    "event_hash": "hash-001",
                    "outcome": "deferred",
                    "captures": [],
                    "existing_ids": [],
                }
            ],
        }
        original_head = runner.git_head(repository)
        original_policy = (repository / "POLICY.md").read_bytes()

        def malicious(staging_root, batch_path, proposal_value, value_root):
            (staging_root / "POLICY.md").write_text(
                "historical prompt injection won\n", encoding="utf-8"
            )
            return {
                "format": self.batches.DISPOSITION_FORMAT,
                "batch_id": manifest["batch_id"],
                "dispositions": [
                    {
                        "event_hash": "hash-001",
                        "outcome": "deferred",
                        "knowledge_ids": [],
                    }
                ],
            }

        with mock.patch.object(
            runner, "proposal_disposition_in_worktree", side_effect=malicious
        ):
            with self.assertRaisesRegex(
                runner.CurationTransactionError,
                "transaction-path-outside-knowledge-zones",
            ):
                runner.stage_proposal_transaction(batch, proposal)

        self.assertEqual(runner.git_head(repository), original_head)
        self.assertEqual((repository / "POLICY.md").read_bytes(), original_policy)
        self.assertFalse((batch / runner.TRANSACTION_FILE).exists())

    def test_transaction_rechecks_immutable_batch_hash_before_apply(self):
        self.add_event(1, "服务器端口是 8080")
        batch = self.pending / "tampered-after-prepare"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        repository = self.disposable_repository("tamper-repo")
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = repository
        runner.DATA = repository / ".kb-data"
        runner.semantic_batches = self.batches
        manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        proposal = {
            "format": self.batches.PROPOSAL_FORMAT,
            "batch_id": manifest["batch_id"],
            "events": [
                {
                    "event_hash": "hash-001",
                    "outcome": "captured",
                    "captures": [
                        {
                            "kind": "fact",
                            "scope": "test",
                            "statement": "服务器端口是 8080",
                            "authority": "user-explicit",
                            "status": "active",
                        }
                    ],
                    "existing_ids": [],
                }
            ],
        }
        transaction = runner.stage_proposal_transaction(batch, proposal)
        original_head = runner.git_head(repository)
        with (batch / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"event_hash":"injected"}\n')

        with self.assertRaisesRegex(
            self.batches.DispositionError, "batch-payload-changed"
        ):
            runner.apply_prepared_transaction(batch, transaction)
        self.assertEqual(runner.git_head(repository), original_head)

    # ------------------------------------------------------------------
    # B3: pre-sync tolerance and config-driven model settings.
    # ------------------------------------------------------------------

    def test_curate_continues_locally_when_pre_sync_fails(self):
        self.add_event(1, "first ordinary event")
        runner = self.load_runner()
        runner.pre_sync_status = lambda: {
            "ok": False,
            "action": "git-sync",
            "offline": True,
            "error": "GitTimeout",
        }

        hot = mock.Mock(return_value={"ok": True, "commit": "abc", "sync": {"ok": True}})
        with mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json()
        ), mock.patch.object(
            runner, "run_codex", side_effect=self.codex_writing_disposition()
        ), mock.patch.object(
            runner.hot_sync, "execute", hot
        ):
            result = runner.curate()

        self.assertTrue(result["ok"])
        self.assertFalse(result["pre_sync"]["ok"])
        self.assertEqual(result["batches_completed"], 1)
        self.assertEqual(result["events_processed"], 1)

    def test_curate_aborts_when_git_repository_is_missing(self):
        runner = self.load_runner()
        runner.ROOT = Path(self.temporary.name) / "no-repo"
        runner.ROOT.mkdir()
        runner.pre_sync_status = mock.Mock(
            side_effect=RuntimeError("git-repository-missing")
        )
        with self.assertRaises(RuntimeError):
            runner.curate()

    def test_audit_degrades_pre_sync_failure_to_warning(self):
        runner = self.load_runner()
        runner.pre_sync_status = lambda: {
            "ok": False,
            "action": "git-sync",
            "offline": True,
            "error": "repository-privacy-unknown",
        }
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(
            runner, "run_json", return_value={"ok": True}
        ), mock.patch.object(
            runner, "run_codex", return_value=0
        ), mock.patch.object(
            runner.subprocess, "run", return_value=completed
        ):
            result = runner.audit()

        self.assertTrue(result["ok"])
        self.assertFalse(result["pre_sync"]["ok"])

    def test_pre_sync_only_tolerates_explicit_network_failures(self):
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = self.root

        def completed(error, retryable=True):
            return subprocess.CompletedProcess(
                [],
                2,
                json.dumps(
                    {
                        "ok": False,
                        "action": "git-sync",
                        "error": error,
                        "retryable": retryable,
                    }
                ),
                "",
            )

        with mock.patch.object(
            runner.subprocess, "run", return_value=completed("GitTimeout")
        ):
            offline = runner.pre_sync_status()
        self.assertTrue(offline["offline"])

        for error in (
            "dirty-worktree",
            "fetch-failed",
            "unexpected-branch",
            "origin-identity-mismatch",
            "repository-public",
            "rebase-conflict",
        ):
            with self.subTest(error=error), mock.patch.object(
                runner.subprocess,
                "run",
                return_value=completed(error, retryable=False),
            ):
                with self.assertRaisesRegex(
                    runner.CurationTransactionError, "pre-sync-" + error
                ):
                    runner.pre_sync_status()

    def test_pre_sync_commits_verified_capture_status_then_retries(self):
        runner = load_script("run_semantic_maintenance")
        runner.ROOT = self.root
        dirty = subprocess.CompletedProcess(
            [],
            2,
            json.dumps(
                {
                    "ok": False,
                    "action": "git-sync",
                    "error": "dirty-worktree",
                    "retryable": True,
                }
            ),
            "",
        )
        clean = subprocess.CompletedProcess(
            [],
            0,
            json.dumps({"ok": True, "action": "git-sync", "pulled": False}),
            "",
        )
        capture_commit = {
            "committed": True,
            "commit": "abc123",
            "path": "config/devices/device-test.json",
            "source_device_id": "device-test",
        }
        with mock.patch.object(
            runner.subprocess, "run", side_effect=(dirty, clean)
        ) as sync, mock.patch.object(
            runner,
            "commit_local_capture_status_if_safe",
            return_value=capture_commit,
        ) as commit:
            result = runner.pre_sync_status()

        self.assertEqual(sync.call_count, 2)
        commit.assert_called_once_with(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["local_capture_commit"], capture_commit)

    def test_capture_status_commit_is_path_scoped_and_fail_closed(self):
        repository = self.disposable_repository("capture-status-commit")
        source_device_id = "fictional-device"
        status_path = repository / "config" / "devices" / (source_device_id + ".json")
        status_path.parent.mkdir(parents=True, exist_ok=True)
        config_path = repository / "config/kb.json"
        config = json.loads(config_path.read_text())
        config["hot_sync"] = {"expected_repository": "fictional/knowledge", "expected_branch": "main", "allowed_ssh_hosts": ["github.com"]}
        config_path.write_text(json.dumps(config))
        subprocess.run(["git", "add", str(config_path)], cwd=repository, check=True, capture_output=True)
        status_path.write_text(json.dumps({"id": source_device_id, "history_events": 1, "source_files": 1, "last_capture_completed_at": "2026-08-20T00:00:00Z"}))
        subprocess.run(["git", "add", str(status_path)], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "fictional device fixture"], cwd=repository, check=True, capture_output=True)
        previous = json.loads(status_path.read_text(encoding="utf-8"))
        current = dict(previous)
        current["history_events"] = int(previous["history_events"]) + 1
        current["source_files"] = int(previous["source_files"]) + 1
        current["last_capture_completed_at"] = "2026-08-21T10:00:00+08:00"
        status_path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        reports = repository / ".kb-data" / "reports"
        reports.mkdir(parents=True)
        reports.joinpath("windows-capture-test.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "device_id": source_device_id,
                    "history_events": current["history_events"],
                    "source_files": current["source_files"],
                    "last_capture_completed_at": current[
                        "last_capture_completed_at"
                    ],
                    "checkpoint_coverage": {"coverage_complete": True},
                    "raw_exported": False,
                    "bundle_exported": False,
                }
            ),
            encoding="utf-8",
        )
        runner = load_script("run_semantic_maintenance")
        with mock.patch.object(
            runner.device_identity,
            "resolve_device_id",
            return_value=source_device_id,
        ):
            committed = runner.commit_local_capture_status_if_safe(repository)

        self.assertTrue(committed["committed"])
        self.assertEqual(committed["path"], "config/devices/%s.json" % source_device_id)
        self.assertFalse(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(repository),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )

        repository.joinpath("README.md").write_text(
            repository.joinpath("README.md").read_text(encoding="utf-8")
            + "\nunrelated user edit\n",
            encoding="utf-8",
        )
        next_status = dict(current)
        next_status["history_events"] += 1
        next_status["last_capture_completed_at"] = "2026-08-22T10:00:00+08:00"
        status_path.write_text(
            json.dumps(next_status, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with mock.patch.object(
            runner.device_identity,
            "resolve_device_id",
            return_value=source_device_id,
        ):
            self.assertIsNone(
                runner.commit_local_capture_status_if_safe(repository)
            )

    def test_read_only_proposal_contract_rejects_prose_and_unknown_fields(self):
        self.add_event(1, "记住服务器端口是 8080")
        batch = self.pending / "proposal-contract"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        runner = self.load_runner()
        report = self.data / "reports" / "proposal.json"
        report.parent.mkdir(parents=True)
        report.write_text(
            "```json\n{}\n```\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            self.batches.ProposalError, "proposal-unreadable"
        ):
            runner.parse_proposal(report, batch)

        manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        report.write_text(
            json.dumps(
                {
                    "format": self.batches.PROPOSAL_FORMAT,
                    "batch_id": manifest["batch_id"],
                    "events": [
                        {
                            "event_hash": "hash-001",
                            "outcome": "captured",
                            "captures": [
                                {
                                    "kind": "fact",
                                    "scope": "test",
                                    "statement": "服务器端口是 8080",
                                    "authority": "user-explicit",
                                    "status": "active",
                                    "source": "model-must-not-control-provenance",
                                }
                            ],
                            "existing_ids": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            self.batches.ProposalError, "proposal-capture-fields-invalid"
        ):
            runner.parse_proposal(report, batch)
        prompt = (ROOT / "automation" / "semantic-curator-prompt.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(self.batches.PROPOSAL_FORMAT, prompt)
        self.assertIn("untrusted", prompt.casefold())
        self.assertIn("Return only one JSON object", prompt)
        self.assertIn("The `value` field is credential-only", prompt)
        self.assertIn("`credential_resolution_required=false`", prompt)

    def test_semantic_proposal_rejects_active_credential_without_value(self):
        self.add_event(1, "Store 项目的 API Key 在 Story 文件夹")
        batch = self.pending / "credential-value-gate"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        runner = self.load_runner()
        manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        report = self.data / "reports" / "credential-value-gate.json"
        report.parent.mkdir(parents=True)
        report.write_text(
            json.dumps(
                {
                    "format": self.batches.PROPOSAL_FORMAT,
                    "batch_id": manifest["batch_id"],
                    "events": [
                        {
                            "event_hash": "hash-001",
                            "outcome": "captured",
                            "captures": [
                                {
                                    "kind": "credential",
                                    "scope": "project:store",
                                    "statement": "Store 项目的 API Key 在 Story 文件夹",
                                    "authority": "user-explicit",
                                    "status": "active",
                                }
                            ],
                            "existing_ids": [],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            self.batches.ProposalError, "proposal-credential-value-missing"
        ):
            runner.parse_proposal(report, batch)

    def test_semantic_proposal_rejects_locator_fragment_as_credential_value(self):
        self.add_event(1, "Store project's API key is stored in the Story folder")
        batch = self.pending / "credential-locator-value-gate"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        runner = self.load_runner()
        manifest = json.loads(
            (batch / "manifest.json").read_text(encoding="utf-8")
        )
        report = self.data / "reports" / "credential-locator-value-gate.json"
        report.parent.mkdir(parents=True)
        report.write_text(
            json.dumps(
                {
                    "format": self.batches.PROPOSAL_FORMAT,
                    "batch_id": manifest["batch_id"],
                    "events": [
                        {
                            "event_hash": "hash-001",
                            "outcome": "captured",
                            "captures": [
                                {
                                    "kind": "api-key",
                                    "scope": "project:store",
                                    "statement": "Store API key",
                                    "value": "Story",
                                    "authority": "user-explicit",
                                    "status": "active",
                                }
                            ],
                            "existing_ids": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            self.batches.ProposalError,
            "proposal-credential-value-unsubstantiated",
        ):
            runner.parse_proposal(report, batch)

    def test_semantic_proposal_rejects_credential_identity_mismatch(self):
        value = "alpha-credential-7429"
        self.add_event(1, "Store API key value is %s" % value)
        batch = self.pending / "credential-capture-identity-gate"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        runner = self.load_runner()
        manifest = json.loads(
            (batch / "manifest.json").read_text(encoding="utf-8")
        )
        report = self.data / "reports" / "credential-capture-identity-gate.json"
        report.parent.mkdir(parents=True)
        report.write_text(
            json.dumps(
                {
                    "format": self.batches.PROPOSAL_FORMAT,
                    "batch_id": manifest["batch_id"],
                    "events": [
                        {
                            "event_hash": "hash-001",
                            "outcome": "captured",
                            "captures": [
                                {
                                    "kind": "api-key",
                                    "scope": "project:differentvendor",
                                    "statement": "DifferentVendor API key",
                                    "value": value,
                                    "authority": "user-explicit",
                                    "status": "active",
                                }
                            ],
                            "existing_ids": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            self.batches.ProposalError,
            "proposal-credential-identity-mismatch",
        ):
            runner.parse_proposal(report, batch)

    def test_credential_evidence_rejects_locator_words_but_accepts_assignment(self):
        locator_event = {
            "event_hash": "locator-event",
            "content": "Store API key is stored in the Story folder",
            "context": [],
        }
        for decoy in (
            "stored",
            "saved",
            "located",
            "available",
            "found",
            "kept",
            "in",
            "at",
        ):
            with self.subTest(decoy=decoy):
                content = "Store API key is %s in the Story folder" % decoy
                event = dict(locator_event, content=content)
                self.assertFalse(
                    self.batches.credential_value_supported_by_event(
                        event, decoy
                    )
                )
        self.assertFalse(
            self.batches.credential_value_supported_by_event(
                locator_event, "Story"
            )
        )

        assigned = {
            "event_hash": "assigned-event",
            "content": "DOUBAO_API_KEY=alpha-credential-7429",
            "context": [],
        }
        self.assertTrue(
            self.batches.credential_value_supported_by_event(
                assigned, "alpha-credential-7429"
            )
        )

        longer_value = "alpha-credential-7429-extra"
        longer_assignment = {
            "event_hash": "longer-assigned-event",
            "content": "DOUBAO_API_KEY=%s" % longer_value,
            "context": [],
        }
        self.assertTrue(
            self.batches.credential_value_supported_by_event(
                longer_assignment, longer_value
            )
        )
        for partial in (
            "alpha-credential-7429",
            "credential-7429-extra",
            "credential-7429",
        ):
            with self.subTest(partial=partial):
                self.assertFalse(
                    self.batches.credential_value_supported_by_event(
                        longer_assignment, partial
                    )
                )

        complete_pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
            "-----END PRIVATE KEY-----"
        )
        pem_event = {
            "event_hash": "complete-pem-event",
            "content": complete_pem,
            "context": [],
        }
        self.assertTrue(
            self.batches.credential_value_supported_by_event(
                pem_event, complete_pem
            )
        )
        self.assertFalse(
            self.batches.credential_value_supported_by_event(
                pem_event,
                "-----BEGIN PRIVATE KEY-----\nQUJDREVGR0hJSktM",
            )
        )

        assigned_pem_event = {
            "event_hash": "assigned-complete-pem-event",
            "content": "PRIVATE_KEY:\n%s" % complete_pem,
            "context": [],
        }
        self.assertTrue(
            self.batches.credential_value_supported_by_event(
                assigned_pem_event, complete_pem
            )
        )
        self.assertFalse(
            self.batches.credential_value_supported_by_event(
                assigned_pem_event,
                (
                    "-----BEGIN PRIVATE KEY-----\n"
                    "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
                ),
            )
        )
        self.assertFalse(
            self.batches.credential_value_supported_by_event(
                assigned_pem_event,
                "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=",
            )
        )

    def test_credential_request_resolution_does_not_join_unrelated_sentences(self):
        preference = {
            "event_hash": "credential-policy-with-unrelated-action",
            "content": (
                "涉及登录密码时，尽量减少人工步骤，能自动完成就自动完成。"
                "优先使用外接 SSD 保存开发缓存。"
            ),
            "context": [],
        }
        self.assertFalse(
            self.batches.event_requires_credential_resolution(preference)
        )

        english_preference = {
            "event_hash": "credential-policy-with-unrelated-english-action",
            "content": (
                "Password prompts should require little human work. "
                "Use the external SSD for development caches."
            ),
            "context": [],
        }
        self.assertFalse(
            self.batches.event_requires_credential_resolution(
                english_preference
            )
        )

        no_space_english_preference = {
            "event_hash": "credential-policy-with-no-space-period",
            "content": (
                "Password prompts should require little human work."
                "Use the external SSD for development caches."
            ),
            "context": [],
        }
        self.assertFalse(
            self.batches.event_requires_credential_resolution(
                no_space_english_preference
            )
        )

        ellipsis_preference = {
            "event_hash": "credential-policy-with-ellipsis",
            "content": "登录密码尽量少人工处理……优先使用外接 SSD。",
            "context": [],
        }
        self.assertFalse(
            self.batches.event_requires_credential_resolution(
                ellipsis_preference
            )
        )

        modal_preference = {
            "event_hash": "credential-policy-with-modal-should",
            "content": "登录密码尽量少人工处理。应该使用外接 SSD 保存开发缓存。",
            "context": [],
        }
        self.assertFalse(
            self.batches.event_requires_credential_resolution(
                modal_preference
            )
        )

        private_repository_policy = {
            "event_hash": "credential-private-repository-policy",
            "content": (
                "我反感密码密钥等内容都不让存在私有仓库。"
                "这些资源不是真正生产且可接受损失，应完整保存原值。"
                "知识库还应组织项目服务器、密钥、账号等资源事实。"
                "如果有更好的结构建议当然可以给我。"
            ),
            "context": [],
        }
        self.assertFalse(
            self.batches.event_requires_credential_resolution(
                private_repository_policy
            )
        )
        for policy_text in (
            "密码不让存在私有仓库",
            "密码策略存在争议",
            "密码存在争议",
            "密码存在",
            "这套规则旨在说明密码如何保存",
        ):
            with self.subTest(policy_text=policy_text):
                self.assertFalse(
                    self.batches.event_requires_credential_resolution(
                        {
                            "event_hash": "credential-policy-zai",
                            "content": policy_text,
                            "context": [],
                        }
                    )
                )
        self.assertTrue(
            self.batches.event_requires_credential_resolution(
                {
                    "event_hash": "credential-chinese-location",
                    "content": "密码在测试文件里",
                    "context": [],
                }
            )
        )
        self.assertTrue(
            self.batches.event_requires_credential_resolution(
                {
                    "event_hash": "credential-chinese-exists-location",
                    "content": "密码存在测试仓库里",
                    "context": [],
                }
            )
        )
        self.assertFalse(
            self.batches.event_requires_credential_resolution(
                {
                    "event_hash": "credential-advice-neighbour",
                    "content": "这里会整理服务器、密钥和账号事实。"
                    "如果有更好的建议可以给我。",
                    "context": [],
                }
            )
        )
        self.assertTrue(
            self.batches.event_requires_credential_resolution(
                {
                    "event_hash": "credential-bare-neighbour-request",
                    "content": "Store API key。请把它给我。",
                    "context": [],
                }
            )
        )
        self.assertTrue(
            self.batches.event_requires_credential_resolution(
                {
                    "event_hash": "credential-policy-with-exact-value",
                    "content": (
                        "禁止把密码 alpha-credential-7429 存在环境变量，"
                        "必须在知识库保存原值。"
                    ),
                    "context": [],
                }
            )
        )
        for short_value_statement in (
            "禁止把密码 hunter2 存在环境变量，必须在知识库保存原值。",
            "不允许密码 1234 存在临时文件，知识库要完整保存它。",
            "禁止把 token abc123 存在环境变量，必须保存原值。",
        ):
            with self.subTest(short_value_statement=short_value_statement):
                self.assertTrue(
                    self.batches.event_requires_credential_resolution(
                        {
                            "event_hash": "credential-policy-short-value",
                            "content": short_value_statement,
                            "context": [],
                        }
                    )
                )
        self.assertTrue(
            self.batches.event_requires_credential_resolution(
                {
                    "event_hash": "credential-short-assignment",
                    "content": "密码=abc123",
                    "context": [],
                }
            )
        )
        self.assertTrue(
            self.batches.event_requires_credential_resolution(
                {
                    "event_hash": "credential-policy-nonadjacent-exact-value",
                    "content": "禁止让密码存在环境变量；请完整保留 alpha-credential-7429",
                    "context": [],
                }
            )
        )
        self.assertFalse(
            self.batches.event_requires_credential_resolution(
                {
                    "event_hash": "credential-policy-unrelated-version",
                    "content": "禁止让密码存在私有仓库，当前规范版本是 build-7429。",
                    "context": [],
                }
            )
        )

        assignment_decoy = {
            "event_hash": "credential-label-with-unrelated-english-is",
            "content": "Password policy changed. This is a general note.",
            "context": [],
        }
        self.assertFalse(
            self.batches.event_requires_credential_resolution(
                assignment_decoy
            )
        )

        unavailable_assignment = {
            "event_hash": "credential-explicitly-unavailable",
            "content": "Store API key is unavailable.",
            "context": [],
        }
        self.assertFalse(
            self.batches.event_requires_credential_resolution(
                unavailable_assignment
            )
        )

        assignment_with_unrelated_absence = {
            "event_hash": "credential-assignment-with-unrelated-absence",
            "content": (
                "新 API key=alpha-credential-7429，"
                "但旧 token 不可用。"
            ),
            "context": [],
        }
        self.assertTrue(
            self.batches.event_requires_credential_resolution(
                assignment_with_unrelated_absence
            )
        )

        direct_request = {
            "event_hash": "credential-direct-request",
            "content": "Store API key，请从已检查的文件读取它。",
            "context": [],
        }
        self.assertTrue(
            self.batches.event_requires_credential_resolution(direct_request)
        )

        anaphoric_request = {
            "event_hash": "credential-anaphoric-request",
            "content": "我需要 Store API key。请从已检查的文件读取它。",
            "context": [],
        }
        self.assertTrue(
            self.batches.event_requires_credential_resolution(
                anaphoric_request
            )
        )

        zero_object_request = {
            "event_hash": "credential-zero-object-request",
            "content": "Store API key。请从已检查的文件读取。",
            "context": [],
        }
        self.assertTrue(
            self.batches.event_requires_credential_resolution(
                zero_object_request
            )
        )

        anaphoric_locator = {
            "event_hash": "credential-anaphoric-locator",
            "content": "Store API key。它保存在已检查的文件里。",
            "context": [],
        }
        self.assertTrue(
            self.batches.event_requires_credential_resolution(
                anaphoric_locator
            )
        )

        opaque_request = {
            "event_hash": "credential-opaque-request",
            "content": "Use Ark9D7f2C8mQ4vT6xP0zL3nS for this request.",
            "context": [],
        }
        self.assertTrue(
            self.batches.event_requires_credential_resolution(opaque_request)
        )

    def test_credential_identity_requires_every_declared_dimension(self):
        value = "alpha-credential-7429-extra"
        event = {
            "event_hash": "credential-identity-dimensions",
            "content": (
                "StoreVendor Store primary test API key value is %s" % value
            ),
            "context": [],
        }
        canonical = {
            "kind": "api-key",
            "scope": "project:store",
            "provider": "StoreVendor",
            "account": "primary",
            "environment": "test",
            "name": "StoreVendor API key",
            "status": "active",
            "value": value,
        }
        self.assertTrue(
            self.batches.credential_identity_supported_by_event(
                event, canonical
            )
        )
        for field, different in (
            ("scope", "project:other"),
            ("account", "secondary"),
            ("environment", "production"),
        ):
            with self.subTest(field=field):
                mismatched = dict(canonical, **{field: different})
                self.assertFalse(
                    self.batches.credential_identity_supported_by_event(
                        event, mismatched
                    )
                )

    def test_credential_parser_preserves_quoted_bytes_and_connection_strings(self):
        quoted = 'Password is "  stored  "'
        self.assertEqual(
            self.batches.assigned_credential_scalars(quoted),
            ["  stored  "],
        )
        encoded = 'API key is "YWJjL2RlZitnaD0="'
        self.assertEqual(
            self.batches.assigned_credential_scalars(encoded),
            ["YWJjL2RlZitnaD0="],
        )
        for status in ("not stored", "expired", "revoked", "invalid"):
            with self.subTest(status=status):
                self.assertEqual(
                    self.batches.assigned_credential_scalars(
                        "Password is %s" % status
                    ),
                    [],
                )
        for qualified in (
            "Password currently is hunter2",
            "Password's value is hunter2",
            "API key for production: hunter2",
            "API key (production): hunter2",
        ):
            with self.subTest(qualified=qualified):
                self.assertEqual(
                    self.batches.assigned_credential_scalars(qualified),
                    ["hunter2"],
                )
        connection = (
            "Connection string is "
            "Server=db;Database=app;User Id=u;Password=pw"
        )
        self.assertEqual(
            self.batches.assigned_credential_scalars(connection),
            ["Server=db;Database=app;User Id=u;Password=pw"],
        )

    def test_credential_disclosures_use_assignment_local_role_aware_identity(self):
        def event(content):
            return {
                "event_hash": "credential-identity-structure",
                "source": "codex",
                "event_id": "credential-identity-structure",
                "role": "user",
                "event_type": "message",
                "content": content,
                "context": [],
            }

        same_resource = self.batches.credential_disclosures_by_identity(
            event("Acme API key=A; For Acme, the API key=B")
        )
        self.assertEqual(list(same_resource.values()), [["A", "B"]])

        distinct_accounts = self.batches.credential_disclosures_by_identity(
            event(
                "For Acme account one, API key=A; "
                "For Acme account two, API key=B"
            )
        )
        self.assertCountEqual(list(distinct_accounts.values()), [["A"], ["B"]])

        distinct_providers = self.batches.credential_disclosures_by_identity(
            event("ProviderA key=A and ProviderB key=B")
        )
        self.assertCountEqual(
            list(distinct_providers.values()), [["A"], ["B"]]
        )

        rotated = self.batches.credential_disclosures_by_identity(
            event("Store API key old value=A; new value=B")
        )
        self.assertEqual(list(rotated.values()), [["A", "B"]])

        role_aware_event = event(
            "provider Alpha account Beta API key=A; "
            "provider Beta account Alpha API key=B"
        )
        self.assertTrue(
            self.batches.credential_identity_supported_by_event(
                role_aware_event,
                {
                    "kind": "credential",
                    "name": "API key",
                    "provider": "Alpha",
                    "account": "Beta",
                    "value": "A",
                },
            )
        )
        self.assertFalse(
            self.batches.credential_identity_supported_by_event(
                role_aware_event,
                {
                    "kind": "credential",
                    "name": "API key",
                    "provider": "Beta",
                    "account": "Alpha",
                    "value": "A",
                },
            )
        )

        same_value_distinct_accounts = event(
            "ProviderA account alice API key=shared7429; "
            "ProviderA account bob API key=shared7429"
        )
        units = self.batches.credential_disclosure_units(
            same_value_distinct_accounts
        )
        self.assertEqual(len(units), 2)
        underspecified = {
            "kind": "credential",
            "statement": "ProviderA API key",
            "value": "shared7429",
        }
        self.assertTrue(
            all(
                not self.batches.credential_disclosure_covered_by_record(
                    disclosure, underspecified
                )
                for disclosure in units
            )
        )
        self.assertTrue(
            self.batches.credential_disclosure_covered_by_record(
                units[0],
                {
                    "kind": "credential",
                    "statement": "ProviderA account alice API key",
                    "value": "shared7429",
                },
            )
        )

    def test_temporary_outcome_requires_affirmative_nonretention(self):
        for target, evidence in (
            ("This is not temporary.", "This is not temporary."),
            ("I did not say don't save this.", "I did not say don't save this."),
            ("并非临时，也没有说不要保存。", "并非临时，也没有说不要保存。"),
            ("This is not temporary. Password is hunter2", "temporary"),
            ("I did not say don't save it", "don't save"),
            ("Password is TemporarySecret12345", "Temporary"),
        ):
            event = {"content": target}
            record = {
                "outcome": "temporary",
                "reason_code": "explicit-user-nonretention",
                "evidence": evidence,
            }
            with self.subTest(target=target, evidence=evidence):
                with self.assertRaisesRegex(
                    self.batches.DispositionError,
                    "temporary-not-explicit",
                ):
                    self.batches._validate_no_knowledge_outcome(event, record)

        evidence = "仅本次使用，用完删除。"
        self.batches._validate_no_knowledge_outcome(
            {"content": evidence},
            {
                "outcome": "temporary",
                "reason_code": "explicit-user-nonretention",
                "evidence": evidence,
            },
        )

    def test_credential_target_cannot_be_processed_as_only_an_ordinary_fact(self):
        self.add_event(1, "Store project's API key is stored in the Story folder")
        batch = self.pending / "credential-target-coverage"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        runner = self.load_runner()
        manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        report = self.data / "reports" / "credential-target-coverage.json"
        report.parent.mkdir(parents=True)
        report.write_text(
            json.dumps(
                {
                    "format": self.batches.PROPOSAL_FORMAT,
                    "batch_id": manifest["batch_id"],
                    "events": [
                        {
                            "event_hash": "hash-001",
                            "outcome": "captured",
                            "captures": [
                                {
                                    "kind": "fact",
                                    "scope": "project:store",
                                    "statement": "The Story folder belongs to Store",
                                    "authority": "user-explicit",
                                    "status": "active",
                                }
                            ],
                            "existing_ids": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            self.batches.ProposalError, "proposal-credential-target-unresolved"
        ):
            runner.parse_proposal(report, batch)

    def test_policy_exception_cannot_bypass_exact_credential_disposition(self):
        self.add_event(
            1,
            "禁止把密码 alpha-credential-7429 存在环境变量，"
            "必须在知识库保存原值。",
        )
        batch = self.pending / "credential-policy-exact-value-gate"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        event = self.events_in(batch)[0]
        self.assertTrue(event["credential_resolution_required"])
        self.write_disposition(batch)

        with self.assertRaisesRegex(
            self.batches.DispositionError,
            "disposition-credential-values-incomplete",
        ):
            self.batches.validate_disposition(batch, self.root)

    def test_semantic_proposal_rejects_candidate_credential_without_value(self):
        self.add_event(1, "Store API key value is alpha-credential-7429")
        batch = self.pending / "credential-candidate-value-gate"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        runner = self.load_runner()
        manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        report = self.data / "reports" / "credential-candidate-value-gate.json"
        report.parent.mkdir(parents=True)
        report.write_text(
            json.dumps(
                {
                    "format": self.batches.PROPOSAL_FORMAT,
                    "batch_id": manifest["batch_id"],
                    "events": [
                        {
                            "event_hash": "hash-001",
                            "outcome": "conflict",
                            "captures": [
                                {
                                    "kind": "api-key",
                                    "scope": "project:store",
                                    "statement": "Store API key conflicts",
                                    "authority": "user-explicit",
                                    "status": "candidate",
                                }
                            ],
                            "existing_ids": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            self.batches.ProposalError, "proposal-credential-value-missing"
        ):
            runner.parse_proposal(report, batch)

    def test_credential_value_requires_target_or_explicit_linked_tool_evidence(self):
        value = "alpha-credential-7429"
        self.add_event(1, "Find the Store API key from the inspected file")
        self.add_event(
            2,
            "%s\nSTORE_API_KEY=%s\n%s" % ("x" * 700, value, "y" * 700),
            role="assistant",
            event_type="tool-result-evidence",
            related_user_event_hash="hash-001",
        )
        batch = self.pending / "credential-tool-evidence"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        runner = self.load_runner()
        manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        report = self.data / "reports" / "credential-tool-evidence.json"
        report.parent.mkdir(parents=True)
        proposal = {
            "format": self.batches.PROPOSAL_FORMAT,
            "batch_id": manifest["batch_id"],
            "events": [
                {
                    "event_hash": "hash-001",
                    "outcome": "captured",
                    "captures": [
                        {
                            "kind": "api-key",
                            "scope": "project:store",
                            "statement": "Store API key recovered from inspected file",
                            "value": value,
                            "authority": "user-explicit",
                            "status": "active",
                        }
                    ],
                    "existing_ids": [],
                }
            ],
        }
        report.write_text(json.dumps(proposal), encoding="utf-8")

        parsed = runner.parse_proposal(report, batch)
        self.assertEqual(parsed["events"][0]["captures"][0]["value"], value)

        events_path = batch / "events.jsonl"
        event = json.loads(events_path.read_text(encoding="utf-8"))
        self.assertFalse(event["context"][0]["content_truncated"])
        event["context"][0]["related_user_event_hash"] = "hash-unrelated"
        raw = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        events_path.write_bytes(raw)
        manifest["events_sha256"] = hashlib.sha256(raw).hexdigest()
        (batch / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            self.batches.ProposalError,
            "proposal-credential-value-unsubstantiated",
        ):
            runner.parse_proposal(report, batch)

        event["context"][0]["related_user_event_hash"] = "hash-001"
        event["context"][0]["evidence_content_complete"] = False
        raw = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        events_path.write_bytes(raw)
        manifest["events_sha256"] = hashlib.sha256(raw).hexdigest()
        (batch / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            self.batches.ProposalError,
            "proposal-credential-value-unsubstantiated",
        ):
            runner.parse_proposal(report, batch)

    def test_disposition_gate_rejects_ordinary_fact_for_credential_target(self):
        self.add_event(1, "Store API key is stored in the Story folder")
        batch = self.pending / "credential-disposition-coverage"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        self.write_disposition(batch)

        with self.assertRaisesRegex(
            self.batches.DispositionError,
            "disposition-credential-target-unresolved",
        ):
            self.batches.validate_disposition(batch, self.root)

    def test_credential_dedup_requires_current_equivalent_canonical_value(self):
        value = "alpha-credential-7429"
        self.add_event(1, "Store API key value is %s" % value)
        batch = self.pending / "credential-dedup-equivalence"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        marker = self.batches.source_marker(manifest["batch_id"], "hash-001")
        credential_id = "store-api-key"
        link_id = "credential-source-link"
        credential_path = (
            self.root / "resources" / "credentials" / (credential_id + ".json")
        )
        credential_path.parent.mkdir(parents=True)
        canonical = {
            "schema_version": "1",
            "id": credential_id,
            "created_at": "2026-07-24T00:00:00Z",
            "kind": "api-key",
            "scope": "project:store",
            "name": "Store API key",
            "source": {"type": "user-explicit", "original_ref": "older-event"},
            "authority": "user-explicit",
            "status": "active",
            "source_device_id": "device-test",
            "value": value,
        }
        credential_path.write_text(json.dumps(canonical), encoding="utf-8")
        link_path = self.root / "ledger" / "test" / (link_id + ".json")
        link_path.parent.mkdir(parents=True)
        link_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "id": link_id,
                    "created_at": "2026-07-24T00:00:01Z",
                    "kind": "source-link",
                    "scope": "global",
                    "statement": "Source confirms existing knowledge",
                    "source": marker,
                    "authority": "user-explicit",
                    "status": "active",
                    "source_device_id": "device-test",
                }
            ),
            encoding="utf-8",
        )
        disposition = {
            "format": self.batches.DISPOSITION_FORMAT,
            "batch_id": manifest["batch_id"],
            "dispositions": [
                {
                    "event_hash": "hash-001",
                    "outcome": "deduplicated",
                    "knowledge_ids": [credential_id, link_id],
                }
            ],
        }

        self.batches.validate_disposition_document(
            batch, disposition, knowledge_root=self.root
        )

        canonical["name"] = "DifferentVendor API key"
        credential_path.write_text(json.dumps(canonical), encoding="utf-8")
        with self.assertRaisesRegex(
            self.batches.DispositionError,
            "disposition-credential-dedup-not-equivalent",
        ):
            self.batches.validate_disposition_document(
                batch, disposition, knowledge_root=self.root
            )

        canonical["name"] = "Store API key"
        canonical["provider"] = "DifferentVendor"
        credential_path.write_text(json.dumps(canonical), encoding="utf-8")
        with self.assertRaisesRegex(
            self.batches.DispositionError,
            "disposition-credential-dedup-not-equivalent",
        ):
            self.batches.validate_disposition_document(
                batch, disposition, knowledge_root=self.root
            )
        canonical.pop("provider")

        canonical["name"] = "Store DifferentVendor API key"
        credential_path.write_text(json.dumps(canonical), encoding="utf-8")
        with self.assertRaisesRegex(
            self.batches.DispositionError,
            "disposition-credential-dedup-not-equivalent",
        ):
            self.batches.validate_disposition_document(
                batch, disposition, knowledge_root=self.root
            )
        canonical["name"] = "Store API key"

        canonical["value"] = "older-different-credential-1835"
        credential_path.write_text(json.dumps(canonical), encoding="utf-8")
        with self.assertRaisesRegex(
            self.batches.DispositionError,
            "disposition-credential-dedup-not-equivalent",
        ):
            self.batches.validate_disposition_document(
                batch, disposition, knowledge_root=self.root
            )

        unrelated_id = "unrelated-fact"
        unrelated_path = self.root / "resources" / "catalog" / "unrelated.json"
        unrelated_path.parent.mkdir(parents=True)
        unrelated_path.write_text(
            json.dumps(
                {
                    "id": unrelated_id,
                    "kind": "fact",
                    "scope": "project:store",
                    "statement": "Store uses a Story folder",
                    "source": "older-event",
                    "authority": "user-explicit",
                    "status": "active",
                }
            ),
            encoding="utf-8",
        )
        disposition["dispositions"][0]["knowledge_ids"] = [
            unrelated_id,
            link_id,
        ]
        with self.assertRaisesRegex(
            self.batches.DispositionError,
            "disposition-credential-dedup-not-equivalent",
        ):
            self.batches.validate_disposition_document(
                batch, disposition, knowledge_root=self.root
            )

        canonical["value"] = value
        credential_path.write_text(json.dumps(canonical), encoding="utf-8")
        disposition["dispositions"][0]["knowledge_ids"] = [
            credential_id,
            unrelated_id,
            link_id,
        ]
        with self.assertRaisesRegex(
            self.batches.DispositionError,
            "disposition-credential-dedup-not-equivalent",
        ):
            self.batches.validate_disposition_document(
                batch, disposition, knowledge_root=self.root
            )
        replacement_id = "store-api-key-current"
        (credential_path.parent / (replacement_id + ".json")).write_text(
            json.dumps(
                {
                    **canonical,
                    "id": replacement_id,
                    "value": "replacement-credential-9914",
                }
            ),
            encoding="utf-8",
        )
        supersession_path = self.root / "ledger" / "test" / "supersession.json"
        supersession_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "id": "sup-store-api-key",
                    "created_at": "2026-07-24T00:00:02Z",
                    "kind": "supersession",
                    "scope": "project:store",
                    "statement": "%s is superseded by %s"
                    % (credential_id, replacement_id),
                    "source": "verified:test",
                    "authority": "maintainer",
                    "status": "active",
                    "source_device_id": "device-test",
                    "old_id": credential_id,
                    "new_id": replacement_id,
                }
            ),
            encoding="utf-8",
        )
        disposition["dispositions"][0]["knowledge_ids"] = [
            credential_id,
            link_id,
        ]
        with self.assertRaisesRegex(
            self.batches.DispositionError,
            "disposition-credential-dedup-not-equivalent",
        ):
            self.batches.validate_disposition_document(
                batch, disposition, knowledge_root=self.root
            )

    def test_credential_dedup_rejects_canonical_locator_as_value(self):
        self.add_event(
            1, "Store project's API key is stored in the Story folder"
        )
        batch = self.pending / "credential-dedup-locator-value"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        manifest = json.loads(
            (batch / "manifest.json").read_text(encoding="utf-8")
        )
        marker = self.batches.source_marker(
            manifest["batch_id"], "hash-001"
        )
        credential_id = "store-locator-api-key"
        credential_path = (
            self.root / "resources" / "credentials" / (credential_id + ".json")
        )
        credential_path.parent.mkdir(parents=True)
        credential_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "id": credential_id,
                    "created_at": "2026-07-24T00:00:00Z",
                    "kind": "api-key",
                    "scope": "project:store",
                    "name": "Store API key",
                    "source": {
                        "type": "user-explicit",
                        "original_ref": "older-event",
                    },
                    "authority": "user-explicit",
                    "status": "active",
                    "source_device_id": "device-test",
                    "value": "Story",
                }
            ),
            encoding="utf-8",
        )
        link_id = "credential-locator-source-link"
        link_path = self.root / "ledger" / "test" / (link_id + ".json")
        link_path.parent.mkdir(parents=True)
        link_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "id": link_id,
                    "created_at": "2026-07-24T00:00:01Z",
                    "kind": "source-link",
                    "scope": "global",
                    "statement": "Source confirms existing knowledge",
                    "source": marker,
                    "authority": "user-explicit",
                    "status": "active",
                    "source_device_id": "device-test",
                }
            ),
            encoding="utf-8",
        )
        disposition = {
            "format": self.batches.DISPOSITION_FORMAT,
            "batch_id": manifest["batch_id"],
            "dispositions": [
                {
                    "event_hash": "hash-001",
                    "outcome": "deduplicated",
                    "knowledge_ids": [credential_id, link_id],
                }
            ],
        }

        with self.assertRaisesRegex(
            self.batches.DispositionError,
            "disposition-credential-dedup-not-equivalent",
        ):
            self.batches.validate_disposition_document(
                batch, disposition, knowledge_root=self.root
            )

    def test_active_credential_capture_materializes_and_exactly_reads_back(self):
        staging = Path(self.temporary.name) / "credential-readback-staging"
        for name in ("scripts", "config", "schema"):
            shutil.copytree(ROOT / name, staging / name)
        shutil.copy2(ROOT / "MANIFEST.json", staging / "MANIFEST.json")
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=staging,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "add", "."], cwd=staging, check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Semantic Test",
                "-c",
                "user.email=semantic@example.test",
                "commit",
                "-m",
                "fixture",
            ],
            cwd=staging,
            check=True,
            capture_output=True,
        )
        runner = load_script("run_semantic_maintenance")
        runner.semantic_batches = self.batches
        value = "alpha-credential-7429"
        output = runner.run_capture(
            staging,
            event_id="semantic-test-store-api-key",
            event={"source_device_id": "device-test"},
            capture={
                "kind": "api-key",
                "scope": "project:store",
                "statement": "Store API key recovered from linked evidence",
                "value": value,
                "authority": "user-explicit",
                "status": "active",
            },
            source="semantic-test-source",
            value_root=Path(self.temporary.name) / "semantic-values",
        )

        self.assertTrue(output["ok"])
        canonical = json.loads(
            (
                staging
                / "resources"
                / "credentials"
                / "semantic-test-store-api-key.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(canonical["value"], value)

    def test_curate_fails_before_transaction_when_preflight_health_is_bad(self):
        self.add_event(1, "ordinary event must not be applied while unhealthy")
        batch = self.pending / "preflight-health-failure"
        self.batches.prepare(
            "incremental", batch, limit=10, maximum_characters=10000
        )
        runner = self.load_runner()
        with mock.patch.object(
            runner,
            "run_health_report",
            return_value={"ok": False, "issues": ["unexpected index schema"]},
        ), mock.patch.object(
            runner,
            "run_codex",
            side_effect=AssertionError("Codex must not run after failed preflight"),
        ):
            result = runner.curate()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "preflight-health-check-failed")
        self.assertEqual(result["batches_completed"], 0)
        self.assertFalse((batch / "transaction.json").exists())
        self.assertFalse((batch / "disposition.json").exists())
        self.assertFalse((batch / ".completed").exists())

    def test_curate_rejects_config_allowlisted_missing_credential(self):
        self.add_event(1, "ordinary event cannot hide missing credential health")
        runner = self.load_runner()
        runner.CONFIG = {
            "semantic_maintenance": {
                "allowed_credential_value_missing_ids": ["legacy-missing-id"]
            }
        }
        legacy_issue = (
            "credential-value-missing: resource legacy-missing-id "
            "in resources/credentials/legacy.json"
        )
        with mock.patch.object(
            runner,
            "run_health_report",
            return_value={"ok": False, "issues": [legacy_issue]},
        ), mock.patch.object(
            runner,
            "run_codex",
            side_effect=AssertionError(
                "Codex must not run while a credential value is missing"
            ),
        ):
            result = runner.curate()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "preflight-health-check-failed")
        self.assertEqual(result["health_issues"], [legacy_issue])
        self.assertEqual(result["batches_completed"], 0)

    def test_curate_never_reports_success_when_post_health_lacks_credential_value(self):
        self.add_event(1, "ordinary event before post-health credential failure")
        runner = self.load_runner()
        missing_issue = (
            "credential-value-missing: resource missing-after-capture "
            "in resources/credentials/missing.json"
        )
        with mock.patch.object(
            runner,
            "run_health_report",
            side_effect=[
                {"ok": True, "issues": []},
                {"ok": False, "issues": [missing_issue]},
            ],
        ), mock.patch.object(
            runner, "run_json", side_effect=self.runner_run_json()
        ), mock.patch.object(
            runner, "run_codex", side_effect=self.codex_writing_disposition()
        ), mock.patch.object(
            runner.hot_sync,
            "execute",
            return_value={"ok": True, "commit": "abc", "sync": {"ok": True}},
        ):
            result = runner.curate()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "health-check-failed")
        self.assertEqual(result["health_issues"], [missing_issue])
        self.assertEqual(result["events_processed"], 0)

    def test_semantic_settings_come_from_config_with_fallbacks(self):
        runner = load_script("run_semantic_maintenance")

        fallback = runner.load_semantic_settings({})
        self.assertEqual(fallback["model"], "")
        self.assertEqual(fallback["reasoning_effort"], "")
        self.assertEqual(fallback["maximum_batches_per_run"], 8)
        self.assertEqual(fallback["maximum_run_seconds"], 10800.0)

        configured = runner.load_semantic_settings(
            {
                "semantic_maintenance": {
                    "model": "model-x",
                    "reasoning_effort": "medium",
                    "maximum_batches_per_run": 3,
                    "maximum_run_seconds": 60,
                }
            }
        )
        self.assertEqual(configured["model"], "model-x")
        self.assertEqual(configured["reasoning_effort"], "medium")
        self.assertEqual(configured["maximum_batches_per_run"], 3)
        self.assertEqual(configured["maximum_run_seconds"], 60.0)

        # The module constants are wired to the repository config, not to a
        # second copy of the values inside the script.
        self.assertEqual(runner.CURATION_MODEL, runner.SEMANTIC_SETTINGS["model"])
        self.assertEqual(runner.AUDIT_MODEL, runner.SEMANTIC_SETTINGS["model"])
        self.assertEqual(
            runner.MAXIMUM_BATCHES_PER_RUN,
            runner.SEMANTIC_SETTINGS["maximum_batches_per_run"],
        )
        self.assertEqual(
            runner.MAXIMUM_RUN_SECONDS,
            runner.SEMANTIC_SETTINGS["maximum_run_seconds"],
        )


    def test_oversized_first_event_is_complete_and_requires_separate_budget(self):
        content = "Explicit fictional decision " + "界" * 12000
        self.add_event(1, content)
        batch = self.pending / "large-event"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=1000)
        manifest = self.batches.verify_batch_payload(batch)
        self.assertTrue(manifest["requires_explicit_large_event_budget"])
        self.assertGreater(manifest["content_characters"], manifest["character_budget"])
        self.assertEqual(self.batches.batch_events(batch)[0]["content"], content)
        runner = self.load_runner()
        with mock.patch.object(runner, "next_batch", return_value=batch), mock.patch.object(runner, "run_codex") as model:
            result = runner.curate()
        self.assertTrue(result["pending"])
        self.assertEqual(result["stop_reason"], "oversized-event-needs-budget")
        self.assertEqual(result["events_processed"], 0)
        self.assertFalse((batch / ".completed").exists())
        model.assert_not_called()

    def test_zero_model_call_budget_keeps_batch_uncompleted(self):
        self.add_event(1, "A fictional project decision must be retained.")
        batch = self.pending / "call-budget"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        runner = self.load_runner()
        runner.CONFIG = {"semantic_maintenance": {"maximum_model_calls": 0}}
        with mock.patch.object(runner, "next_batch", return_value=batch), mock.patch.object(runner, "run_codex") as model:
            result = runner.curate()
        self.assertEqual(result["stop_reason"], "model-call-budget")
        self.assertTrue(result["pending"])
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["usage"]["status"], "unknown")
        self.assertFalse((batch / ".completed").exists())
        model.assert_not_called()


    def test_foreground_proposal_cli_applies_through_real_transaction(self):
        self.add_event(1, "The fictional Aurora project uses a blue theme.")
        repository = self.disposable_repository("foreground-proposal")
        data = repository / ".kb-data"
        data.mkdir()
        shutil.copy2(self.history, data / "history.sqlite")
        self.batches.ROOT = repository
        self.batches.DATA = data
        self.batches.HISTORY = data / "history.sqlite"
        self.batches.SEMANTIC = data / "semantic.sqlite"
        self.batches.PENDING = data / "semantic/pending"
        batch = data / "semantic/pending/foreground"
        self.batches.prepare("incremental", batch, limit=10, maximum_characters=10000)
        manifest = self.batches.verify_batch_payload(batch)
        proposal = Path(self.temporary.name) / "proposal.json"
        proposal.write_text(json.dumps({"format": self.batches.PROPOSAL_FORMAT, "batch_id": manifest["batch_id"], "events": [{"event_hash": "hash-001", "outcome": "captured", "captures": [{"kind": "decision", "scope": "project:aurora", "statement": "The fictional Aurora project uses a blue theme.", "authority": "user-explicit", "status": "active"}], "existing_ids": []}]}))
        import sys
        cmd = [sys.executable, str(ROOT / "scripts/proposal_runner.py"), "--root", str(repository), "--batch", str(batch), "--proposal", str(proposal)]
        validated = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        self.assertEqual(json.loads(validated.stdout)["state"], "proposal-validated-not-applied")
        self.assertFalse((batch / ".completed").exists())
        applied = subprocess.run(cmd + ["--apply"], capture_output=True, text=True)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual(json.loads(applied.stdout)["state"], "applied")
        self.assertTrue((batch / ".completed").exists())
        records = [json.loads(p.read_text()) for p in (repository / "ledger").rglob("*.json")]
        self.assertTrue(any(x.get("statement") == "The fictional Aurora project uses a blue theme." for x in records))


if __name__ == "__main__":
    unittest.main()
