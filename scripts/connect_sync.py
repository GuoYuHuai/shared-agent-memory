#!/usr/bin/env python3
"""Connect an installed local knowledge root to an existing private GitHub repo.

This operation verifies metadata and Git history, then commits only the sync
configuration locally. It never creates a repository, pushes, or merges history.
"""

import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import git_sync
import install
import workspace_lock


class ConnectionError(ValueError):
    def __init__(self, reason, recovery):
        super().__init__(reason)
        self.reason, self.recovery = reason, recovery


def reject(reason, recovery):
    raise ConnectionError(reason, recovery)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def verify_private_repository(repository):
    """Use authenticated GitHub metadata; anonymous 404 is not proof of privacy."""
    environment = git_sync.git_environment()
    environment['GH_PROMPT_DISABLED'] = '1'
    try:
        result = subprocess.run(
            ['gh', 'api', '--hostname', 'github.com', '--method', 'GET',
             'repos/' + repository], capture_output=True, text=True,
            timeout=35, check=False, env=environment,
        )
        metadata = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        'ok': metadata.get('private') is True,
        'private': metadata.get('private'),
        'repository': metadata.get('full_name'),
        'source': 'authenticated-github-api',
        'archived': metadata.get('archived') is True,
        'disabled': metadata.get('disabled') is True,
    }


def valid_remote_url(url, repository):
    # Reuse the sync identity contract, and exclude credentials/custom ports.
    # This entrypoint intentionally accepts github.com itself, not SSH aliases.
    if not isinstance(url, str) or url != url.strip() or any(c.isspace() for c in url):
        return False
    if not git_sync.remote_matches_policy(url, {
            'expected_repository': repository, 'allowed_ssh_hosts': ['github.com']}):
        return False
    if '://' in url:
        try:
            parsed = urlsplit(url)
            if parsed.password or parsed.port is not None:
                return False
            if parsed.scheme == 'https' and parsed.username is not None:
                return False
            if parsed.scheme == 'ssh' and parsed.username != 'git':
                return False
        except ValueError:
            return False
    return True


