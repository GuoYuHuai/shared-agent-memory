import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]


class LocalOnlyTests(unittest.TestCase):
    def test_capture_commit_and_retrieve_without_remote_or_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'local knowledge'
            env = dict(os.environ, AI_KNOWLEDGE_ROOT=str(root), PYTHONDONTWRITEBYTECODE='1')
            def run(script, *args):
                result = subprocess.run([sys.executable, str(TOOLS / 'scripts' / script), *args],
                                        text=True, capture_output=True, env=env)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                return json.loads(result.stdout)
            run('install.py', '--root', str(root))
            run('kb.py', '--root', str(root), 'capture', '--id', 'fictional-preference',
                '--kind', 'preference', '--scope', 'garden', '--statement', 'theme green',
                '--source', 'fictional:user', '--authority', 'user-stated', '--json')
            sync = run('hot_sync.py')
            self.assertEqual(sync['status'], 'local-only')
            self.assertTrue(sync['committed'])
            self.assertFalse(sync['remote_verified'])
            self.assertFalse(sync['sync']['pushed'])
            result = run('retrieve.py', '--root', str(root), 'theme', '--scope', 'garden')
            self.assertEqual(result['results'][0]['id'], 'fictional-preference')
            self.assertEqual(result['freshness']['sync_attempts'], 0)
            self.assertFalse((root / '.kb-data/history.sqlite').exists())
            self.assertEqual(subprocess.check_output(['git', '-C', str(root), 'remote'], text=True), '')


if __name__ == '__main__':
    unittest.main()
