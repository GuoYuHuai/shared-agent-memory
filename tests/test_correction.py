import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import correction
import kb
import workspace_lock


class AtomicCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='knowledge-correction-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / '知识 folder'
        (self.root / 'ledger/by-id').mkdir(parents=True)
        (self.root / 'config').mkdir()
        (self.root / 'MANIFEST.json').write_text(json.dumps({'schema_version': '1'}))
        (self.root / 'config/kb.json').write_text('{}')
        (self.root / 'schema').mkdir()
        shutil.copy2(ROOT / 'schema/knowledge.sql', self.root / 'schema/knowledge.sql')
        subprocess.run(['git', 'init', '--quiet'], cwd=self.root, check=True, capture_output=True)
        subprocess.run(['git', '-c', 'user.name=Correction Tests',
                        '-c', 'user.email=correction@example.test', 'commit',
                        '--allow-empty', '-m', 'Test fixture', '--quiet'],
                       cwd=self.root, check=True, capture_output=True)
        self.old = self.event('old', 'Atlas reports use paragraphs', fact_key='report-format')

    def event(self, identifier, statement, **extra):
        event = dict(schema_version='1', id=identifier, kind='preference',
                     scope='project:atlas', statement=statement, source='fictional:user:1',
                     authority='user-stated', status='active', created_at='2020-01-01T00:00:00Z',
                     source_device_id='fixture-device')
        event.update(extra)
        (self.root / 'ledger/by-id' / (identifier + '.json')).write_text(json.dumps(event))
        return event

    def correct(self, statement='Atlas reports use tables', **kwargs):
        return correction.correct(self.root, 'old', statement, 'fictional:user:2', **kwargs)

    def ledger(self):
        return [(raw, ref) for path in kb.ledger_files(self.root) for raw, ref in kb.iter_json_values(path)]

    def search(self, query):
        args = kb.build_parser().parse_args(['--root', str(self.root), 'search', query, '--json'])
        return kb.run(args)

    def test_one_publication_makes_replacement_and_edge_visible_together(self):
        original = kb.atomic_json_create
        observed = []
        def publish(path, payload):
            self.assertEqual([row['id'] for row, _ in self.ledger()], ['old'])
            self.assertIsInstance(payload, list)
            self.assertEqual(len(payload), 2)
            original(path, payload)
            observed.append([row['id'] for row, _ in self.ledger()])
        with mock.patch.object(kb, 'atomic_json_create', side_effect=publish):
            result = self.correct()
        self.assertTrue(result['saved'])
        self.assertTrue(result['ok'])
        self.assertFalse(result['duplicate'])
        self.assertEqual(len(observed), 1)
        self.assertEqual(set(observed[0]), {'old', result['new_id'], result['supersession_id']})
        pair = json.loads((self.root / result['ledger']).read_text())
        self.assertEqual(pair[0]['kind'], 'preference')
        self.assertEqual(pair[0]['scope'], 'project:atlas')
        self.assertEqual(pair[0]['fact_key'], 'report-format')
        self.assertEqual(pair[1]['old_id'], 'old')
        self.assertEqual(pair[1]['new_id'], pair[0]['id'])
        self.assertTrue(all(row['source'] == 'fictional:user:2' for row in pair))
        self.assertTrue(all(not kb.event_validation_errors(row) for row in pair))
        self.assertEqual(json.loads((self.root / 'ledger/by-id/old.json').read_text()), self.old)
        report = self.search('Atlas reports')
        self.assertEqual(report['results'][0]['id'], result['new_id'])
        self.assertEqual(report['current_count'], 1)
        self.assertEqual(len(kb.find_events_by_id(self.root, result['new_id'])), 1)

    def test_identical_retry_ignores_generated_time_and_device(self):
        first = self.correct()
        data = (self.root / first['ledger']).read_bytes()
        with mock.patch.object(kb, 'iso_now', return_value='2040-02-03T00:00:00Z'), \
                mock.patch.object(kb, 'configured_device_id', return_value='different-device'):
            retry = self.correct()
        self.assertTrue(retry['duplicate'])
        self.assertEqual(retry['id'], first['id'])
        self.assertEqual((self.root / first['ledger']).read_bytes(), data)
        self.assertEqual(len(self.ledger()), 3)

    def test_changed_request_cannot_correct_an_already_replaced_fact(self):
        self.correct()
        with self.assertRaisesRegex(kb.ConflictError, 'already corrected'):
            self.correct('Atlas reports use lists')
        self.assertEqual(len(self.ledger()), 3)

    def test_unknown_id_invalid_input_and_inference_never_publish(self):
        with self.assertRaises(kb.NotFoundError):
            correction.correct(self.root, 'missing', 'New statement', 'user:2')
        for authority in ('agent-inference', 'not-verified', 'maintainer', ''):
            with self.assertRaises(kb.KBError):
                self.correct(authority=authority)
        for statement, source in (('', 'user:2'), ('A fact', '')):
            with self.assertRaises(kb.KBError):
                correction.correct(self.root, 'old', statement, source)
        self.assertEqual(len(self.ledger()), 1)

    def test_lock_and_future_schema_refuse_without_publishing(self):
        with workspace_lock.acquire(self.root):
            with self.assertRaises(workspace_lock.WorkspaceBusy):
                self.correct()
        (self.root / 'MANIFEST.json').write_text('{"schema_version":"99"}')
        with self.assertRaisesRegex(kb.KBError, 'unsupported knowledge schema'):
            self.correct()
        self.assertEqual(len(self.ledger()), 1)

    def test_failure_before_atomic_publish_leaves_only_old_record(self):
        with mock.patch.object(kb, 'atomic_json_create', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                self.correct()
        self.assertEqual([row['id'] for row, _ in self.ledger()], ['old'])
        self.assertEqual(self.search('Atlas')['current_count'], 1)

    def test_projection_failure_reports_saved_and_retry_repairs(self):
        (self.root / '.kb-data').mkdir()
        with mock.patch.object(kb, 'refresh_index_after_write', side_effect=kb.KBError('index failed')):
            result = self.correct()
        self.assertFalse(result['ok'])
        self.assertTrue(result['saved'])
        self.assertTrue(result['readback'])
        self.assertTrue(result['recovery_required'])
        self.assertEqual(result['index_status'], 'pending-refresh-failed')
        self.assertEqual(len(self.ledger()), 3)
        retry = self.correct()
        self.assertTrue(retry['ok'])
        self.assertTrue(retry['duplicate'])
        self.assertEqual(retry['index_status'], 'current')
        report = self.search('Atlas')
        self.assertEqual(report['current_count'], 1)
        current_facts = [row for row in report['results']
                         if row.get('current_eligible') and row.get('kind') not in kb.LIFECYCLE_KINDS]
        self.assertEqual([row['id'] for row in current_facts], [result['new_id']])
        self.assertFalse(next(row for row in report['results'] if row['id'] == 'old')['current_eligible'])

    def test_publication_error_after_link_reports_complete_saved_pair(self):
        original = kb.atomic_json_create
        def fail_after_publish(path, payload):
            original(path, payload)
            raise OSError('simulated directory fsync failure')
        with mock.patch.object(kb, 'atomic_json_create', side_effect=fail_after_publish):
            result = self.correct()
        self.assertTrue(result['saved'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['recovery']['failures'][0]['stage'], 'publication-durability')
        self.assertEqual(len(self.ledger()), 3)
        retry = self.correct()
        self.assertTrue(retry['duplicate'])
        self.assertTrue(retry['ok'])

    def test_retry_reports_durability_failure_without_claiming_an_unsaved_fact(self):
        self.correct()
        with mock.patch.object(correction.os, 'fsync', side_effect=OSError('fsync failed')):
            retry = self.correct()
        self.assertTrue(retry['saved'])
        self.assertTrue(retry['duplicate'])
        self.assertFalse(retry['ok'])
        self.assertEqual(retry['recovery']['failures'][0]['stage'], 'ledger-durability')
        self.assertEqual(len(self.ledger()), 3)

    def test_exact_value_requires_explicit_file_preserves_bytes_and_old_value(self):
        self.old = self.event('old', 'Atlas deployment secret', kind='token', value='OLD-FICTIONAL-VALUE')
        with self.assertRaisesRegex(kb.KBError, 'explicit value_file'):
            self.correct('Atlas deployment secret changed')
        value_path = self.root / 'new-value.txt'
        exact = b'NEW-FICTIONAL-VALUE\r\nsecond line\r\n'
        value_path.write_bytes(exact)
        result = self.correct('Atlas deployment secret changed', value_file=value_path)
        self.assertTrue(result['ok'], result)
        pair = json.loads((self.root / result['ledger']).read_text())
        self.assertEqual(pair[0]['value'].encode(), exact)
        self.assertNotIn('value', pair[1])
        self.assertEqual(json.loads((self.root / 'ledger/by-id/old.json').read_text())['value'], 'OLD-FICTIONAL-VALUE')
        resource = json.loads((self.root / result['resource']).read_text())
        self.assertEqual(resource['value'].encode(), exact)
        self.assertTrue(resource['source']['ref'].endswith('#[0]'))
        self.assertNotIn('NEW-FICTIONAL', json.dumps(result))
        self.assertTrue(self.correct('Atlas deployment secret changed', value_file=value_path)['duplicate'])

    def test_new_credential_claim_needs_value_and_unreadable_file_writes_nothing(self):
        with self.assertRaisesRegex(kb.KBError, 'credential-value-missing'):
            self.correct('Atlas API key is sk-fictional12345678')
        with self.assertRaisesRegex(kb.KBError, 'cannot read correction value_file'):
            self.correct(value_file=self.root / 'missing.txt')
        self.assertEqual(len(self.ledger()), 1)

    def test_conflicting_old_identity_and_competing_edges_fail_closed(self):
        different = dict(self.old, statement='different claim')
        (self.root / 'ledger/duplicate.json').write_text(json.dumps(different))
        with self.assertRaisesRegex(kb.ConflictError, 'conflicting records'):
            self.correct()
        (self.root / 'ledger/duplicate.json').unlink()
        self.event('edge-a', 'first correction', kind='supersession', old_id='old', new_id='a')
        self.event('edge-b', 'second correction', kind='supersession', old_id='old', new_id='b', created_at='2030-01-01T00:00:00Z')
        with self.assertRaisesRegex(kb.ConflictError, 'ambiguous lifecycle'):
            self.correct()
        self.assertFalse(any(row['id'].startswith('corr-') for row, _ in self.ledger()))

    def test_canonical_resource_without_ledger_can_be_corrected(self):
        path = self.root / 'resources/catalog'
        path.mkdir(parents=True)
        raw = dict(self.old, id='canonical-only', kind='resource', statement='Atlas endpoint')
        (path / 'canonical-only.json').write_text(json.dumps(raw))
        result = correction.correct(self.root, 'canonical-only', 'Atlas new endpoint', 'user:2')
        self.assertTrue(result['ok'], result)
        self.assertEqual(kb.lifecycle_maps(self.root)[0]['canonical-only'], result['new_id'])

    def test_atomic_correction_retries_after_later_correction_without_extra_fact(self):
        first = self.correct()
        second = correction.correct(self.root, first['id'], 'Atlas reports use charts', 'user:3')
        retry = self.correct()
        self.assertTrue(retry['duplicate'])
        self.assertEqual(kb.lifecycle_maps(self.root)[0]['old'], second['id'])
        self.assertEqual(len(self.ledger()), 5)

    def test_symlinked_publication_directory_refused(self):
        path = self.root / 'ledger/by-id'
        (path / 'old.json').replace(self.root / 'ledger/old.json')
        path.rmdir()
        with tempfile.TemporaryDirectory(prefix='outside-correction-') as outside:
            path.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(kb.KBError, 'symlink'):
                self.correct()
            self.assertEqual(list(Path(outside).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
