import argparse
import importlib.util
import json
import sqlite3
import shutil
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import kb
import retrieve
import workspace_lock


class PublicRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='knowledge-retrieval-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'ledger').mkdir()
        (self.root / 'config').mkdir()
        self.config = {'retrieval': {'scope_aliases': {'project:atlas': ['星图']}, 'query_aliases': {'release': ['发布计划']}}}
        self.write_config()
        (self.root / '.gitignore').write_text('.kb-data/\ngenerated/\n')
        for command in (['init', '-b', 'main'], ['config','user.name','Fixture'], ['config','user.email','fixture@example.test'], ['add','.'], ['commit','-m','fixture']):
            subprocess.run(['git', *command], cwd=self.root, check=True, capture_output=True)

    def write_config(self):
        (self.root / 'config/kb.json').write_text(json.dumps(self.config))

    def event(self, id, statement, **extra):
        event = dict(schema_version='1', id=id, statement=statement, created_at='2026-10-01T00:00:00Z', kind='fact', scope='project:atlas', source='synthetic:test', authority='user-stated', status='active', source_device_id='device-a')
        event.update(extra)
        (self.root / 'ledger' / (id+'.json')).write_text(json.dumps(event))
        return event

    def search(self, query, scope='project:atlas', **extra):
        args = kb.build_parser().parse_args(['--root', str(self.root), 'search', query, '--scope', scope, '--json'])
        for k, v in extra.items():
            setattr(args, k, v)
        return kb.run(args)

    def lifecycle_fixture(self):
        self.event('old', 'Atlas reports use paragraphs', status='candidate')
        self.event('new', 'Atlas reports use tables')
        self.event('edge', 'Atlas report preference correction', kind='supersession', old_id='old', new_id='new')
        self.event('review', 'Atlas report preference accepted', kind='candidate-resolution', candidate_id='old', resolution='accepted')

    def test_normal_search_excludes_lifecycle_events_in_hot_and_indexed_paths(self):
        self.lifecycle_fixture()
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                if indexed:
                    kb.init_knowledge_base(self.root)
                report = self.search('Atlas')
                self.assertEqual(report['retrieval_mode'], 'indexed' if indexed else 'hot-only')
                self.assertEqual(report['current_count'], 1)
                self.assertEqual({row['id'] for row in report['results']}, {'old', 'new'})
                self.assertEqual([row['id'] for row in report['results'] if row['current_eligible']], ['new'])
                self.assertFalse(next(row for row in report['results'] if row['id'] == 'old')['current_eligible'])

    def test_explicit_lifecycle_kind_is_audit_evidence_never_current(self):
        self.lifecycle_fixture()
        for indexed in (False, True):
            if indexed:
                kb.init_knowledge_base(self.root)
            for kind, event_id in (('supersession', 'edge'), ('candidate-resolution', 'review')):
                with self.subTest(indexed=indexed, kind=kind):
                    report = self.search('Atlas', kind=kind)
                    self.assertEqual([row['id'] for row in report['results']], [event_id])
                    self.assertEqual(report['current_count'], 0)
                    self.assertEqual(report['conclusion'], 'lifecycle-audit-evidence')
                    self.assertEqual(report['results'][0]['effective_status'], 'lifecycle-event')
                    self.assertFalse(report['results'][0]['current_eligible'])

    def test_existing_raw_history_is_excluded_without_opt_in_for_new_config(self):
        import ingest_history
        self.event('curated', 'marker curated fact')
        kb.init_knowledge_base(self.root)
        connection = ingest_history.initialize_database(self.root / '.kb-data/history.sqlite')
        try:
            connection.execute("""INSERT INTO source_events(event_hash,source_device_id,source,source_path,source_offset,source_line,thread_id,event_id,role,event_type,event_timestamp,cwd,content,raw_json,ingested_at)
                VALUES ('fictional-history','device-a','codex','fictional.jsonl',0,1,'thread-atlas','event-a','user','message','2026-01-01T00:00:00Z','project:atlas','marker raw history','{}','2026-01-01T00:00:00Z')""")
            connection.commit()
        finally:
            connection.close()
        self.config['retrieval']['include_raw_history'] = False
        self.write_config()
        excluded = self.search('marker')
        self.assertFalse(excluded['raw_history_included'])
        self.assertEqual([r['id'] for r in excluded['results']], ['curated'])
        self.config['retrieval']['include_raw_history'] = True
        self.write_config()
        included = self.search('marker')
        self.assertTrue(any(r['result_type'] == 'source-history' for r in included['results']))
        del self.config['retrieval']['include_raw_history']
        self.write_config()
        legacy = self.search('marker')
        self.assertTrue(any(r['result_type'] == 'source-history' for r in legacy['results']))

    def test_copied_indexes_resolve_only_current_root_without_rebuilding(self):
        import sqlite3
        self.event('portable', 'moving marker')
        kb.init_knowledge_base(self.root)
        kb.find_events_by_id(self.root, 'portable')  # Populate hot locator too.
        with tempfile.TemporaryDirectory(prefix='portable-destination-') as tmp:
            destination = Path(tmp) / 'new mounted path'
            shutil.copytree(self.root, destination)
            database = sqlite3.connect(destination / '.kb-data/knowledge.sqlite')
            try:
                old_ref = database.execute('SELECT ledger_ref FROM history_events WHERE event_id=?', ('portable',)).fetchone()[0]
            finally:
                database.close()
            self.assertTrue(old_ref.startswith(str(self.root.resolve())))
            self.event('portable', 'wrong old-root content')
            args = kb.build_parser().parse_args(['--root',str(destination),'search','moving','--json'])
            with mock.patch.object(kb, 'rebuild_index', side_effect=AssertionError('must not rebuild')):
                report = kb.run(args)
            self.assertEqual(report['results'][0]['statement'], 'moving marker')
            located = kb.find_events_by_id(destination, 'portable')[0]
            self.assertTrue(located['ledger_ref'].startswith(str(destination.resolve())))

    def test_windows_index_reference_is_reanchored_and_identity_checked(self):
        raw = self.event('portable', 'moving marker')
        fingerprint = kb.event_fingerprint(kb.normalize_event(raw))
        event = kb.load_event_reference(r'C:\old-drive\knowledge\ledger\portable.json', self.root, 'portable', fingerprint)
        self.assertEqual(event['statement'], 'moving marker')
        with self.assertRaises(kb.ConflictError):
            kb.load_event_reference(r'C:\old-drive\knowledge\ledger\portable.json', self.root, 'other-id', fingerprint)
        with self.assertRaises(kb.ConflictError):
            kb.load_event_reference(r'C:\old-drive\knowledge\ledger\portable.json', self.root, 'portable', 'wrong-fingerprint')

    def test_index_reanchor_refuses_traversal_and_symlink_outside_root(self):
        raw = self.event('portable', 'moving marker')
        fingerprint = kb.event_fingerprint(kb.normalize_event(raw))
        for reference in ('/old/ledger/../ledger/portable.json', '/outside/arbitrary.json'):
            with self.assertRaises(kb.KBError):
                kb.load_event_reference(reference, self.root, 'portable', fingerprint)
        with tempfile.TemporaryDirectory(prefix='outside-ledger-') as tmp:
            target = Path(tmp) / 'record.json'
            target.write_text(json.dumps(raw))
            (self.root / 'ledger/escape.json').symlink_to(target)
            with self.assertRaises(kb.KBError):
                kb.load_event_reference('/old/ledger/escape.json', self.root, 'portable', fingerprint)

    def test_expired_and_future_and_disputed_never_current(self):
        self.event('expiry', 'expiry marker', expires_at='2020-01-01T00:00:00Z')
        self.event('future', 'future marker', valid_from='2999-01-01T00:00:00Z')
        self.event('dispute', 'dispute marker', status='disputed')
        kb.init_knowledge_base(self.root)
        report = self.search('marker')
        self.assertEqual(report['current_count'], 0)
        self.assertEqual(report['conclusion'], 'current-value-unconfirmed')
        self.assertEqual({r['effective_status'] for r in report['results']}, {'expired','not-yet-effective','disputed'})

    def test_source_time_outranks_import_time_and_cold_offline_has_same_order(self):
        self.event('current', 'region', source_time='2026-01-01T00:00:00Z')
        self.event('old', 'region', source_time='2020-01-01T00:00:00Z', created_at='2026-10-20T00:00:00Z')
        kb.init_knowledge_base(self.root)
        self.assertEqual(self.search('region')['results'][0]['id'], 'current')
        shutil.rmtree(self.root / '.kb-data')
        (self.root / '.kb-data').symlink_to(self.root / 'unavailable-data')
        self.assertEqual(self.search('region')['results'][0]['id'], 'current')
        self.assertFalse((self.root / 'unavailable-data').exists())

    def test_shared_fact_key_reveals_conflicting_sibling_not_matching_query(self):
        self.event('east', 'zone is east', fact_key='deployment-region')
        self.event('west', 'zone is west', fact_key='deployment-region')
        self.event('unrelated', 'deployment-region maintenance note', fact_key='different-property')
        self.event('other-project', 'deployment-region elsewhere', fact_key='deployment-region', scope='project:beacon')
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                if indexed:
                    kb.init_knowledge_base(self.root)
                report = self.search('east')
                self.assertEqual({r['id'] for r in report['results']}, {'east','west'})
                self.assertEqual(report['current_count'], 0)
                self.assertTrue(all(r['conflicts_with'] for r in report['results']))

    def test_hot_ledger_short_punctuation_queries_are_literal(self):
        for marker, event_id in (('%', 'percent'), ('_', 'underscore'), ('\\', 'slash')):
            self.event(event_id, 'Atlas literal ' + marker + ' marker')
        self.event('plain', 'Atlas plain text marker')
        self.event('other-percent', 'Other project uses %', scope='project:beacon')
        for marker, event_id in (('%', 'percent'), ('_', 'underscore'), ('\\', 'slash')):
            with self.subTest(marker=marker):
                report = self.search(marker)
                self.assertEqual([row['id'] for row in report['results']], [event_id])
        self.assertEqual(self.search('@')['count'], 0)
        self.assertEqual([row['id'] for row in self.search('%', scope='project:beacon')['results']], ['other-percent'])
        self.assertFalse((self.root / '.kb-data').exists())

    def test_old_hot_locator_rebuilds_to_discover_fact_key_siblings(self):
        self.event('east', 'zone is east', fact_key='deployment-region')
        self.event('west', 'zone is west', fact_key='deployment-region')
        self.search('east')
        connection = sqlite3.connect(kb.hot_locator_path(self.root))
        try:
            connection.execute("UPDATE locator_meta SET value='2' WHERE key='version'")
            connection.execute("UPDATE locator_events SET searchable=replace(searchable, 'deployment-region', '')")
            connection.execute("UPDATE locator_event_fts SET searchable=replace(searchable, 'deployment-region', '')")
            connection.commit()
        finally:
            connection.close()
        report = self.search('east')
        self.assertEqual({row['id'] for row in report['results']}, {'east', 'west'})
        self.assertEqual(report['current_count'], 0)
        self.assertFalse((self.root / '.kb-data').exists())

    def test_busy_refresh_preserves_aliases_scope_siblings_and_current_count(self):
        self.event('east', '发布计划 east', fact_key='deployment-region')
        self.event('west', '发布计划 west', fact_key='deployment-region')
        self.event('other', '发布计划 east', fact_key='deployment-region', scope='project:beacon')
        kb.init_knowledge_base(self.root)
        with workspace_lock.acquire(self.root):
            report = retrieve.retrieve(self.root, 'release east', scope='星图', offline=True)
        self.assertEqual({row['id'] for row in report['results']}, {'east', 'west'})
        self.assertEqual(report['current_count'], 0)
        self.assertTrue(all(not row['current_eligible'] for row in report['results']))
        self.assertEqual(report['conclusion'], 'local-index-incomplete')
        self.assertTrue(report['index_refresh_failed'])
        self.assertGreater(report['alias_queries_attempted'], 1)

    def test_read_only_projection_fallback_keeps_exact_values_and_candidate_status(self):
        self.event('exact', 'Atlas exact configuration', value='first\r\nsecond\r\n')
        self.event('guess', 'Atlas inferred configuration', status='candidate', authority='agent-inference')
        kb.init_knowledge_base(self.root)
        with mock.patch.object(kb, 'update_index', side_effect=sqlite3.OperationalError('attempt to write a readonly database')):
            report = retrieve.retrieve(self.root, 'Atlas', scope='project:atlas', include_values=True, offline=True)
        self.assertEqual(report['current_count'], 1)
        self.assertEqual(next(row for row in report['results'] if row['id']=='exact')['value'], 'first\r\nsecond\r\n')
        self.assertFalse(next(row for row in report['results'] if row['id']=='guess')['current_eligible'])
        self.assertEqual(report['freshness']['index_status'], 'refresh-failed:OperationalError')

    def test_busy_empty_result_remains_incomplete_not_verified_absence(self):
        kb.init_knowledge_base(self.root)
        with workspace_lock.acquire(self.root):
            report = retrieve.retrieve(self.root, 'missing fact', offline=True)
        self.assertEqual(report['count'], 0)
        self.assertEqual(report['current_count'], 0)
        self.assertEqual(report['conclusion'], 'local-index-incomplete')

    def test_fallback_keeps_dispatcher_query_limit_validation(self):
        kb.init_knowledge_base(self.root)
        with workspace_lock.acquire(self.root):
            for limit in (0, 1001):
                with self.subTest(limit=limit), self.assertRaisesRegex(kb.KBError, '--limit must be between'):
                    retrieve.retrieve(self.root, 'marker', limit=limit, offline=True)

    def test_malformed_source_and_invalid_alias_are_not_hidden_by_fallback(self):
        kb.init_knowledge_base(self.root)
        broken = self.root / 'ledger/broken.json'
        broken.write_text('{bad-json')
        with self.assertRaises(kb.KBError):
            retrieve.retrieve(self.root, 'unknown', offline=True)
        broken.unlink()
        self.config['retrieval']['scope_aliases']['project:beacon'] = ['星图']
        self.write_config()
        with workspace_lock.acquire(self.root), self.assertRaisesRegex(kb.KBError, 'ambiguous scope alias'):
            retrieve.retrieve(self.root, 'unknown', scope='星图', offline=True)

    def test_invalid_new_source_after_sync_does_not_return_pre_sync_evidence(self):
        self.event('local', 'marker')
        kb.init_knowledge_base(self.root)
        self.config['hot_sync'] = {'expected_repository': 'fiction/example'}
        self.write_config()
        def sync(*args, **kwargs):
            (self.root / 'ledger/broken.json').write_text('{bad-json')
            return {'ok':True}
        callback = mock.Mock(side_effect=sync)
        with self.assertRaises(kb.KBError):
            retrieve.retrieve(self.root, 'marker', sync_fn=callback)
        callback.assert_called_once()

    def test_foreground_tracks_canonical_add_edit_move_delete_and_recovers_corrupt_index(self):
        kb.init_knowledge_base(self.root)
        path = self.root / 'projects/atlas.json'
        path.parent.mkdir(exist_ok=True)
        record = {'id':'canonical-atlas', 'kind':'fact', 'scope':'project:atlas',
                  'statement':'Atlas project is cyan', 'authority':'user-stated', 'status':'active'}
        path.write_text(json.dumps(record))
        self.assertEqual(retrieve.retrieve(self.root, 'cyan', scope='project:atlas', offline=True)['count'], 1)
        record.update(statement='Atlas project is amber', scope='project:beacon')
        path.write_text(json.dumps(record))
        self.assertEqual(retrieve.retrieve(self.root, 'cyan', scope='project:atlas', offline=True)['count'], 0)
        self.assertEqual(retrieve.retrieve(self.root, 'amber', scope='project:atlas', offline=True)['count'], 0)
        moved = path.with_name('beacon.json')
        path.rename(moved)
        report = retrieve.retrieve(self.root, 'amber', scope='project:beacon', offline=True)
        self.assertEqual(report['results'][0]['path'], 'projects/beacon.json')
        (self.root / '.kb-data/knowledge.sqlite').write_bytes(b'not a SQLite database')
        recovered = retrieve.retrieve(self.root, 'amber', scope='project:beacon', offline=True)
        self.assertEqual(recovered['current_count'], 1)
        self.assertTrue(list((self.root / '.kb-data/reports').glob('knowledge-corrupt-*')))
        moved.unlink()
        self.assertEqual(retrieve.retrieve(self.root, 'amber', scope='project:beacon', offline=True)['count'], 0)

    def test_scope_alias_does_not_include_other_projects(self):
        self.event('atlas', 'release')
        self.event('beacon', 'release', scope='project:beacon')
        kb.init_knowledge_base(self.root)
        self.assertEqual([r['id'] for r in self.search('发布计划', scope='星图')['results']], ['atlas'])

    def test_capture_metadata_is_immutable_and_materialized(self):
        argv = ['--root',str(self.root),'capture','--id','resource-example','--kind','resource','--scope','project:atlas','--statement','location marker','--source','synthetic:test','--authority','user-stated','--observed-at','2020-01-01T00:00:00Z','--expires-at','2021-01-01T00:00:00Z','--json']
        kb.run(kb.build_parser().parse_args(argv))
        self.assertEqual(self.search('marker')['current_count'], 0)
        args = kb.build_parser().parse_args(argv)
        args.expires_at = '2022-01-01T00:00:00Z'
        with self.assertRaises(kb.ConflictError):
            kb.run(args)

    def test_missing_index_is_readable_from_tool_root_schema(self):
        self.event('entry','marker')
        self.assertFalse((self.root/'schema').exists())
        kb.init_knowledge_base(self.root)
        self.assertEqual(self.search('marker')['count'], 1)

    def test_new_event_after_index_is_found_by_foreground_retrieve(self):
        kb.init_knowledge_base(self.root)
        self.event('new', 'later marker')
        report = retrieve.retrieve(self.root, 'later', offline=True)
        self.assertEqual(report['results'][0]['id'], 'new')
        self.assertEqual(report['freshness']['sync_attempts'], 0)

    def test_sync_failure_preserves_local_results_and_does_not_retry(self):
        self.event('local', 'marker')
        self.config['hot_sync'] = {'expected_repository': 'fiction/example'}
        self.write_config()
        callback = mock.Mock(return_value={'ok':False,'status':'fetch-failed'})
        report = retrieve.retrieve(self.root, 'marker', sync_fn=callback)
        callback.assert_called_once()
        self.assertEqual(report['results'][0]['id'], 'local')
        self.assertEqual(report['freshness']['sync_attempts'], 1)
        self.assertEqual(report['freshness']['status'], 'failed:fetch-failed')

    def test_successful_sync_retries_once_and_finds_new_evidence(self):
        self.config['hot_sync'] = {'expected_repository': 'fiction/example'}
        self.write_config()
        def sync(*args, **kwargs):
            self.event('remote', 'remote marker')
            return {'ok':True}
        callback = mock.Mock(side_effect=sync)
        report = retrieve.retrieve(self.root, 'remote', sync_fn=callback)
        callback.assert_called_once()
        self.assertEqual(report['results'][0]['id'], 'remote')
        self.assertEqual(report['freshness']['status'], 'verified')

    def test_divergent_corrections_do_not_choose_last_writer(self):
        self.event('original', 'port original')
        self.event('choice-a', 'port A')
        self.event('choice-b', 'port B')
        self.event('edge-a','correction',kind='supersession',old_id='original',new_id='choice-a')
        self.event('edge-b','correction',kind='supersession',old_id='original',new_id='choice-b',created_at='2026-10-02T00:00:00Z')
        with self.assertRaisesRegex(kb.ConflictError, 'ambiguous lifecycle decisions'):
            kb.init_knowledge_base(self.root)
        self.assertTrue((self.root/'ledger/edge-a.json').is_file())
        self.assertTrue((self.root/'ledger/edge-b.json').is_file())

    def test_future_manifest_blocks_writes_without_changing_ledger(self):
        (self.root / 'MANIFEST.json').write_text('{"schema_version":"999"}')
        args = kb.build_parser().parse_args(['--root',str(self.root),'capture','--id','future','--kind','fact','--scope','global','--statement','marker','--source','synthetic:test','--authority','user-stated'])
        with self.assertRaisesRegex(kb.KBError, 'unsupported knowledge schema'):
            kb.run(args)
        self.assertEqual(list((self.root/'ledger').iterdir()), [])

    def test_missing_result_is_not_universal_absence(self):
        report = retrieve.retrieve(self.root, 'unknown', offline=True)
        self.assertEqual(report['conclusion'], 'not-found-in-local-snapshot')

if __name__ == '__main__':
    unittest.main()
