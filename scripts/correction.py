#!/usr/bin/env python3
"""Publish a replacement fact and its supersession as one immutable file."""
from pathlib import Path
import json
import os

import kb
import workspace_lock


AUTHORITIES = frozenset({
    'user-stated', 'user-explicit', 'verified', 'verified-system', 'system-verified',
})
_GENERATED = {'created_at', 'source_device_id'}


def _semantic(event):
    return {key: value for key, value in event.items() if key not in _GENERATED}


def _ledger_records(root):
    records = []
    for path in kb.ledger_files(root):
        try:
            path.relative_to(root / 'ledger')
        except ValueError as error:
            raise kb.KBError('ledger source escapes knowledge root') from error
        for raw, reference in kb.iter_json_values(path):
            records.append((raw, kb.normalize_event(raw, reference), reference))
    return records


def _old_record(root, old_id, records):
    matches = [event for _, event, _ in records if event['id'] == old_id]
    if not matches:
        # Canonical JSON resources can exist without an imported ledger event.
        matches = [kb.normalize_event(dict(raw, statement=raw.get('statement', raw.get('name', ''))))
                   for _, raw in kb.resource_matches(root, old_id)]
    if not matches:
        raise kb.NotFoundError('cannot correct unknown id: ' + old_id)
    if len({kb.event_fingerprint(event) for event in matches}) != 1:
        raise kb.ConflictError('old id has conflicting records: ' + old_id)
    old = matches[0]
    if old['kind'] in kb.LIFECYCLE_KINDS:
        raise kb.KBError('correct a knowledge record, not a lifecycle event')
    if not old['kind'] or not old['scope']:
        raise kb.KBError('old record has no kind or scope')
    return old


def _read_pair(path, expected):
    """Only a complete matching operation may be treated as an idempotent retry."""
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as error:
        raise kb.ConflictError('existing correction cannot be verified') from error
    if (not isinstance(payload, list) or len(payload) != 2
            or any(not isinstance(event, dict) for event in payload)
            or any(kb.event_validation_errors(event) for event in payload)
            or [_semantic(event) for event in payload] != [_semantic(event) for event in expected]):
        raise kb.ConflictError('correction identity already contains different content')
    return payload


def _project(root, path, pair, duplicate, publication_error=None):
    """The ledger is already committed. Projection failures never erase that fact."""
    new, edge = pair
    relative = path.relative_to(root).as_posix()
    report = {
        'ok': True, 'saved': True, 'readback': True, 'duplicate': duplicate,
        'id': new['id'], 'new_id': new['id'], 'old_id': edge['old_id'],
        'supersession_id': edge['id'], 'ledger': relative,
        'index_status': 'pending', 'recovery_required': False,
    }
    failures = []
    if publication_error:
        failures.append({'stage': 'publication-durability', 'error_type': publication_error})
    def confirm_durability():
        # A previous publication may have raised after linking the complete file.
        # Retry must repair durability as well as the disposable projections.
        with path.open('r+b') as handle:
            os.fsync(handle.fileno())
        kb.fsync_directory(path.parent)

    actions = ([('ledger-durability', confirm_durability)] if duplicate else []) + [
        ('hot-locator', lambda: kb.hot_locator_record_source(root, path)),
        ('resource', lambda: kb.materialize_resource(root, new, relative + '#[0]')),
        ('index', lambda: kb.refresh_index_after_write(root)),
    ]
    for stage, action in actions:
        try:
            value = action()
            if stage == 'resource':
                report['resource'] = value.relative_to(root).as_posix() if value else None
            elif stage == 'index':
                report['index_status'] = value
        except Exception as error:
            # Do not echo exception text: it may contain an exact resource value.
            failures.append({'stage': stage, 'error_type': type(error).__name__})
            if stage == 'index':
                report['index_status'] = 'pending-refresh-failed'
    if failures:
        report.update(ok=False, recovery_required=True, recovery={
            'failures': failures,
            'action': 'retry the identical correction to repair projections; do not create another fact',
        })
    return report


