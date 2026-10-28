import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import automation
import ingest_history


class PublicAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '知识 with spaces'
        self.root.mkdir()
        (self.root / 'POLICY.md').write_text('# Fictional test policy\n')
        # Isolate device probing from mocked subprocess boundaries. Real child
        # processes still use the same current platform identity.
        device = automation.device_identity.resolve_device_id(self.root)
        fingerprint = automation.device_identity.platform_fingerprint()
        for name, value in [('resolve_device_id', device), ('platform_fingerprint', fingerprint)]:
            patcher = mock.patch.object(automation.device_identity, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_plan_is_not_registration_and_no_model_stays_pending(self):
        state = automation.plan(self.root, 'curate')
        self.assertEqual(state['registration'], 'not-registered')
        self.assertEqual(state['trial'], 'not-run')
        result = automation.run(self.root, 'curate')
        self.assertTrue(result['pending'])
        self.assertIsNone(automation.read(self.root, 'curate')['last_success'])

    def test_real_subprocess_health_has_readback_but_no_scheduler_claim(self):
        automation.plan(self.root, 'health')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/automation.py'), '--root', str(self.root), 'run', 'health'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])
        state = automation.read(self.root, 'health')
        self.assertEqual(state['trial'], 'succeeded')
        self.assertEqual(state['registration'], 'not-registered')
        self.assertEqual(state['last_run']['usage']['status'], 'not-applicable')

    def test_failed_run_keeps_prior_success_and_unknown_usage(self):
        automation.run(self.root, 'health')
        success = automation.read(self.root, 'health')['last_success']
        (self.root / 'ledger').mkdir()
        (self.root / 'ledger/bad.json').write_text('{invalid')
        result = automation.run(self.root, 'health')
        self.assertFalse(result['ok'])
        state = automation.read(self.root, 'health')
        self.assertEqual(state['trial'], 'failed')
        self.assertEqual(state['last_success'], success)
        self.assertEqual(result['usage']['status'], 'unknown')

    def test_portable_drive_never_inherits_other_device_success(self):
        with mock.patch.object(automation.device_identity, 'resolve_device_id', return_value='device-a'), mock.patch.object(automation.device_identity, 'platform_fingerprint', return_value='fingerprint-a'):
            automation.plan(self.root, 'health')
            automation.run(self.root, 'health')
            original = automation.directory(self.root) / 'health.json'
            preserved = original.read_bytes()
        with mock.patch.object(automation.device_identity, 'resolve_device_id', return_value='device-b'), mock.patch.object(automation.device_identity, 'platform_fingerprint', return_value='fingerprint-b'):
            state = automation.read(self.root, 'health')
            self.assertIsNone(state.get('last_success'))
            self.assertNotEqual(state.get('trial'), 'succeeded')
            planned = automation.plan(self.root, 'health')
            self.assertEqual(planned['registration'], 'not-registered')
            self.assertEqual(planned['trial'], 'not-run')
            self.assertNotEqual(automation.directory(self.root) / 'health.json', original)
        self.assertEqual(original.read_bytes(), preserved)

    def test_unowned_legacy_receipt_is_preserved_not_reported_as_local_success(self):
        legacy = self.root / '.ai-knowledge/automation/health.json'
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({'job':'health','registration':'registered','trial':'succeeded','last_success':'fictional-time'}))
        preserved = legacy.read_bytes()
        state = automation.read(self.root, 'health')
        self.assertEqual(state['registration'], 'unverified')
        self.assertIsNone(state['last_success'])
        self.assertFalse(automation.disable(self.root, 'health')['ok'])
        state = automation.plan(self.root, 'health')
        self.assertEqual(state['registration'], 'not-registered')
        self.assertEqual(legacy.read_bytes(), preserved)

    def test_moved_tool_root_invalidates_prior_execution_evidence(self):
        automation.plan(self.root, 'health')
        automation.run(self.root, 'health')
        with mock.patch.object(automation, 'TOOL_ROOT', self.root / 'different-tool-install'):
            state = automation.read(self.root, 'health')
            self.assertEqual(state['registration'], 'unverified')
            self.assertIsNone(state['last_success'])
            self.assertFalse(automation.disable(self.root, 'health')['ok'])

    def test_history_capture_passes_the_bound_device_identity(self):
        (self.root / 'config').mkdir()
        (self.root / 'config/kb.json').write_text(json.dumps({'history_capture': {'enabled':True,'source':'codex'}}))
        with mock.patch.object(automation.device_identity, 'resolve_device_id', return_value='bound-device'):
            command = automation.command(self.root, 'capture')
        self.assertEqual(command[command.index('--device-id')+1], 'bound-device')

    def test_missing_remote_and_disabled_history_do_not_spawn_commands(self):
        self.assertIsNone(automation.command(self.root, 'sync'))
        self.assertIsNone(automation.command(self.root, 'capture'))

    def test_bounded_history_rounds_resume_without_loss_or_duplicate(self):
        home = self.root / 'fictional-home'
        source_dir = home / '.codex/sessions'
        source_dir.mkdir(parents=True)
        source = source_dir / 'demo.jsonl'
        records = [{"type": "session_meta", "payload": {"id": "fictional-thread", "cwd": "/fictional"}}]
        records += [{"type": "response_item", "timestamp": "2026-01-01T00:00:00Z", "payload": {"type": "message", "role": "user", "id": str(i), "content": [{"type": "input_text", "text": 'fictional fact ' + str(i)}]}} for i in range(6)]
        source.write_text('\n'.join(json.dumps(x) for x in records) + '\n')
        db = ingest_history.initialize_database(self.root / '.kb-data/history.sqlite')
        try:
            totals = []
            for _ in range(5):
                totals.append(ingest_history.scan_history(db, 'codex', 'device-fictional', self.root / '.kb-data/cp', home, max_lines=2))
            self.assertEqual(sum(x['inserted'] for x in totals), 6)
            self.assertEqual(totals[0]['coverage_status'], 'partial')
            self.assertEqual(totals[-1]['coverage_status'], 'scanned-discovered-sources')
            self.assertEqual(totals[-1]['lines'], 0)
            self.assertEqual(sum(x['duplicates'] for x in totals), 0)
        finally:
            db.close()

    def test_registration_failure_is_not_registered(self):
        failed = subprocess.CompletedProcess([], 1, '', '')
        with (
            mock.patch.object(automation.platform, 'system', return_value='Darwin'),
            mock.patch.object(automation.os, 'getuid', return_value=501, create=True),
            mock.patch.object(automation, 'launchctl', return_value=failed),
        ):
            state = automation.register(self.root, 'health', 60)
        self.assertEqual(state['registration'], 'failed')
        self.assertEqual(state['trial'], 'not-run')

    def test_budget_failure_remains_pending(self):
        with mock.patch.object(automation, 'command', return_value=['fictional-command']), mock.patch.object(automation.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fictional-command', 1)):
            result = automation.run(self.root, 'capture', timeout=1)
        self.assertEqual(result['reason'], 'time-budget-exhausted')
        self.assertTrue(result['pending'])
        self.assertIsNone(automation.read(self.root, 'capture').get('last_success'))


    def test_zero_exit_without_positive_receipt_never_advances_success(self):
        for body in ("noise", "{}", "[]", '{"ok":true}'):
            with self.subTest(body=body), mock.patch.object(automation, "command", return_value=[sys.executable, "-c", "print(" + repr(body) + ")"]):
                result = automation.run(self.root, "sync")
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "invalid-job-receipt")
            self.assertIsNone(automation.read(self.root, "sync").get("last_success"))

    def test_interval_change_is_rejected_without_rewriting_receipt(self):
        import plistlib
        automation.plan(self.root, "health", 1800)
        automation.update_state(self.root, "health", {"registration": "registered"})
        path = automation.directory(self.root) / "health.plist"
        path.write_bytes(plistlib.dumps({"Label": automation.label(self.root, "health"), "StartInterval": 1800,
            "ProgramArguments": [sys.executable, str(Path(automation.__file__).resolve()), "--root", str(self.root), "run", "health"]}))
        before = (automation.directory(self.root) / "health.json").read_bytes()
        with (
            mock.patch.object(automation.platform, "system", return_value="Darwin"),
            mock.patch.object(automation.os, "getuid", return_value=501, create=True),
            mock.patch.object(automation, "launchctl", return_value=subprocess.CompletedProcess([], 0, "", "")),
        ):
            result = automation.register(self.root, "health", 3600)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "interval-change-requires-disable-and-register")
        self.assertEqual((automation.directory(self.root) / "health.json").read_bytes(), before)

    def test_plan_cannot_relabel_registered_interval(self):
        automation.plan(self.root, "health", 1800)
        automation.update_state(self.root, "health", {"registration": "registered"})
        receipt = automation.directory(self.root) / "health.json"
        before = receipt.read_bytes()
        result = automation.plan(self.root, "health", 60)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "interval-change-requires-disable-and-register")
        self.assertEqual(receipt.read_bytes(), before)
        same = automation.plan(self.root, "health", 1800)
        self.assertEqual(same["registration"], "registered")
        self.assertEqual(same["interval_seconds"], 1800)
        self.assertEqual(receipt.read_bytes(), before)

    def test_registration_and_disable_share_operation_lock(self):
        automation.plan(self.root, "health")
        before = (automation.directory(self.root) / "health.json").read_bytes()
        with automation.scheduler_operation(self.root):
            for operation in (automation.plan, automation.register, automation.disable):
                result = operation(self.root, "health")
                self.assertEqual(result["reason"], "scheduler-operation-running")
        self.assertEqual((automation.directory(self.root) / "health.json").read_bytes(), before)

    def test_proposal_mutation_before_lock_is_revalidated_before_apply(self):
        from contextlib import contextmanager, redirect_stdout
        from types import SimpleNamespace
        import io
        import proposal_runner
        proposal = self.root / "proposal.json"
        proposal.write_text('{"events":[]}')
        @contextmanager
        def acquire(root):
            proposal.write_text('{changed-to-invalid')
            yield
        apply = mock.Mock()
        host = SimpleNamespace(ROOT=self.root, parse_proposal=lambda path, batch: json.loads(path.read_text()),
            load_transaction=lambda batch: None, stage_proposal_transaction=apply)
        batches = SimpleNamespace(verify_batch_payload=lambda batch: {"batch_id": "fictional"})
        argv = ["proposal_runner.py", "--root", str(self.root), "--batch", str(self.root), "--proposal", str(proposal), "--apply"]
        with mock.patch.dict(sys.modules, {"run_semantic_maintenance": host, "semantic_batches": batches, "workspace_lock": SimpleNamespace(acquire=acquire)}), mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {}), redirect_stdout(io.StringIO()):
            self.assertEqual(proposal_runner.main(), 2)
        apply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
