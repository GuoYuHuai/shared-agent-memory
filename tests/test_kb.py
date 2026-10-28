import json
import importlib.util
from contextlib import closing
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import datetime as dt
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CLI = REPOSITORY_ROOT / "scripts" / "kb.py"
SCHEMA = REPOSITORY_ROOT / "schema" / "knowledge.sql"
EVENTS_SCHEMA = REPOSITORY_ROOT / "schema" / "events.sql"


def load_kb_module():
    spec = importlib.util.spec_from_file_location("kb_for_projection_test", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_workspace_lock_module():
    path = REPOSITORY_ROOT / "scripts" / "workspace_lock.py"
    spec = importlib.util.spec_from_file_location("workspace_lock_for_cli_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class KnowledgeBaseCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "AI-Knowledge"
        (self.root / "schema").mkdir(parents=True)
        shutil.copy2(SCHEMA, self.root / "schema" / "knowledge.sql")
        (self.root / "config").mkdir()
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "kb-tests@example.test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Knowledge Tests"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(["git", "add", "schema"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "test-root"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *arguments, expected=0, env=None):
        command = [sys.executable, str(CLI), "--root", str(self.root)] + list(arguments)
        result = subprocess.run(command, text=True, capture_output=True, env=env)
        self.assertEqual(
            result.returncode,
            expected,
            "command: %s\nstdout:\n%s\nstderr:\n%s" % (command, result.stdout, result.stderr),
        )
        return result

    def json_cli(self, *arguments, expected=0, env=None):
        result = self.run_cli(*arguments, "--json", expected=expected, env=env)
        return json.loads(result.stdout)

    def write_devices(self, devices, stale_after_days=14):
        (self.root / "config" / "devices.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "stale_after_days": stale_after_days,
                    "devices": devices,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def init_history(self, events=()):
        database = self.root / ".kb-data" / "history.sqlite"
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(database))) as connection, connection:
            connection.executescript(EVENTS_SCHEMA.read_text(encoding="utf-8"))
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            for index, event in enumerate(events, 1):
                connection.execute(
                    """
                    INSERT INTO source_events(
                        event_hash, source_device_id, source, source_path,
                        source_offset, source_line, thread_id, event_id,
                        parent_event_id, role, event_type, event_timestamp,
                        cwd, content, raw_json, ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.get("event_hash", "history-hash-%d" % index),
                        event.get("source_device_id", "history-device"),
                        event.get("source", "codex"),
                        event.get("source_path", "/history/source-%d.jsonl" % index),
                        index * 100,
                        index,
                        event.get("thread_id", "thread-history"),
                        event.get("event_id", "message-%d" % index),
                        None,
                        event.get("role", "user"),
                        event.get("event_type", "message"),
                        event.get("event_timestamp", now),
                        event.get("cwd", "/projects/example"),
                        event["content"],
                        json.dumps({"content": event["content"]}),
                        event.get("ingested_at", now),
                    ),
                )
        return database

    def capture_rotated_key(self, event_id, statement, secret):
        return self.json_cli(
            "capture",
            "--id",
            event_id,
            "--kind",
            "api-key",
            "--scope",
            "project:gamma",
            "--statement",
            statement,
            "--value",
            secret,
            "--status",
            "active",
            "--source",
            "thread:gamma/%s" % event_id,
            "--authority",
            "user-explicit",
            "--source-device-id",
            "mac-mini-primary",
        )

    def test_init_and_show_paths(self):
        initialized = self.json_cli("init")
        self.assertTrue(initialized["ok"])
        self.assertEqual(initialized["canonical_documents"], 0)
        self.assertTrue((self.root / ".kb-data" / "knowledge.sqlite").is_file())

        paths = self.json_cli("show-paths")
        self.assertEqual(paths["root"], str(self.root.resolve()))
        self.assertTrue(paths["source_device_id"])
        self.assertEqual(paths["ledger"], str(self.root.resolve() / "ledger"))

    def test_capture_reports_saved_after_projection_failure_and_repairs_same_id(self):
        self.json_cli("init")
        module = load_kb_module()
        args = module.build_parser().parse_args(['--root', str(self.root), 'capture',
            '--id', 'capture-recovery', '--kind', 'fact', '--scope', 'project:atlas',
            '--statement', 'Atlas exact configuration', '--value', 'first\r\nsecond\r\n',
            '--source', 'fictional:recovery', '--authority', 'user-stated'])
        with mock.patch.object(module, 'hot_locator_record_source', side_effect=OSError('fictional-private-detail')):
            result = module.run(args)
        self.assertFalse(result['ok'])
        self.assertTrue(result['saved'])
        self.assertTrue(result['readback'])
        self.assertEqual(result['recovery']['failures'][0]['stage'], 'hot-locator')
        self.assertNotIn('fictional-private-detail', json.dumps(result))
        path = self.root / result['ledger']
        original = path.read_bytes()
        self.assertEqual(json.loads(original)['value'], 'first\r\nsecond\r\n')
        repaired = module.run(args)
        self.assertTrue(repaired['ok'], repaired)
        self.assertTrue(repaired['duplicate'])
        self.assertEqual(repaired['index_status'], 'current')
        self.assertEqual(path.read_bytes(), original)

    def test_capture_after_publication_durability_error_is_saved_not_an_unsaved_retry(self):
        self.json_cli("init")
        module = load_kb_module()
        args = module.build_parser().parse_args(['--root', str(self.root), 'capture',
            '--id', 'published-capture', '--kind', 'fact', '--scope', 'project:atlas',
            '--statement', 'Atlas durable fact', '--source', 'fictional:recovery', '--authority', 'user-stated'])
        publish = module.atomic_json_create
        def interrupted(path, value):
            publish(path, value)
            raise OSError('fictional directory durability error')
        with mock.patch.object(module, 'atomic_json_create', side_effect=interrupted):
            result = module.run(args)
        self.assertFalse(result['ok'])
        self.assertTrue(result['saved'])
        self.assertTrue(result['readback'])
        self.assertEqual(result['recovery']['failures'][0]['stage'], 'publication-durability')
        original = (self.root / result['ledger']).read_bytes()
        repaired = module.run(args)
        self.assertTrue(repaired['ok'], repaired)
        self.assertTrue(repaired['duplicate'])
        self.assertEqual((self.root / result['ledger']).read_bytes(), original)

    def test_capture_readback_failure_does_not_claim_verified_capture(self):
        self.json_cli("init")
        module = load_kb_module()
        args = module.build_parser().parse_args(['--root', str(self.root), 'capture',
            '--id', 'unreadable-capture', '--kind', 'fact', '--scope', 'project:atlas',
            '--statement', 'Atlas fact', '--source', 'fictional:recovery', '--authority', 'user-stated'])
        with mock.patch.object(module, 'load_event_reference', side_effect=PermissionError('fictional readback denied')):
            result = module.run(args)
        self.assertFalse(result['ok'])
        self.assertTrue(result['saved'])
        self.assertFalse(result['readback'])
        self.assertTrue(result['recovery_required'])
        self.assertEqual(result['recovery']['failures'][0]['stage'], 'readback')
        self.assertTrue(module.run(args)['readback'])

    def test_hot_mutations_fail_retryably_before_writes_while_workspace_is_locked(self):
        for event_id in (
            "evt-lock-old",
            "evt-lock-new",
            "evt-lock-review",
        ):
            self.json_cli(
                "capture",
                "--id",
                event_id,
                "--kind",
                "fact",
                "--scope",
                "project:lock-regression",
                "--statement",
                "Lock regression fixture %s" % event_id,
                "--source",
                "thread:lock-regression/%s" % event_id,
                "--authority",
                "user-explicit",
                "--source-device-id",
                "test-device",
            )

        materialize_id = "resource-lock-regression-repository"
        materialize_ledger = self.root / "ledger" / "2026" / "08"
        materialize_ledger.mkdir(parents=True, exist_ok=True)
        (materialize_ledger / (materialize_id + ".json")).write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "id": materialize_id,
                    "created_at": "2026-08-02T00:00:00Z",
                    "kind": "repository",
                    "scope": "global:test",
                    "statement": "Synthetic repository awaiting materialization",
                    "source": "thread:lock-regression/materialize",
                    "authority": "verified-current-system-state",
                    "status": "active",
                    "source_device_id": "test-device",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        def workspace_snapshot():
            snapshot = {}
            for path in sorted(self.root.rglob("*")):
                relative = path.relative_to(self.root)
                if relative.parts[0] in (".git", "generated"):
                    continue
                if path.is_dir():
                    snapshot[relative.as_posix()] = ("directory", None)
                elif path.is_file():
                    snapshot[relative.as_posix()] = ("file", path.read_bytes())
            return snapshot

        capture_arguments = (
            "capture",
            "--id",
            "evt-lock-blocked-then-retried",
            "--kind",
            "fact",
            "--scope",
            "project:lock-regression",
            "--statement",
            "Capture succeeds only after the workspace lock is released",
            "--source",
            "thread:lock-regression/retry",
            "--authority",
            "user-explicit",
            "--source-device-id",
            "test-device",
        )
        blocked_commands = (
            capture_arguments,
            ("materialize-resources",),
            (
                "supersede",
                "--old-id",
                "evt-lock-old",
                "--new-id",
                "evt-lock-new",
                "--source",
                "thread:lock-regression/supersede",
            ),
            (
                "resolve-candidate",
                "evt-lock-review",
                "--action",
                "reject",
            ),
        )
        before = workspace_snapshot()
        workspace_lock = load_workspace_lock_module()
        with workspace_lock.acquire(self.root, blocking=False):
            for command in blocked_commands:
                with self.subTest(command=command[0]):
                    result = self.json_cli(*command, expected=6)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"], "workspace-mutation-running")
                    self.assertEqual(result["exit_code"], 6)
                    self.assertTrue(result["retryable"])
                    self.assertEqual(workspace_snapshot(), before)

            plain = self.run_cli(*capture_arguments, expected=6)
            self.assertEqual(plain.stdout, "")
            self.assertEqual(
                plain.stderr,
                "error: workspace-mutation-running\n",
            )

            paths = self.json_cli("show-paths")
            self.assertTrue(paths["ok"])
            self.assertEqual(paths["root"], str(self.root.resolve()))
            self.assertEqual(workspace_snapshot(), before)

        captured = self.json_cli(*capture_arguments)
        self.assertTrue(captured["ok"])
        self.assertFalse(captured.get("duplicate", False))
        repeated = self.json_cli(*capture_arguments)
        self.assertTrue(repeated["ok"])
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(repeated["id"], captured["id"])
        self.assertEqual(
            len(
                list(
                    (self.root / "ledger").rglob(
                        "evt-lock-blocked-then-retried.json"
                    )
                )
            ),
            1,
        )
        self.assertIsNone(captured["candidate"])
        self.assertFalse(
            (
                self.root
                / "inbox"
                / "candidates"
                / "evt-lock-blocked-then-retried.json"
            ).exists()
        )

    def test_capture_is_immutable_searchable_and_device_attributed(self):
        self.json_cli("init")
        captured = self.json_cli(
            "capture",
            "--id",
            "evt-test-fact-1",
            "--kind",
            "fact",
            "--scope",
            "project:atlas",
            "--statement",
            "The release train uses build 157",
            "--value",
            "build-157",
            "--source",
            "thread:abc/message:12",
            "--authority",
            "user-explicit",
            "--source-device-id",
            "mac-mini-primary",
        )
        self.assertEqual(captured["source_device_id"], "mac-mini-primary")
        ledger = self.root / captured["ledger"]
        self.assertTrue(ledger.is_file())
        self.assertIsNone(captured["candidate"])
        event = json.loads(ledger.read_text(encoding="utf-8"))
        self.assertEqual(event["id"], "evt-test-fact-1")
        self.assertEqual(event["source_device_id"], "mac-mini-primary")
        self.assertTrue(event["created_at"])
        self.assertTrue(event["source"])

        searched = self.json_cli("search", "release train")
        self.assertEqual(searched["count"], 1)
        self.assertEqual(searched["results"][0]["id"], "evt-test-fact-1")
        self.assertEqual(
            searched["results"][0]["value"], "[stored; use --include-values]"
        )
        self.assertEqual(searched["results"][0]["source_device_id"], "mac-mini-primary")
        full = self.json_cli("search", "release train", "--include-values")
        self.assertEqual(full["results"][0]["value"], "build-157")

        conflict = self.json_cli(
            "capture",
            "--id",
            "evt-test-fact-1",
            "--kind",
            "fact",
            "--scope",
            "project:atlas",
            "--statement",
            "A conflicting replacement",
            "--source",
            "thread:different",
            "--authority",
            "user-explicit",
            "--source-device-id",
            "mac-mini-primary",
            expected=4,
        )
        self.assertFalse(conflict["ok"])
        self.assertIn("different content", conflict["error"])
        self.assertEqual(json.loads(ledger.read_text(encoding="utf-8"))["statement"], "The release train uses build 157")

    def test_atomic_json_writes_flush_parent_directory_before_success(self):
        module = load_kb_module()
        immutable = self.root / "ledger" / "2026" / "07" / "event.json"
        mutable = self.root / "inbox" / "candidates" / "event.json"

        with mock.patch.object(
            module,
            "fsync_directory",
            wraps=module.fsync_directory,
        ) as flush_directory:
            module.atomic_json_create(immutable, {"id": "event"})
            self.assertTrue(
                any(
                    call.args == (immutable.parent,)
                    for call in flush_directory.call_args_list
                )
            )

        module.atomic_json_create(mutable, {"state": "pending"})
        with mock.patch.object(
            module,
            "fsync_directory",
            wraps=module.fsync_directory,
        ) as flush_directory:
            module.atomic_json_replace(mutable, {"state": "accepted"})
            self.assertTrue(
                any(
                    call.args == (mutable.parent,)
                    for call in flush_directory.call_args_list
                )
            )
        self.assertEqual(
            json.loads(mutable.read_text(encoding="utf-8")),
            {"state": "accepted"},
        )

    @unittest.skipUnless(os.name == "nt", "Windows-specific directory handle behavior")
    def test_windows_directory_flush_accepts_unsupported_directory_flush(self):
        module = load_kb_module()
        module.fsync_directory(self.root)

    def test_atomic_json_create_publishes_only_a_complete_file(self):
        module = load_kb_module()
        path = self.root / "ledger" / "event.json"
        original_link = module.os.link
        observed = []

        def inspect_then_publish(source, destination):
            self.assertFalse(Path(destination).exists())
            observed.append(json.loads(Path(source).read_text(encoding="utf-8")))
            return original_link(source, destination)

        with mock.patch.object(module.os, "link", side_effect=inspect_then_publish):
            module.atomic_json_create(path, {"id": "event", "statement": "complete"})

        self.assertEqual(observed, [{"id": "event", "statement": "complete"}])
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            {"id": "event", "statement": "complete"},
        )
        self.assertEqual(list(path.parent.glob("*.tmp-*")), [])

    def test_directory_flush_failure_is_not_reported_as_atomic_success(self):
        module = load_kb_module()
        parent = self.root / "ledger"
        parent.mkdir(parents=True)
        path = parent / "event.json"
        with mock.patch.object(
            module,
            "fsync_directory",
            side_effect=OSError("simulated-directory-flush-failure"),
        ):
            with self.assertRaises(OSError):
                module.atomic_json_create(path, {"id": "event"})
        # The bytes may exist, but the caller saw failure and will retry or
        # reconcile; the API never claimed an unflushed directory entry.
        self.assertTrue(path.is_file())

    def test_hot_locator_git_delta_reads_only_the_new_ledger_candidate(self):
        module = load_kb_module()

        def event(index):
            return {
                "id": "evt-locator-%04d" % index,
                "created_at": "2026-07-23T10:00:00Z",
                "kind": "fact",
                "scope": "global",
                "statement": "historical locator fact %d" % index,
                "source": "test:locator",
                "authority": "user-explicit",
                "status": "active",
                "source_device_id": "mac-test",
            }

        ledger = self.root / "ledger" / "2026" / "07"
        ledger.mkdir(parents=True)
        for index in range(80):
            (ledger / ("evt-locator-%04d.json" % index)).write_text(
                json.dumps(event(index)), encoding="utf-8"
            )
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "locator@example.test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Locator Test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(["git", "add", "ledger"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "baseline"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        self.assertEqual(
            module.find_events_by_id(self.root, "missing-locator-id"), []
        )

        added = ledger / "evt-locator-9999.json"
        added.write_text(json.dumps(event(9999)), encoding="utf-8")
        subprocess.run(["git", "add", "ledger"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "incoming-device-event"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        parsed = []
        original = module.locator_json_values

        def observe(path):
            parsed.append(path)
            yield from original(path)

        with mock.patch.object(
            module, "ledger_files", side_effect=AssertionError("full scan")
        ), mock.patch.object(module, "locator_json_values", side_effect=observe):
            matches = module.find_events_by_id(self.root, "evt-locator-9999")
        self.assertEqual([item["id"] for item in matches], ["evt-locator-9999"])
        self.assertEqual(parsed, [added.resolve()])

    def test_hot_locator_recovers_corruption_and_candidate_crash_window(self):
        module = load_kb_module()
        event = {
            "id": "evt-locator-recovery",
            "created_at": "2026-07-23T10:00:00Z",
            "kind": "fact",
            "scope": "global",
            "statement": "locator recovery fact",
            "source": "test:locator-recovery",
            "authority": "user-explicit",
            "status": "active",
            "source_device_id": "mac-test",
        }
        self.assertEqual(module.find_events_by_id(self.root, event["id"]), [])
        ledger = self.root / "ledger" / "2026" / "07" / (event["id"] + ".json")
        candidate = self.root / "inbox" / "candidates" / (event["id"] + ".json")
        ledger.parent.mkdir(parents=True)
        candidate.parent.mkdir(parents=True)
        ledger.write_text(json.dumps(event), encoding="utf-8")
        pending = dict(event)
        pending["ledger_path"] = ledger.relative_to(self.root).as_posix()
        candidate.write_text(json.dumps(pending), encoding="utf-8")
        self.assertEqual(
            module.find_events_by_id(self.root, event["id"])[0]["statement"],
            event["statement"],
        )

        database = module.hot_locator_path(self.root)
        database.write_bytes(b"not-a-sqlite-database")
        self.assertEqual(
            module.find_events_by_id(self.root, event["id"])[0]["statement"],
            event["statement"],
        )

    def test_non_git_locator_fails_closed_without_full_tree_scan(self):
        module = load_kb_module()
        exported = self.root.parent / "Exported-AI-Knowledge"
        (exported / "ledger").mkdir(parents=True)
        (exported / "resources").mkdir()

        with mock.patch.object(
            module, "ledger_files", side_effect=AssertionError("full ledger scan")
        ), mock.patch.object(
            module,
            "resource_json_files",
            side_effect=AssertionError("full resource scan"),
        ):
            with self.assertRaisesRegex(
                module.KBError, "completeness unavailable outside.*Git"
            ):
                module.find_events_by_id(exported, "unknown-event")

    def test_cold_offline_ledger_search_uses_safe_hot_fts(self):
        module = load_kb_module()
        secret = "HOT-LOCATOR-SECRET-MUST-NOT-APPEAR-7755"
        event = {
            "id": "evt-hot-search-safe",
            "created_at": "2026-07-23T10:00:00Z",
            "kind": "credential",
            "scope": "project:同步",
            "statement": "知识库开发凭据 " + secret,
            "value": "different-private-value",
            "source": "thread:hot-search/" + secret,
            "authority": "user-explicit",
            "status": "active",
            "source_device_id": "mac-test",
        }
        ledger = self.root / "ledger" / "2026" / "07" / (event["id"] + ".json")
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "locator@example.test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Locator Test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(["git", "add", "ledger"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "hot-search"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        module.hot_locator_record_source(self.root, ledger)
        (self.root / ".kb-data").symlink_to(self.root / "missing-cold-data")
        args = mock.Mock(
            kind=None,
            scope=None,
            limit=12,
            include_values=False,
        )
        with mock.patch.object(
            module, "ledger_files", side_effect=AssertionError("full scan")
        ), mock.patch.object(
            module, "iter_json_values", side_effect=AssertionError("full parse")
        ):
            report = module.search_hot_layer(self.root, args, "同步")
        item = next(result for result in report["results"] if result["id"] == event["id"])
        self.assertEqual(item["value"], "[stored; use --include-values]")
        database = module.hot_locator_path(self.root)
        self.assertNotIn(secret.encode("utf-8"), database.read_bytes())

    def test_hot_locator_hides_credential_text_even_for_generic_event_kind(self):
        module = load_kb_module()
        secret = "sk-GENERIC-KIND-SECRET-882211"
        event = {
            "id": "evt-generic-kind-secret",
            "created_at": "2026-07-23T10:00:00Z",
            "kind": "fact",
            "scope": "project:generic",
            "statement": "temporary api key " + secret,
            "source": "thread:generic-secret",
            "authority": "user-explicit",
            "status": "active",
            "source_device_id": "mac-test",
        }
        ledger = self.root / "ledger" / "2026" / "07" / (event["id"] + ".json")
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps(event), encoding="utf-8")
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "locator@example.test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Locator Test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(["git", "add", "ledger"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "generic-secret"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        module.hot_locator_record_source(self.root, ledger)
        database = module.hot_locator_path(self.root)
        self.assertNotIn(secret.encode("utf-8"), database.read_bytes())

    def test_resource_locator_bounds_capture_and_get_to_matching_files(self):
        module = load_kb_module()
        resources = self.root / "resources" / "catalog"
        resources.mkdir(parents=True)
        for index in range(80):
            record = {
                "id": "resource-existing-%04d" % index,
                "kind": "service",
                "scope": "global",
                "name": "existing resource %d" % index,
            }
            (resources / ("existing-%04d.json" % index)).write_text(
                json.dumps(record), encoding="utf-8"
            )
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "locator@example.test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Locator Test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(["git", "add", "resources"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "resource-baseline"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        self.assertEqual(module.resource_matches(self.root, "missing-resource"), [])

        parsed = []
        original = module.iter_resource_records

        def observe(path):
            parsed.append(path)
            yield from original(path)

        event = {
            "id": "resource-new-bounded",
            "created_at": "2026-07-23T10:00:00Z",
            "kind": "service",
            "scope": "global",
            "statement": "new bounded service",
            "source": "test:resource-locator",
            "authority": "user-explicit",
            "status": "active",
            "source_device_id": "mac-test",
        }
        with mock.patch.object(
            module,
            "resource_json_files",
            side_effect=AssertionError("full resource scan"),
        ), mock.patch.object(module, "iter_resource_records", side_effect=observe):
            created = module.materialize_resource(
                self.root, event, "ledger/2026/07/resource-new-bounded.json"
            )
            matches = module.resource_matches(self.root, event["id"])
        self.assertEqual(matches[0][0], created.resolve())
        self.assertTrue(parsed)
        self.assertEqual(set(parsed), {created.resolve()})

    def test_credential_value_persists_but_never_enters_fts(self):
        self.json_cli("init")
        secret = "sk-simulated-EXACT-VALUE-90210"
        captured = self.json_cli(
            "capture",
            "--id",
            "cred-event-1",
            "--kind",
            "credential",
            "--scope",
            "project:beacon",
            "--statement",
            "Bailian development credential",
            "--value",
            secret,
            "--source",
            "thread:credentials/message:7",
            "--authority",
            "user-explicit",
            "--source-device-id",
            "mac-mini-primary",
        )
        event = json.loads((self.root / captured["ledger"]).read_text(encoding="utf-8"))
        self.assertEqual(event["value"], secret)

        default = self.json_cli("search", "Bailian development")
        self.assertEqual(default["results"][0]["value"], "[stored; use --include-values]")
        self.assertNotIn(secret, json.dumps(default, ensure_ascii=False))
        included = self.json_cli("search", "Bailian development", "--include-values")
        self.assertEqual(included["results"][0]["value"], secret)
        by_secret = self.json_cli("search", secret)
        self.assertEqual(by_secret["count"], 0)

        database = sqlite3.connect(str(self.root / ".kb-data" / "knowledge.sqlite"))
        try:
            history_rows = database.execute(
                "SELECT statement, source, authority, status, source_device_id, bundle_id, content FROM history_fts"
            ).fetchall()
        finally:
            database.close()
        self.assertNotIn(secret, " ".join(" ".join(str(value or "") for value in row) for row in history_rows))

    def test_natural_credential_kinds_hide_values_and_materialize(self):
        # POLICY.md §2 names API keys, tokens, passwords, certificates and
        # database connections explicitly; the natural --kind spellings must
        # behave exactly like --kind credential.
        self.json_cli("init")
        secret = "NATURAL-KIND-SENTINEL-9c1f"
        for index, kind in enumerate(
            ("api_key", "token", "password", "ssh-key", "certificate", "database-connection")
        ):
            event_id = "cred-natural-%d" % index
            captured = self.json_cli(
                "capture",
                "--id",
                event_id,
                "--kind",
                kind,
                "--scope",
                "project:beacon",
                "--statement",
                "natural credential kind %s" % kind,
                "--value",
                secret + kind,
                "--status",
                "active",
                "--source",
                "thread:natural/message:%d" % index,
                "--authority",
                "user-explicit",
                "--source-device-id",
                "mac-mini-primary",
            )
            self.assertTrue(
                captured.get("resource"),
                "kind %r must materialize a stable resources/ record" % kind,
            )
            full = self.json_cli("get-resource", event_id, "--include-values")
            self.assertEqual(full["record"]["value"], secret + kind)

        default = self.json_cli("search", "natural credential kind")
        self.assertNotIn(secret, json.dumps(default, ensure_ascii=False))
        for item in default["results"]:
            if item["result_type"] == "history":
                self.assertTrue(item["credential"], item["id"])

    def test_unknown_value_bearing_kind_is_sensitive_and_exactly_retrievable(self):
        self.json_cli("init")
        secret = "FREE-KIND-EXACT-SENTINEL-774411"
        captured = self.json_cli(
            "capture",
            "--id",
            "resource-free-kind-v1",
            "--kind",
            "future-vendor-access-profile",
            "--scope",
            "project:future",
            "--statement",
            "Future vendor reusable access",
            "--value",
            secret,
            "--status",
            "active",
            "--source",
            "thread:future/message:1",
            "--authority",
            "user-explicit",
            "--source-device-id",
            "mac-mini-primary",
        )
        self.assertEqual(
            captured["resource"],
            "resources/credentials/resource-free-kind-v1.json",
        )
        default = self.json_cli("search", "Future vendor reusable access")
        item = next(
            result
            for result in default["results"]
            if result["id"] == "resource-free-kind-v1"
        )
        self.assertTrue(item["credential"])
        self.assertEqual(item["value"], "[stored; use --include-values]")
        self.assertNotIn(secret, json.dumps(default, ensure_ascii=False))
        self.assertEqual(self.json_cli("search", secret)["count"], 0)

        redacted = self.json_cli("get-resource", "resource-free-kind-v1")
        self.assertTrue(redacted["credential"])
        self.assertEqual(
            redacted["record"]["value"], "[stored; use --include-values]"
        )
        full = self.json_cli(
            "get-resource", "resource-free-kind-v1", "--include-values"
        )
        self.assertEqual(full["record"]["kind"], "future-vendor-access-profile")
        self.assertEqual(full["record"]["value"], secret)

        database = sqlite3.connect(str(self.root / ".kb-data" / "knowledge.sqlite"))
        try:
            indexed = " ".join(
                str(item or "")
                for row in database.execute("SELECT * FROM history_fts")
                for item in row
            )
        finally:
            database.close()
        self.assertNotIn(secret, indexed)

        catalog_secret = "CATALOG-FREE-KIND-SENTINEL-228899"
        catalog = self.root / "resources" / "catalog" / "future-profile.json"
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_text(
            json.dumps(
                {
                    "id": "resource-catalog-free-kind",
                    "kind": "future-profile",
                    "scope": "project:future",
                    "name": "Future catalog access profile",
                    "value": catalog_secret,
                    "created_at": "2026-07-23T12:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        self.json_cli("rebuild-index")
        catalog_default = self.json_cli(
            "get-resource", "resource-catalog-free-kind"
        )
        self.assertTrue(catalog_default["credential"])
        self.assertEqual(
            catalog_default["record"]["value"], "[stored; use --include-values]"
        )
        self.assertNotIn(
            catalog_secret,
            json.dumps(
                self.json_cli("search", "Future catalog access profile"),
                ensure_ascii=False,
            ),
        )

    def test_value_file_preserves_private_key_and_default_search_hides_it(self):
        self.json_cli("init")
        secret = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\r\n"
            "PRIVATE-KEY-VALUE-NOT-IN-ARGV-44881\r\n"
            "-----END OPENSSH PRIVATE KEY-----\r\n"
        )
        value_file = Path(self.temporary.name) / "generated-test-key"
        value_file.write_bytes(secret.encode("utf-8"))

        captured = self.json_cli(
            "capture",
            "--id",
            "cred-event-value-file-1",
            "--kind",
            "credential",
            "--scope",
            "project:beacon",
            "--statement",
            "Generated SSH development key",
            "--value-file",
            str(value_file),
            "--source",
            "thread:credentials/message:8",
            "--authority",
            "user-explicit",
            "--source-device-id",
            "mac-mini-primary",
        )
        event = json.loads((self.root / captured["ledger"]).read_text(encoding="utf-8"))
        self.assertEqual(event["value"].encode("utf-8"), value_file.read_bytes())
        self.assertIsNone(captured["candidate"])

        default = self.json_cli("search", "Generated SSH development key")
        self.assertEqual(default["results"][0]["value"], "[stored; use --include-values]")
        self.assertNotIn("PRIVATE-KEY-VALUE-NOT-IN-ARGV-44881", json.dumps(default))
        included = self.json_cli(
            "search", "Generated SSH development key", "--include-values"
        )
        self.assertEqual(included["results"][0]["value"].encode("utf-8"), value_file.read_bytes())
        by_secret = self.json_cli("search", "PRIVATE-KEY-VALUE-NOT-IN-ARGV-44881")
        self.assertEqual(by_secret["count"], 0)

        conflict = self.run_cli(
            "capture",
            "--kind",
            "credential",
            "--scope",
            "project:beacon",
            "--statement",
            "Invalid dual value source",
            "--value",
            "inline",
            "--value-file",
            str(value_file),
            "--source",
            "thread:credentials/message:9",
            "--authority",
            "user-explicit",
            expected=2,
        )
        self.assertIn("not allowed with argument --value", conflict.stderr)

    def test_canonical_resource_redaction_and_full_retrieval(self):
        credential_dir = self.root / "resources" / "credentials"
        credential_dir.mkdir(parents=True)
        secret = "sim-password-FULL-1888"
        record = {
            "id": "resource:beacon:test-server",
            "kind": "credential-%s" % secret,
            "scope": "project:beacon:%s" % secret,
            "name": "PetNova %s test server" % secret,
            "provider": "example",
            "purpose": "development simulation",
            "account": "dev-user@example.test",
            "value": secret,
            "source": {"type": "user-explicit", "thread_id": "thread-1"},
            "authority": "user-explicit",
            "source_device_id": "mac-mini-primary",
            "created_at": "2026-07-22T10:00:00Z",
            "status": "active",
        }
        record["source_device_id"] = "mac-mini-%s" % secret
        resource_path = credential_dir / ("beacon-%s.json" % secret)
        resource_path.write_text(json.dumps(record), encoding="utf-8")
        self.json_cli("init")

        redacted = self.json_cli("get-resource", "resource:beacon:test-server")
        self.assertEqual(redacted["record"]["value"], "[stored; use --include-values]")
        self.assertEqual(redacted["record"]["account"], "[stored; use --include-values]")
        self.assertNotIn(secret, json.dumps(redacted))
        full = self.json_cli("get-resource", "resource:beacon:test-server", "--include-values")
        self.assertEqual(full["record"]["value"], secret)
        self.assertEqual(full["record"]["account"], "dev-user@example.test")
        self.assertIn(secret, full["record"]["name"])
        self.assertIn(secret, full["record"]["kind"])
        self.assertIn(secret, full["record"]["scope"])
        self.assertIn(secret, full["record"]["source_device_id"])

        searched = self.json_cli("search", "development simulation")
        self.assertEqual(searched["count"], 1)
        self.assertNotIn(secret, json.dumps(searched))
        hot_args = type(
            "Args",
            (),
            {
                "kind": None,
                "scope": None,
                "limit": 20,
                "include_values": False,
            },
        )()
        hot = load_kb_module().search_hot_layer(
            self.root,
            hot_args,
            "development simulation",
        )
        self.assertEqual(hot["count"], 1)
        self.assertNotIn(secret, json.dumps(hot))
        database = sqlite3.connect(str(self.root / ".kb-data" / "knowledge.sqlite"))
        try:
            indexed = " ".join(str(item or "") for row in database.execute("SELECT * FROM canonical_fts") for item in row)
        finally:
            database.close()
        self.assertNotIn(secret, indexed)
        self.assertNotIn("dev-user@example.test", indexed)
        self.init_history()
        health = self.json_cli("health")
        self.assertTrue(health["ok"], health["issues"])

    def test_canonical_credential_json_escaping_never_enters_fts(self):
        credential_dir = self.root / "resources" / "credentials"
        credential_dir.mkdir(parents=True)
        secret = 'synthetic-"quoted"\\line\nnext'
        record = {
            "id": "resource:escaped:credential",
            "kind": "credential",
            "scope": "project:escaped",
            "name": "Escaped credential %s" % secret,
            "purpose": "JSON escaping regression %s" % secret,
            "value": secret,
            "source": {"type": "user-explicit", "thread_id": "thread-escaped"},
            "authority": "user-explicit",
            "source_device_id": "device-escaped",
            "created_at": "2026-07-30T00:00:00Z",
            "status": "active",
        }
        (credential_dir / "escaped.json").write_text(
            json.dumps(record),
            encoding="utf-8",
        )
        self.json_cli("init")

        database = sqlite3.connect(
            str(self.root / ".kb-data" / "knowledge.sqlite")
        )
        try:
            indexed = " ".join(
                str(item or "")
                for row in database.execute("SELECT * FROM canonical_fts")
                for item in row
            )
        finally:
            database.close()
        self.assertNotIn(secret, indexed)
        self.assertNotIn(
            json.dumps(secret, ensure_ascii=False)[1:-1],
            indexed,
        )

        hidden = self.json_cli("get-resource", record["id"])
        self.assertNotIn(secret, json.dumps(hidden, ensure_ascii=False))
        full = self.json_cli("get-resource", record["id"], "--include-values")
        self.assertEqual(full["record"]["value"], secret)

    def test_missing_credential_value_is_never_reported_as_stored(self):
        credential_dir = self.root / "resources" / "credentials"
        credential_dir.mkdir(parents=True)
        record = {
            "id": "resource:missing:credential",
            "kind": "credential",
            "scope": "project:missing",
            "name": "Missing exact vendor credential",
            "source": {"type": "user-explicit", "thread_id": "thread-missing"},
            "authority": "user-explicit",
            "source_device_id": "device-missing",
            "created_at": "2026-07-24T00:00:00Z",
            "status": "active",
        }
        (credential_dir / "missing.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
        module = load_kb_module()
        hot = module.search_hot_layer(
            self.root,
            type(
                "Args",
                (),
                {
                    "kind": None,
                    "scope": None,
                    "limit": 20,
                    "include_values": False,
                },
            )(),
            "Missing exact vendor credential",
        )
        hot_item = hot["results"][0]
        self.assertEqual(hot_item["credential_value_status"], "missing")
        self.assertEqual(
            hot_item["value"],
            "[missing; exact credential value was not captured]",
        )
        self.json_cli("init")
        self.init_history()

        searched = self.json_cli("search", "Missing exact vendor credential")
        item = searched["results"][0]
        self.assertEqual(item["credential_value_status"], "missing")
        self.assertEqual(
            item["value"], "[missing; exact credential value was not captured]"
        )
        self.assertNotIn("[stored; use --include-values]", json.dumps(item))

        missing = self.json_cli(
            "get-resource", "resource:missing:credential", expected=5
        )
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"], "credential-value-missing")

        health = self.json_cli("health", expected=5)
        self.assertTrue(
            any("credential-value-missing" in issue for issue in health["issues"])
        )

    def test_superseded_missing_credential_is_healthy_only_with_exact_replacement(self):
        credential_dir = self.root / "resources" / "credentials"
        credential_dir.mkdir(parents=True)
        old_id = "resource:missing:credential"
        replacement_id = "resource-complete-credential"
        (credential_dir / "missing.json").write_text(
            json.dumps(
                {
                    "id": old_id,
                    "kind": "credential",
                    "scope": "project:missing",
                    "name": "Legacy credential locator without an exact value",
                    "source": {
                        "type": "user-explicit",
                        "thread_id": "thread-missing",
                    },
                    "authority": "user-explicit",
                    "source_device_id": "device-missing",
                    "created_at": "2026-07-24T00:00:00Z",
                    "status": "active",
                }
            ),
            encoding="utf-8",
        )
        self.json_cli("init")
        self.init_history()
        self.json_cli(
            "capture",
            "--id",
            replacement_id,
            "--kind",
            "api-key",
            "--scope",
            "project:missing",
            "--statement",
            "Recovered exact vendor API key",
            "--source",
            "verified:source-device",
            "--authority",
            "verified-current-system",
            "--status",
            "active",
            "--value",
            "exact-test-value",
        )
        self.json_cli(
            "supersede",
            "--old-id",
            old_id,
            "--new-id",
            replacement_id,
            "--source",
            "verified:credential-recovery",
        )

        health = self.json_cli("health")
        self.assertTrue(health["ok"])
        self.assertFalse(
            any(
                old_id in issue and "credential-value-missing" in issue
                for issue in health["issues"]
            )
        )

    def test_superseded_missing_credential_stays_unhealthy_with_incomplete_replacement(self):
        credential_dir = self.root / "resources" / "credentials"
        credential_dir.mkdir(parents=True)
        old_id = "resource-old-missing-credential"
        replacement_id = "resource-new-still-missing-credential"
        for resource_id in (old_id, replacement_id):
            (credential_dir / (resource_id + ".json")).write_text(
                json.dumps(
                    {
                        "id": resource_id,
                        "kind": "api key",
                        "scope": "project:missing",
                        "name": "Incomplete development credential",
                        "source": {"type": "user-explicit"},
                        "authority": "user-explicit",
                        "source_device_id": "device-missing",
                        "created_at": "2026-07-24T00:00:00Z",
                        "status": "active",
                    }
                ),
                encoding="utf-8",
            )
        self.json_cli("init")
        self.init_history()
        self.json_cli(
            "supersede",
            "--old-id",
            old_id,
            "--new-id",
            replacement_id,
            "--source",
            "verified:credential-recovery",
        )

        health = self.json_cli("health", expected=5)
        self.assertFalse(health["ok"])
        missing_ids = " ".join(
            issue
            for issue in health["issues"]
            if "credential-value-missing" in issue
        )
        self.assertIn(old_id, missing_ids)
        self.assertIn(replacement_id, missing_ids)

    def test_active_missing_ledger_credential_is_not_masked_by_catalog_record(self):
        event_id = "resource-ledger-missing-same-id"
        catalog = self.root / "resources" / "catalog"
        catalog.mkdir(parents=True)
        (catalog / (event_id + ".json")).write_text(
            json.dumps(
                {
                    "id": event_id,
                    "kind": "service",
                    "scope": "project:missing",
                    "name": "Ordinary service metadata",
                    "source": {"type": "verified-current-system"},
                    "source_device_id": "device-missing",
                    "observed_at": "2026-07-24T00:00:00Z",
                    "status": "active",
                }
            ),
            encoding="utf-8",
        )
        ledger = self.root / "ledger" / "2026" / "07"
        ledger.mkdir(parents=True)
        (ledger / (event_id + ".json")).write_text(
            json.dumps(
                {
                    "id": event_id,
                    "created_at": "2026-07-24T00:00:00Z",
                    "kind": "api key",
                    "scope": "project:missing",
                    "statement": "Reusable development API key",
                    "source": "thread:missing/same-id",
                    "authority": "user-explicit",
                    "status": "active",
                    "source_device_id": "device-missing",
                }
            ),
            encoding="utf-8",
        )
        self.json_cli("init")
        self.init_history()

        health = self.json_cli("health", expected=5)
        self.assertTrue(
            any(
                event_id in issue and "credential-value-missing" in issue
                for issue in health["issues"]
            )
        )

    def test_active_credential_capture_requires_exact_value(self):
        rejected = self.json_cli(
            "capture",
            "--id",
            "resource-missing-active-credential",
            "--kind",
            "api-key",
            "--scope",
            "project:missing",
            "--statement",
            "Reusable vendor API key",
            "--source",
            "thread:missing/message:1",
            "--authority",
            "user-explicit",
            "--status",
            "active",
            expected=1,
        )
        self.assertIn("credential-value-missing", rejected["error"])
        self.assertFalse(
            (self.root / "ledger").exists()
            and any(
                (self.root / "ledger").rglob(
                    "resource-missing-active-credential.json"
                )
            )
        )

    def test_credential_placeholders_are_not_exact_values(self):
        for index, placeholder in enumerate(
            (
                "[redacted]",
                "stored elsewhere",
                "stored in Story",
                "REDACTED_VALUE",
                "value stored elsewhere",
                "unknown",
                "TBD",
                "N/A",
                "not available",
                "not captured",
                "EXAMPLE",
                "Dummy",
                "sample",
                "<YOUR_API_KEY>",
                "<your api key>",
                "YOUR-API-KEY-HERE",
                "<API_KEY>",
                "<TOKEN>",
                "REPLACE_WITH_YOUR_API_KEY",
                "INSERT_API_KEY_HERE",
                "changeme",
                "CHANGE ME",
                "change-me",
                "replace_me",
                "<REPLACE-ME>",
                "***",
                "xxx",
                "...",
                "••••",
            )
        ):
            with self.subTest(placeholder=placeholder):
                rejected = self.json_cli(
                    "capture",
                    "--id",
                    "resource-placeholder-credential-%d" % index,
                    "--kind",
                    "api-key",
                    "--scope",
                    "project:missing",
                    "--statement",
                    "Reusable vendor API key",
                    "--source",
                    "thread:missing/placeholder",
                    "--authority",
                    "user-explicit",
                    "--status",
                    "active",
                    "--value",
                    placeholder,
                    expected=1,
                )
                self.assertIn("credential-value-missing", rejected["error"])

    def test_credential_capture_gate_cannot_be_bypassed_by_classification(self):
        cases = (
            (
                "missing-project-fact",
                "project-fact",
                "Vendor API key is stored in Story",
                "thread:missing/project-fact",
                "user-explicit",
            ),
            (
                "missing-active-inference",
                "credential",
                "Reusable vendor credential",
                "thread:missing/inference",
                "agent-inference",
            ),
            (
                "missing-secret-source",
                "resource",
                "Reusable vendor access",
                "file:secrets/vendor",
                "user-explicit",
            ),
            (
                "missing-free-kind-note",
                "note",
                "Vendor API key is stored in Story",
                "thread:missing/free-kind",
                "user-explicit",
            ),
            (
                "missing-free-kind-token",
                "note",
                "Vendor token is stored in Story",
                "thread:missing/free-token",
                "user-explicit",
            ),
            (
                "missing-free-kind-ssh-key",
                "note",
                "Vendor SSH key is stored in Story",
                "thread:missing/free-ssh-key",
                "user-explicit",
            ),
            (
                "missing-compound-claim",
                "note",
                "Old API key is missing; new API key is stored in Story",
                "thread:missing/compound",
                "user-explicit",
            ),
            (
                "missing-policy-prefaced-claim",
                "note",
                "Per policy, vendor API key is stored in Story",
                "thread:missing/policy-preface",
                "user-explicit",
            ),
        )
        for event_id, kind, statement, source, authority in cases:
            with self.subTest(event_id=event_id):
                rejected = self.json_cli(
                    "capture",
                    "--id",
                    event_id,
                    "--kind",
                    kind,
                    "--scope",
                    "project:missing",
                    "--statement",
                    statement,
                    "--source",
                    source,
                    "--authority",
                    authority,
                    "--status",
                    "active",
                    expected=1,
                )
                self.assertIn("credential-value-missing", rejected["error"])

        module = load_kb_module()
        self.assertTrue(
            module.usable_credential_value(
                "changeme-7f2c8a91d4e6407bab0cb65f-real-looking-token"
            )
        )

    def test_credential_kind_and_field_aliases_cannot_bypass_classification(self):
        module = load_kb_module()
        for index, kind in enumerate(
            ("api key", "client-secret", "database-url", "session-cookie")
        ):
            with self.subTest(kind=kind):
                self.assertTrue(module.is_credential_kind(kind))
                rejected = self.json_cli(
                    "capture",
                    "--id",
                    "resource-kind-alias-%d" % index,
                    "--kind",
                    kind,
                    "--scope",
                    "project:alias",
                    "--statement",
                    "Reusable development credential",
                    "--source",
                    "thread:alias/kind",
                    "--authority",
                    "user-explicit",
                    "--status",
                    "active",
                    expected=1,
                )
                self.assertIn("credential-value-missing", rejected["error"])

        for field in (
            "api_key",
            "apiKey",
            "clientSecret",
            "databaseUrl",
            "sessionCookie",
        ):
            with self.subTest(field=field):
                record = {
                    "id": "resource-field-alias",
                    "kind": "note",
                    "statement": "Reusable development access",
                    field: "synthetic-alias-value",
                }
                self.assertTrue(module.event_is_credential_resource(record))
                self.assertTrue(module.credential_value_present(record))
                redacted = module.redact_resource(record)
                self.assertNotIn("synthetic-alias-value", json.dumps(redacted))

    def test_credential_claim_classifier_ignores_reference_only_metadata(self):
        module = load_kb_module()
        negative_cases = (
            {
                "id": "sup-credential-recovery",
                "kind": "supersession",
                "statement": (
                    "resource-missing-credential is superseded by "
                    "resource-complete-api-key"
                ),
                "source": "verified:credential-recovery",
            },
            {
                "id": "resource-repository-fingerprint",
                "kind": "repository",
                "statement": "Dedicated GitHub SSH key fingerprint is SHA256:test",
                "source": "verified:repository",
            },
            {
                "id": "resource-service-auth-method",
                "kind": "service",
                "statement": "Service supports API key authentication",
                "source": "verified:service",
            },
            {
                "id": "ordinary-secretary-note",
                "kind": "note",
                "statement": "Ordinary project status",
                "source": "thread:secretary-notes",
            },
            {
                "id": "fact-missing-credential-diagnostic",
                "kind": "project-fact",
                "statement": "The vendor API key was not captured and remains missing",
                "source": "verified:diagnostic",
            },
            {
                "id": "fact-missing-resource-diagnostic",
                "kind": "project-fact",
                "statement": (
                    "The Story/API Key record was not captured; the old artifact "
                    "was located under resources/credentials and remains missing"
                ),
                "source": "verified:diagnostic",
            },
        )
        for event in negative_cases:
            with self.subTest(event_id=event["id"]):
                self.assertFalse(module.event_requires_credential_value(event))
                self.assertFalse(module.event_is_sensitive(event))
        self.assertTrue(
            module.event_requires_credential_value(
                {
                    "id": "free-kind-positive",
                    "kind": "note",
                    "statement": "Vendor API key is stored in Story",
                    "source": "thread:positive",
                }
            )
        )
        self.assertTrue(
            module.event_is_sensitive(
                {
                    "id": "free-kind-token-positive",
                    "kind": "note",
                    "statement": "Vendor token is stored in Story",
                    "source": "thread:positive",
                }
            )
        )

    def test_missing_credential_history_is_marked_in_hot_and_indexed_search(self):
        module = load_kb_module()
        event = {
            "id": "evt-missing-history-credential",
            "created_at": "2026-07-24T00:00:00Z",
            "kind": "project-fact",
            "scope": "project:missing",
            "statement": "Missing history API key is stored in Story",
            "source": "thread:missing/history",
            "authority": "user-explicit",
            "status": "active",
            "source_device_id": "device-missing",
        }
        ledger = self.root / "ledger" / "2026" / "07" / (event["id"] + ".json")
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps(event), encoding="utf-8")
        module.hot_locator_record_source(self.root, ledger)
        args = type(
            "Args",
            (),
            {
                "kind": None,
                "scope": None,
                "limit": 20,
                "include_values": False,
            },
        )()
        hot = module.search_hot_layer(self.root, args, "Missing history API key")
        hot_item = next(item for item in hot["results"] if item["id"] == event["id"])
        self.assertEqual(hot_item["credential_value_status"], "missing")
        self.assertEqual(
            hot_item["value"],
            "[missing; exact credential value was not captured]",
        )

        self.json_cli("init")
        indexed = self.json_cli("search", "Missing history API key")
        indexed_item = next(
            item for item in indexed["results"] if item["id"] == event["id"]
        )
        self.assertEqual(indexed_item["credential_value_status"], "missing")
        self.assertEqual(
            indexed_item["value"],
            "[missing; exact credential value was not captured]",
        )

    def test_active_explicit_credential_is_immediately_retrievable(self):
        secret = "explicit-secret-value-88012"
        captured = self.json_cli(
            "capture",
            "--id",
            "resource-explicit-test-credential-v1",
            "--kind",
            "credential",
            "--scope",
            "global:test",
            "--statement",
            "Reusable development test credential",
            "--value",
            secret,
            "--source",
            "thread:test/message:resource",
            "--authority",
            "user-explicit",
            "--status",
            "active",
        )
        self.assertEqual(
            captured["resource"],
            "resources/credentials/resource-explicit-test-credential-v1.json",
        )
        hidden = self.json_cli(
            "get-resource", "resource-explicit-test-credential-v1"
        )
        self.assertNotIn(secret, json.dumps(hidden))
        self.assertEqual(hidden["record"]["value"], "[stored; use --include-values]")
        full = self.json_cli(
            "get-resource",
            "resource-explicit-test-credential-v1",
            "--include-values",
        )
        self.assertEqual(full["record"]["value"], secret)
        self.assertNotIn("statement", full["record"])
        self.json_cli("init")
        database = sqlite3.connect(
            str(self.root / ".kb-data" / "knowledge.sqlite")
        )
        try:
            indexed = " ".join(
                str(item or "")
                for row in database.execute("SELECT * FROM canonical_fts")
                for item in row
            )
        finally:
            database.close()
        self.assertNotIn(secret, indexed)
        self.init_history()
        health = self.json_cli("health")
        self.assertTrue(health["ok"], health["issues"])
        repeated = self.json_cli(
            "capture",
            "--id",
            "resource-explicit-test-credential-v1",
            "--kind",
            "credential",
            "--scope",
            "global:test",
            "--statement",
            "Reusable development test credential",
            "--value",
            secret,
            "--source",
            "thread:test/message:resource",
            "--authority",
            "user-explicit",
            "--status",
            "active",
        )
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(repeated["resource"], captured["resource"])

    def test_active_inferred_resource_remains_review_only(self):
        captured = self.json_cli(
            "capture",
            "--id",
            "resource-inferred-only-v1",
            "--kind",
            "resource",
            "--scope",
            "global:test",
            "--statement",
            "Inferred resource that still needs review",
            "--source",
            "agent:audit",
            "--authority",
            "agent-inference",
            "--status",
            "active",
        )
        self.assertIsNone(captured["resource"])
        missing = self.run_cli(
            "get-resource", "resource-inferred-only-v1", "--json", expected=3
        )
        self.assertIn("resource not found", missing.stdout)

    def test_agent_authority_variants_default_to_review_candidates(self):
        for index, authority in enumerate(
            (
                "agent-inference-from-user-output-and-history",
                "agent-recommendation",
                "agent-observation",
            ),
            1,
        ):
            event_id = "agent-derived-default-%d" % index
            captured = self.json_cli(
                "capture",
                "--id",
                event_id,
                "--kind",
                "fact",
                "--scope",
                "project:test",
                "--statement",
                "Agent-derived statement %d" % index,
                "--source",
                "agent:test",
                "--authority",
                authority,
            )
            self.assertEqual(
                captured["candidate"],
                "inbox/candidates/%s.json" % event_id,
            )
            ledger = next((self.root / "ledger").rglob(event_id + ".json"))
            event = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(event["status"], "candidate")

    def test_agent_verified_defaults_to_active_not_inference(self):
        captured = self.json_cli(
            "capture",
            "--id",
            "agent-verified-active",
            "--kind",
            "fact",
            "--scope",
            "project:test",
            "--statement",
            "Verified observation with exact evidence",
            "--source",
            "agent:neutral-audit",
            "--authority",
            "agent-verified",
        )
        self.assertIsNone(captured["candidate"])
        ledger = next(
            (self.root / "ledger").rglob("agent-verified-active.json")
        )
        self.assertEqual(
            json.loads(ledger.read_text(encoding="utf-8"))["status"],
            "active",
        )

    def test_agent_verified_inference_cannot_bypass_review(self):
        captured = self.json_cli(
            "capture",
            "--id",
            "agent-verified-inference-review",
            "--kind",
            "api-key",
            "--scope",
            "project:test",
            "--statement",
            "Mixed authority label remains an inference",
            "--value",
            "INFERRED-SECRET-MUST-STAY-CANDIDATE",
            "--source",
            "agent:neutral-audit",
            "--authority",
            "agent-verified-inference",
        )
        self.assertEqual(
            captured["candidate"],
            "inbox/candidates/agent-verified-inference-review.json",
        )
        self.assertIsNone(captured["resource"])
        ledger = next(
            (self.root / "ledger").rglob(
                "agent-verified-inference-review.json"
            )
        )
        self.assertEqual(
            json.loads(ledger.read_text(encoding="utf-8"))["status"],
            "candidate",
        )

    def test_accepting_inferred_resource_makes_exact_value_retrievable(self):
        secret = "ACCEPTED-INFERENCE-EXACT-7788"
        captured = self.json_cli(
            "capture",
            "--id",
            "resource-inferred-accepted-v1",
            "--kind",
            "vendor-login-profile",
            "--scope",
            "global:test",
            "--statement",
            "Inferred reusable vendor login awaiting review",
            "--value",
            secret,
            "--source",
            "agent:audit",
            "--authority",
            "agent-inference",
        )
        self.assertIsNone(captured["resource"])
        self.assertTrue((self.root / captured["candidate"]).is_file())
        self.run_cli(
            "get-resource",
            "resource-inferred-accepted-v1",
            "--json",
            expected=3,
        )

        accepted = self.json_cli(
            "resolve-candidate",
            "resource-inferred-accepted-v1",
            "--action",
            "accept",
        )
        self.assertEqual(accepted["candidate_state"], "accepted")
        self.assertEqual(
            accepted["resource"],
            "resources/credentials/resource-inferred-accepted-v1.json",
        )
        resource = self.json_cli(
            "get-resource",
            "resource-inferred-accepted-v1",
            "--include-values",
        )
        self.assertEqual(resource["record"]["value"], secret)
        self.assertEqual(
            resource["record"]["authority"],
            "maintainer-accepted-inference",
        )
        self.assertEqual(
            resource["record"]["accepted_candidate_id"],
            "resource-inferred-accepted-v1",
        )
        self.assertEqual(
            len(resource["record"]["source"]["event_ids"]),
            2,
        )

        repeated = self.json_cli(
            "resolve-candidate",
            "resource-inferred-accepted-v1",
            "--action",
            "accept",
        )
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(repeated["resource"], accepted["resource"])
        self.assertEqual(
            len(list((self.root / "ledger").rglob("cres-*.json"))),
            1,
        )
        self.init_history()
        self.json_cli("init")
        healthy = self.json_cli("health")
        self.assertTrue(healthy["ok"])

    def test_missing_credential_candidate_cannot_be_accepted_or_resolved(self):
        event_id = "resource-inferred-missing-credential"
        ledger_before = set((self.root / "ledger").rglob("*.json"))
        rejected_capture = self.json_cli(
            "capture",
            "--id",
            event_id,
            "--kind",
            "clientSecret",
            "--scope",
            "project:missing",
            "--statement",
            "Inferred reusable client credential",
            "--source",
            "agent:test",
            "--authority",
            "agent-inference",
            "--status",
            "candidate",
            expected=1,
        )
        self.assertIn(
            "credential-value-missing", rejected_capture["error"]
        )
        candidate_path = (
            self.root / "inbox" / "candidates" / (event_id + ".json")
        )
        self.assertFalse(candidate_path.exists())
        self.assertEqual(
            set((self.root / "ledger").rglob("*.json")), ledger_before
        )

        # Defense in depth for a legacy/manual candidate created before the
        # capture gate existed.
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_candidate = {
            "schema_version": "1",
            "id": event_id,
            "created_at": "2026-07-24T00:00:00Z",
            "kind": "clientSecret",
            "scope": "project:missing",
            "statement": "Inferred reusable client credential",
            "source": "agent:test",
            "authority": "agent-inference",
            "status": "candidate",
            "source_device_id": "device-test",
            "candidate_state": "pending-review",
            "ledger_path": "ledger/2026/07/%s.json" % event_id,
        }
        candidate_path.write_text(
            json.dumps(legacy_candidate), encoding="utf-8"
        )
        before = candidate_path.read_bytes()
        resolution_files_before = set((self.root / "ledger").rglob("cres-*.json"))

        rejected_accept = self.json_cli(
            "resolve-candidate",
            event_id,
            "--action",
            "accept",
            expected=1,
        )
        self.assertIn(
            "credential-value-missing", rejected_accept["error"]
        )
        self.assertEqual(candidate_path.read_bytes(), before)
        self.assertEqual(
            set((self.root / "ledger").rglob("cres-*.json")),
            resolution_files_before,
        )

    def test_accept_candidate_conflict_and_kb_error_leave_no_partial_state(self):
        value = "candidate-atomic-value-7788"
        event_id = "resource-candidate-atomic-v1"
        captured = self.json_cli(
            "capture",
            "--id",
            event_id,
            "--kind",
            "api-key",
            "--scope",
            "project:atomic",
            "--statement",
            "Atomic candidate API key",
            "--value",
            value,
            "--source",
            "agent:test",
            "--authority",
            "agent-inference",
            "--status",
            "candidate",
        )
        candidate_path = self.root / captured["candidate"]
        candidate_before = candidate_path.read_bytes()
        resolution_before = set((self.root / "ledger").rglob("cres-*.json"))
        resource_path = (
            self.root / "resources" / "credentials" / (event_id + ".json")
        )
        resource_path.parent.mkdir(parents=True)
        resource_path.write_text(
            json.dumps(
                {
                    "id": event_id,
                    "kind": "api-key",
                    "scope": "project:atomic",
                    "name": "Conflicting canonical value",
                    "status": "active",
                    "authority": "user-explicit",
                    "value": "different-canonical-value-9914",
                }
            ),
            encoding="utf-8",
        )
        resource_before = resource_path.read_bytes()

        conflict = self.json_cli(
            "resolve-candidate",
            event_id,
            "--action",
            "accept",
            expected=4,
        )
        self.assertIn("different canonical record", conflict["error"])
        self.assertEqual(candidate_path.read_bytes(), candidate_before)
        self.assertEqual(resource_path.read_bytes(), resource_before)
        self.assertEqual(
            set((self.root / "ledger").rglob("cres-*.json")),
            resolution_before,
        )

        resource_path.unlink()
        module = load_kb_module()
        arguments = mock.Mock(
            event_id=event_id, action="accept", note=None
        )
        with mock.patch.object(
            module,
            "materialize_accepted_candidate",
            side_effect=module.KBError("synthetic materialization failure"),
        ):
            with self.assertRaisesRegex(
                module.KBError, "synthetic materialization failure"
            ):
                module.resolve_candidate(self.root, arguments)
        self.assertEqual(candidate_path.read_bytes(), candidate_before)
        self.assertFalse(resource_path.exists())
        self.assertEqual(
            set((self.root / "ledger").rglob("cres-*.json")),
            resolution_before,
        )

    def test_health_rejects_legacy_missing_and_half_accepted_candidates(self):
        self.json_cli("init")
        self.init_history()
        ledger = self.root / "ledger" / "2026" / "07"
        ledger.mkdir(parents=True)
        missing_id = "resource-legacy-missing-candidate"
        missing_event = {
            "schema_version": "1",
            "id": missing_id,
            "created_at": "2026-07-24T00:00:00Z",
            "kind": "api-key",
            "scope": "project:legacy",
            "statement": "Legacy credential candidate",
            "source": "legacy:test",
            "authority": "agent-inference",
            "status": "candidate",
            "source_device_id": "device-test",
        }
        (ledger / (missing_id + ".json")).write_text(
            json.dumps(missing_event), encoding="utf-8"
        )

        accepted_id = "resource-half-accepted-candidate"
        candidate_path = (
            self.root / "inbox" / "candidates" / (accepted_id + ".json")
        )
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "id": accepted_id,
                    "created_at": "2026-07-24T00:00:00Z",
                    "kind": "api-key",
                    "scope": "project:legacy",
                    "statement": "Half accepted API key",
                    "value": "legacy-half-accepted-value-1835",
                    "source": "legacy:test",
                    "authority": "agent-inference",
                    "status": "candidate",
                    "source_device_id": "device-test",
                    "candidate_state": "accepted",
                    "resolved_at": "2026-07-24T00:01:00Z",
                    "resolution_event": "cres-missing",
                    "resolution_ledger": "ledger/2026/07/cres-missing.json",
                }
            ),
            encoding="utf-8",
        )

        report = self.json_cli("health", expected=5)
        joined = "\n".join(report["issues"])
        self.assertIn(
            "credential-value-missing: event %s" % missing_id, joined
        )
        self.assertIn(
            "accepted-candidate-resolution-missing: %s" % accepted_id,
            joined,
        )
        self.assertIn(
            "accepted-candidate-resource-missing: %s" % accepted_id,
            joined,
        )

    def test_materialize_resources_backfills_existing_ledger(self):
        ledger = self.root / "ledger" / "2026" / "07"
        ledger.mkdir(parents=True)
        event = {
            "schema_version": "1",
            "id": "resource-existing-ledger-v1",
            "created_at": "2026-07-22T10:00:00Z",
            "kind": "repository",
            "scope": "global:test",
            "statement": "Existing private test repository",
            "source": "thread:test/message:existing",
            "authority": "verified-current-system-state",
            "status": "active",
            "source_device_id": "mac-test",
        }
        (ledger / "resource-existing-ledger-v1.json").write_text(
            json.dumps(event), encoding="utf-8"
        )
        result = self.json_cli("materialize-resources")
        self.assertEqual(result["created"], 1)
        resource = self.json_cli("get-resource", "resource-existing-ledger-v1")
        self.assertEqual(resource["record"]["statement"], event["statement"])

    def test_materialize_resources_fails_for_unresolved_missing_credential(self):
        ledger = self.root / "ledger" / "2026" / "07"
        ledger.mkdir(parents=True)
        event_id = "resource-unmaterializable-missing-credential"
        (ledger / (event_id + ".json")).write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "id": event_id,
                    "created_at": "2026-07-24T00:00:00Z",
                    "kind": "database url",
                    "scope": "project:missing",
                    "statement": "Reusable development database URL",
                    "source": "thread:missing/materialize",
                    "authority": "user-explicit",
                    "status": "active",
                    "source_device_id": "device-missing",
                }
            ),
            encoding="utf-8",
        )

        result = self.json_cli("materialize-resources", expected=5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "credential-value-missing")
        self.assertTrue(
            any(event_id in issue for issue in result["issues"])
        )
        self.assertFalse(
            (self.root / "resources" / "credentials" / (event_id + ".json")).exists()
        )

    def test_materialize_resources_normalizes_camel_case_credential_value(self):
        ledger = self.root / "ledger" / "2026" / "07"
        ledger.mkdir(parents=True)
        event_id = "resource-camel-case-credential"
        synthetic_value = "synthetic-client-secret-value"
        (ledger / (event_id + ".json")).write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "id": event_id,
                    "created_at": "2026-07-24T00:00:00Z",
                    "kind": "service",
                    "scope": "project:alias",
                    "statement": "Reusable development client credential",
                    "source": "thread:alias/materialize",
                    "authority": "user-explicit",
                    "status": "active",
                    "source_device_id": "device-alias",
                    "clientSecret": synthetic_value,
                }
            ),
            encoding="utf-8",
        )

        result = self.json_cli("materialize-resources")
        self.assertEqual(result["created"], 1)
        hidden = self.json_cli("get-resource", event_id)
        self.assertEqual(
            hidden["record"]["value"], "[stored; use --include-values]"
        )
        self.assertNotIn(synthetic_value, json.dumps(hidden))
        full = self.json_cli("get-resource", event_id, "--include-values")
        self.assertEqual(full["record"]["value"], synthetic_value)

    def test_rebuild_unifies_devices_and_deduplicates_identical_imports(self):
        (self.root / ".kb-data").mkdir()
        ledger = self.root / "ledger" / "imports"
        ledger.mkdir(parents=True)
        first = {
            "id": "evt-device-a",
            "created_at": "2026-07-22T12:00:00Z",
            "kind": "fact",
            "scope": "project:a",
            "statement": "Alpha device supplied this durable fact",
            "source": "codex:thread-a",
            "authority": "user-explicit",
            "status": "candidate",
            "source_device_id": "device-a",
            "bundle_id": "bundle-a",
        }
        second = {
            "id": "evt-device-b",
            "created_at": "2026-07-22T12:01:00Z",
            "kind": "fact",
            "scope": "project:b",
            "statement": "Beta device supplied another durable fact",
            "source": "claude:thread-b",
            "authority": "user-explicit",
            "status": "candidate",
            "source_device_id": "device-b",
            "bundle_id": "bundle-b",
        }
        (ledger / "bundle-a.jsonl").write_text(json.dumps(first) + "\n" + json.dumps(first) + "\n", encoding="utf-8")
        (ledger / "bundle-b.JSON").write_text(
            json.dumps({"events": [second]}), encoding="utf-8"
        )
        uppercase_canonical = self.root / "projects" / "upper" / "FACT.MD"
        uppercase_canonical.parent.mkdir(parents=True)
        uppercase_canonical.write_text(
            "# Uppercase canonical extension needle\n", encoding="utf-8"
        )
        rebuilt = self.json_cli("rebuild-index")
        self.assertEqual(rebuilt["history_events"], 2)
        self.assertEqual(rebuilt["deduplicated_events"], 1)
        alpha = self.json_cli("search", "durable fact", "--scope", "project:a")
        beta = self.json_cli("search", "durable fact", "--scope", "project:b")
        self.assertEqual(alpha["results"][0]["source_device_id"], "device-a")
        self.assertEqual(beta["results"][0]["source_device_id"], "device-b")
        uppercase = self.json_cli("search", "uppercase canonical extension")
        self.assertEqual(uppercase["results"][0]["id"], "projects/upper/FACT.MD")

    def test_incremental_index_reads_only_changed_source_content(self):
        canonical = self.root / "projects" / "sample" / "NOW.json"
        canonical.parent.mkdir(parents=True)
        canonical.write_text(
            json.dumps(
                {
                    "id": "project-incremental-source",
                    "kind": "project-fact",
                    "scope": "project:incremental",
                    "title": "incremental canonical old",
                    "created_at": "2026-07-23T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        ledger = self.root / "ledger" / "bulk"
        ledger.mkdir(parents=True)
        for index in range(40):
            event = {
                "id": "evt-incremental-%03d" % index,
                "created_at": "2026-07-23T00:%02d:00Z" % index,
                "kind": "fact",
                "scope": "project:incremental",
                "statement": "unchanged incremental fact %03d" % index,
                "source": "test:incremental",
                "authority": "user-explicit",
                "status": "active",
                "source_device_id": "device-test",
            }
            (ledger / ("event-%03d.json" % index)).write_text(
                json.dumps(event), encoding="utf-8"
            )
        subprocess.run(
            ["git", "init", "--initial-branch=main"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Index Test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "index-test@example.invalid"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial sources"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        self.json_cli("init")

        canonical.write_text(
            json.dumps(
                {
                    "id": "project-incremental-source",
                    "kind": "project-fact",
                    "scope": "project:incremental",
                    "title": "incremental canonical changed and longer",
                    "updated_at": "2026-07-23T01:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        new_event = {
            "id": "evt-incremental-new",
            "created_at": "2026-07-23T01:01:00Z",
            "kind": "fact",
            "scope": "project:incremental",
            "statement": "incremental-new-needle",
            "source": "test:incremental",
            "authority": "user-explicit",
            "status": "active",
            "source_device_id": "device-test",
        }
        new_path = ledger / "event-new.json"
        new_path.write_text(json.dumps(new_event), encoding="utf-8")

        module = load_kb_module()
        original_canonical = module.canonical_document
        original_ledger = module.iter_json_values
        canonical_reads = []
        ledger_reads = []

        def traced_canonical(root, path):
            canonical_reads.append(path.relative_to(root).as_posix())
            return original_canonical(root, path)

        def traced_ledger(path):
            ledger_reads.append(path.name)
            yield from original_ledger(path)

        with mock.patch.object(
            module, "canonical_document", side_effect=traced_canonical
        ), mock.patch.object(
            module, "iter_json_values", side_effect=traced_ledger
        ), mock.patch.object(
            module,
            "canonical_files",
            side_effect=AssertionError("Git delta enumerated canonical source tree"),
        ), mock.patch.object(
            module,
            "ledger_files",
            side_effect=AssertionError("Git delta enumerated ledger source tree"),
        ):
            updated = module.update_index(self.root)

        self.assertEqual(updated["mode"], "incremental")
        self.assertEqual(updated["detection_mode"], "git-delta")
        self.assertEqual(updated["canonical_sources_checked"], 1)
        self.assertEqual(updated["ledger_sources_checked"], 1)
        self.assertEqual(updated["canonical_sources_read"], 1)
        self.assertEqual(updated["ledger_sources_read"], 1)
        self.assertEqual(updated["history_events_inserted"], 1)
        self.assertEqual(canonical_reads, ["projects/sample/NOW.json"])
        self.assertEqual(ledger_reads, ["event-new.json"])
        self.assertEqual(updated["history_events"], 41)
        self.assertEqual(
            self.json_cli("search", "incremental-new-needle")["results"][0]["id"],
            "evt-incremental-new",
        )
        self.assertEqual(
            self.json_cli("search", "incremental canonical changed")["results"][0][
                "id"
            ],
            "project-incremental-source",
        )

        # Persistent dirty paths are re-statted, but no historical source set
        # is enumerated and no unchanged source content is reparsed.
        with mock.patch.object(
            module,
            "canonical_document",
            side_effect=AssertionError("unchanged canonical source was reparsed"),
        ), mock.patch.object(
            module,
            "iter_json_values",
            side_effect=AssertionError("unchanged ledger source was reparsed"),
        ), mock.patch.object(
            module,
            "canonical_files",
            side_effect=AssertionError("no-change update enumerated canonical tree"),
        ), mock.patch.object(
            module,
            "ledger_files",
            side_effect=AssertionError("no-change update enumerated ledger tree"),
        ):
            noop = module.update_index(self.root)
        self.assertEqual(noop["canonical_sources_read"], 0)
        self.assertEqual(noop["ledger_sources_read"], 0)
        self.assertEqual(noop["history_events_inserted"], 0)
        self.assertEqual(noop["detection_mode"], "git-delta")
        self.assertEqual(noop["canonical_sources_checked"], 1)
        self.assertEqual(noop["ledger_sources_checked"], 1)

    def test_incremental_index_falls_back_for_ledger_mutation_and_schema_change(self):
        ledger = self.root / "ledger" / "test"
        ledger.mkdir(parents=True)
        event = {
            "id": "evt-fallback-original",
            "created_at": "2026-07-23T00:00:00Z",
            "kind": "fact",
            "scope": "global:test",
            "statement": "fallback original fact",
            "source": "test:fallback",
            "authority": "user-explicit",
            "status": "active",
            "source_device_id": "device-test",
        }
        source = ledger / "event.json"
        source.write_text(json.dumps(event), encoding="utf-8")
        self.json_cli("init")
        module = load_kb_module()

        original_stat = source.stat()
        event["statement"] = "fallback modified fact"
        source.write_text(json.dumps(event), encoding="utf-8")
        os.utime(
            source,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        self.assertEqual(source.stat().st_size, original_stat.st_size)
        mutated = module.update_index(self.root)
        self.assertEqual(mutated["mode"], "full-rebuild")
        self.assertEqual(mutated["fallback_reason"], "ledger-source-mutated")
        self.assertEqual(
            self.json_cli("search", "fallback modified fact")["results"][0]["id"],
            "evt-fallback-original",
        )

        with closing(sqlite3.connect(str(self.root / ".kb-data" / "knowledge.sqlite"))) as database, database:
            database.execute(
                "UPDATE kb_meta SET value='obsolete' WHERE key='index_projection_version'"
            )
        structural = module.update_index(self.root)
        self.assertEqual(structural["mode"], "full-rebuild")
        self.assertEqual(structural["fallback_reason"], "projection-version")

    def test_projection_version_rebuild_reclassifies_credential_metadata(self):
        ledger = self.root / "ledger" / "test"
        ledger.mkdir(parents=True)
        event = {
            "id": "resource-reference-only-fingerprint",
            "created_at": "2026-07-23T00:00:00Z",
            "kind": "repository",
            "scope": "global:test",
            "statement": "Dedicated GitHub SSH key fingerprint is SHA256:test",
            "source": "verified:repository",
            "authority": "verified-system",
            "status": "active",
            "source_device_id": "device-test",
        }
        (ledger / "event.json").write_text(json.dumps(event), encoding="utf-8")
        self.json_cli("init")

        database_path = self.root / ".kb-data" / "knowledge.sqlite"
        with closing(sqlite3.connect(str(database_path))) as connection, connection:
            connection.execute(
                "UPDATE history_events SET is_credential=1 WHERE event_id=?",
                (event["id"],),
            )
            connection.execute(
                "UPDATE kb_meta SET value='obsolete-classifier' "
                "WHERE key='index_projection_version'"
            )

        rebuilt = load_kb_module().update_index(self.root)
        self.assertEqual(rebuilt["mode"], "full-rebuild")
        self.assertEqual(rebuilt["fallback_reason"], "projection-version")
        found = self.json_cli("search", "Dedicated GitHub SSH key fingerprint")
        item = next(result for result in found["results"] if result["id"] == event["id"])
        self.assertFalse(item["credential"])
        self.assertNotIn("credential_value_status", item)

    def legacy_lifecycle_projection_fixture(self):
        ledger = self.root / "ledger"
        ledger.mkdir(exist_ok=True)
        records = []
        for decision in ("accepted", "rejected"):
            base = {"schema_version": "1", "created_at": "2026-01-01T00:00:00Z",
                    "kind": "fact", "scope": "project:upgrade", "statement": "upgrade-marker " + decision,
                    "source": "fictional:upgrade", "authority": "agent-inference",
                    "status": "candidate", "source_device_id": "fixture-device"}
            records.append(dict(base, id=decision))
            records.append(dict(base, id="review-" + decision, kind="candidate-resolution",
                                authority="maintainer", status="active", candidate_id=decision,
                                resolution=decision, created_at="2026-02-01T00:00:00Z"))
        source = ledger / "upgrade.json"
        source.write_text(json.dumps(records), encoding="utf-8")
        self.json_cli("init")
        database = self.root / ".kb-data/knowledge.sqlite"
        # A connection context commits but does not close. Close this writer
        # before a test snapshots the database bytes, so a later GC-triggered
        # WAL checkpoint cannot be mistaken for a mutation by the read path.
        with closing(sqlite3.connect(database)) as connection:
            with connection:
                # Version 6/1 omitted these valid IDs from its projection.
                connection.execute("DELETE FROM lifecycle_resolutions")
                connection.execute("UPDATE kb_meta SET value='6' WHERE key='index_projection_version'")
                connection.execute("UPDATE kb_meta SET value='1' WHERE key='lifecycle_projection_version'")
        return source, database

    def test_legacy_lifecycle_projection_read_falls_back_without_mutating_cache(self):
        source, database = self.legacy_lifecycle_projection_fixture()
        original_source, original_database = source.read_bytes(), database.read_bytes()
        module = load_kb_module()
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            with self.assertRaises(LookupError):
                module.lifecycle_maps_from_database(connection)
            self.assertEqual(module.lifecycle_maps_for_read(self.root, connection)[1],
                             {"accepted": "accepted", "rejected": "rejected"})
        self.assertEqual(module.lifecycle_maps_for_read(self.root)[1],
                         {"accepted": "accepted", "rejected": "rejected"})
        self.assertEqual(source.read_bytes(), original_source)
        self.assertEqual(database.read_bytes(), original_database)

    def test_legacy_lifecycle_projection_update_rebuilds_without_source_changes(self):
        source, database = self.legacy_lifecycle_projection_fixture()
        original_source, original_mtime = source.read_bytes(), source.stat().st_mtime_ns
        module = load_kb_module()
        result = module.update_index(self.root)
        self.assertEqual(result["mode"], "full-rebuild")
        self.assertEqual(result["fallback_reason"], "projection-version")
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(dict(connection.execute("SELECT candidate_id, resolution FROM lifecycle_resolutions")),
                             {"accepted": "accepted", "rejected": "rejected"})
            metadata = dict(connection.execute("SELECT key, value FROM kb_meta"))
        self.assertEqual(metadata["index_projection_version"], module.INDEX_PROJECTION_VERSION)
        self.assertEqual(metadata["lifecycle_projection_version"], module.LIFECYCLE_PROJECTION_VERSION)
        self.assertEqual(source.read_bytes(), original_source)
        self.assertEqual(source.stat().st_mtime_ns, original_mtime)
        self.assertEqual(module.SCHEMA_VERSION, "1")

    def test_legacy_lifecycle_projection_foreground_retrieve_repairs_old_decisions(self):
        source, database = self.legacy_lifecycle_projection_fixture()
        original_source = source.read_bytes()
        load_kb_module()  # Makes this tool release's scripts importable.
        import retrieve
        with mock.patch.object(retrieve.device_identity, "platform_fingerprint", return_value="fictional-device"):
            result = retrieve.retrieve(self.root, "upgrade-marker", scope="project:upgrade", offline=True)
        self.assertEqual({item["id"]: item["effective_status"] for item in result["results"]},
                         {"accepted": "accepted", "rejected": "rejected"})
        self.assertEqual(result["current_count"], 1)
        self.assertEqual(result["freshness"]["index_status"], "refreshed")
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(dict(connection.execute("SELECT candidate_id, resolution FROM lifecycle_resolutions")),
                             {"accepted": "accepted", "rejected": "rejected"})
        self.assertEqual(source.read_bytes(), original_source)

    def test_git_delta_includes_ignored_canonical_sources(self):
        (self.root / ".gitignore").write_text(
            "projects/*/DISCOVERED.json\nprojects/opaque/\n.kb-data/\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "init", "--initial-branch=main"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Index Test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "index-test@example.invalid"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial sources"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        self.json_cli("init")

        discovered = self.root / "projects" / "sample" / "DISCOVERED.json"
        discovered.parent.mkdir(parents=True)
        discovered.write_text(
            json.dumps(
                {
                    "id": "ignored-discovered-source",
                    "scope": "project:sample",
                    "title": "ignored source remains discoverable",
                }
            ),
            encoding="utf-8",
        )
        ignored = subprocess.run(
            ["git", "check-ignore", str(discovered.relative_to(self.root))],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(ignored.returncode, 0)

        module = load_kb_module()
        with mock.patch.object(
            module,
            "canonical_files",
            side_effect=AssertionError("ignored source forced a full tree scan"),
        ):
            updated = module.update_index(self.root)
        self.assertEqual(updated["detection_mode"], "git-delta")
        self.assertEqual(updated["canonical_sources_checked"], 1)
        self.assertEqual(updated["canonical_sources_read"], 1)
        found = self.json_cli("search", "ignored source discoverable")
        self.assertEqual(found["results"][0]["id"], "ignored-discovered-source")

        opaque = self.root / "projects" / "opaque" / "fact.json"
        opaque.parent.mkdir(parents=True)
        opaque.write_text(
            json.dumps(
                {
                    "id": "ignored-directory-source",
                    "scope": "project:opaque",
                    "title": "ignored directory safe fallback",
                }
            ),
            encoding="utf-8",
        )
        fallback = module.update_index(self.root)
        self.assertEqual(fallback["mode"], "incremental")
        self.assertEqual(fallback["detection_mode"], "filesystem-scan")
        found = self.json_cli("search", "ignored directory safe fallback")
        self.assertEqual(found["results"][0]["id"], "ignored-directory-source")

    def test_incremental_index_quarantines_corrupt_disposable_database(self):
        ledger = self.root / "ledger" / "test"
        ledger.mkdir(parents=True)
        event = {
            "id": "evt-corrupt-index-recovery",
            "created_at": "2026-07-23T00:00:00Z",
            "kind": "fact",
            "scope": "global:test",
            "statement": "corrupt index recovery needle",
            "source": "test:corrupt-index",
            "authority": "user-explicit",
            "status": "active",
            "source_device_id": "device-test",
        }
        (ledger / "event.json").write_text(json.dumps(event), encoding="utf-8")
        self.json_cli("init")
        database = self.root / ".kb-data" / "knowledge.sqlite"
        database.write_bytes(b"not-a-sqlite-database")

        recovered = load_kb_module().update_index(self.root)
        self.assertEqual(recovered["mode"], "full-rebuild")
        self.assertEqual(recovered["fallback_reason"], "database-corrupt")
        self.assertTrue(recovered["quarantined"])
        self.assertTrue(
            all((self.root / path).is_file() for path in recovered["quarantined"])
        )
        found = self.json_cli("search", "corrupt index recovery needle")
        self.assertEqual(found["results"][0]["id"], "evt-corrupt-index-recovery")

    def test_search_includes_verbatim_history_with_default_credential_hiding(self):
        secret = "sk-superSecretHistoryToken-778899"
        self.init_history(
            [
                {
                    "event_hash": "history-normal",
                    "source_device_id": "laptop-b",
                    "thread_id": "thread-normal",
                    "cwd": "/projects/atlas",
                    "content": "The onboarding walkthrough needs a simulator verification pass.",
                },
                {
                    "event_hash": "history-secret",
                    "source_device_id": "laptop-b",
                    "thread_id": "thread-secret",
                    "cwd": "/projects/beacon",
                    "content": "Development API key is %s" % secret,
                },
            ]
        )
        self.json_cli("init")
        normal = self.json_cli("search", "simulator verification")
        history_result = next(item for item in normal["results"] if item["result_type"] == "source-history")
        self.assertEqual(history_result["source"], "codex")
        self.assertEqual(history_result["source_device_id"], "laptop-b")
        self.assertEqual(history_result["thread_id"], "thread-normal")
        self.assertTrue(history_result["timestamp"])
        self.assertIn("simulator", history_result["snippet"])

        hidden = self.json_cli("search", secret)
        secret_result = next(item for item in hidden["results"] if item["result_type"] == "source-history")
        self.assertTrue(secret_result["credential_suspected"])
        self.assertEqual(secret_result["snippet"], "[credential-like source hidden; rerun with --include-values]")
        self.assertNotIn(secret, json.dumps(hidden))
        included = self.json_cli("search", secret, "--include-values")
        secret_result = next(item for item in included["results"] if item["result_type"] == "source-history")
        self.assertIn(secret, secret_result["content"])
        exact = self.json_cli("get-history", secret_result["id"], "--include-values")
        self.assertEqual(exact["content"], "Development API key is %s" % secret)
        exact_hidden = self.json_cli("get-history", secret_result["id"])
        self.assertNotIn(secret, json.dumps(exact_hidden))

    def test_source_history_hides_tool_evidence_and_standalone_opaque_tokens(self):
        tool_sentinel = "toolEvidencePayloadMarkerWithoutCredentialLabel"
        opaque_sentinel = "Ark9D7f2C8a91E4b67A0c53F6e81B2d94Q7x5M3p8"
        self.init_history(
            [
                {
                    "event_hash": "history-tool-evidence",
                    "role": "assistant",
                    "event_type": "tool-result-evidence",
                    "content": tool_sentinel,
                },
                {
                    "event_hash": "history-opaque-user-token",
                    "role": "user",
                    "content": opaque_sentinel,
                },
            ]
        )
        self.json_cli("init")

        for query, event_hash, sentinel in (
            ("PayloadMarker", "history-tool-evidence", tool_sentinel),
            ("Ark9D7f2C8", "history-opaque-user-token", opaque_sentinel),
        ):
            with self.subTest(event_hash=event_hash):
                hidden = self.json_cli("search", query)
                result = next(
                    item
                    for item in hidden["results"]
                    if item["id"] == "history:%s" % event_hash
                )
                self.assertTrue(result["credential_suspected"])
                self.assertNotIn(sentinel, json.dumps(hidden))

                exact_hidden = self.json_cli(
                    "get-history", "history:%s" % event_hash
                )
                self.assertTrue(exact_hidden["credential_suspected"])
                self.assertNotIn(sentinel, json.dumps(exact_hidden))

                included = self.json_cli(
                    "get-history",
                    "history:%s" % event_hash,
                    "--include-values",
                )
                self.assertEqual(included["content"], sentinel)

    def test_search_default_limit_and_context_budget_are_config_driven(self):
        (self.root / "config" / "kb.json").write_text(
            json.dumps({
                "retrieval": {"default_limit": 3, "maximum_context_characters": 1000},
                "history_database": ".kb-data/history.sqlite",
            }),
            encoding="utf-8",
        )
        self.init_history([
            {
                "event_hash": "budget-%02d" % index,
                "event_id": "budget-event-%02d" % index,
                "content": "budgetword " + ("x" * 2500) + " tail-%02d" % index,
            }
            for index in range(8)
        ])
        self.json_cli("init")
        result = self.json_cli("search", "budgetword", "--include-values")
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["context_budget_characters"], 1000)
        self.assertGreater(result["truncated_fields"], 0)
        payload_size = sum(
            len(str(item.get(field, "")))
            for item in result["results"]
            for field in ("snippet", "statement", "content", "value")
        )
        self.assertLessEqual(payload_size, 1000)

        event_id = result["results"][0]["id"]
        exact = self.json_cli("get-history", event_id, "--include-values")
        self.assertGreater(len(exact["content"]), 2500)

    def test_search_opens_knowledge_and_source_history_sqlite_read_only(self):
        self.json_cli("init")
        self.json_cli(
            "capture",
            "--id",
            "evt-readonly-search",
            "--kind",
            "fact",
            "--scope",
            "project:readonly",
            "--statement",
            "readonlymarker curated fact",
            "--source",
            "thread:readonly/curated",
            "--authority",
            "user-explicit",
            "--status",
            "active",
            "--source-device-id",
            "test-device",
        )
        history_database = self.init_history(
            [{"content": "readonlymarker verbatim source history"}]
        )
        knowledge_database = self.root / ".kb-data" / "knowledge.sqlite"
        before = {
            path.resolve(): path.stat().st_mtime_ns
            for path in (knowledge_database, history_database)
        }
        original_modes = {
            path.resolve(): path.stat().st_mode
            for path in (knowledge_database, history_database)
        }
        for path in (knowledge_database, history_database):
            path.chmod(0o444)

        module = load_kb_module()
        opened = []
        real_connect = module.connect_database_read_only

        def inspect_read_only(path, timeout=10):
            connection = real_connect(path, timeout=timeout)
            opened.append(
                (Path(path).resolve(), connection.execute("PRAGMA query_only").fetchone()[0])
            )
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden_search_write(value TEXT)")
            return connection

        try:
            args = module.build_parser().parse_args(
                [
                    "--root",
                    str(self.root),
                    "search",
                    "readonlymarker",
                    "--limit",
                    "12",
                ]
            )
            with mock.patch.object(
                module,
                "connect_database_read_only",
                side_effect=inspect_read_only,
            ):
                searched = module.search(self.root, args)
        finally:
            for path, mode in original_modes.items():
                path.chmod(mode)

        self.assertGreaterEqual(searched["count"], 2)
        self.assertEqual(
            {path for path, _query_only in opened},
            {knowledge_database.resolve(), history_database.resolve()},
        )
        self.assertTrue(all(query_only == 1 for _path, query_only in opened))
        self.assertEqual(
            {
                path.resolve(): path.stat().st_mtime_ns
                for path in (knowledge_database, history_database)
            },
            before,
        )

    def test_search_without_index_uses_hot_layer_without_creating_sqlite(self):
        (self.root / ".kb-data").mkdir()
        self.init_history(
            [{"event_hash": "fallback-source", "content": "fallbackmarker source history"}]
        )
        resource_dir = self.root / "resources" / "catalog"
        resource_dir.mkdir(parents=True)
        resource_dir.joinpath("fallback.json").write_text(
            json.dumps(
                {
                    "id": "resource-search-fallback",
                    "kind": "resource",
                    "scope": "global:test",
                    "title": "fallbackmarker current resource",
                    "statement": "fallbackmarker current resource",
                    "authority": "user-explicit",
                    "status": "active",
                    "created_at": "2026-08-16T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        database = self.root / ".kb-data" / "knowledge.sqlite"
        self.assertFalse(database.exists())

        searched = self.json_cli("search", "fallbackmarker")

        self.assertEqual(searched["retrieval_mode"], "hot-only")
        self.assertTrue(searched["cold_data_available"])
        self.assertEqual(
            [item["id"] for item in searched["results"]],
            ["resource-search-fallback", "history:fallback-source"],
        )
        self.assertTrue(searched["history_available"])
        self.assertFalse(database.exists())

        for payload in (b"", b"not-a-sqlite-database"):
            with self.subTest(index_payload=payload):
                database.write_bytes(payload)
                before = (database.read_bytes(), database.stat().st_mtime_ns)
                degraded = self.json_cli("search", "fallbackmarker")
                self.assertEqual(
                    [item["id"] for item in degraded["results"]],
                    ["resource-search-fallback", "history:fallback-source"],
                )
                self.assertIn(
                    degraded["knowledge_index_status"],
                    (
                        "unreadable-read-only-fallback",
                        "invalid-read-only-fallback",
                    ),
                )
                self.assertEqual(
                    (database.read_bytes(), database.stat().st_mtime_ns),
                    before,
                )

    def test_conflicting_imported_event_id_is_rejected(self):
        (self.root / ".kb-data").mkdir()
        ledger = self.root / "ledger"
        ledger.mkdir(parents=True)
        base = {
            "id": "evt-shared-id",
            "created_at": "2026-07-22T12:00:00Z",
            "kind": "fact",
            "scope": "global",
            "statement": "Original statement",
            "source": "thread:one",
            "authority": "user-explicit",
            "status": "candidate",
            "source_device_id": "device-a",
        }
        conflict = dict(base, statement="Conflicting statement")
        (ledger / "one.json").write_text(json.dumps(base), encoding="utf-8")
        (ledger / "two.json").write_text(json.dumps(conflict), encoding="utf-8")
        result = self.json_cli("rebuild-index", expected=4)
        self.assertIn("conflicting immutable payloads", result["error"])

    def test_health_reports_fresh_stale_and_never_synced_devices(self):
        self.write_devices(
            [
                {"id": "device-fresh", "expected": True},
                {"id": "device-never", "expected": True},
            ],
            stale_after_days=14,
        )
        self.json_cli("init")
        self.init_history()
        self.json_cli(
            "capture",
            "--id",
            "evt-health-fresh",
            "--kind",
            "fact",
            "--scope",
            "global",
            "--statement",
            "Fresh device heartbeat fact",
            "--source",
            "thread:health",
            "--authority",
            "user-explicit",
            "--source-device-id",
            "device-fresh",
        )
        report = self.json_cli("health", expected=5)
        devices = {item["id"]: item for item in report["devices"]}
        self.assertFalse(devices["device-fresh"]["never_synced"])
        self.assertFalse(devices["device-fresh"]["stale"])
        self.assertTrue(devices["device-never"]["never_synced"])
        self.assertTrue(any("never synced" in issue for issue in report["issues"]))
        self.assertEqual(report["history"]["source_events"], 0)

    def test_health_reports_preserved_ambiguous_evidence_relations_as_warning(self):
        self.json_cli("init")
        self.init_history(
            [
                {
                    "event_hash": "tool-evidence-ambiguous",
                    "event_type": "tool_result",
                    "role": "assistant",
                    "content": "body omitted from assertion",
                }
            ]
        )
        database = self.root / ".kb-data" / "history.sqlite"
        with closing(sqlite3.connect(str(database))) as connection, connection:
            connection.executemany(
                """
                INSERT INTO source_event_user_relations(
                    event_hash, related_user_event_hash, first_seen_at
                ) VALUES (?, ?, ?)
                """,
                [
                    (
                        "tool-evidence-ambiguous",
                        "user-event-one",
                        "2026-08-17T00:00:00Z",
                    ),
                    (
                        "tool-evidence-ambiguous",
                        "user-event-two",
                        "2026-08-17T00:00:00Z",
                    ),
                ],
            )

        report = self.json_cli("health")

        self.assertTrue(report["ok"])
        self.assertEqual(report["history"]["evidence_relation_conflicts"], 1)
        self.assertIn(
            "history evidence relation conflicts preserved: 1",
            report["warnings"],
        )

    def test_health_accepts_verified_windows_local_registration_without_importing_history(self):
        devices = self.root / "config" / "devices"
        devices.mkdir()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        registrations = {
            "windows-captured": {
                "id": "windows-captured",
                "name": "WINDOWS-CAPTURED",
                "expected": True,
                "platform": "Windows",
                "history_storage": "local",
                "history_status": "local-history-captured",
                "initial_scan_verified": True,
                "history_events": 33836,
                "source_files": 544,
                "last_capture_completed_at": now,
                "raw_history_shared": False,
                "bundle_shared": False,
            },
            "windows-hot-only": {
                "id": "windows-hot-only",
                "name": "WINDOWS-HOT-ONLY",
                "expected": True,
                "platform": "Windows",
                "history_storage": "local",
                "history_status": "hot-only",
                "hot_only_reason": "no-history-sources-found",
                "initial_scan_verified": False,
                "history_events": 0,
                "source_files": 0,
                "first_initialized_at": now,
                "raw_history_shared": False,
                "bundle_shared": False,
            },
        }
        for device_id, registration in registrations.items():
            (devices / (device_id + ".json")).write_text(
                json.dumps(registration),
                encoding="utf-8",
            )
        self.json_cli("init")
        self.init_history()

        report = self.json_cli("health")
        by_id = {item["id"]: item for item in report["devices"]}
        captured = by_id["windows-captured"]
        self.assertTrue(captured["registration_verified"])
        self.assertEqual(
            captured["registration_status"],
            "verified-on-source-device",
        )
        self.assertEqual(captured["reported_remote_history_events"], 33836)
        self.assertEqual(captured["event_count"], 0)
        self.assertEqual(captured["history_event_count"], 0)
        self.assertEqual(
            captured["last_seen_source"],
            "windows-local-registration",
        )
        self.assertFalse(captured["never_synced"])

        hot_only = by_id["windows-hot-only"]
        self.assertTrue(hot_only["registration_verified"])
        self.assertEqual(
            hot_only["registration_status"],
            "hot-only-no-local-history",
        )
        self.assertEqual(hot_only["reported_remote_history_events"], 0)
        self.assertEqual(
            hot_only["last_seen_source"],
            "windows-local-registration",
        )
        self.assertFalse(hot_only["never_synced"])

    def test_health_rejects_incomplete_windows_local_registration(self):
        devices = self.root / "config" / "devices"
        devices.mkdir()
        incomplete = {
            "windows-incomplete": {
                "id": "windows-incomplete",
                "expected": True,
                "platform": "Windows",
                "history_storage": "local",
                "history_status": "local-history-captured",
                "initial_scan_verified": True,
                "history_events": 100,
                "source_files": 2,
                "raw_history_shared": False,
                "bundle_shared": False,
            },
            "windows-hot-only-incomplete": {
                "id": "windows-hot-only-incomplete",
                "expected": True,
                "platform": "Windows",
                "history_storage": "local",
                "history_status": "hot-only",
                "hot_only_reason": "history-sources-empty",
                "initial_scan_verified": False,
                "history_events": 0,
                "source_files": 0,
                "raw_history_shared": False,
                "bundle_shared": False,
            },
        }
        for device_id, registration in incomplete.items():
            (devices / (device_id + ".json")).write_text(
                json.dumps(registration),
                encoding="utf-8",
            )
        self.json_cli("init")
        self.init_history()

        report = self.json_cli("health", expected=5)
        by_id = {item["id"]: item for item in report["devices"]}
        for device_id in incomplete:
            device = by_id[device_id]
            self.assertFalse(device["registration_verified"])
            self.assertEqual(device["registration_status"], "incomplete")
            self.assertTrue(device["never_synced"])

    def test_dangling_cold_data_keeps_hot_search_capture_and_resource_access_working(self):
        cold_target = self.root.parent / "Volumes" / "Portable-KB" / "AI-Knowledge-Data"
        data_link = self.root / ".kb-data"
        data_link.symlink_to(cold_target, target_is_directory=True)
        original_link = data_link.readlink()

        hot_documents = {
            self.root / "profile" / "USER.md": "# User\nofflinehotword profile preference\n",
            self.root / "projects" / "sample" / "NOW.md": "# Project\nofflinehotword project fact\n",
            self.root / "playbooks" / "offline.md": "# Workflow\nofflinehotword workflow rule\n",
        }
        for path, content in hot_documents.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        resources = self.root / "resources"
        credentials = resources / "credentials"
        credentials.mkdir(parents=True)
        (resources / "general.json").write_text(
            json.dumps(
                {
                    "id": "resource:offline:general",
                    "kind": "resource",
                    "scope": "global",
                    "title": "Offline resource",
                    "purpose": "offlinehotword reusable material",
                }
            ),
            encoding="utf-8",
        )
        secret = "sim-secret-OFFLINE-8844"
        (credentials / "service.json").write_text(
            json.dumps(
                {
                    "id": "resource:offline:credential",
                    "kind": "credential",
                    "scope": "project:offline",
                    "name": "Offline test credential",
                    "purpose": "development simulation fallback service",
                    "value": secret,
                    "source": "user-explicit",
                    "created_at": "2026-07-22T00:00:00Z",
                    "status": "active",
                }
            ),
            encoding="utf-8",
        )

        captured = self.json_cli(
            "capture",
            "--id",
            "evt-offline-hot-ledger",
            "--kind",
            "fact",
            "--scope",
            "global",
            "--statement",
            "offlinehotword captured while the SSD is absent",
            "--source",
            "thread:offline",
            "--authority",
            "user-explicit",
            "--source-device-id",
            "device-offline",
        )
        self.assertEqual(captured["index_status"], "pending-cold-data-unavailable")
        self.assertIsNone(captured["indexed"])
        self.assertTrue((self.root / captured["ledger"]).is_file())
        self.assertIsNone(captured["candidate"])

        ledger_secret = "offline-ledger-secret-VALUE-FILE-7712\n"
        ledger_value_file = Path(self.temporary.name) / "offline-ledger-secret"
        ledger_value_file.write_bytes(ledger_secret.encode("utf-8"))
        credential_capture = self.json_cli(
            "capture",
            "--id",
            "evt-offline-hot-credential",
            "--kind",
            "credential",
            "--scope",
            "project:offline",
            "--statement",
            "offlineledgercredential development SSH access",
            "--value-file",
            str(ledger_value_file),
            "--source",
            "thread:offline",
            "--authority",
            "user-explicit",
            "--source-device-id",
            "device-offline",
        )
        self.assertEqual(
            credential_capture["index_status"], "pending-cold-data-unavailable"
        )

        searched = self.json_cli("search", "offlinehotword")
        self.assertEqual(searched["retrieval_mode"], "hot-only")
        self.assertFalse(searched["cold_data_available"])
        self.assertFalse(searched["history_available"])
        self.assertIn("evt-offline-hot-ledger", {item["id"] for item in searched["results"]})
        canonical_zones = {
            Path(item["path"]).parts[0]
            for item in searched["results"]
            if item["result_type"] == "canonical"
        }
        self.assertEqual(canonical_zones, {"profile", "projects", "resources", "playbooks"})

        hidden_search = self.json_cli("search", "development simulation fallback")
        self.assertNotIn(secret, json.dumps(hidden_search, ensure_ascii=False))
        full_search = self.json_cli(
            "search", "development simulation fallback", "--include-values"
        )
        self.assertIn(secret, json.dumps(full_search, ensure_ascii=False))
        hidden_ledger_credential = self.json_cli(
            "search", "offlineledgercredential development SSH access"
        )
        self.assertEqual(
            hidden_ledger_credential["results"][0]["value"],
            "[stored; use --include-values]",
        )
        self.assertNotIn(ledger_secret.strip(), json.dumps(hidden_ledger_credential))
        full_ledger_credential = self.json_cli(
            "search",
            "offlineledgercredential development SSH access",
            "--include-values",
        )
        self.assertEqual(full_ledger_credential["results"][0]["value"], ledger_secret)
        self.assertEqual(
            self.json_cli("search", ledger_secret.strip())["count"],
            0,
        )
        hidden_resource = self.json_cli(
            "get-resource", "resource:offline:credential"
        )
        self.assertEqual(hidden_resource["record"]["value"], "[stored; use --include-values]")
        full_resource = self.json_cli(
            "get-resource", "resource:offline:credential", "--include-values"
        )
        self.assertEqual(full_resource["record"]["value"], secret)

        history_error = self.json_cli(
            "get-history", "history:anything", expected=3
        )
        self.assertIn("cold-data-unavailable", history_error["error"])
        rebuild_error = self.json_cli("rebuild-index", expected=1)
        self.assertIn("cold-data-unavailable", rebuild_error["error"])

        # No command above may create a fallback target or replace the dangling
        # link with a local directory.
        self.assertTrue(data_link.is_symlink())
        self.assertEqual(data_link.readlink(), original_link)
        self.assertFalse(cold_target.exists())
        self.assertFalse((self.root.parent / "Volumes").exists())

        # Once the same cold target becomes reachable, one rebuild catches up
        # every hot capture without replaying or rewriting it.
        cold_target.mkdir(parents=True)
        rebuilt = self.json_cli("rebuild-index")
        self.assertEqual(rebuilt["history_events"], 2)
        self.assertTrue((cold_target / "knowledge.sqlite").is_file())
        indexed = self.json_cli("search", "offlinehotword")
        self.assertEqual(indexed["retrieval_mode"], "indexed")
        self.assertIn("evt-offline-hot-ledger", {item["id"] for item in indexed["results"]})

    def test_hot_only_search_preserves_default_limit_and_context_budget_without_creating_data_root(self):
        (self.root / "config" / "kb.json").write_text(
            json.dumps(
                {"retrieval": {"default_limit": 2, "maximum_context_characters": 1000}}
            ),
            encoding="utf-8",
        )
        profile = self.root / "profile"
        profile.mkdir()
        for index in range(5):
            (profile / ("long-%d.md" % index)).write_text(
                "# Hot document %d\nhotbudget %s tail-%d\n"
                % (index, "x" * 2400, index),
                encoding="utf-8",
            )
        result = self.json_cli("search", "hotbudget", "--include-values")
        self.assertEqual(result["retrieval_mode"], "hot-only")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["context_budget_characters"], 1000)
        self.assertGreater(result["truncated_fields"], 0)
        payload_size = sum(
            len(str(item.get(field, "")))
            for item in result["results"]
            for field in ("snippet", "statement", "content", "value")
        )
        self.assertLessEqual(payload_size, 1000)
        self.assertFalse((self.root / ".kb-data").exists())


    def test_non_credential_events_are_searchable_by_id_kind_and_scope(self):
        # Credential events already indexed id/kind/scope; plain events only
        # indexed statement+value, so a Chinese-statement workflow could never
        # be found by its own id fragment or classification.
        self.json_cli("init")
        self.json_cli(
            "capture",
            "--id",
            "workflow-device-zebrafish-v1",
            "--kind",
            "workflow",
            "--scope",
            "project:gamma",
            "--statement",
            "新设备接入的标准流程说明",
            "--source",
            "thread:wf/1",
            "--authority",
            "user-explicit",
            "--source-device-id",
            "mac-mini-primary",
        )
        by_id_fragment = self.json_cli("search", "zebrafish")
        self.assertIn(
            "workflow-device-zebrafish-v1",
            [item["id"] for item in by_id_fragment["results"]],
        )
        by_kind_word = self.json_cli("search", "workflow gamma")
        self.assertIn(
            "workflow-device-zebrafish-v1",
            [item["id"] for item in by_kind_word["results"]],
        )

    def test_short_cjk_terms_constrain_indexed_search(self):
        # Two-character Chinese words are invisible to the trigram tokenizer;
        # they must still constrain results instead of being silently dropped.
        self.json_cli("init")
        for event_id, statement in (
            ("evt-cjk-1", "SSD 扩容盘的主要用途说明"),
            ("evt-cjk-2", "SSD 备份盘的接口参数"),
        ):
            self.json_cli(
                "capture",
                "--id",
                event_id,
                "--kind",
                "fact",
                "--scope",
                "project:gamma",
                "--statement",
                statement,
                "--source",
                "thread:cjk/%s" % event_id,
                "--authority",
                "user-explicit",
                "--source-device-id",
                "mac-mini-primary",
            )
        mixed = self.json_cli("search", "SSD 用途")
        ids = [item["id"] for item in mixed["results"]]
        self.assertIn("evt-cjk-1", ids)
        self.assertNotIn("evt-cjk-2", ids)

    def test_all_short_cjk_terms_match_per_term_not_whole_phrase(self):
        # A query of only short terms used to run one LIKE over the literal
        # phrase, so non-adjacent terms returned nothing.
        self.json_cli("init")
        self.json_cli(
            "capture",
            "--id",
            "evt-cjk-3",
            "--kind",
            "fact",
            "--scope",
            "project:gamma",
            "--statement",
            "数据库密码需要按季度轮换一次",
            "--source",
            "thread:cjk/evt-cjk-3",
            "--authority",
            "user-explicit",
            "--source-device-id",
            "mac-mini-primary",
        )
        found = self.json_cli("search", "密码 轮换")
        self.assertEqual([item["id"] for item in found["results"]], ["evt-cjk-3"])

    def test_short_cjk_terms_constrain_source_history_results(self):
        self.json_cli("init")
        self.init_history(
            events=(
                {"content": "SSD 用于扩容和缓存用途", "event_id": "hist-cjk-1"},
                {"content": "SSD 已经完成格式化", "event_id": "hist-cjk-2"},
            )
        )
        mixed = self.json_cli("search", "SSD 用途")
        history_event_ids = [
            item["event_id"]
            for item in mixed["results"]
            if item["result_type"] == "source-history"
        ]
        self.assertIn("hist-cjk-1", history_event_ids)
        self.assertNotIn("hist-cjk-2", history_event_ids)

    def test_supersede_demotes_old_resource_and_keeps_exact_old_value(self):
        self.json_cli("init")
        old_secret = "GAMMA-OLD-ROTATED-SENTINEL-1111"
        new_secret = "GAMMA-NEW-ACTIVE-SENTINEL-2222"
        captured_old = self.capture_rotated_key(
            "resource-gamma-relay-key-v1", "gammarelay api key first rotation", old_secret
        )
        self.capture_rotated_key(
            "resource-gamma-relay-key-v2", "gammarelay api key second rotation", new_secret
        )
        old_ledger = self.root / captured_old["ledger"]
        old_resource = self.root / captured_old["resource"]
        ledger_before = old_ledger.read_bytes()
        resource_before = old_resource.read_bytes()

        superseded = self.json_cli(
            "supersede",
            "--old-id",
            "resource-gamma-relay-key-v1",
            "--new-id",
            "resource-gamma-relay-key-v2",
            "--source",
            "thread:gamma/rotation",
            "--source-device-id",
            "mac-mini-primary",
        )
        self.assertTrue(superseded["id"].startswith("sup-"))
        self.assertFalse(superseded["duplicate"])
        self.assertTrue((self.root / superseded["ledger"]).is_file())
        supersession_event = json.loads(
            (self.root / superseded["ledger"]).read_text(encoding="utf-8")
        )
        self.assertNotIn(old_secret, json.dumps(supersession_event, ensure_ascii=False))
        self.assertNotIn(new_secret, json.dumps(supersession_event, ensure_ascii=False))
        # POLICY §4: the superseded record and its exact value are never rewritten.
        self.assertEqual(old_ledger.read_bytes(), ledger_before)
        self.assertEqual(old_resource.read_bytes(), resource_before)

        # The ordering and annotations must hold both right away and after an
        # explicit rebuild-index (persisted-index round trip).
        for _ in range(2):
            searched = self.json_cli("search", "gammarelay api key")
            ids = [item["id"] for item in searched["results"]]
            self.assertIn("resource-gamma-relay-key-v1", ids)
            self.assertIn("resource-gamma-relay-key-v2", ids)
            self.assertLess(
                ids.index("resource-gamma-relay-key-v2"),
                ids.index("resource-gamma-relay-key-v1"),
            )
            for item in searched["results"]:
                if item["id"] == "resource-gamma-relay-key-v1":
                    self.assertEqual(item["superseded_by"], "resource-gamma-relay-key-v2")
                if item["id"] == "resource-gamma-relay-key-v2":
                    self.assertNotIn("superseded_by", item)
            self.assertNotIn(old_secret, json.dumps(searched, ensure_ascii=False))
            self.assertNotIn(new_secret, json.dumps(searched, ensure_ascii=False))
            self.json_cli("rebuild-index")

        redacted = self.json_cli("get-resource", "resource-gamma-relay-key-v1")
        self.assertEqual(redacted["superseded_by"], "resource-gamma-relay-key-v2")
        self.assertEqual(redacted["record"]["value"], "[stored; use --include-values]")
        full = self.json_cli(
            "get-resource", "resource-gamma-relay-key-v1", "--include-values"
        )
        self.assertEqual(full["superseded_by"], "resource-gamma-relay-key-v2")
        self.assertEqual(full["record"]["value"], old_secret)
        replacement = self.json_cli("get-resource", "resource-gamma-relay-key-v2")
        self.assertNotIn("superseded_by", replacement)

    def test_supersede_is_idempotent_chains_and_rejects_unknown_ids(self):
        self.json_cli("init")
        self.capture_rotated_key(
            "resource-gamma-relay-key-v1", "gammarelay api key first rotation", "GAMMA-SENTINEL-1"
        )
        self.capture_rotated_key(
            "resource-gamma-relay-key-v2", "gammarelay api key second rotation", "GAMMA-SENTINEL-2"
        )
        self.capture_rotated_key(
            "resource-gamma-relay-key-v3", "gammarelay api key third rotation", "GAMMA-SENTINEL-3"
        )
        first = self.json_cli(
            "supersede",
            "--old-id",
            "resource-gamma-relay-key-v1",
            "--new-id",
            "resource-gamma-relay-key-v2",
            "--source",
            "thread:gamma/rotation",
            "--source-device-id",
            "mac-mini-primary",
        )
        repeated = self.json_cli(
            "supersede",
            "--old-id",
            "resource-gamma-relay-key-v1",
            "--new-id",
            "resource-gamma-relay-key-v2",
            "--source",
            "thread:gamma/rotation-retry",
            "--source-device-id",
            "mac-mini-primary",
        )
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(len(list((self.root / "ledger").rglob("sup-*.json"))), 1)

        self.json_cli(
            "supersede",
            "--old-id",
            "resource-gamma-relay-key-v2",
            "--new-id",
            "resource-gamma-relay-key-v3",
            "--source",
            "thread:gamma/rotation",
            "--source-device-id",
            "mac-mini-primary",
        )
        # Chains resolve to the newest replacement for every older generation.
        chained = self.json_cli("get-resource", "resource-gamma-relay-key-v1")
        self.assertEqual(chained["superseded_by"], "resource-gamma-relay-key-v3")
        middle = self.json_cli("get-resource", "resource-gamma-relay-key-v2")
        self.assertEqual(middle["superseded_by"], "resource-gamma-relay-key-v3")

        missing = self.json_cli(
            "supersede",
            "--old-id",
            "resource-gamma-relay-key-v1",
            "--new-id",
            "resource-does-not-exist",
            "--source",
            "thread:gamma/rotation",
            expected=3,
        )
        self.assertFalse(missing["ok"])
        self.assertIn("resource-does-not-exist", missing["error"])

    def test_supersession_rejects_cycles_before_writing_ledger(self):
        self.json_cli("init")
        self.capture_rotated_key(
            "resource-cycle-v1", "cycleproof reusable key first", "CYCLE-ONE"
        )
        self.capture_rotated_key(
            "resource-cycle-v2", "cycleproof reusable key second", "CYCLE-TWO"
        )
        self.json_cli(
            "supersede",
            "--old-id",
            "resource-cycle-v1",
            "--new-id",
            "resource-cycle-v2",
            "--source",
            "thread:cycle/forward",
        )
        ledger_count = len(list((self.root / "ledger").rglob("sup-*.json")))
        rejected = self.json_cli(
            "supersede",
            "--old-id",
            "resource-cycle-v2",
            "--new-id",
            "resource-cycle-v1",
            "--source",
            "thread:cycle/reverse",
            expected=4,
        )
        self.assertIn("supersession cycle", rejected["error"])
        self.assertEqual(
            len(list((self.root / "ledger").rglob("sup-*.json"))),
            ledger_count,
        )

    def test_long_supersession_chain_is_resolved_once_not_quadratically(self):
        module = load_kb_module()

        class CountingEdges(dict):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.lookups = 0

            def __getitem__(self, key):
                self.lookups += 1
                return super().__getitem__(key)

        edges = CountingEdges(
            ("resource-chain-%05d" % index, "resource-chain-%05d" % (index + 1))
            for index in range(20000)
        )
        resolved = module.resolve_supersession_edges(edges)

        self.assertEqual(
            resolved["resource-chain-00000"],
            "resource-chain-20000",
        )
        self.assertEqual(
            resolved["resource-chain-19999"],
            "resource-chain-20000",
        )
        self.assertLessEqual(edges.lookups, len(edges) * 2)

    def test_same_timestamp_conflicting_lifecycle_events_are_rejected(self):
        (self.root / ".kb-data").mkdir()
        ledger = self.root / "ledger" / "imports"
        ledger.mkdir(parents=True)
        base = {
            "schema_version": "1",
            "created_at": "2026-07-23T10:11:12Z",
            "kind": "supersession",
            "scope": "global:test",
            "source": "import:test",
            "authority": "maintainer",
            "status": "active",
            "source_device_id": "device-test",
            "old_id": "resource-old",
        }
        first = dict(
            base,
            id="sup-same-time-a",
            statement="resource-old is superseded by resource-new-a",
            new_id="resource-new-a",
        )
        second = dict(
            base,
            id="sup-same-time-b",
            statement="resource-old is superseded by resource-new-b",
            new_id="resource-new-b",
        )
        (ledger / "a.json").write_text(json.dumps(first), encoding="utf-8")
        (ledger / "b.json").write_text(json.dumps(second), encoding="utf-8")
        result = self.json_cli("rebuild-index", expected=4)
        self.assertIn("ambiguous lifecycle decisions", result["error"])

    def test_global_lifecycle_beats_layer_but_curated_beats_raw_history(self):
        resource_dir = self.root / "resources" / "catalog"
        resource_dir.mkdir(parents=True)
        resource_dir.joinpath("old.json").write_text(
            json.dumps(
                {
                    "id": "resource-global-priority-old",
                    "kind": "resource",
                    "scope": "global:test",
                    "title": "prioritymarker canonical fact",
                    "statement": "prioritymarker canonical fact",
                    "status": "active",
                    "created_at": "2025-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        self.init_history(
            [
                {
                    "event_hash": "priority-raw-new",
                    "event_id": "priority-raw-new",
                    "event_timestamp": "2099-01-01T00:00:00Z",
                    "content": "prioritymarker raw history says something newer",
                }
            ]
        )
        self.json_cli("init")
        initial = self.json_cli("search", "prioritymarker")
        self.assertEqual(initial["results"][0]["id"], "resource-global-priority-old")

        self.json_cli(
            "capture",
            "--id",
            "evt-global-priority-new",
            "--kind",
            "fact",
            "--scope",
            "global:test",
            "--statement",
            "prioritymarker canonical fact replacement",
            "--status",
            "active",
            "--source",
            "thread:priority/new",
            "--authority",
            "user-explicit",
            "--source-device-id",
            "mac-mini-primary",
        )
        self.json_cli(
            "supersede",
            "--old-id",
            "resource-global-priority-old",
            "--new-id",
            "evt-global-priority-new",
            "--source",
            "thread:priority/supersede",
        )
        searched = self.json_cli("search", "prioritymarker")
        ids = [item["id"] for item in searched["results"]]
        self.assertLess(
            ids.index("evt-global-priority-new"),
            ids.index("resource-global-priority-old"),
        )
        old = next(
            item
            for item in searched["results"]
            if item["id"] == "resource-global-priority-old"
        )
        self.assertEqual(old["superseded_by"], "evt-global-priority-new")

        database = sqlite3.connect(str(self.root / ".kb-data" / "knowledge.sqlite"))
        try:
            self.assertEqual(
                database.execute(
                    "SELECT value FROM kb_meta "
                    "WHERE key='lifecycle_projection_version'"
                ).fetchone()[0],
                load_kb_module().LIFECYCLE_PROJECTION_VERSION,
            )
            self.assertEqual(
                database.execute(
                    "SELECT new_id FROM lifecycle_supersessions "
                    "WHERE old_id='resource-global-priority-old'"
                ).fetchone()[0],
                "evt-global-priority-new",
            )
        finally:
            database.close()
        module = load_kb_module()
        connection = module.connect_database(self.root)
        try:
            with mock.patch.object(
                module,
                "lifecycle_maps",
                side_effect=AssertionError("indexed read must not rescan ledger"),
            ):
                projected, _ = module.lifecycle_maps_for_read(
                    self.root, connection
                )
        finally:
            connection.close()
        self.assertEqual(
            projected["resource-global-priority-old"],
            "evt-global-priority-new",
        )

    def test_search_orders_lifecycle_then_authority_then_recency(self):
        ledger = self.root / "ledger" / "imports"
        ledger.mkdir(parents=True)

        def event(event_id, created_at, authority, status):
            return {
                "schema_version": "1",
                "id": event_id,
                "created_at": created_at,
                "kind": "fact",
                "scope": "global:ranking",
                "statement": "rankmarker authority and recency fixture",
                "source": "fixture:%s" % event_id,
                "authority": authority,
                "status": status,
                "source_device_id": "test-device",
            }

        fixtures = [
            event("evt-rank-user-old", "2024-01-01T00:00:00Z", "user-explicit", "active"),
            event("evt-rank-user-new", "2025-01-01T00:00:00Z", "user-stated", "active"),
            event(
                "evt-rank-verified-future",
                "2099-01-01T00:00:00Z",
                "verified-current-system-state",
                "active",
            ),
            event(
                "evt-rank-inference",
                "2100-01-01T00:00:00Z",
                "agent-inference",
                "candidate",
            ),
            event(
                "evt-rank-active-question",
                "2100-06-01T00:00:00Z",
                "user-explicit",
                "active-question",
            ),
            event(
                "evt-rank-superseded-user",
                "2101-01-01T00:00:00Z",
                "user-explicit",
                "superseded",
            ),
        ]
        ledger.joinpath("ranking.json").write_text(
            json.dumps({"events": fixtures}),
            encoding="utf-8",
        )
        self.json_cli("init")

        searched = self.json_cli("search", "rankmarker")

        self.assertEqual(
            [item["id"] for item in searched["results"]],
            [
                "evt-rank-user-new",
                "evt-rank-user-old",
                "evt-rank-verified-future",
                "evt-rank-active-question",
                "evt-rank-inference",
                "evt-rank-superseded-user",
            ],
        )

    def test_search_folds_same_id_projections_but_not_equal_text_across_ids(self):
        event_id = "resource-dedup-projection"
        statement = "dedupmarker exact projection fixture"
        record = {
            "schema_version": "1",
            "id": event_id,
            "created_at": "2026-08-16T12:00:00Z",
            "kind": "resource",
            "scope": "global:dedup",
            "statement": statement,
            "source": "fixture:dedup",
            "authority": "user-explicit",
            "status": "active",
            "source_device_id": "test-device",
        }
        resource_dir = self.root / "resources" / "catalog"
        resource_dir.mkdir(parents=True)
        resource_dir.joinpath("dedup.json").write_text(
            json.dumps(dict(record, title=statement)),
            encoding="utf-8",
        )
        ledger = self.root / "ledger" / "imports"
        ledger.mkdir(parents=True)
        ledger.joinpath("dedup.json").write_text(
            json.dumps(
                {
                    "events": [
                        record,
                        dict(record, id="evt-dedup-same-text", source="fixture:other-id"),
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.json_cli("init")

        searched = self.json_cli("search", "dedupmarker")

        self.assertEqual(
            [item["id"] for item in searched["results"]],
            [event_id, "evt-dedup-same-text"],
        )
        folded = searched["results"][0]
        self.assertEqual(folded["result_type"], "canonical")
        self.assertEqual(folded["result_types"], ["canonical", "history"])
        self.assertEqual(folded["duplicate_count"], 2)

    def test_same_id_conflicting_payloads_remain_visible(self):
        module = load_kb_module()
        folded = module.fold_exact_result_duplicates(
            [
                {
                    "id": "same-id-conflict",
                    "result_type": "canonical",
                    "kind": "fact",
                    "scope": "project:one",
                    "title": "first payload",
                    "content": json.dumps({"value": "credential-one"}),
                },
                {
                    "id": "same-id-conflict",
                    "result_type": "history",
                    "kind": "fact",
                    "scope": "project:one",
                    "statement": "first payload",
                    "value": "credential-two",
                },
            ]
        )

        self.assertEqual(len(folded), 2)
        self.assertTrue(
            all(
                item["projection_conflict"] == "same-id-different-payload"
                for item in folded
            )
        )

    def test_default_search_detects_different_hidden_values_for_same_id(self):
        event_id = "credential-conflict-same-id"
        statement = "credential-conflict"
        canonical_secret = "CANONICAL-SECRET-VALUE-1199"
        ledger_secret = "LEDGER-SECRET-VALUE-2288"
        resource_dir = self.root / "resources" / "catalog"
        resource_dir.mkdir(parents=True)
        resource_dir.joinpath("credential-conflict.json").write_text(
            json.dumps(
                {
                    "id": event_id,
                    "kind": "api-key",
                    "scope": "project:credential-conflict",
                    "value": canonical_secret,
                    "authority": "user-explicit",
                    "status": "active",
                    "created_at": "2026-08-16T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        ledger = self.root / "ledger" / "2026" / "08"
        ledger.mkdir(parents=True)
        ledger.joinpath(event_id + ".json").write_text(
            json.dumps(
                {
                    "id": event_id,
                    "created_at": "2026-08-16T00:00:00Z",
                    "kind": "api-key",
                    "scope": "project:credential-conflict",
                    "statement": statement,
                    "value": ledger_secret,
                    "source": "thread:credential-conflict",
                    "authority": "user-explicit",
                    "status": "active",
                    "source_device_id": "test-device",
                }
            ),
            encoding="utf-8",
        )
        initialized = self.json_cli("init")
        self.assertGreaterEqual(initialized["canonical_documents"], 1, initialized)

        searched = self.json_cli("search", "credential-conflict")
        matching = [
            item for item in searched["results"] if item["id"] == event_id
        ]
        self.assertEqual(len(matching), 2, searched)
        self.assertTrue(
            all(
                item["projection_conflict"] == "same-id-different-payload"
                for item in matching
            )
        )
        serialized = json.dumps(searched)
        self.assertNotIn(canonical_secret, serialized)
        self.assertNotIn(ledger_secret, serialized)
        self.assertNotIn("_credential_value_fingerprint", serialized)

        # A disposable index can outlive a moved/missing ledger reference.
        # Missing exact-value proof must remain a visible conflict, never be
        # folded merely because both public placeholders look the same.
        ledger.joinpath(event_id + ".json").unlink()
        stale = self.json_cli("search", "credential-conflict")
        stale_matching = [
            item for item in stale["results"] if item["id"] == event_id
        ]
        self.assertEqual(len(stale_matching), 2, stale)
        self.assertTrue(
            all(
                item["projection_conflict"] == "same-id-different-payload"
                for item in stale_matching
            )
        )

    def test_resolve_candidate_reject_sorts_last_and_accept_restores(self):
        self.json_cli("init")
        for event_id, statement in (
            ("evt-review-keep", "deltatopic scheduling fact to keep"),
            ("evt-review-drop", "deltatopic scheduling fact to drop"),
        ):
            self.json_cli(
                "capture",
                "--id",
                event_id,
                "--kind",
                "fact",
                "--scope",
                "project:delta",
                "--statement",
                statement,
                "--source",
                "thread:delta/%s" % event_id,
                "--authority",
                "user-explicit",
                "--status",
                "candidate",
                "--source-device-id",
                "mac-mini-primary",
            )

        rejected = self.json_cli(
            "resolve-candidate",
            "evt-review-drop",
            "--action",
            "reject",
            "--note",
            "duplicate of keep",
        )
        self.assertEqual(rejected["candidate_state"], "rejected")
        candidate_path = self.root / "inbox" / "candidates" / "evt-review-drop.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        self.assertEqual(candidate["candidate_state"], "rejected")
        self.assertTrue(candidate["resolved_at"])
        self.assertEqual(candidate["resolution_note"], "duplicate of keep")
        self.assertTrue((self.root / rejected["ledger"]).is_file())

        searched = self.json_cli("search", "deltatopic scheduling")
        review_ids = [
            item["id"] for item in searched["results"] if item["id"].startswith("evt-review-")
        ]
        self.assertEqual(review_ids[-1], "evt-review-drop")
        drop = next(item for item in searched["results"] if item["id"] == "evt-review-drop")
        self.assertEqual(drop["candidate_state"], "rejected")

        accepted = self.json_cli("resolve-candidate", "evt-review-keep", "--action", "accept")
        self.assertEqual(accepted["candidate_state"], "accepted")
        accepted_candidate = json.loads(
            (self.root / "inbox" / "candidates" / "evt-review-keep.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(accepted_candidate["candidate_state"], "accepted")

        searched = self.json_cli("search", "deltatopic scheduling")
        review_ids = [
            item["id"] for item in searched["results"] if item["id"].startswith("evt-review-")
        ]
        self.assertEqual(review_ids, ["evt-review-keep", "evt-review-drop"])
        keep = next(item for item in searched["results"] if item["id"] == "evt-review-keep")
        self.assertEqual(keep["candidate_state"], "accepted")

        repeated = self.json_cli(
            "resolve-candidate", "evt-review-drop", "--action", "reject"
        )
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(len(list((self.root / "ledger").rglob("cres-*.json"))), 2)
        # Rejected candidates are demoted, never deleted.
        self.assertTrue(candidate_path.is_file())

        missing = self.json_cli(
            "resolve-candidate", "evt-review-missing", "--action", "accept", expected=3
        )
        self.assertIn("candidate not found", missing["error"])

        self.json_cli("rebuild-index")
        searched = self.json_cli("search", "deltatopic scheduling")
        review_ids = [
            item["id"] for item in searched["results"] if item["id"].startswith("evt-review-")
        ]
        self.assertEqual(review_ids, ["evt-review-keep", "evt-review-drop"])

    def test_hot_only_search_applies_supersession_and_rejection_lifecycle(self):
        cold_target = self.root.parent / "missing-cold-target"
        (self.root / ".kb-data").symlink_to(cold_target, target_is_directory=True)
        self.capture_rotated_key(
            "resource-gamma-relay-key-v1", "gammarelay api key first rotation", "HOT-SENTINEL-1"
        )
        self.capture_rotated_key(
            "resource-gamma-relay-key-v2", "gammarelay api key second rotation", "HOT-SENTINEL-2"
        )
        superseded = self.json_cli(
            "supersede",
            "--old-id",
            "resource-gamma-relay-key-v1",
            "--new-id",
            "resource-gamma-relay-key-v2",
            "--source",
            "thread:gamma/rotation",
            "--source-device-id",
            "mac-mini-primary",
        )
        self.assertEqual(superseded["index_status"], "pending-cold-data-unavailable")
        self.json_cli(
            "capture",
            "--id",
            "evt-gamma-hot-drop",
            "--kind",
            "fact",
            "--scope",
            "project:gamma",
            "--statement",
            "gammarelay hot layer candidate to reject",
            "--source",
            "thread:gamma/hot-drop",
            "--authority",
            "user-explicit",
            "--status",
            "candidate",
            "--source-device-id",
            "mac-mini-primary",
        )
        resolved = self.json_cli(
            "resolve-candidate", "evt-gamma-hot-drop", "--action", "reject"
        )
        self.assertEqual(resolved["index_status"], "pending-cold-data-unavailable")

        searched = self.json_cli("search", "gammarelay")
        self.assertEqual(searched["retrieval_mode"], "hot-only")
        ids = [item["id"] for item in searched["results"]]
        self.assertLess(
            ids.index("resource-gamma-relay-key-v2"),
            ids.index("resource-gamma-relay-key-v1"),
        )
        self.assertEqual(ids[-1], "evt-gamma-hot-drop")
        for item in searched["results"]:
            if item["id"] == "resource-gamma-relay-key-v1":
                self.assertEqual(item["superseded_by"], "resource-gamma-relay-key-v2")
            if item["id"] == "evt-gamma-hot-drop":
                self.assertEqual(item["candidate_state"], "rejected")
        # Lifecycle commands never create a replacement cold-data directory.
        self.assertTrue((self.root / ".kb-data").is_symlink())
        self.assertFalse(cold_target.exists())


if __name__ == "__main__":
    unittest.main()
