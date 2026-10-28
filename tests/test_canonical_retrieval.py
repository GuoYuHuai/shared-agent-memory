"""Indexed and direct retrieval must agree on ordinary Markdown queries."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import kb


class CanonicalRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='canonical-retrieval-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ('config', 'ledger', 'projects'):
            (self.root / directory).mkdir()
        (self.root / 'config/kb.json').write_text(json.dumps({
            'retrieval': {'include_raw_history': False},
        }), encoding='utf-8')
        (self.root / '.gitignore').write_text('.kb-data/\ngenerated/\n', encoding='utf-8')
        for command in (
            ['init', '-b', 'main'], ['add', '.'],
            ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
             '-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture'],
        ):
            subprocess.run(['git', *command], cwd=self.root, check=True, capture_output=True)

    def note(self, name, title, content, scope='project:atlas', **metadata):
        fields = {'title': title, 'scope': scope, 'authority': 'verified', **metadata}
        path = self.root / 'projects' / (name + '.md')
        path.write_text('---\n' + ''.join(f'{key}: {value}\n' for key, value in fields.items())
                        + '---\n' + content, encoding='utf-8')
        return 'projects/' + path.name

    def event(self, event_id, statement):
        event = {'schema_version': '1', 'id': event_id, 'statement': statement,
                 'created_at': '2026-10-01T00:00:00Z', 'kind': 'fact',
                 'scope': 'project:atlas', 'source': 'synthetic:test',
                 'authority': 'verified', 'status': 'active', 'source_device_id': 'device-a'}
        (self.root / 'ledger' / (event_id + '.json')).write_text(
            json.dumps(event), encoding='utf-8')

    def search(self, query, *, hot=False, limit=12, scope='project:atlas'):
        args = kb.build_parser().parse_args([
            '--root', str(self.root), 'search', query, '--limit', str(limit), '--json',
        ])
        args.scope = scope
        return kb.search(self.root, args, force_hot=hot)

    def test_exact_title_survives_candidate_limit_and_scope_filter(self):
        target = self.note('zz-target', 'API Documentation',
                           'GET /products returns the catalogue.\n' * 300)
        for index in range(80):
            self.note(f'meeting-{index}', f'Meeting {index}', 'Update api documentation.\n')
            self.note(f'foreign-{index}', 'API Documentation', 'Foreign project.\n',
                      scope='project:other')
        kb.init_knowledge_base(self.root)
        for hot in (False, True):
            with self.subTest(hot=hot):
                report = self.search('api documentation', hot=hot)
                self.assertEqual(report['retrieval_mode'], 'hot-only' if hot else 'indexed')
                self.assertEqual(report['results'][0]['id'], target)
                self.assertTrue(all(item['scope'] == 'project:atlas' for item in report['results']))
                self.assertEqual(report['count'], 12)

    def test_short_exact_title_survives_candidate_limit(self):
        target = self.note('zz-target', '发票', 'Records are retained.\n' * 100)
        for index in range(70):
            self.note(f'first-{index}', f'Notes {index}', '保存发票。\n')
        kb.init_knowledge_base(self.root)
        self.assertEqual(self.search('发票')['results'][0]['id'], target)

    def test_exact_title_does_not_promote_rejected_knowledge(self):
        self.note('old', 'API Documentation', 'Retired instructions.\n', status='rejected')
        current = self.note('current', 'Current guide', 'Current API Documentation.\n')
        kb.init_knowledge_base(self.root)
        for hot in (False, True):
            with self.subTest(hot=hot):
                report = self.search('API Documentation', hot=hot)
                self.assertEqual(report['results'][0]['id'], current)
                self.assertEqual(report['current_count'], 1)
                old = next(item for item in report['results'] if item['id'] == 'projects/old.md')
                self.assertFalse(old['current_eligible'])

    def test_indexed_like_metacharacters_are_literal_for_notes_and_events(self):
        cases = [('percent', '%'), ('underscore', '_'), ('backslash', '\\')]
        self.note('ordinary', 'Ordinary', 'invoice ordinary wording.\n')
        self.event('ordinary-event', 'invoice ordinary wording')
        for name, character in cases:
            self.note(name, name, f'invoice literal {character} marker.\n')
            self.event(name + '-event', f'invoice literal {character} marker')
            self.note('foreign-' + name, name, f'invoice literal {character} marker.\n',
                      scope='project:other')
        kb.init_knowledge_base(self.root)
        for name, character in cases:
            expected = {'projects/' + name + '.md', name + '-event'}
            for query in (character, 'invoice ' + character):
                with self.subTest(query=query):
                    report = self.search(query)
                    self.assertEqual(report['retrieval_mode'], 'indexed')
                    self.assertEqual({item['id'] for item in report['results']}, expected)
        for query in ('%', '_', '\\'):
            self.assertEqual(self.search(query, scope='project:absent')['count'], 0)

    def test_literal_symbol_matching_agrees_for_markdown(self):
        self.note('ordinary', 'Ordinary', 'invoice ordinary wording.\n')
        cases = [('percent', '%'), ('underscore', '_'), ('backslash', '\\')]
        for name, character in cases:
            self.note(name, name, f'invoice literal {character} marker.\n')
        kb.init_knowledge_base(self.root)
        for name, character in cases:
            for query in (character, 'invoice ' + character):
                for hot in (False, True):
                    with self.subTest(query=query, hot=hot):
                        report = self.search(query, hot=hot)
                        self.assertEqual({item['id'] for item in report['results']},
                                         {'projects/' + name + '.md'})

    def test_short_query_excerpt_contains_match_at_end_of_long_note(self):
        target = self.note('long', 'Expense notes',
                           'Routine background. ' * 100 + '\n发票应保留三年。\n')
        kb.init_knowledge_base(self.root)
        for hot in (False, True):
            with self.subTest(hot=hot):
                report = self.search('发票', hot=hot)
                self.assertEqual(report['results'][0]['id'], target)
                snippet = report['results'][0]['snippet']
                self.assertIn('发票应保留三年', snippet)
                self.assertLessEqual(len(snippet), 480)


if __name__ == '__main__':
    unittest.main()
