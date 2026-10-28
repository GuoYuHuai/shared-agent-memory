"""Git routing checks use real local configuration, never a live GitHub remote."""
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
import connect_sync
import git_sync
import install


class GitTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='knowledge-transport-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'knowledge'
        self.root.mkdir()
        self.config = self.base / 'global.gitconfig'
        self.config.write_text('')
        self.environment = mock.patch.dict(os.environ, {
            'GIT_CONFIG_GLOBAL': str(self.config), 'GIT_CONFIG_NOSYSTEM': '1',
            'AI_KNOWLEDGE_DEVICE_STATE': str(self.base / 'device.json'),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.command('init', '-q', '-b', 'main')
        self.url = 'https://github.com/example/knowledge.git'
        self.command('remote', 'add', 'origin', self.url)
        (self.root / 'config').mkdir()
        (self.root / 'config/kb.json').write_text(json.dumps({'hot_sync': {
            'expected_repository': 'example/knowledge', 'expected_branch': 'main',
            'allowed_ssh_hosts': ['github.com', 'github-knowledge'],
        }}))
        self.policy = git_sync.sync_policy(self.root)

    def command(self, *arguments, root=None):
        return subprocess.run(['git', *arguments], cwd=root or self.root,
                              env=git_sync.git_environment(), check=True,
                              capture_output=True, text=True).stdout.strip()

    def global_setting(self, key, value):
        self.command('config', '--file', str(self.config), '--add', key, value)

    def test_fetch_rewrite_is_rejected_before_any_network_or_privacy_probe(self):
        self.global_setting('url.' + str(self.base / 'other') + '.insteadOf', self.url)
        with mock.patch.object(git_sync, 'repository_privacy_status') as privacy, \
                mock.patch.object(git_sync, 'git', wraps=git_sync.git) as invoked:
            result = git_sync.sync('origin', pull=True, push=True, root=self.root)
        self.assertEqual(result['error'], 'origin-identity-mismatch')
        privacy.assert_not_called()
        self.assertFalse(any(call.args[0][0] in ('fetch', 'push', 'ls-remote') and '--get-url' not in call.args[0] for call in invoked.call_args_list))

    def test_push_only_rewrite_is_also_rejected(self):
        for destination in (str(self.base / 'other'), 'ext::anything'):
            with self.subTest(destination=destination):
                self.config.write_text('')
                self.global_setting('url.' + destination + '.pushInsteadOf', self.url)
                self.assertEqual(self.command('remote', 'get-url', 'origin'), self.url)
                self.assertFalse(git_sync.configured_origin_status(root=self.root)['ok'])

    def test_every_fetch_and_push_url_must_match(self):
        for key in ('remote.origin.url', 'remote.origin.pushurl'):
            with self.subTest(key=key):
                if key.endswith('pushurl'):
                    self.command('config', '--add', key, self.url)
                self.command('config', '--add', key, 'https://github.com/other/knowledge.git')
                self.assertFalse(git_sync.configured_origin_status(root=self.root)['ok'])
                self.command('config', '--unset-all', key)
                if key.endswith('.url'):
                    self.command('config', '--add', key, self.url)

    def test_repository_index_and_config_environment_cannot_redirect_commands(self):
        other = self.base / 'other'
        other.mkdir()
        self.command('init', '-q', str(other))
        original_index = self.base / 'foreign-index'
        original_index.write_bytes(b'preserve foreign index')
        injected = {'GIT_DIR': str(other / '.git'), 'GIT_WORK_TREE': str(other),
                    'GIT_COMMON_DIR': str(other / '.git'), 'GIT_INDEX_FILE': str(original_index),
                    'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'remote.origin.url',
                    'GIT_CONFIG_VALUE_0': 'https://github.com/other/repo.git',
                    'GIT_CONFIG_PARAMETERS': "'remote.origin.url=https://github.com/other/repo.git'",
                    'GIT_EXEC_PATH': str(other)}
        with mock.patch.dict(os.environ, injected):
            self.assertEqual(Path(git_sync.value(['rev-parse', '--show-toplevel'], root=self.root)), self.root)
            self.assertTrue(git_sync.configured_origin_status(root=self.root)['ok'])
            (self.root / 'note').write_text('fictional')
            self.assertEqual(git_sync.git(['add', 'note'], root=self.root).returncode, 0)
            for key in injected:
                self.assertNotIn(key, git_sync.git_environment())
        self.assertEqual(original_index.read_bytes(), b'preserve foreign index')
        self.assertFalse((other / '.git/index').exists())

    def test_normal_global_auth_proxy_and_reviewed_ssh_rewrite_are_retained(self):
        self.global_setting('credential.helper', 'fixture-helper')
        self.global_setting('http.proxy', 'http://localhost:9999')
        self.global_setting('url.git@github-knowledge:.insteadOf', 'https://github.com/')
        self.assertTrue(git_sync.configured_origin_status(root=self.root)['ok'])
        self.assertTrue(git_sync.literal_fetch_status(self.url, self.policy, self.root))
        self.assertEqual(git_sync.value(['config', '--get', 'credential.helper'], root=self.root), 'fixture-helper')
        self.assertEqual(git_sync.value(['config', '--get', 'http.proxy'], root=self.root), 'http://localhost:9999')
        self.assertEqual(git_sync.git_environment()['GIT_CONFIG_GLOBAL'], str(self.config))

    def test_install_does_not_inherit_foreign_repository_or_index(self):
        (self.root / 'unrelated').write_text('preserve unrelated working copy')
        self.command('add', 'unrelated')
        self.command('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                     '-c', 'commit.gpgsign=false', 'commit', '-qm', 'unrelated history')
        (self.root / 'staged').write_text('preserve staged work')
        self.command('add', 'staged')
        original_head = self.command('rev-parse', 'HEAD')
        original_index = (self.root / '.git/index').read_bytes()
        original_config = (self.root / '.git/config').read_bytes()
        target = self.base / 'new installation'
        with mock.patch.dict(os.environ, {
                'GIT_DIR': str(self.root / '.git'), 'GIT_WORK_TREE': str(self.root),
                'GIT_INDEX_FILE': str(self.root / '.git/index'),
                'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'remote.origin.url',
                'GIT_CONFIG_VALUE_0': 'https://github.com/other/repo.git'}):
            result = install.install(target, None, 'local')
        self.assertTrue(result['ok'])
        self.assertEqual(self.command('rev-parse', 'HEAD'), original_head)
        self.assertEqual((self.root / '.git/index').read_bytes(), original_index)
        self.assertEqual((self.root / '.git/config').read_bytes(), original_config)
        self.assertEqual(Path(self.command('rev-parse', '--show-toplevel', root=target)), target)
        self.assertEqual(self.command('log', '-1', '--format=%s', root=target), 'Initialize user knowledge configuration')
        self.assertTrue((target / '.ai-knowledge/install.json').is_file())

    def test_explicit_ssh_program_command_and_variant_are_preserved(self):
        settings = {'GIT_SSH': r'C:\Program Files\PuTTY\plink.exe',
                    'GIT_SSH_COMMAND': 'plink -batch -i "identity with spaces.ppk"',
                    'GIT_SSH_VARIANT': 'plink'}
        with mock.patch.dict(os.environ, settings):
            environment = git_sync.git_environment()
        for key, value in settings.items():
            self.assertEqual(environment[key], value)
        self.assertEqual(environment['GIT_ALLOW_PROTOCOL'], 'https:ssh')
        self.assertEqual(environment['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(environment['SSH_ASKPASS_REQUIRE'], 'never')

    def test_git_uses_configured_ssh_and_environment_override_without_interpretation(self):
        # Git executes this fictional SSH command locally. It writes its argv
        # and exits before any network contact; this is not Plink certification.
        program = self.base / 'fictional ssh.py'
        report = self.base / 'ssh-result.json'
        program.write_text('import json,os,sys\nfrom pathlib import Path\n'
                           'Path(os.environ["AK_SSH_REPORT"]).write_text(json.dumps({'
                           '"args":sys.argv[1:],"variant":os.getenv("GIT_SSH_VARIANT"),'
                           '"askpass":os.getenv("SSH_ASKPASS_REQUIRE")}))\n'
                           'raise SystemExit(23)\n', encoding='utf-8')
        command = '"' + Path(sys.executable).as_posix() + '" "' + program.as_posix() + '"'
        self.global_setting('core.sshCommand', command + ' "configured identity"')
        self.global_setting('ssh.variant', 'simple')
        inherited = {key: value for key, value in os.environ.items()
                     if key not in ('GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_SSH_VARIANT')}
        inherited['AK_SSH_REPORT'] = str(report)
        with mock.patch.dict(os.environ, inherited, clear=True):
            self.assertNotIn('GIT_SSH_COMMAND', git_sync.git_environment())
            result = git_sync.git(['ls-remote', 'git@github.com:example/knowledge.git'], root=self.root)
            self.assertNotEqual(result.returncode, 0)
            recorded = json.loads(report.read_text())
            self.assertEqual(recorded['args'][0], 'configured identity')
            self.assertEqual(recorded['askpass'], 'never')
            report.unlink()
            with mock.patch.dict(os.environ, {'GIT_SSH_COMMAND': command + ' "environment identity"',
                                             'GIT_SSH_VARIANT': 'plink'}):
                result = git_sync.git(['ls-remote', 'git@github.com:example/knowledge.git'], root=self.root)
            self.assertNotEqual(result.returncode, 0)
            recorded = json.loads(report.read_text())
            self.assertEqual(recorded['args'][0], 'environment identity')
            self.assertEqual(recorded['variant'], 'plink')

    def test_git_commands_have_no_stdin_terminal_and_keep_timeout(self):
        with mock.patch.object(git_sync.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            git_sync.git(['fetch', 'origin'], root=self.root, timeout=17)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['timeout'], 17)
        self.assertEqual(kwargs['start_new_session'], os.name != 'nt')
        self.assertEqual(kwargs['creationflags'], subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

    def test_file_and_ext_protocols_are_blocked_even_if_global_config_allows_them(self):
        self.global_setting('protocol.allow', 'always')
        remote = self.base / 'remote.git'
        self.command('init', '-q', '--bare', str(remote))
        marker = self.base / 'must-not-exist'
        with mock.patch.dict(os.environ, {'GIT_ALLOW_PROTOCOL': 'file:ext:https:ssh'}):
            for url in (str(remote), 'ext::touch ' + str(marker)):
                result = git_sync.git(['ls-remote', url], root=self.root)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('not allowed', result.stderr)
        self.assertFalse(marker.exists())

    def test_new_connection_checks_rewrites_before_contacting_remote(self):
        knowledge = self.base / 'new knowledge'
        install.install(knowledge, None, 'local')
        before = {name: (knowledge / name).read_bytes() for name in (
            'config/kb.json', '.ai-knowledge/install.json', '.git/config')}
        self.global_setting('url.' + str(self.base / 'other') + '.insteadOf', self.url)
        checker = mock.Mock()
        with mock.patch.object(git_sync, 'git', wraps=git_sync.git) as invoked:
            result = connect_sync.connect(knowledge, 'example/knowledge', private_checker=checker)
        self.assertEqual(result['reason'], 'origin-url-rewritten', result)
        checker.assert_not_called()
        self.assertEqual(before, {name: (knowledge / name).read_bytes() for name in before})
        self.assertFalse(any(call.args[0][0] in ('fetch', 'push', 'ls-remote') and '--get-url' not in call.args[0] for call in invoked.call_args_list))

    def test_routes_are_checked_again_immediately_before_network(self):
        self.assertTrue(git_sync.configured_origin_status(root=self.root)['ok'])
        self.command('config', 'remote.origin.pushurl', 'https://github.com/other/repo.git')
        with mock.patch.object(git_sync, 'git', wraps=git_sync.git) as invoked:
            with self.assertRaisesRegex(git_sync.SyncError, 'origin-identity-mismatch'):
                git_sync.network_git(['fetch', 'origin'], 'origin', self.root)
        self.assertFalse(any(call.args[0][0] == 'fetch' for call in invoked.call_args_list))

    def test_anonymous_probe_drops_injected_config_and_allows_only_https(self):
        with mock.patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'url.x.insteadOf',
                                         'GIT_CONFIG_VALUE_0': 'https://github.com/', 'GIT_DIR': 'elsewhere'}):
            environment = git_sync.anonymous_git_probe_environment()
        self.assertEqual(environment['GIT_ALLOW_PROTOCOL'], 'https')
        self.assertEqual(environment['GIT_CONFIG_GLOBAL'], os.devnull)
        self.assertEqual(environment['GIT_CONFIG_NOSYSTEM'], '1')
        self.assertNotIn('GIT_CONFIG_COUNT', environment)
        self.assertNotIn('GIT_DIR', environment)

    def test_local_history_probe_rejects_a_rewritten_local_source(self):
        probe = self.base / 'probe.git'
        self.command('init', '-q', '--bare', str(probe))
        self.global_setting('url.' + str(self.base / 'other') + '.insteadOf', str(self.root))
        with self.assertRaisesRegex(git_sync.SyncError, 'local-history-url-rewritten'):
            git_sync.fetch_local_history(probe, self.root, '0' * 40)


if __name__ == '__main__':
    unittest.main()