def checked_git(arguments, root, reason, recovery, timeout=45):
    try:
        result = git_sync.git(arguments, root=root, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        reject(reason, recovery)
    if result.returncode:
        reject(reason, recovery)
    return result.stdout.strip()


def remote_history(root, remote_url, branch, local_head):
    """Inspect remote refs and common ancestry without fetching into the user repo."""
    recovery = 'Check Git authentication and connectivity, then retry connect-sync.'
    listing = checked_git(['ls-remote', '--refs', remote_url], root,
                          'remote-unreachable', recovery)
    refs = {}
    for line in listing.splitlines():
        fields = line.split('\t')
        if len(fields) != 2 or not re.fullmatch(r'[0-9a-f]{40,64}', fields[0]):
            reject('remote-refs-invalid', recovery)
        refs[fields[1]] = fields[0]
    if not refs:
        return {'status': 'empty', 'remote_head': None, 'local_head': local_head}
    remote_ref = 'refs/heads/' + branch
    if remote_ref not in refs:
        reject('remote-branch-missing',
               'The remote is not empty. Clone and adopt its existing knowledge branch; do not initialize or merge unrelated histories.')
    with tempfile.TemporaryDirectory(prefix='agent-knowledge-connect-') as directory:
        probe = Path(directory)
        checked_git(['init', '--bare', '--quiet', str(probe)], root,
                    'history-probe-failed', recovery)
        checked_git(['fetch', '--quiet', '--no-tags', str(root),
                     local_head + ':refs/heads/local'], probe,
                    'local-history-unavailable', 'Complete or repair the local Git history before retrying.')
        checked_git(['fetch', '--quiet', '--no-tags', remote_url,
                     remote_ref + ':refs/heads/remote'], probe,
                    'remote-history-unavailable', recovery)
        fetched = checked_git(['rev-parse', 'refs/heads/remote'], probe,
                              'remote-history-unavailable', recovery)
        if fetched != refs[remote_ref]:
            reject('remote-changed-during-check', 'Retry after other devices finish syncing.')
        common = git_sync.git(['merge-base', 'refs/heads/local', 'refs/heads/remote'], root=probe)
        if common.returncode == 1:
            reject('unrelated-remote-history',
                   'Clone the existing private knowledge repository into a separate directory and adopt it. Preserve this local knowledge for reviewed import; never merge unrelated histories automatically.')
        if common.returncode or not common.stdout.strip():
            reject('history-check-failed', 'Resolve incomplete Git history before retrying.')
        return {'status': 'related', 'remote_head': fetched,
                'local_head': local_head, 'common_ancestor': common.stdout.strip()}


def read_object(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        reject('invalid-installation', 'Run the installer against the existing knowledge directory and review its diagnostics.')
    if not isinstance(value, dict):
        reject('invalid-installation', 'Repair the installation metadata before retrying.')
    return value


def atomic_bytes(path, data, mode=None):
    temporary = path.with_name(path.name + '.connect-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_object(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
                 path.stat().st_mode & 0o777)


def _connect(root, repository, remote_url, private_checker):
    config_path, marker = root / 'config/kb.json', root / '.ai-knowledge/install.json'
    for relative in ('config', 'config/kb.json', '.ai-knowledge', '.ai-knowledge/install.json',
                     '.git', '.git/config', '.git/index', 'MANIFEST.json', 'POLICY.md', 'AGENTS.md'):
        if (root / relative).is_symlink():
            reject('managed-path-is-symlink', 'Use the original installed directory and inspect its path ownership.')
    if not (root / '.git').is_dir():
        reject('installation-git-directory-missing', 'Connect an installed knowledge repository, not a worktree or arbitrary folder.')
    initial_bytes = {path: path.read_bytes() for path in (config_path, marker, root / '.git/config')}
    config, receipt, manifest = read_object(config_path), read_object(marker), read_object(root / 'MANIFEST.json')
    if manifest.get('schema_version') != '1' or receipt.get('data_schema') != '1':
        reject('unsupported-schema', 'Use a compatible tool version; do not rewrite the knowledge schema.')
    if not install.owner_matches(receipt, install.installation_owner(root)):
        reject('installation-owner-mismatch', 'Attach this existing knowledge directory on the current device with the installer, then retry.')
    if receipt.get('mode') == 'removable':
        install.verify_removable_binding(root, receipt, install.removable_volume(root))
    hot = config.get('hot_sync')
    if not isinstance(hot, dict):
        reject('sync-policy-invalid', 'Restore the installed sync configuration before retrying.')
    previous_repository = hot.get('expected_repository') or None
    if previous_repository != (receipt.get('repository') or None):
        reject('inconsistent-repository-binding', 'Reconcile the installation receipt and sync policy before reconnecting.')
    if previous_repository and previous_repository.casefold() != repository.casefold():
        reject('already-bound-to-another-repository', 'Keep the existing binding; a different repository requires a reviewed migration.')
    branch = hot.get('expected_branch')
    if not isinstance(branch, str) or git_sync.git(['check-ref-format', '--branch', branch], root=root).returncode:
        reject('sync-branch-invalid', 'Restore a valid expected branch in the sync policy.')
    if git_sync.value(['symbolic-ref', '--quiet', '--short', 'HEAD'], root=root) != branch:
        reject('unexpected-branch', 'Check out the configured knowledge branch before connecting sync.')
    local_head = checked_git(['rev-parse', '--verify', 'HEAD'], root,
                             'local-history-missing', 'Complete local installation before connecting sync.')
    if git_sync.rebase_state_present(root) or any((root / '.git' / name).exists()
            for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'index.lock', 'config.lock')):
        reject('git-operation-in-progress', 'Finish the existing Git operation before retrying.')
    tracked = checked_git(['ls-files', '--error-unmatch', '--', 'config/kb.json'], root,
                          'config-not-tracked', 'Restore the installed tracked configuration before connecting sync.')
    dirty = checked_git(['status', '--porcelain', '--', 'config/kb.json'], root,
                        'config-state-unavailable', 'Resolve the local Git state before retrying.')
    if dirty or tracked != 'config/kb.json':
        reject('dirty-sync-config', 'Review and commit or restore your changes to config/kb.json, then retry.')
    # status can hide edits under assume-unchanged / skip-worktree flags.
    config_blob = checked_git(['hash-object', '--path=config/kb.json', 'config/kb.json'], root,
                              'config-state-unavailable', 'Resolve the local Git state before retrying.')
    if config_blob != git_sync.value(['rev-parse', 'HEAD:config/kb.json'], root=root):
        reject('dirty-sync-config', 'Review and commit or restore your changes to config/kb.json, then retry.')
    origin = git_sync.git(['config', '--local', '--get-all', 'remote.origin.url'], root=root)
    if origin.returncode not in (0, 1):
        reject('origin-unreadable', 'Inspect the existing Git origin configuration.')
    origins = origin.stdout.strip().splitlines() if origin.returncode == 0 else []
    if len(origins) > 1 or (origins and not valid_remote_url(origins[0], repository)):
        reject('foreign-origin', 'Preserve the existing origin; use a reviewed migration or clone the intended repository separately.')
    selected = remote_url or (origins[0] if origins else 'https://github.com/' + repository + '.git')
    if not valid_remote_url(selected, repository):
        reject('remote-url-mismatch', 'Use an HTTPS or git SSH URL on github.com for exactly the selected owner/repository, without embedded credentials.')
    if origins and remote_url and selected != origins[0]:
        reject('origin-change-not-supported', 'Keep the existing matching origin URL; this command does not replace remote configuration.')
    # A push URL or nonstandard refspec can otherwise send later sync elsewhere.
    push_urls = git_sync.git(['config', '--get-all', 'remote.origin.pushurl'], root=root)
    if push_urls.returncode == 0 or push_urls.returncode not in (0, 1):
        reject('custom-push-url', 'Review and remove the custom push URL before using the standard knowledge sync path.')
    if origins:
        for args in (['remote', 'get-url', 'origin'], ['remote', 'get-url', '--push', 'origin']):
            effective = checked_git(args, root, 'origin-unreadable', 'Inspect the existing Git origin configuration.')
            if not valid_remote_url(effective, repository):
                reject('origin-url-rewritten', 'Review Git URL rewrite settings before connecting this repository.')
        refspec = git_sync.git(['config', '--get-all', 'remote.origin.fetch'], root=root)
        if refspec.returncode or refspec.stdout.strip() != '+refs/heads/*:refs/remotes/origin/*':
            reject('custom-fetch-refspec', 'Review the existing origin fetch rules before using standard knowledge sync.')
    try:
        private = private_checker(repository)
    except Exception:
        reject('privacy-unverified', 'Authenticate gh for github.com and verify access to the existing private repository, then retry.')
    if not isinstance(private, dict) or private.get('ok') is not True or private.get('private') is not True:
        reject('privacy-unverified', 'Use an existing private repository and authenticated GitHub metadata. A missing repository or anonymous 404 does not establish privacy.')
    if str(private.get('repository', '')).casefold() != repository.casefold():
        reject('metadata-identity-mismatch', 'Verify the exact owner/repository; repository redirects are not accepted automatically.')
    if private.get('archived') or private.get('disabled'):
        reject('remote-read-only', 'Choose an active private knowledge repository.')
    history = remote_history(root, selected, branch, local_head)
    updated_config, updated_receipt = copy.deepcopy(config), copy.deepcopy(receipt)
    updated_config['hot_sync'].update({'enabled': True, 'expected_repository': repository})
    allowed = updated_config['hot_sync'].get('allowed_ssh_hosts')
    if not isinstance(allowed, list) or 'github.com' not in allowed:
        reject('sync-policy-invalid', 'Restore the installed allowed_ssh_hosts configuration before retrying.')
    evidence = {'source': private.get('source') or 'injected-private-checker',
                'repository': repository, 'checked_at': utc_now(), 'private': True}
    updated_receipt.update({'repository': repository, 'private_verified': True,
                            'private_identity': evidence['source'], 'private_verification': evidence})
    updated_receipt['sync_connection'] = {'checked_at': evidence['checked_at'],
        'history': history, 'status': 'ready-for-first-push' if history['status'] == 'empty' else 'ready-for-sync',
        'pushed': False}
    if (any(path.read_bytes() != original for path, original in initial_bytes.items())
            or git_sync.value(['rev-parse', 'HEAD'], root=root) != local_head
            or git_sync.value(['symbolic-ref', '--quiet', '--short', 'HEAD'], root=root) != branch):
        reject('local-state-changed-during-check', 'Keep the concurrent changes and retry once other writers have finished.')
    # Snapshot only files this operation can modify. Unrelated staged/working
    # changes stay intact. commit --only limits the commit to the sync config.
    paths = [config_path, marker, root / '.git/config', root / '.git/index',
             root / '.git/COMMIT_EDITMSG', root / '.git/logs/HEAD',
             root / '.git/logs/refs/heads' / branch]
    snapshots = {path: (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
                 for path in paths}
    changed_head = None
    try:
        if not origins:
            checked_git(['remote', 'add', 'origin', selected], root,
                        'origin-add-failed', 'Inspect local Git configuration and retry.')
            for args in (['remote', 'get-url', 'origin'], ['remote', 'get-url', '--push', 'origin']):
                effective = checked_git(args, root, 'origin-unreadable', 'Inspect local Git configuration and retry.')
                if not valid_remote_url(effective, repository):
                    reject('origin-url-rewritten', 'Review Git URL rewrite settings before connecting this repository.')
        save_object(config_path, updated_config)
        save_object(marker, updated_receipt)
        if updated_config != config:
            checked_git(['-c', 'user.name=AI Knowledge', '-c', 'user.email=knowledge@localhost',
                         '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
                         'commit', '--only', '-m', 'Connect private knowledge sync', '--', 'config/kb.json'],
                        root, 'config-commit-failed', 'Resolve the local Git commit error and retry; prior configuration is restored.')
            changed_head = git_sync.value(['rev-parse', 'HEAD'], root=root)
        if read_object(config_path) != updated_config or read_object(marker) != updated_receipt:
            reject('connection-readback-failed', 'Retry after resolving concurrent file edits.')
        git_sync.sync_policy(root)  # Validate the configuration consumed by sync.
    except BaseException:
        # A command may have committed before reporting an exception; detect it
        # and reset only our single config commit, never unrelated user work.
        current = git_sync.value(['rev-parse', 'HEAD'], root=root)
        if current != local_head:
            parent = git_sync.value(['rev-parse', current + '^'], root=root)
            files = git_sync.value(['diff-tree', '--no-commit-id', '--name-only', '-r', current], root=root)
            if parent != local_head or files != 'config/kb.json':
                reject('rollback-blocked-by-concurrent-commit', 'Preserve this checkout and inspect the concurrent commit before retrying.')
            checked_git(['update-ref', 'refs/heads/' + branch, local_head, current], root,
                        'rollback-head-failed', 'Preserve this checkout and restore the previous config commit before retrying.')
        restore_failed = False
        for path, snapshot in snapshots.items():
            try:
                if snapshot is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_bytes(path, snapshot[0], snapshot[1])
            except OSError:
                restore_failed = True
        if restore_failed:
            reject('rollback-incomplete', 'Filesystem access prevented full recovery. Preserve this checkout and inspect config/kb.json, the installation receipt and Git state before retrying.')
        raise
    return {'ok': True, 'action': 'connect-sync', 'status': updated_receipt['sync_connection']['status'],
            'repository': repository, 'branch': branch, 'private_verified': True,
            'verification': evidence, 'history': history, 'local_config_commit': changed_head,
            'pushed': False, 'synced': False,
            'next_step': 'Run sync when ready to publish committed knowledge; connection alone has not uploaded data.'}


def connect(root, repository, remote_url=None, *, private_checker=None):
    """Connect existing data; the injectable checker is for local contract tests."""
    try:
        if not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            reject('repository-invalid', 'Provide the existing private repository as owner/name.')
        root = Path(root).expanduser()
        if root.is_symlink() or not root.is_dir():
            reject('knowledge-root-unavailable', 'Use an existing installed knowledge directory.')
        root = root.resolve()
        if not (root / '.git').is_dir() or not (root / '.ai-knowledge/install.json').is_file():
            reject('invalid-installation', 'Install or attach this existing knowledge directory before connecting sync.')
        if (root / 'generated').is_symlink() or (root / 'generated/workspace-mutation.lock').is_symlink():
            reject('managed-path-is-symlink', 'Inspect the installed workspace lock path before connecting sync.')
        with workspace_lock.acquire(root):
            return _connect(root, repository, remote_url, private_checker or verify_private_repository)
    except ConnectionError as error:
        return {'ok': False, 'action': 'connect-sync', 'reason': error.reason, 'recovery': error.recovery,
                'pushed': False, 'synced': False}
    except workspace_lock.WorkspaceBusy:
        return {'ok': False, 'action': 'connect-sync', 'reason': 'workspace-busy',
                'recovery': 'Wait for the current knowledge operation to finish and retry.'}
    except (OSError, ValueError, git_sync.SyncError, subprocess.TimeoutExpired):
        # Never echo command stderr, raw URLs, or arbitrary exception messages.
        return {'ok': False, 'action': 'connect-sync', 'reason': 'connection-failed',
                'recovery': 'Inspect local installation and Git access; no upload was attempted.',
                'pushed': False, 'synced': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--remote-url')
    args = parser.parse_args(argv)
    result = connect(args.root, args.repository, args.remote_url)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