def correct(root, old_id, statement, source, authority='user-stated', value_file=None):
    """Atomically correct a record. This function owns the workspace mutation lock.

    Returns saved=True even if post-publication projection recovery is needed.
    Raises a KBError for invalid inputs/conflicts before publishing. Exact values
    require value_file; the prior value is never carried into the new record.
    """
    root = Path(root).expanduser().resolve()
    if not isinstance(old_id, str) or not kb.ID_RE.fullmatch(old_id):
        raise kb.KBError('invalid old id')
    if not isinstance(statement, str) or not statement.strip():
        raise kb.KBError('correction statement is required')
    if not isinstance(source, str) or not source.strip():
        raise kb.KBError('correction source is required')
    if not isinstance(authority, str) or authority not in AUTHORITIES:
        raise kb.KBError('correction requires user-stated or verified authority')
    with workspace_lock.acquire(root, blocking=False):
        kb.require_writable_schema(root)
        # Do not follow symlinks to publish outside the selected knowledge store.
        for path in (root / 'ledger', root / 'ledger/by-id'):
            if path.is_symlink():
                raise kb.KBError('correction ledger directory is a symlink')
        records = _ledger_records(root)
        old = _old_record(root, old_id, records)
        needs_value = kb.has_value_field(old) or kb.event_requires_credential_value(old)
        if needs_value and value_file is None:
            raise kb.KBError('exact value correction requires an explicit value_file')
        event = {
            'schema_version': kb.SCHEMA_VERSION,
            'kind': old['kind'], 'scope': old['scope'],
            'statement': statement.strip(), 'source': source.strip(),
            'authority': authority, 'status': 'active', 'correction_of': old_id,
        }
        if old.get('fact_key'):
            event['fact_key'] = old['fact_key']
        if value_file is not None:
            try:
                with Path(value_file).expanduser().open('r', encoding='utf-8', newline='') as handle:
                    event['value'] = handle.read()
            except (OSError, UnicodeError) as error:
                raise kb.KBError('cannot read correction value_file') from error
        if kb.credential_value_missing(event):
            raise kb.KBError('credential-value-missing: supply an exact value_file')
        operation = kb.sha256_text(kb.stable_json({
            'format': 'atomic-correction-v1', 'old_id': old_id,
            'old_fingerprint': kb.event_fingerprint(old), 'replacement': event,
        }))
        event['id'] = 'corr-' + operation
        now, device = kb.iso_now(), kb.configured_device_id(root)
        event.update(created_at=now, source_device_id=device)
        edge = {
            'schema_version': kb.SCHEMA_VERSION, 'id': 'sup-' + operation,
            'kind': 'supersession', 'scope': old['scope'],
            'statement': old_id + ' is superseded by ' + event['id'],
            'source': source.strip(), 'authority': authority, 'status': 'active',
            'created_at': now, 'source_device_id': device,
            'old_id': old_id, 'new_id': event['id'],
        }
        pair = [event, edge]
        for row in pair:
            errors = kb.event_validation_errors(row)
            if errors:
                raise kb.KBError('invalid correction event: ' + ', '.join(errors))
        selected, _ = kb.select_lifecycle_events(records)
        direct = {key: decision['target'] for key, decision in selected.items()}
        kb.resolve_supersession_edges(direct)  # Reject pre-existing lifecycle cycles.
        if old_id in direct and direct[old_id] != event['id']:
            raise kb.ConflictError('record already corrected; correct its current replacement')
        path = root / 'ledger/by-id' / (event['id'] + '.json')
        if path.is_symlink():
            raise kb.ConflictError('correction path is a symlink')
        if path.exists():
            saved = _read_pair(path, pair)
            if direct.get(old_id) != event['id']:
                raise kb.ConflictError('existing correction relationship is inconsistent')
            return _project(root, path, saved, duplicate=True)
        if old_id in direct:
            raise kb.ConflictError('replacement exists without the expected atomic correction file')
        if any(row['id'] in (event['id'], edge['id']) for _, row, _ in records):
            raise kb.ConflictError('correction id already exists in another ledger file')
        for record_id in (event['id'], edge['id']):
            if kb.resource_matches(root, record_id):
                raise kb.ConflictError('correction id already exists as a resource')
        # A single publication exposes both the new fact and its correction edge.
        try:
            kb.atomic_json_create(path, pair)
        except Exception as error:
            if not path.exists():
                raise
            saved = _read_pair(path, pair)
            return _project(root, path, saved, duplicate=False,
                            publication_error=type(error).__name__)
        saved = _read_pair(path, pair)
        return _project(root, path, saved, duplicate=False)
