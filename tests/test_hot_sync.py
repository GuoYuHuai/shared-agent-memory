"""First-use Git identity checks in actual isolated subprocess environments."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]


class HotSyncIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'knowledge'
        self.home = self.base / 'home'
        self.home.mkdir()
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_', 'GIT_CONFIG_')):
                self.env.pop(key)
        self.env.update({'HOME': str(self.home), 'USERPROFILE': str(self.home),
                         'XDG_CONFIG_HOME': str(self.home / '.config'),
                         'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': str(self.home / '.gitconfig'),
                         'AI_KNOWLEDGE_ROOT': str(self.root),
                         'AI_KNOWLEDGE_DEVICE_STATE': str(self.base / 'device.json'),
                         'PYTHONDONTWRITEBYTECODE': '1'})
        self.script('install.py', '--root', str(self.root), '--mode', 'local')

    def run_process(self, arguments):
        result = subprocess.run(arguments, capture_output=True, text=True,
                                cwd=str(self.root if self.root.exists() else self.base), env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def script(self, name, *args):
        return json.loads(self.run_process([sys.executable, str(TOOLS / 'scripts' / name), *args]))

    def git(self, *args):
        return self.run_process(['git', *args])

    def fact(self):
        (self.root / 'profile/preference.md').write_text('fictional preference: concise reports\n')

    def test_fresh_machine_fallback_commits_without_persisting_identity(self):
        self.assertFalse((self.home / '.gitconfig').exists())
        self.fact()
        before = (self.root / '.git/config').read_bytes()
        result = self.script('hot_sync.py')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'local-only')
        self.assertTrue(result['committed'])
        self.assertEqual(self.git('show', '-s', '--format=%an <%ae>', 'HEAD'), 'AI Knowledge <knowledge@localhost>')
        self.assertEqual((self.root / '.git/config').read_bytes(), before)
        self.assertFalse((self.home / '.gitconfig').exists())
        self.assertEqual(result['identity']['fallback_fields'], ['user.name', 'user.email'])

    def test_user_identity_and_signing_configuration_remain_unchanged(self):
        self.git('config', '--global', 'user.name', 'Existing Person')
        self.git('config', '--global', 'user.email', 'person@example.test')
        self.git('config', '--global', 'commit.gpgsign', 'true')
        self.fact()
        global_before = (self.home / '.gitconfig').read_bytes()
        local_before = (self.root / '.git/config').read_bytes()
        result = self.script('hot_sync.py')
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.git('show', '-s', '--format=%an <%ae>', 'HEAD'), 'Existing Person <person@example.test>')
        self.assertEqual(result['identity']['fallback_fields'], [])
        self.assertEqual((self.home / '.gitconfig').read_bytes(), global_before)
        self.assertEqual((self.root / '.git/config').read_bytes(), local_before)

    def test_fallback_preserves_unrelated_staged_changes(self):
        self.fact()
        unrelated = self.root / 'unrelated.txt'
        unrelated.write_text('staged user content')
        self.git('add', 'unrelated.txt')
        before = self.git('diff', '--cached')
        unrelated.write_text('newer unstaged user content')
        result = self.script('hot_sync.py')
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.git('diff', '--cached'), before)
        self.assertEqual(unrelated.read_text(), 'newer unstaged user content')
        self.assertEqual(self.git('show', '--pretty=format:', '--name-only', 'HEAD'), 'profile/preference.md')


if __name__ == '__main__':
    unittest.main()
