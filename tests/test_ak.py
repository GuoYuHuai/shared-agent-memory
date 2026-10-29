import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL / 'scripts'))
import kb
import install
import ak


class EverydayCLI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ak-cli-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / '知识 with spaces'

    def cli(self, *args, ok=True, root=None):
        result = subprocess.run([sys.executable, str(TOOL / 'scripts/ak.py'), '--root', str(root or self.root), '--json', *args],
            capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.returncode, 0 if ok else 2, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        return json.loads(result.stdout)

    def init(self):
        return self.cli('init')

    def configure_sync(self, enabled=True, repository='fictional/memory'):
        path = self.root / 'config/kb.json'
        config = json.loads(path.read_text())
        config['hot_sync'].update(enabled=enabled, expected_repository=repository)
        path.write_text(json.dumps(config))

    def dispatch(self, *arguments):
        return ak.dispatch(ak.parser().parse_args(['--root', str(self.root), *arguments]))

    def test_save_shares_only_explicitly_enabled_bound_sync(self):
        self.init()
        for enabled, repository, expected in ((False, 'fictional/memory', False),
                (True, None, False), ('true', 'fictional/memory', False),
                (True, 'fictional/memory', True)):
            with self.subTest(enabled=enabled, repository=repository):
                self.configure_sync(enabled, repository)
                with mock.patch.object(ak.sync_runner, 'run_sync', return_value={
                        'ok':True, 'sync':{'ok':True, 'pushed':True}}) as sync:
                    result = self.dispatch('remember', 'Fictional reusable fact', '--source', 'fictional:user')
                self.assertTrue(result['saved'])
                self.assertEqual(result['synced'], expected)
                self.assertEqual(sync.call_count, int(expected))

    def test_failed_sync_keeps_save_and_retry_uses_same_record(self):
        self.init()
        self.configure_sync()
        args = ('remember', 'Fictional reusable fact', '--id', 'saved-offline', '--source', 'fictional:user')
        for outcome in ({'ok':False, 'status':'network-deadline'}, {'ok':True, 'pending':True},
                        {'ok':False, 'status':'sync-recovery-required', 'recovery_required':True},
                        {'ok':True}, {'ok':True, 'sync':{'ok':False, 'pushed':True}}):
            with self.subTest(outcome=outcome), mock.patch.object(ak.sync_runner, 'run_sync', return_value=outcome):
                result = self.dispatch(*args)
                self.assertFalse(result['ok'])
                self.assertTrue(result['saved'])
                self.assertTrue(result['readback'])
                self.assertTrue(result['pending'])
                self.assertFalse(result['synced'])
                self.assertEqual(result['id'], 'saved-offline')
                self.assertIn('Saved locally: saved-offline', ak.human(result))
                if outcome.get('recovery_required'):
                    self.assertTrue(result['recovery_required'])
                    self.assertTrue(result['sync_recovery_required'])
        self.assertEqual(len(list((self.root / 'ledger/by-id').glob('*.json'))), 1)
        with mock.patch.object(ak.sync_runner, 'run_sync', return_value={
                'ok':True, 'sync':{'ok':True, 'pushed':True}}):
            repeated = self.dispatch(*args)
        self.assertTrue(repeated['duplicate'])
        self.assertTrue(repeated['synced'])

    def test_offline_save_and_correction_do_not_start_sync(self):
        self.init()
        self.configure_sync()
        with mock.patch.object(ak.sync_runner, 'run_sync') as sync:
            saved = self.dispatch('remember', 'Fictional port 4310', '--source', 'fictional:user', '--offline')
            corrected = self.dispatch('correct', saved['id'], 'Fictional port 4311', '--source', 'fictional:user', '--offline')
        sync.assert_not_called()
        for result in (saved, corrected):
            self.assertTrue(result['ok'])
            self.assertTrue(result['pending'])
            self.assertEqual(result['sync']['status'], 'offline')

    def test_correction_shares_and_keeps_the_local_pair_on_failure(self):
        self.init()
        saved = self.dispatch('remember', 'Fictional port 4310', '--source', 'fictional:user')
        self.configure_sync()
        with mock.patch.object(ak.sync_runner, 'run_sync', return_value={'ok':False, 'status':'sync-failed'}) as sync:
            corrected = self.dispatch('correct', saved['id'], 'Fictional port 4311', '--source', 'fictional:user')
        sync.assert_called_once_with(self.root)
        self.assertFalse(corrected['ok'])
        pair = json.loads((self.root / corrected['ledger']).read_text())
        self.assertEqual(pair[0]['statement'], 'Fictional port 4311')
        self.assertEqual(pair[1]['old_id'], saved['id'])

    def test_actual_sync_unavailable_returns_nonzero_after_real_local_save(self):
        self.init()
        self.configure_sync()
        # No origin exists: the real hot_sync child refuses before network I/O.
        result = self.cli('remember', 'Fictional disconnected record', '--id', 'disconnected', '--source', 'fictional:user', ok=False)
        self.assertTrue(result['saved'])
        self.assertTrue(result['readback'])
        self.assertTrue(result['pending'])
        self.assertFalse(result['synced'])
        found = self.cli('recall', 'disconnected', '--offline')
        self.assertEqual(found['results'][0]['id'], 'disconnected')

    def test_new_folder_remember_recall_correction(self):
        self.init()
        saved = self.cli('remember', 'Atlas preview port 4310', '--scope', 'Atlas', '--id', 'atlas-port', '--source', 'fictional:user')
        self.assertFalse(saved['synced'])
        corrected = self.cli('correct', saved['id'], 'Atlas preview port 4311', '--source', 'fictional:correction')
        self.assertTrue(corrected['ok'])
        found = self.cli('recall', 'preview port', '--scope', 'Atlas', '--offline')
        self.assertEqual(found['results'][0]['id'], corrected.get('id') or corrected['new_id'])
        self.assertNotIn('atlas-port', [r.get('id') for r in found['results'] if r.get('current_eligible')])
        self.assertEqual(next(r for r in found['results'] if r.get('id') == 'atlas-port')['effective_status'], 'superseded')

    def test_external_ledger_append_is_seen_without_manual_reindex(self):
        self.init()
        self.cli('recall', 'external', '--offline')
        event = {'schema_version': '1', 'id': 'external-record', 'kind': 'fact', 'scope': 'Atlas',
                 'statement': 'external preview port 4312', 'source': 'fictional:other-agent',
                 'created_at': kb.iso_now(), 'authority': 'user-stated', 'status': 'active', 'source_device_id': 'fixture'}
        kb.atomic_json_create(self.root / 'ledger/by-id/external-record.json', event)
        found = self.cli('recall', 'external', '--offline')
        self.assertEqual(found['results'][0]['id'], 'external-record')

    def test_unavailable_storage_never_creates_a_replacement(self):
        result = self.cli('remember', 'test', '--source', 'fictional:user', ok=False)
        self.assertIn('unavailable', result['error'])
        self.assertFalse(self.root.exists())

    def test_status_has_no_index_or_network_side_effects(self):
        self.init()
        index = self.root / '.git/index'
        before = index.read_bytes()
        policy = self.root / 'POLICY.md'
        stamp = policy.stat().st_mtime_ns
        os.utime(policy, ns=(stamp + 2000000000, stamp + 2000000000))
        result = self.cli('status')
        self.assertEqual(result['storage'], 'local')
        self.assertEqual(result['remote_freshness'], 'not-applicable')
        self.assertEqual(result['persistent_activation'], 'not-verified')
        self.assertFalse(list((self.root / '.kb-data').glob('*.sqlite')))
        self.assertEqual(index.read_bytes(), before)

    def test_connections_preserve_project_rules_and_disconnect(self):
        self.init()
        target = self.base / 'project/AGENTS.md'
        target.parent.mkdir()
        original = '# Project\nUse unittest.\n'
        target.write_text(original)
        result = self.cli('connect', '--instructions', str(target))
        self.assertEqual(result['persistent_activation'], 'not-verified')
        self.assertIn('recall', target.read_text())
        self.assertEqual(len(self.cli('status')['connections']), 1)
        self.cli('disconnect', '--instructions', str(target))
        self.assertEqual(target.read_text(), original)

    def test_symlink_instructions_not_resolved_before_validation(self):
        self.init()
        target = self.base / 'real.md'
        target.write_text('Keep this.')
        link = self.base / 'AGENTS.md'
        link.symlink_to(target)
        self.cli('connect', '--instructions', str(link), ok=False)
        self.assertEqual(target.read_text(), 'Keep this.')

    def test_status_reports_damaged_connection(self):
        self.init()
        target = self.base / 'AGENTS.md'
        self.cli('connect', '--instructions', str(target))
        target.write_text(target.read_text().replace('recall', 'changed-recall'))
        result = self.cli('status', ok=False)
        self.assertIn('repair', result['reason'])
        self.assertIn(str(target), result['recovery'])

    def test_candidate_not_promoted_to_current_fact(self):
        self.init()
        self.cli('remember', 'Atlas guessed port 4313', '--source', 'fictional:inference', '--authority', 'agent-inferred')
        found = self.cli('recall', 'guessed port', '--offline')
        self.assertEqual(found.get('current_count', 0), 0)

    def test_no_exact_value_loss(self):
        self.init()
        value = self.base / 'exact.txt'
        value.write_bytes(b'first\r\nsecond\r\n')
        self.cli('remember', 'Atlas configuration', '--id', 'config-value', '--source', 'fictional:user', '--value-file', str(value))
        found = self.cli('recall', 'Atlas configuration', '--include-values', '--offline')
        self.assertEqual(found['results'][0]['value'], 'first\r\nsecond\r\n')

    def test_saved_capture_with_readonly_index_is_explicit_and_same_id_repairs(self):
        self.init()
        self.cli('remember', 'Atlas initial fact', '--id', 'initial', '--source', 'fictional:user')
        database = self.root / '.kb-data/knowledge.sqlite'
        original_mode = database.stat().st_mode
        database.chmod(0o444)
        try:
            if os.access(database, os.W_OK):
                self.skipTest('host does not enforce this read-only file fixture')
            args = ('remember', 'Atlas later fact', '--id', 'later', '--source', 'fictional:user')
            partial = self.cli(*args, ok=False)
            self.assertTrue(partial['saved'])
            self.assertTrue(partial['readback'])
            self.assertEqual(partial['id'], 'later')
            self.assertEqual(partial['index_status'], 'pending-refresh-failed')
            self.assertIn('--id later', partial['recovery']['action'])
            self.assertIn('Saved locally: later', ak.human(partial))
            self.assertIn('--id later', ak.human(partial))
            event = self.root / partial['ledger']
            unchanged = event.read_bytes()
            repeated = self.cli(*args, ok=False)
            self.assertTrue(repeated['duplicate'])
            self.assertEqual(event.read_bytes(), unchanged)
            found = self.cli('recall', 'later', '--offline')
            self.assertEqual(found['results'][0]['id'], 'later')
            self.assertTrue(found['index_refresh_failed'])
        finally:
            database.chmod(original_mode)
            # SQLite may create companions with the fixture's read-only mode.
            # Restoring storage permissions includes those task-created files.
            for suffix in ('-wal', '-shm'):
                companion = Path(str(database) + suffix)
                if companion.exists():
                    companion.chmod(original_mode)
        repaired = self.cli(*args)
        self.assertTrue(repaired['duplicate'])
        self.assertTrue(repaired['readback'])
        self.assertEqual(repaired['index_status'], 'current')
        self.assertEqual(event.read_bytes(), unchanged)
        self.assertEqual(len(list((self.root / 'ledger/by-id').glob('*.json'))), 2)

    def test_unsupported_schema_is_not_rewritten(self):
        self.init()
        path = self.root / 'MANIFEST.json'
        content = path.read_text().replace('"schema_version": "1"', '"schema_version": "future"')
        path.write_text(content)
        self.cli('remember', 'test', '--source', 'fictional:user', ok=False)
        self.assertEqual(path.read_text(), content)


if __name__ == '__main__':
    unittest.main()
