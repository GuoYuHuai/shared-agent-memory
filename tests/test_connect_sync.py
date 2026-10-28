"""Real local Git contracts; no live GitHub privacy or network claim is made."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import connect_sync
import install


class ConnectSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / '知识 with spaces'
        self.remote = self.base / 'remote.git'
        # Keep all identity reads in this disposable fixture.
        self.environment = mock.patch.dict('os.environ', {
            'AI_KNOWLEDGE_DEVICE_STATE': str(self.base / 'device.json'),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        install.install(self.root, None, 'local')
        self.git('init', '--bare', '--initial-branch=main', str(self.remote))
        self.repository = 'example/knowledge'
        self.url = 'https://github.com/example/knowledge.git'
        self.private = mock.Mock(return_value={'ok': True, 'private': True,
            'repository': self.repository, 'source': 'local-test-fixture'})
        self.real_git = connect_sync.git_sync.git
        self.transport = mock.patch.object(connect_sync.git_sync, 'git', side_effect=self.local_transport)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def git(self, *args, cwd=None):
        result = subprocess.run(['git', *args], cwd=str(cwd or self.root),
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def local_transport(self, arguments, timeout=180, root=None):
        # Only change the destination of network-reading commands. Ref listing,
        # object fetch, ancestry decisions, commits and rollback use real Git.
        args = list(arguments)
        if args and args[0] in ('ls-remote', 'fetch') and '--get-url' not in args:
            args = [str(self.remote) if value in (self.url, 'git@github.com:example/knowledge.git')
                    else value for value in args]
        environment = connect_sync.git_sync.git_environment()
        environment['GIT_ALLOW_PROTOCOL'] = 'https:ssh:file'
        with mock.patch.object(connect_sync.git_sync, 'git_environment', return_value=environment):
            return self.real_git(args, timeout=timeout, root=root)

    def connect(self, **kwargs):
        return connect_sync.connect(self.root, self.repository, private_checker=self.private, **kwargs)

    def snapshot(self):
        paths = ['config/kb.json', '.ai-knowledge/install.json', '.git/config',
                 'POLICY.md', 'AGENTS.md', 'MANIFEST.json']
        return {path: (self.root / path).read_bytes() for path in paths}, self.git('rev-parse', 'HEAD')

    def seed_related(self):
        self.git('push', str(self.remote), 'HEAD:refs/heads/main')

    def test_empty_remote_connects_without_pushing_and_can_reinstall(self):
        before = self.snapshot()
        result = self.connect()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'ready-for-first-push')
        self.assertFalse(result['pushed'])
        self.assertFalse(result['synced'])
        self.assertEqual(self.git('ls-remote', '--refs', str(self.remote)), '')
        self.assertEqual(self.git('remote', 'get-url', 'origin'), self.url)
        self.assertEqual(self.git('show', '--format=', '--name-only', 'HEAD'), 'config/kb.json')
        config = json.loads((self.root / 'config/kb.json').read_text())
        marker = json.loads((self.root / '.ai-knowledge/install.json').read_text())
        self.assertTrue(config['hot_sync']['enabled'])
        self.assertEqual(config['hot_sync']['expected_repository'], self.repository)
        self.assertEqual(marker['repository'], self.repository)
        self.assertTrue(marker['private_verified'])
        self.assertEqual(marker['private_verification']['source'], 'local-test-fixture')
        for path in ('POLICY.md', 'AGENTS.md', 'MANIFEST.json'):
            self.assertEqual((self.root / path).read_bytes(), before[0][path])
        self.assertEqual(self.git('status', '--porcelain'), '')
        installed = install.install(self.root, None, 'local')
        self.assertEqual(installed['installation']['repository'], self.repository)

    def test_related_history_and_ssh_transport(self):
        self.seed_related()
        result = self.connect(remote_url='git@github.com:example/knowledge.git')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'ready-for-sync')
        self.assertEqual(result['history']['status'], 'related')
        self.assertEqual(self.git('remote', 'get-url', 'origin'), 'git@github.com:example/knowledge.git')
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'main', cwd=self.remote))

    def test_unrelated_remote_rejected_with_no_local_changes(self):
        unrelated = self.base / 'unrelated'
        unrelated.mkdir()
        self.git('init', '--initial-branch=main', cwd=unrelated)
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=test@example.test',
                 'commit', '--allow-empty', '-m', 'unrelated', cwd=unrelated)
        self.git('push', str(self.remote), 'main', cwd=unrelated)
        before = self.snapshot()
        result = self.connect()
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'unrelated-remote-history')
        self.assertIn('Clone', result['recovery'])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git('remote'), '')

    def test_nonempty_remote_missing_expected_branch_is_not_empty(self):
        self.git('push', str(self.remote), 'HEAD:refs/heads/other')
        before = self.snapshot()
        result = self.connect()
        self.assertEqual(result['reason'], 'remote-branch-missing')
        self.assertEqual(self.snapshot(), before)

    def test_unverified_public_missing_or_wrong_metadata_does_not_write(self):
        for metadata, reason in [
            ({'ok': False, 'private': None}, 'privacy-unverified'),
            ({'ok': True, 'private': False}, 'privacy-unverified'),
            ({'ok': True, 'private': True, 'repository': 'elsewhere/knowledge'}, 'metadata-identity-mismatch'),
        ]:
            with self.subTest(metadata=metadata):
                before = self.snapshot()
                self.private.return_value = metadata
                result = self.connect()
                self.assertEqual(result['reason'], reason)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.git('remote'), '')

    def test_dirty_config_rejected_before_remote_checks(self):
        path = self.root / 'config/kb.json'
        path.write_text(path.read_text() + '\n')
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged:
                    self.git('add', 'config/kb.json')
                before = self.snapshot()
                result = self.connect()
                self.assertEqual(result['reason'], 'dirty-sync-config')
                self.assertEqual(self.snapshot(), before)
                self.private.assert_not_called()

    def test_skip_worktree_cannot_hide_dirty_config(self):
        self.git('update-index', '--skip-worktree', 'config/kb.json')
        path = self.root / 'config/kb.json'
        path.write_text(path.read_text() + '\n')
        before = self.snapshot()
        result = self.connect()
        self.assertEqual(result['reason'], 'dirty-sync-config')
        self.assertEqual(self.snapshot(), before)
        self.private.assert_not_called()

    def test_unknown_schema_and_wrong_owner_are_refused(self):
        marker = self.root / '.ai-knowledge/install.json'
        original = json.loads(marker.read_text())
        for change, reason in [({'data_schema': 'future'}, 'unsupported-schema'),
                               ({'knowledge_root': '/different/path'}, 'installation-owner-mismatch')]:
            with self.subTest(change=change):
                marker.write_text(json.dumps({**original, **change}))
                before = self.snapshot()
                result = self.connect()
                self.assertEqual(result['reason'], reason)
                self.assertEqual(self.snapshot(), before)
                self.private.assert_not_called()

    def test_checker_error_does_not_echo_credentials_or_write(self):
        self.private.side_effect = RuntimeError('https://token:secret@github.com')
        before = self.snapshot()
        result = self.connect()
        self.assertEqual(result['reason'], 'privacy-unverified')
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(self.snapshot(), before)

    def test_arbitrary_directory_is_not_created_or_initialized(self):
        ordinary = self.base / 'ordinary'
        ordinary.mkdir()
        (ordinary / 'note.md').write_text('keep')
        result = connect_sync.connect(ordinary, self.repository, private_checker=self.private)
        self.assertEqual(result['reason'], 'invalid-installation')
        self.assertEqual([p.name for p in ordinary.iterdir()], ['note.md'])
        self.private.assert_not_called()

    def test_foreign_origin_and_existing_binding_are_preserved(self):
        self.git('remote', 'add', 'origin', 'https://github.com/other/knowledge.git')
        before = self.snapshot()
        result = self.connect()
        self.assertEqual(result['reason'], 'foreign-origin')
        self.assertEqual(self.snapshot(), before)
        self.private.assert_not_called()
        self.git('remote', 'remove', 'origin')
        self.assertTrue(self.connect()['ok'])
        before = self.snapshot()
        result = connect_sync.connect(self.root, 'other/knowledge', private_checker=self.private)
        self.assertEqual(result['reason'], 'already-bound-to-another-repository')
        self.assertEqual(self.snapshot(), before)

    def test_unrelated_staged_and_uncommitted_knowledge_tasks_preserved(self):
        staged = self.root / 'projects/staged.md'
        staged.write_text('staged fact')
        self.git('add', 'projects/staged.md')
        staged.write_text('newer unstaged fact')
        (self.root / 'profile/untracked.md').write_text('untracked fact')
        marker = self.root / '.ai-knowledge/install.json'
        receipt = json.loads(marker.read_text())
        receipt['tasks'] = ['keep-managed-task']
        receipt['custom'] = {'must': 'survive'}
        marker.write_text(json.dumps(receipt))
        staged_before = self.git('diff', '--cached')
        result = self.connect()
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.git('diff', '--cached'), staged_before)
        self.assertEqual(staged.read_text(), 'newer unstaged fact')
        self.assertEqual((self.root / 'profile/untracked.md').read_text(), 'untracked fact')
        after = json.loads(marker.read_text())
        self.assertEqual(after['tasks'], ['keep-managed-task'])
        self.assertEqual(after['custom'], {'must': 'survive'})

    def test_repeat_connection_has_no_extra_commit_or_duplicate_origin(self):
        self.assertTrue(self.connect()['ok'])
        head = self.git('rev-parse', 'HEAD')
        self.assertTrue(self.connect()['ok'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.git('remote'), 'origin')

    def test_concurrent_config_change_is_preserved_and_connection_rejected(self):
        path = self.root / 'config/kb.json'
        original = path.read_text()
        metadata = self.private.return_value
        def change_during_check(repository):
            path.write_text(original + '\n')
            return metadata
        self.private.side_effect = change_during_check
        result = self.connect()
        self.assertEqual(result['reason'], 'local-state-changed-during-check')
        self.assertEqual(path.read_text(), original + '\n')
        self.assertEqual(self.git('remote'), '')

    def test_commit_failure_restores_config_receipt_origin_and_staging(self):
        (self.root / 'projects/staged.md').write_text('keep staged')
        self.git('add', 'projects/staged.md')
        before, index = self.snapshot(), (self.root / '.git/index').read_bytes()
        transport = self.local_transport
        def fail_commit(arguments, timeout=180, root=None):
            if 'commit' in arguments:
                return subprocess.CompletedProcess(arguments, 1, '', 'sensitive stderr must not appear')
            return transport(arguments, timeout=timeout, root=root)
        with mock.patch.object(connect_sync.git_sync, 'git', side_effect=fail_commit):
            result = self.connect()
        self.assertEqual(result['reason'], 'config-commit-failed')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.root / '.git/index').read_bytes(), index)
        self.assertNotIn('sensitive', json.dumps(result))

    def test_failure_after_commit_rolls_back_head_files_and_index(self):
        before = self.snapshot()
        index = (self.root / '.git/index').read_bytes()
        with mock.patch.object(connect_sync.git_sync, 'sync_policy', side_effect=ValueError('forced failure')):
            result = self.connect()
        self.assertFalse(result['ok'])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.root / '.git/index').read_bytes(), index)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_partial_marker_write_failure_rolls_back(self):
        before = self.snapshot()
        original = connect_sync.save_object
        def fail_marker(path, value):
            if path.name == 'install.json':
                raise OSError('fixture failure')
            return original(path, value)
        with mock.patch.object(connect_sync, 'save_object', side_effect=fail_marker):
            result = self.connect()
        self.assertFalse(result['ok'])
        self.assertEqual(self.snapshot(), before)

    def test_custom_push_url_and_origin_change_refused(self):
        self.git('remote', 'add', 'origin', self.url)
        self.git('config', 'remote.origin.pushurl', 'https://github.com/other/repo.git')
        before = self.snapshot()
        result = self.connect()
        self.assertEqual(result['reason'], 'custom-push-url')
        self.assertEqual(self.snapshot(), before)
        self.git('config', '--unset', 'remote.origin.pushurl')
        before = self.snapshot()
        result = self.connect(remote_url='git@github.com:example/knowledge.git')
        self.assertEqual(result['reason'], 'origin-change-not-supported')
        self.assertEqual(self.snapshot(), before)

    def test_unsafe_or_wrong_urls_refused_without_echo(self):
        for url in ('https://token:secret@github.com/example/knowledge.git',
                    'https://github.com/other/repo.git',
                    'https://github.com:8443/example/knowledge.git',
                    'https://github.com/example/knowledge.git?token=secret',
                    'file:///tmp/private.git'):
            with self.subTest(url=url):
                before = self.snapshot()
                result = self.connect(remote_url=url)
                self.assertEqual(result['reason'], 'remote-url-mismatch')
                self.assertEqual(self.snapshot(), before)
                self.assertNotIn('secret', json.dumps(result))

    def test_default_checker_uses_authenticated_metadata_and_rejects_404(self):
        with mock.patch.object(connect_sync.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 1, '{"message":"Not Found"}', '')) as run:
            result = connect_sync.verify_private_repository(self.repository)
        self.assertFalse(result['ok'])
        self.assertEqual(run.call_args.args[0][:5], ['gh', 'api', '--hostname', 'github.com', '--method'])
        with mock.patch.object(connect_sync.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({'private': True, 'full_name': self.repository}), '')):
            result = connect_sync.verify_private_repository(self.repository)
        self.assertTrue(result['ok'])
        self.assertEqual(result['repository'], self.repository)


if __name__ == '__main__':
    unittest.main()
