#!/usr/bin/env python3
"""Fixed synthetic retrieval evidence; creates and removes its own data root."""
import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def run(output=None, engine=None):
    spec = importlib.util.spec_from_file_location('evaluation_kb', engine or ROOT / 'scripts/kb.py')
    kb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kb)
    fixture = json.loads((ROOT / 'evals/retrieval_cases.json').read_text())
    outcomes = []
    with tempfile.TemporaryDirectory(prefix='knowledge-eval-') as tmp:
        root = Path(tmp)
        (root / 'schema').mkdir()
        shutil.copyfile(ROOT / 'schema/knowledge.sql', root / 'schema/knowledge.sql')
        (root / 'config').mkdir()
        (root / 'config/kb.json').write_text(json.dumps(fixture['config']))
        (root / 'ledger').mkdir()
        for event in fixture['events']:
            (root / 'ledger' / (event['id'] + '.json')).write_text(json.dumps(event, ensure_ascii=False))
        kb.init_knowledge_base(root)
        for case in fixture['cases']:
            args = kb.build_parser().parse_args(['--root', str(root), 'search', case['query'], '--scope', case['scope'], '--limit', '5', '--json'])
            start = time.monotonic()
            result = kb.run(args)
            current = [r for r in result['results'] if r.get('current_eligible', kb.lifecycle_status_rank(r) == 0)]
            ids = [r['id'] for r in current]
            expected = case['expected']
            ok = (bool(ids) and ids[0] == expected) if expected else not ids
            outcomes.append({'id': case['id'], 'pass': ok, 'expected_current_first': expected, 'actual_current_ids': ids, 'returned_ids': [r['id'] for r in result['results']], 'seconds': round(time.monotonic()-start, 6), 'context_characters': len(json.dumps(result['results'], ensure_ascii=False))})
    report = {'fixture': 'synthetic-v1', 'cases': len(outcomes), 'passed': sum(x['pass'] for x in outcomes), 'correct_current_first_rate': sum(x['pass'] for x in outcomes)/len(outcomes), 'model_usage': 'not-used', 'supplementary_user_questions': 'not-measured', 'scope': 'local deterministic retrieval, not cross-product or physical-device validation', 'results': outcomes}
    answered = [x for x in outcomes if x['expected_current_first']]
    report['expected_source_recall_at_3'] = sum(x['expected_current_first'] in x['returned_ids'][:3] for x in answered) / len(answered)
    report['expired_or_disputed_current_cases'] = sum(bool({'atlas-expired','atlas-disputed'} & set(x['actual_current_ids'])) for x in outcomes)
    report['no_evidence_but_current_answer_cases'] = sum(not x['expected_current_first'] and bool(x['actual_current_ids']) for x in outcomes)
    if output:
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    return report

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output')
    parser.add_argument('--engine', type=Path)
    args = parser.parse_args()
    report = run(args.output, args.engine)
    print(json.dumps({k:v for k,v in report.items() if k!='results'}, indent=2))
