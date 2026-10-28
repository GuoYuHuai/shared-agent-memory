import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL / 'scripts'))
import kb
import install


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

    def test_unsupported_schema_is_not_rewritten(self):
        self.init()
        path = self.root / 'MANIFEST.json'
        content = path.read_text().replace('"schema_version": "1"', '"schema_version": "future"')
        path.write_text(content)
        self.cli('remember', 'test', '--source', 'fictional:user', ok=False)
        self.assertEqual(path.read_text(), content)


if __name__ == '__main__':
    unittest.main()
