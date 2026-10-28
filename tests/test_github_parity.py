"""Compare real local projections and the remote adapter on fictional ledger data."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import kb

NODE = """
import fs from 'node:fs';
const {GithubKnowledge}=await import(process.argv[1]);
const {records,query,scope}=JSON.parse(fs.readFileSync(0,'utf8'));
const connector={supportsPinnedRef:true,
  repository:async()=>({full_name:'fictional/knowledge',private:true}),
  head:async()=> 'fictional-commit',
  listFiles:async()=>({complete:true,paths:['ledger/events.json']}),
  read:async()=>({content:JSON.stringify(records)})};
const result=await new GithubKnowledge(connector,'fictional/knowledge').query(query,{scope,includeHistory:true});
console.log(JSON.stringify(result));
"""


def event(identifier, statement='marker', **extra):
    record = dict(schema_version='1', id=identifier, statement=statement,
                  kind='fact', scope='project:atlas', source='fictional:user',
                  authority='user-stated', status='active', source_device_id='fixture',
                  created_at='2026-01-01T00:00:00Z')
    record.update(extra)
    return record


def resolution(identifier, target, timestamp='2026-02-01T00:00:00.000001Z'):
    return event(identifier, 'review marker', kind='candidate-resolution',
                 candidate_id='candidate', resolution=target, created_at=timestamp)


@unittest.skipUnless(shutil.which('node'), 'Node is required for the optional cross-adapter contract test')
class GithubProjectionParity(unittest.TestCase):
    def remote(self, records, query='marker', scope='project:atlas'):
        result = subprocess.run(['node', '--input-type=module', '-e', NODE,
                                 (ROOT / 'adapters/github.mjs').as_uri()],
                                input=json.dumps(dict(records=records, query=query, scope=scope)),
                                text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def local(self, records, query, scope, indexed):
        # TemporaryDirectory removes every fixture/index/Git object on all exits.
        with tempfile.TemporaryDirectory(prefix='knowledge-github-parity-') as temporary:
            root = Path(temporary)
            (root / 'ledger').mkdir()
            (root / 'config').mkdir()
            (root / 'config/kb.json').write_text('{"retrieval":{"include_raw_history":false}}')
            (root / '.gitignore').write_text('.kb-data/\ngenerated/\n')
            (root / 'ledger/events.json').write_text(json.dumps(records))
            subprocess.run(['git', 'init', '--quiet', str(root)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Parity Tests',
                            '-c', 'user.email=fixture@example.test', 'commit', '--quiet',
                            '--allow-empty', '-m', 'Fictional fixture'], check=True, capture_output=True)
            if indexed:
                kb.init_knowledge_base(root)
            args = kb.build_parser().parse_args(['--root', str(root), 'search', query,
                                                 '--scope', scope, '--json'])
            return kb.run(args)

    def assert_parity(self, records, expected, query='marker', scope='project:atlas'):
        remote = self.remote(records, query, scope)
        self.assertTrue(remote['complete'], remote)
        state = lambda report: {row['id']: (row['effective_status'], row['current_eligible'])
                                for row in report['results']}
        self.assertEqual(state(remote), expected)
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                local = self.local(records, query, scope, indexed)
                self.assertEqual(state(local), expected)
                self.assertEqual(local['current_count'], remote['current_count'])

    def test_accepted_and_rejected_candidate_are_not_their_review_events(self):
        for decision in ('accepted', 'rejected'):
            with self.subTest(decision=decision):
                self.assert_parity([event('candidate', status='candidate', authority='agent-inference'),
                                    resolution('review', decision)],
                                   {'candidate': (decision, decision == 'accepted')})

    def test_candidate_ids_can_equal_their_resolution_names(self):
        for decision in ('accepted', 'rejected'):
            with self.subTest(candidate_id=decision):
                records = [event(decision, status='candidate', authority='agent-inference'),
                           dict(resolution('review', decision), candidate_id=decision)]
                self.assert_parity(records, {decision: (decision, decision == 'accepted')})

    def test_review_order_preserves_microseconds_and_timezone_equivalence(self):
        candidate = event('candidate', status='candidate', authority='agent-inference')
        early = resolution('z-early', 'accepted', '2026-02-01T08:00:00.000001+08:00')
        late = resolution('a-late', 'rejected', '2026-02-01T00:00:00.000002Z')
        for rows in ([early, late], [late, early]):
            self.assert_parity([candidate, *rows], {'candidate': ('rejected', False)})

    def test_temporal_and_explicit_conflict_states(self):
        records = [event('future', valid_from='2999-01-01'),
                   event('expired', expires_at='2000-01-01'),
                   event('disputed', conflicts_with=['not-yet-shared']),
                   event('active', authority='agent-verified')]
        self.assert_parity(records, {'future': ('not-yet-effective', False),
                                    'expired': ('expired', False),
                                    'disputed': ('disputed', False), 'active': ('active', True)})

    def test_fact_key_siblings_are_scoped_and_visible_without_lexical_match(self):
        records = [event('east', 'region east', fact_key='deployment-region'),
                   event('west', 'region west', fact_key='deployment-region'),
                   event('another', 'region east', fact_key='deployment-region', scope='project:other')]
        self.assert_parity(records, {'east': ('disputed', False), 'west': ('disputed', False)}, query='east')
        self.assert_parity(records, {'another': ('active', True)}, query='east', scope='project:other')

    def test_accepted_candidate_and_active_fact_with_same_key_are_disputed(self):
        records = [event('candidate', 'marker uses cobalt', status='candidate', authority='agent-inference', fact_key='theme'),
                   resolution('review', 'accepted'), event('active', 'marker uses coral', fact_key='theme')]
        self.assert_parity(records, {'candidate': ('disputed', False), 'active': ('disputed', False)}, query='cobalt')

    def test_two_accepted_candidates_with_same_key_are_disputed(self):
        records = [event('candidate', 'marker uses cobalt', status='candidate', authority='agent-inference', fact_key='theme'),
                   event('second', 'marker uses coral', status='candidate', authority='agent-inference', fact_key='theme'),
                   resolution('review', 'accepted'), dict(resolution('second-review', 'accepted'), candidate_id='second')]
        self.assert_parity(records, {'candidate': ('disputed', False), 'second': ('disputed', False)})

    def test_accepted_candidate_conflicts_do_not_cross_scope(self):
        records = [event('candidate', 'marker uses cobalt', status='candidate', authority='agent-inference', fact_key='theme'),
                   resolution('review', 'accepted'), event('foreign', 'marker uses coral', fact_key='theme', scope='project:other')]
        self.assert_parity(records, {'candidate': ('accepted', True)})
        self.assert_parity(records, {'foreign': ('active', True)}, scope='project:other')

    def test_rejected_expired_future_and_superseded_candidates_do_not_create_conflicts(self):
        records = [event('active', fact_key='theme')]
        expected = {'active': ('active', True)}
        for identifier, decision, extra, status in (
                ('rejected', 'rejected', {}, 'rejected'),
                ('expired', 'accepted', {'expires_at': '2000-01-01'}, 'expired'),
                ('future', 'accepted', {'valid_from': '2999-01-01'}, 'not-yet-effective'),
                ('superseded', 'accepted', {}, 'superseded')):
            records += [event(identifier, status='candidate', authority='agent-inference', fact_key='theme', **extra),
                        dict(resolution('review-' + identifier, decision), candidate_id=identifier)]
            expected[identifier] = (status, False)
        records.append(event('edge', 'audit marker', kind='supersession', old_id='superseded', new_id='active', fact_key='theme'))
        self.assert_parity(records, expected)

    def test_supersession_chain_has_one_current_fact(self):
        records = [event('a'), event('b'), event('c'),
                   event('ab', 'audit', kind='supersession', old_id='a', new_id='b'),
                   event('bc', 'audit', kind='supersession', old_id='b', new_id='c')]
        self.assert_parity(records, {'a': ('superseded', False), 'b': ('superseded', False),
                                    'c': ('active', True)})

    def test_contradictory_reviews_and_cycles_fail_without_current_evidence(self):
        candidate = event('candidate', status='candidate', authority='agent-inference')
        cases = [([candidate, resolution('yes', 'accepted'), resolution('no', 'rejected')], 'lifecycle-conflict'),
                 ([event('a'), event('b'), event('ab', kind='supersession', old_id='a', new_id='b'),
                   event('ba', kind='supersession', old_id='b', new_id='a')], 'lifecycle-cycle')]
        for records, reason in cases:
            with self.subTest(reason=reason):
                remote = self.remote(records)
                self.assertFalse(remote['complete'])
                self.assertEqual(remote['reason'], reason)
                self.assertEqual(remote['current_count'], 0)
                self.assertFalse(remote['absence_confirmed'])
                for indexed in (False, True):
                    with self.assertRaises(kb.ConflictError):
                        self.local(records, 'marker', 'project:atlas', indexed)


if __name__ == '__main__':
    unittest.main()
