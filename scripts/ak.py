#!/usr/bin/env python3
"""Save, find and correct knowledge shared by your Agents."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import device_identity
import install
import kb
import retrieve
import workspace_lock
import sync_runner

TOOL_ROOT = Path(__file__).resolve().parents[1]


def share_saved(root, report, offline=False):
    """Keep local durability independent of one optional foreground sync."""
    report = dict(report, synced=False, sync_pending=False)
    if not report.get('saved') or not report.get('readback') or report.get('ok') is False:
        return report
    try:
        config = install.read(root / 'config/kb.json')
        hot = config.get('hot_sync', {})
        configured = hot.get('enabled') is True and bool(hot.get('expected_repository'))
    except (OSError, ValueError, AttributeError):
        report.update(ok=False, pending=True, sync_pending=True, sync={'ok':False, 'status':'configuration-unavailable'})
        return report
    if not configured:
        report['sync'] = {'ok':True, 'status':'local-only', 'attempted':False}
        return report
    if offline:
        report.update(pending=True, sync_pending=True, sync={'ok':True, 'status':'offline', 'attempted':False})
        return report
    outcome = sync_runner.run_sync(root)
    remote = outcome.get('sync')
    confirmed = (outcome.get('ok') is True and not outcome.get('pending')
                 and not outcome.get('skipped') and isinstance(remote, dict)
                 and remote.get('ok') is True and remote.get('pushed') is True
                 and not remote.get('skipped'))
    report.update(synced=confirmed, pending=not confirmed, sync_pending=not confirmed, sync=outcome)
    if not confirmed:
        report['ok'] = False
        if outcome.get('recovery_required'):
            report['recovery_required'] = True
            report['sync_recovery_required'] = True
        report['sync_recovery'] = ('The local record is saved. Check Git status and any reported conflict or unfinished operation before retrying sync; do not create another record.')
    return report


def installed_root(path):
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError('Use the real knowledge directory, not a symbolic link.')
    root = path.resolve()
    for name in ('MANIFEST.json', 'POLICY.md', 'config/kb.json', '.ai-knowledge/install.json'):
        target = root / name
        if not target.is_file() or target.is_symlink():
            raise ValueError('Knowledge is unavailable or not installed here. Reconnect its storage, or run init for the intended directory: ' + str(root))
    kb.require_writable_schema(root)
    receipt = install.read(root / '.ai-knowledge/install.json')
    if not install.owner_matches(receipt, install.installation_owner(root)):
        raise ValueError('This copy needs to be attached on this device. Run init for this existing directory before using it.')
    if receipt.get('mode') == 'removable':
        install.verify_removable_binding(root, receipt, install.removable_volume(root))
    return root


def status(root):
    import git_sync
    import instruction_link
    config = install.read(root / 'config/kb.json')
    sync = config.get('hot_sync', {})
    state = {}
    try:
        state = install.read(root / 'generated/retrieval-freshness.json')
    except (OSError, ValueError):
        pass
    if not isinstance(state, dict) or state.get('binding') != retrieve.freshness_binding(root, config):
        state = {}
    remote_time = state.get('remote_verified_at')
    parsed = kb.parse_time(remote_time)
    age = (kb.utc_now() - parsed).total_seconds() if parsed else None
    recent = age is not None and 0 <= age <= config.get('retrieval', {}).get('freshness_seconds', 3600)
    configured = bool(sync.get('enabled') and sync.get('expected_repository'))
    links = instruction_link.connections(root)
    broken = [link for link in links if not link.get('ok')]
    report = {'ok': not broken, 'action': 'status', 'knowledge_root': str(root),
            'storage': 'private-git' if configured else 'local',
            'repository': sync.get('expected_repository'),
            'origin_configured': bool(git_sync.value(['remote', 'get-url', 'origin'], root=root)),
            'remote_freshness': 'recently-verified' if configured and recent else 'not-verified' if configured else 'not-applicable',
            'remote_verified_at': remote_time,
            'local_changes': bool(git_sync.value(['--no-optional-locks', 'status', '--porcelain'], root=root)),
            'connections': links, 'persistent_activation': 'not-verified',
            'history_import': bool(config.get('history_capture', {}).get('enabled')),
            'next_step': 'Test retrieval in a new Agent chat.' if links else 'Connect an instruction file used by your Agent with connect --instructions FILE.'}
    if broken:
        report.update(reason='One or more instruction connections need repair.',
                      recovery='; '.join(str(link.get('instructions')) + ': ' + str(link.get('recovery') or link.get('reason')) for link in broken))
        report['next_step'] = report['recovery']
    return report


def dispatch(args):
    if args.command == 'init':
        return install.install(args.root, args.repository, args.mode)
    root = installed_root(args.root)
    if args.command == 'remember':
        tokens = ['--root', str(root), 'capture', '--kind', args.kind, '--scope', args.scope,
                  '--statement', args.statement, '--source', args.source, '--authority', args.authority]
        if args.id:
            tokens += ['--id', args.id]
        if args.value_file:
            tokens += ['--value-file', str(args.value_file)]
        return share_saved(root, dict(kb.run(kb.build_parser().parse_args(tokens)), action='remember'), args.offline)
    if args.command == 'recall':
        return dict(retrieve.retrieve(root, args.query, scope=args.scope, limit=args.limit,
                    include_values=args.include_values, offline=args.offline, fresh=args.fresh), action='recall')
    if args.command == 'correct':
        import correction
        return share_saved(root, dict(correction.correct(root, args.old_id, args.statement, args.source,
                    authority=args.authority, value_file=args.value_file), action='correct'), args.offline)
    if args.command in ('connect', 'disconnect'):
        import instruction_link
        return getattr(instruction_link, args.command)(root, args.instructions)
    if args.command == 'connect-sync':
        import connect_sync
        return connect_sync.connect(root, args.repository, args.remote_url)
    if args.command == 'status':
        return status(root)
    if args.command == 'sync':
        result = subprocess.run([sys.executable, str(TOOL_ROOT / 'scripts/hot_sync.py')],
            env=dict(os.environ, AI_KNOWLEDGE_ROOT=str(root), PYTHONDONTWRITEBYTECODE='1'),
            capture_output=True, text=True)
        try:
            report = json.loads(result.stdout or result.stderr)
        except ValueError:
            raise ValueError('Sync did not return a result. Check Git access and run status; your saved records remain in the knowledge folder.')
        if result.returncode:
            report['ok'] = False
        return report
    raise ValueError('Unknown command')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True, help='Your knowledge directory, separate from these tools')
    p.add_argument('--json', action='store_true', help='Print structured output for an Agent or script')
    commands = p.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Create a local knowledge folder or attach an existing copy')
    init.add_argument('--repository', help='Existing knowledge repository identity, owner/name; use connect-sync to enable a new remote')
    init.add_argument('--mode', choices=['local', 'removable'], default='local')
    remember = commands.add_parser('remember', help='Save a fact, preference or decision with its source')
    remember.add_argument('statement')
    remember.add_argument('--source', required=True)
    remember.add_argument('--scope', default='global', help='Project or context; default: global')
    remember.add_argument('--kind', default='fact')
    remember.add_argument('--authority', default='user-stated', help='Use agent-inferred for unconfirmed inferences')
    remember.add_argument('--id', help='Stable record ID for safe retries')
    remember.add_argument('--value-file', type=Path, help='Read an exact value without placing it in command arguments')
    remember.add_argument('--offline', action='store_true', help='Save locally without attempting configured sync')
    recall = commands.add_parser('recall', help='Find current knowledge and check the configured sync freshness')
    recall.add_argument('query')
    recall.add_argument('--scope')
    recall.add_argument('--limit', type=int, default=12)
    recall.add_argument('--include-values', action='store_true')
    recall.add_argument('--offline', action='store_true')
    recall.add_argument('--fresh', action='store_true', help='Check the configured remote even when a recent snapshot exists')
    correct = commands.add_parser('correct', help='Replace an earlier fact while keeping its source history')
    correct.add_argument('old_id')
    correct.add_argument('statement')
    correct.add_argument('--source', required=True)
    correct.add_argument('--authority', choices=['user-stated', 'verified'], default='user-stated')
    correct.add_argument('--value-file', type=Path)
    correct.add_argument('--offline', action='store_true', help='Save the correction locally without attempting configured sync')
    for name in ('connect', 'disconnect'):
        link = commands.add_parser(name, help=('Connect to' if name == 'connect' else 'Disconnect from') + ' an Agent instruction file')
        link.add_argument('--instructions', type=Path, required=True)
    remote = commands.add_parser('connect-sync', help='Connect this local knowledge to your private GitHub repository')
    remote.add_argument('--repository', required=True)
    remote.add_argument('--remote-url', help='Optional SSH remote URL for the same repository')
    commands.add_parser('sync', help='Commit saved knowledge and sync it if a private remote is configured')
    commands.add_parser('status', help='Show this installation, connections and known freshness')
    return p


def human(report):
    if report.get('ok') is False:
        detail = report.get('reason') or report.get('error') or (report.get('sync') or {}).get('error') or (report.get('sync') or {}).get('status') or (report.get('index') or {}).get('status')
        recovery = report.get('recovery') or report.get('sync_recovery')
        if isinstance(recovery, dict):
            recovery = recovery.get('action') or recovery
        if report.get('saved'):
            record_id = report.get('id') or report.get('new_id') or 'see the saved record'
            opening = ('Saved locally: ' if report.get('readback') else 'Local write completed: ') + str(record_id)
            opening += '\nVerification or a follow-up step needs attention.'
        else:
            opening = 'Could not complete the operation.'
        return '\n'.join(str(v) for v in (opening, detail, recovery) if v)
    action = report.get('action')
    if action in ('remember', 'correct'):
        shared = ('Synchronized with your private repository.' if report.get('synced')
                  else 'Remote sync is pending; your record is available locally.' if report.get('pending')
                  else 'Available to connected agents using this folder.')
        return 'Saved locally: ' + str(report.get('id') or report.get('new_id')) + '\n' + shared
    if action == 'recall':
        lines = ['Freshness: ' + report['freshness']['status']]
        for item in report.get('results', []):
            lines.extend(['', str(item.get('statement') or item.get('snippet') or item.get('title') or ''),
                          'ID: ' + str(item.get('id') or item.get('path')),
                          'Source: ' + str(item.get('source') or item.get('source_ref')),
                          'Status: ' + str(item.get('effective_status') or item.get('status') or item.get('lifecycle') or 'see source')])
            if 'value' in item:
                lines.append('Value: ' + str(item['value']))
        if not report.get('results'):
            lines.append('No matching record in this snapshot.')
        if report.get('conclusion'):
            lines.append('Coverage: ' + report['conclusion'])
        return '\n'.join(lines)
    if 'installation' in report:
        return 'Knowledge folder ready: ' + report['installation']['knowledge_root'] + '\nNext: connect --instructions FILE, then test recall in a new chat.'
    if action == 'status':
        return '\n'.join(['Knowledge: ' + report['knowledge_root'], 'Storage: ' + report['storage'],
                          'Remote freshness: ' + report['remote_freshness'],
                          'Instruction connections: ' + str(len(report['connections'])), report['next_step']])
    if action == 'connect':
        return 'Instruction file connected: ' + report['instructions'] + '\nOpen a new Agent chat and check that it retrieves a saved record. Activation is not yet verified.'
    if action == 'disconnect':
        return 'Instruction file disconnected: ' + report['instructions'] + '\nYour knowledge files have been kept.'
    if action == 'connect-sync':
        return 'Private repository connected: ' + report['repository'] + '\nNothing has been uploaded. Run sync when ready.'
    if action == 'hot-sync':
        if report.get('pending') or report.get('skipped'):
            return 'Sync is waiting: ' + str(report.get('skipped') or report.get('status')) + '\nSaved records remain available locally.'
        if report.get('status') == 'local-only':
            return 'Saved knowledge is committed locally. Remote sync is not enabled.'
        remote = report.get('sync') or {}
        if remote.get('pushed'):
            return 'Local knowledge has been synchronized with the private remote. Other devices still need to sync their copies.'
        if remote.get('ok'):
            return 'The remote was checked. No upload was reported; use --json for the detailed result.'
    # Less common operations retain their explicit state instead of flattening
    # planned, written, pending and successful outcomes into a "Done" message.
    return json.dumps(report, ensure_ascii=False, indent=2)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if getattr(args, 'limit', 1) < 1:
            raise ValueError('--limit must be positive')
        report = dispatch(args)
    except (kb.KBError, ValueError, OSError, sqlite3.Error, workspace_lock.WorkspaceBusy,
            device_identity.DeviceIdentityError, subprocess.SubprocessError) as error:
        report = {'ok': False, 'error': str(error)}
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else human(report))
    return 2 if report.get('ok') is False else 0


if __name__ == '__main__':
    sys.exit(main())
