import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL / 'scripts'))
import sync_runner


class ForegroundSync(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sam-sync-runner-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.script = self.root / 'worker.py'

    def run_worker(self, source, timeout=5):
        self.script.write_text(source, encoding='utf-8')
        return sync_runner.run_sync(self.root, timeout=timeout, script=self.script)

    def test_success_requires_complete_remote_receipt(self):
        cases = [
            ({'ok':True, 'sync':{'ok':True, 'pushed':True}}, True),
            ({'ok':True}, False),
            ({'ok':True, 'pending':True}, False),
            ({'ok':True, 'sync':{'ok':False, 'pushed':True}}, False),
            ({'ok':True, 'sync':{'ok':True, 'pushed':False}}, False),
            ({'ok':False, 'sync':{'ok':True, 'pushed':True}}, False),
            ([], False),
        ]
        for payload, success in cases:
            with self.subTest(payload=payload):
                result = self.run_worker('print(' + repr(json.dumps(payload)) + ')')
                self.assertEqual(result['ok'], success)
                self.assertEqual(result['pending'], not success)

    def test_invalid_output_and_exception_text_are_not_returned(self):
        for source in ('print("fictional-password-in-bad-output")',
                       'raise RuntimeError("fictional-password-in-exception")',
                       'print(\'{"ok":false,"error":"fictional-password"}\')',
                       'print(\'{"ok":true,"sync":{"ok":true,"pushed":true}}\'); raise SystemExit(3)'):
            result = self.run_worker(source)
            self.assertFalse(result['ok'])
            self.assertNotIn('fictional-password', json.dumps(result))

    def test_failed_cleanup_requires_recovery_not_success_or_blind_retry(self):
        result = self.run_worker('print(\'{"ok":false,"sync":{"ok":false,"error":"rebase-cleanup-failed","retryable":false}}\')')
        self.assertEqual(result['status'], 'sync-recovery-required')
        self.assertTrue(result['recovery_required'])

    def test_network_deadline_during_real_rebase_allows_completion_then_defers_push(self):
        self.check_late_rebase(conflict=False)

    def test_network_deadline_during_conflicting_rebase_allows_verified_abort(self):
        self.check_late_rebase(conflict=True)

    def check_late_rebase(self, conflict):
        repo = self.root / 'knowledge'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', *args], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
        git('init', '-q', '-b', 'main')
        git('config', 'user.name', 'Fictional User')
        git('config', 'user.email', 'fictional@example.test')
        git('config', 'commit.gpgsign', 'false')
        git('config', 'maintenance.auto', 'false')
        (repo / 'base.txt').write_text('base')
        git('add', '.')
        git('commit', '-qm', 'base')
        git('checkout', '-qb', 'peer')
        (repo / 'peer-fact.txt').write_text('peer fact')
        if conflict:
            (repo / 'base.txt').write_text('peer conflicting value\n')
        git('add', '.')
        git('commit', '-qm', 'peer fact')
        peer = git('rev-parse', 'HEAD')
        git('checkout', '-q', 'main')
        (repo / 'saved-fact.txt').write_text('locally saved fact')
        if conflict:
            (repo / 'base.txt').write_text('local conflicting value\n')
        git('add', '.')
        git('commit', '-qm', 'local fact')
        original = git('rev-parse', 'HEAD')
        git('update-ref', 'refs/remotes/origin/main', peer)
        # A real Git hook delays the rebase beyond the network deadline.
        hook = repo / '.git/hooks/pre-rebase'
        hook.write_text('#!/bin/sh\n"' + Path(sys.executable).as_posix() + '" -c "import time; time.sleep(1)"\n')
        hook.chmod(0o755)
        forbidden = self.root / 'remote-request-started'
        source = ('import sys,json,subprocess\nfrom pathlib import Path\n'
                  'sys.path.insert(0,' + repr(str(TOOL / 'scripts')) + ')\n'
                  'import git_sync,sync_runner\n'
                  'result=git_sync.rebase_onto("origin","main",Path(' + repr(str(repo)) + '))\n'
                  'assert result["ok"] is ' + repr(not conflict) + ',result\n'
                  'assert result["cleanup_verified"] is True,result\n'
                  'try:\n sync_runner.run_network([sys.executable,"-c",' + repr('from pathlib import Path; Path(' + repr(str(forbidden)) + ').touch()') + '],timeout=5)\n'
                  'except subprocess.TimeoutExpired:\n print(json.dumps({"ok":False,"error":"network-deadline"}))\n')
        started = time.monotonic()
        result = self.run_worker(source, timeout=0.2)
        self.assertGreater(time.monotonic() - started, 0.8)
        self.assertEqual(result['status'], 'network-deadline')
        self.assertEqual(git('rev-parse', 'HEAD') == original, conflict)
        self.assertEqual(git('branch', '--show-current'), 'main')
        self.assertEqual(git('status', '--porcelain'), '')
        self.assertEqual((repo / 'saved-fact.txt').read_text(), 'locally saved fact')
        self.assertEqual(git('show', 'refs/remotes/origin/main:peer-fact.txt'), 'peer fact')
        if not conflict:
            self.assertEqual((repo / 'peer-fact.txt').read_text(), 'peer fact')
        self.assertFalse(forbidden.exists())
        for name in ('index.lock', 'rebase-merge', 'rebase-apply'):
            self.assertFalse((repo / '.git' / name).exists())

    def test_timeout_stops_only_network_worker_and_descendant(self):
        # The child inherits output pipes. Merely killing its parent would leave
        # communicate waiting and the marker continuing to change.
        marker = self.root / 'ticks'
        child = ('import time; from pathlib import Path; p=Path(' + repr(str(marker)) + '); '
                 '\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(0.05)')
        source = ('import subprocess,sys,time\n'
                  'subprocess.Popen([sys.executable,"-c",' + repr(child) + '])\n'
                  'time.sleep(60)\n')
        started = time.monotonic()
        self.script.write_text(source, encoding='utf-8')
        with mock.patch.dict(os.environ, {sync_runner.DEADLINE_ENV:str(time.time() + 1)}):
            with self.assertRaises(subprocess.TimeoutExpired):
                sync_runner.run_network([sys.executable, str(self.script)], timeout=5)
        self.assertLess(time.monotonic() - started, 7)
        self.assertTrue(marker.exists(), 'descendant did not start')
        last = marker.read_text()
        time.sleep(0.2)
        self.assertEqual(marker.read_text(), last)

    @unittest.skipIf(os.name == 'nt', 'POSIX detached-session fixture')
    def test_detached_transport_pipe_cannot_block_deadline_collection(self):
        pid_file = self.root / 'detached.pid'
        detached = ('import os,time; from pathlib import Path; '
                    'Path(' + repr(str(pid_file)) + ').write_text(str(os.getpid())); time.sleep(60)')
        launcher = self.root / 'transport.py'
        launcher.write_text('import subprocess,sys,time\n'
            'subprocess.Popen([sys.executable,"-c",' + repr(detached) + '],start_new_session=True)\n'
            'time.sleep(60)\n', encoding='utf-8')
        collector = self.root / 'collector.py'
        collector.write_text('import sys,os,time,subprocess\n'
            'sys.path.insert(0,' + repr(str(TOOL / 'scripts')) + ')\nimport sync_runner\n'
            'os.environ[sync_runner.DEADLINE_ENV]=str(time.time()+1)\n'
            'try:\n sync_runner.run_network([sys.executable,' + repr(str(launcher)) + '],timeout=5)\n'
            'except subprocess.TimeoutExpired:\n print("network-timeout")\n', encoding='utf-8')
        try:
            started = time.monotonic()
            # A separate collector with its own timeout keeps a regression from
            # hanging the suite. The detached fixture PID is cleaned in finally.
            result = subprocess.run([sys.executable, str(collector)], capture_output=True,
                                    text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'network-timeout')
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(pid_file.exists())
            # Containment deliberately cannot claim to kill a helper that has
            # created a different POSIX session; only pipe collection is bounded.
            os.kill(int(pid_file.read_text()), 0)
        finally:
            if pid_file.exists():
                pid = int(pid_file.read_text())
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(os.name == 'nt', 'native Windows job assignment gate')
    def test_windows_job_assignment_failure_never_releases_transport(self):
        marker = self.root / 'must-not-start'
        command = [sys.executable, '-c', 'from pathlib import Path; Path(' + repr(str(marker)) + ').touch()']
        processes = []
        original = subprocess.Popen
        def launched(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with mock.patch.dict(os.environ, {sync_runner.DEADLINE_ENV:str(time.time() + 2)}), \
                mock.patch.object(sync_runner, '_windows_job', side_effect=OSError('fixture assignment failure')), \
                mock.patch.object(sync_runner.subprocess, 'Popen', side_effect=launched):
            with self.assertRaises(OSError):
                sync_runner.run_network(command, timeout=2)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse(marker.exists())

    def test_local_git_has_no_foreground_deadline(self):
        import git_sync
        with mock.patch.dict(os.environ, {sync_runner.DEADLINE_ENV:str(time.time() - 1)}), \
                mock.patch.object(git_sync.subprocess, 'run') as run:
            git_sync.git(['rebase', 'origin/main'])
            self.assertIsNone(run.call_args.kwargs['timeout'])
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(git_sync.subprocess, 'run') as run:
            git_sync.git(['status'], timeout=17)
            self.assertEqual(run.call_args.kwargs['timeout'], 17)

    def test_expired_deadline_prevents_every_remote_operation_and_privacy_probe(self):
        import git_sync
        with mock.patch.dict(os.environ, {sync_runner.DEADLINE_ENV:str(time.time() - 1)}), \
                mock.patch.object(git_sync, 'configured_origin_status', return_value={'ok':True}), \
                mock.patch.object(sync_runner.subprocess, 'Popen') as launched:
            for command in (['fetch', 'origin'], ['push', 'origin', 'main'], ['ls-remote', 'origin']):
                with self.subTest(command=command), self.assertRaises(subprocess.TimeoutExpired):
                    git_sync.network_git(command, 'origin', self.root)
            privacy = git_sync.repository_privacy_status(self.root, {
                'expected_repository':'fictional/memory', 'expected_branch':'main'})
            self.assertFalse(privacy['ok'])
        launched.assert_not_called()

    def test_timeout_stops_real_git_and_its_ssh_helper(self):
        marker = self.root / 'ssh-ticks'
        helper = self.root / 'fictional_ssh.py'
        helper.write_text('import time\nfrom pathlib import Path\np=Path(' + repr(str(marker)) + ')\n'
                          'while True:\n p.write_text(str(time.monotonic())); time.sleep(0.05)\n', encoding='utf-8')
        # The configured helper only writes a fixture marker; no network contact.
        command = '"' + Path(sys.executable).as_posix() + '" "' + helper.as_posix() + '"'
        source = ('import os,sys\n'
                  'sys.path.insert(0,' + repr(str(TOOL / 'scripts')) + ')\nimport git_sync\n'
                  'os.environ["GIT_SSH_COMMAND"]=' + repr(command) + '\n'
                  'os.environ["GIT_SSH_VARIANT"]="simple"\n'
                  'import sync_runner\n'
                  'sync_runner.run_network(["git","ls-remote","git@github.com:fictional/memory.git"],timeout=5,env=git_sync.git_environment())\n')
        result = self.run_worker(source, timeout=1)
        self.assertFalse(result['ok'])
        self.assertTrue(marker.exists(), 'SSH helper did not start')
        last = marker.read_text()
        time.sleep(0.2)
        self.assertEqual(marker.read_text(), last)


if __name__ == '__main__':
    unittest.main()
