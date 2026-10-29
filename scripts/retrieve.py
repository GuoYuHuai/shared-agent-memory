#!/usr/bin/env python3
"""Foreground retrieval: refresh disposable index, bounded aliases, one sync.

Unlike kb.py search (read-only), this entry point may refresh indexes and pull
an already configured private repository. It never installs credentials,
creates remotes, commits, pushes, or resolves conflicts.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys

import git_sync
import kb
import workspace_lock
import device_identity


def freshness_binding(root, config):
    return {'device': device_identity.platform_fingerprint(),
            'root': str(root.resolve()),
            'knowledge_base_id': device_identity.knowledge_base_id(root),
            'repository': config.get('hot_sync', {}).get('expected_repository'),
            'origin': git_sync.value(['remote', 'get-url', 'origin'], root=root),
            'head': git_sync.value(['rev-parse', '--verify', 'HEAD'], root=root),
            'branch': git_sync.value(['symbolic-ref', '--quiet', 'HEAD'], root=root)}


def retrieve(root, query, scope=None, kind=None, limit=12, include_values=False,
             offline=False, sync_fn=None, fresh=False):
    config_path = root / 'config/kb.json'
    config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.is_file() else {}
    settings = config.get('retrieval', {})
    maximum_age = max(0, int(settings.get('freshness_seconds', 3600)))
    state_path = root / 'generated/retrieval-freshness.json'
    state = {}
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    if not isinstance(state, dict) or state.get('binding') != freshness_binding(root, config):
        state = {}
    verified_at = kb.parse_time(state.get('remote_verified_at'))
    now = kb.utc_now()
    cached_fresh = bool(verified_at and 0 <= (now - verified_at).total_seconds() <= maximum_age)
    sync_status = 'offline' if offline else ('recently-verified' if cached_fresh else 'not-verified')
    sync_attempts = 0
    index_status = 'hot-only'

    def query_local():
        nonlocal index_status
        force_hot = False
        if kb.cold_data_available(root):
            try:
                with workspace_lock.acquire(root, blocking=False):
                    kb.update_index(root)
                index_status = 'refreshed'
            except (OSError, sqlite3.Error, workspace_lock.WorkspaceBusy) as error:
                index_status = 'refresh-failed:' + type(error).__name__
                force_hot = True
            except kb.KBError as error:
                # An unplug after the availability check is a storage failure.
                # Invalid source/schema/lifecycle data must remain explicit.
                if not str(error).startswith('cold-data-unavailable:'):
                    raise
                index_status = 'refresh-failed:' + type(error).__name__
                force_hot = True
        args = argparse.Namespace(root=root, command='search', query=query, scope=scope, kind=kind,
                                  limit=limit, include_values=include_values, force_hot=force_hot)
        # Keep aliases, sibling conflict discovery, eligibility and budgets
        # identical when the disposable cold projection cannot be refreshed.
        result = kb.run(args)
        if force_hot:
            result['conclusion'] = 'local-index-incomplete'
            result['index_refresh_failed'] = True
        return result

    report = query_local()
    need_sync = fresh or not cached_fresh or not report.get('current_count', report['count'])
    if need_sync and not offline:
        if (not config.get('hot_sync', {}).get('expected_repository')
                or config.get('hot_sync', {}).get('enabled') is False):
            sync_status = 'not-configured'
        else:
            sync_attempts = 1
            try:
                with workspace_lock.acquire(root, blocking=False):
                    outcome = (sync_fn or git_sync.sync)('origin', pull=True, push=False, root=root)
            except Exception as error:
                sync_status = 'failed:' + type(error).__name__
            else:
                if outcome.get('ok') and not outcome.get('skipped'):
                    sync_status = 'verified'
                    try:
                        state_path.parent.mkdir(exist_ok=True)
                        state_path.write_text(json.dumps({'remote_verified_at': kb.iso_now(),
                            'binding': freshness_binding(root, config)}, indent=2)+'\n', encoding='utf-8')
                    except OSError:
                        # Cache persistence is optional; next recall rechecks.
                        pass
                    # Source failures after a successful pull cannot be hidden
                    # behind a failed-sync label and the pre-pull result list.
                    report = query_local()
                else:
                    sync_status = 'failed:' + str(outcome.get('status', outcome.get('skipped', 'unknown')))
    report['freshness'] = {'status': sync_status, 'sync_attempts': sync_attempts,
                           'maximum_age_seconds': maximum_age,
                           'index_status': index_status,
                           'remote_verified_at': kb.iso_now() if sync_status == 'verified' else state.get('remote_verified_at')}
    if not report.get('current_count', report['count']) and not report.get('index_refresh_failed'):
        report['conclusion'] = ('current-value-unconfirmed' if report['count'] else
                                'not-found-in-verified-shared-snapshot' if sync_status in ('verified','recently-verified') else
                                'not-found-in-local-snapshot')
    report['coverage'] = 'shared hot knowledge; other devices unshared history and unsaved proposals are not covered'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('query')
    parser.add_argument('--scope')
    parser.add_argument('--kind')
    parser.add_argument('--limit', type=int, default=12)
    parser.add_argument('--include-values', action='store_true')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--fresh', action='store_true', help='Check the configured remote even when a recent snapshot is cached; offline takes precedence')
    args = parser.parse_args()
    try:
        result = retrieve(**vars(args))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (kb.KBError, ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 2

if __name__ == '__main__':
    sys.exit(main())
