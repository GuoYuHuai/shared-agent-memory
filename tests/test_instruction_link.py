import json
from contextlib import contextmanager
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import device_identity
import install
import instruction_link as link


class InstructionLinkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='knowledge-instruction-test-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'fictional knowledge'
        self.project = self.base / 'fictional project'
        self.project.mkdir()
        self.target = self.project / 'any-host-rules.md'
        for name, value in (('resolve_device_id', 'fictional-device'), ('platform_fingerprint', 'a' * 64)):
            patcher = mock.patch.object(device_identity, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        install.install(self.root, None, 'local')

    def receipt(self, root=None, target=None):
        return link._receipt_path(root or self.root, target or self.target)

    def interrupt_disconnect(self, after_removal=False):
        actual_write, actual_save = link._atomic_write, link._save_receipt
        def interrupted_write(path, content, expected=None):
            if path == self.target:
                raise OSError('interruption before removing block')
            actual_write(path, content, expected)
        def interrupted_save(path, receipt):
            if receipt['connection'] == 'disconnected':
                raise OSError('interruption after removing block')
            actual_save(path, receipt)
        patcher = mock.patch.object(link, '_save_receipt', side_effect=interrupted_save) if after_removal else \
            mock.patch.object(link, '_atomic_write', side_effect=interrupted_write)
        with patcher:
            return link.disconnect(self.root, self.target)

    def test_new_arbitrary_instruction_file_has_paths_routes_not_private_values(self):
        (self.root / 'POLICY.md').write_text('Fictional private value: DO-NOT-COPY-THIS-VALUE')
        connected = link.connect(self.root, self.target)
        self.assertTrue(connected['ok'], connected)
        self.assertEqual(connected['connection'], 'connected')
        self.assertEqual(connected['persistent_activation'], 'not-verified')
        text = self.target.read_text()
        self.assertIn(str(self.root), text)
        self.assertIn(str(ROOT), text)
        for value in ('POLICY.md', 'scripts/ak.py', 'recall QUERY', 'remember STATEMENT', 'correct OLD_ID', 'sync', 'hot_sync', '--value-file'):
            self.assertIn(value, text)
        self.assertNotIn('DO-NOT-COPY-THIS-VALUE', text)
        self.assertNotIn('DO-NOT-COPY-THIS-VALUE', self.receipt().read_text())
        status = link.status(self.root, self.target)
        self.assertTrue(status['receipt_owner_verified'])
        self.assertEqual(status['persistent_activation'], 'not-verified')

    def test_existing_crlf_and_later_user_text_survive_disconnect_exactly(self):
        original = b'# Other project instructions\r\nKeep these exact.\r\n'
        self.target.write_bytes(original)
        self.assertTrue(link.connect(self.root, self.target)['ok'])
        later = b'\n## A later user addition\r\nKeep this too.\r\n'
        self.target.write_bytes(self.target.read_bytes() + later)
        result = link.disconnect(self.root, self.target)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.target.read_bytes(), original + later)
        self.assertEqual(link.status(self.root, self.target)['connection'], 'disconnected')
        self.assertEqual(link.connections(self.root), [])

    def test_repeat_connect_is_byte_idempotent(self):
        self.target.write_text('Existing text without final newline')
        first = link.connect(self.root, self.target)
        before = self.target.read_bytes(), self.receipt().read_bytes()
        second = link.connect(self.root, self.target)
        self.assertTrue(first['ok'] and second['ok'])
        self.assertTrue(second['repeated'])
        self.assertEqual(before, (self.target.read_bytes(), self.receipt().read_bytes()))
        self.assertEqual(self.target.read_text().count(link.BEGIN), 1)

    def test_same_path_upgrade_refreshes_only_owned_block_and_repeat_is_idempotent(self):
        original = b'# Keep the existing rules\r\n'
        later = b'\nUser additions after connection\r\n'
        self.target.write_bytes(original)
        link.connect(self.root, self.target)
        self.target.write_bytes(self.target.read_bytes() + later)
        old = json.loads(self.receipt().read_text())['managed_block']
        upgraded = old.replace('recall QUERY', 'recall UPDATED_QUERY')
        with mock.patch.object(link, '_block', return_value=upgraded):
            result = link.connect(self.root, self.target)
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['updated'])
            receipt = json.loads(self.receipt().read_text())
            self.assertEqual(receipt['managed_block'], upgraded)
            self.assertEqual(receipt['block_hash'], link.digest(upgraded.encode()))
            self.assertNotIn('pending_update', receipt)
            before = self.target.read_bytes(), self.receipt().read_bytes()
            self.assertTrue(link.connect(self.root, self.target)['repeated'])
            self.assertEqual(before, (self.target.read_bytes(), self.receipt().read_bytes()))
        self.assertEqual(self.target.read_text().count(link.BEGIN), 1)
        self.assertTrue(link.disconnect(self.root, self.target)['ok'])
        self.assertEqual(self.target.read_bytes(), original + later)

    def test_upgrade_refuses_user_edits_inside_the_previous_block(self):
        link.connect(self.root, self.target)
        old = json.loads(self.receipt().read_text())['managed_block']
        self.target.write_text(self.target.read_text().replace('recall QUERY', 'custom command'))
        before = self.target.read_bytes(), self.receipt().read_bytes()
        with mock.patch.object(link, '_block', return_value=old.replace('recall QUERY', 'recall NEW_QUERY')):
            self.assertEqual(link.connect(self.root, self.target)['reason'], 'managed-block-edited')
        self.assertEqual(before, (self.target.read_bytes(), self.receipt().read_bytes()))

    def test_upgrade_interrupted_before_file_write_recovers_or_rolls_back(self):
        original = b'User rules\r\n'
        self.target.write_bytes(original)
        link.connect(self.root, self.target)
        old = json.loads(self.receipt().read_text())['managed_block']
        upgraded = old.replace('recall QUERY', 'recall NEW_QUERY')
        actual_write = link._atomic_write
        def interrupted(path, content, expected=None):
            if path == self.target:
                raise OSError('interruption before target replacement')
            actual_write(path, content, expected)
        for requested in (upgraded, old):
            with self.subTest(retry_block='upgraded' if requested == upgraded else 'rollback'):
                with mock.patch.object(link, '_block', return_value=upgraded), \
                        mock.patch.object(link, '_atomic_write', side_effect=interrupted):
                    self.assertFalse(link.connect(self.root, self.target)['ok'])
                receipt = json.loads(self.receipt().read_text())
                self.assertEqual(receipt['connection'], 'pending')
                self.assertEqual(receipt['pending_update']['managed_block'], old)
                self.assertIn(old.encode(), self.target.read_bytes())
                with mock.patch.object(link, '_block', return_value=requested):
                    self.assertTrue(link.connect(self.root, self.target)['ok'])
                self.assertEqual(json.loads(self.receipt().read_text())['managed_block'], requested)
                self.assertTrue(link.status(self.root, self.target)['ok'])
                with mock.patch.object(link, '_block', return_value=old):
                    self.assertTrue(link.connect(self.root, self.target)['ok'])
        self.assertTrue(link.disconnect(self.root, self.target)['ok'])
        self.assertEqual(self.target.read_bytes(), original)

    def test_upgrade_interrupted_after_file_write_finalizes_without_duplicate(self):
        link.connect(self.root, self.target)
        old = json.loads(self.receipt().read_text())['managed_block']
        upgraded = old.replace('recall QUERY', 'recall NEW_QUERY')
        actual_save = link._save_receipt
        def interrupted(path, receipt):
            if receipt['connection'] == 'connected':
                raise OSError('interruption during receipt finalization')
            actual_save(path, receipt)
        with mock.patch.object(link, '_block', return_value=upgraded):
            with mock.patch.object(link, '_save_receipt', side_effect=interrupted):
                self.assertFalse(link.connect(self.root, self.target)['ok'])
            before = self.target.read_bytes()
            self.assertIn(upgraded.encode(), before)
            self.assertEqual(json.loads(self.receipt().read_text())['connection'], 'pending')
            result = link.connect(self.root, self.target)
            self.assertTrue(result['ok'], result)
            self.assertEqual(self.target.read_bytes(), before)
            self.assertEqual(self.target.read_text().count(link.BEGIN), 1)
            self.assertNotIn('pending_update', json.loads(self.receipt().read_text()))

    def test_tool_relocation_requires_old_tool_disconnect_without_changing_files(self):
        self.target.write_text('Keep project instructions')
        link.connect(self.root, self.target)
        paths = [self.root / relative for relative in
                 ('.ai-knowledge/install.json', 'MANIFEST.json', 'config/kb.json', 'AGENTS.md')]
        paths += [self.target, self.receipt()]
        before = [path.read_bytes() for path in paths]
        changed_tool = self.base / 'new tool directory'
        with mock.patch.object(install, 'TOOL_ROOT', changed_tool):
            with self.assertRaisesRegex(ValueError, 'disconnect instruction links with the previous tool directory'):
                install.install(self.root, None, 'local')
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertFalse((self.root / '.ai-knowledge/backups').exists())
        self.assertTrue(link.disconnect(self.root, self.target)['ok'])
        with mock.patch.object(install, 'TOOL_ROOT', changed_tool), mock.patch.object(link, 'TOOL_ROOT', changed_tool):
            self.assertTrue(install.install(self.root, None, 'local')['ok'])
            self.assertTrue(link.connect(self.root, self.target)['ok'])
            self.assertIn(str(changed_tool), self.target.read_text())

    def test_same_tool_installation_keeps_active_link_and_new_devices_can_rebind(self):
        link.connect(self.root, self.target)
        before = self.target.read_bytes(), self.receipt().read_bytes()
        self.assertTrue(install.install(self.root, None, 'local')['ok'])
        self.assertEqual(before, (self.target.read_bytes(), self.receipt().read_bytes()))
        self.assertTrue(link.status(self.root, self.target)['ok'])
        changed_tool = self.base / 'new device tool'
        for field, value in (('resolve_device_id', 'another-device'), ('platform_fingerprint', 'c' * 64)):
            with self.subTest(different=field), mock.patch.object(device_identity, field, return_value=value), \
                    mock.patch.object(install, 'TOOL_ROOT', changed_tool), mock.patch.object(link, 'TOOL_ROOT', changed_tool):
                self.assertTrue(install.install(self.root, None, 'local')['ok'])
                status = link.status(self.root, self.target)
                self.assertFalse(status['ok'])
                self.assertEqual(status['connection'], 'unverified')
                self.assertEqual(status['persistent_activation'], 'not-verified')
        self.assertEqual(before, (self.target.read_bytes(), self.receipt().read_bytes()))

    def test_tool_relocation_rechecks_receipts_inside_workspace_lock(self):
        actual_acquire = install.workspace_lock.acquire
        observations = []
        paths = [self.root / relative for relative in
                 ('.ai-knowledge/install.json', 'MANIFEST.json', 'config/kb.json', 'AGENTS.md')]
        @contextmanager
        def acquire_with_concurrent_connection(root, *args, **kwargs):
            with mock.patch.object(install.workspace_lock, 'acquire', actual_acquire):
                self.assertTrue(link.connect(self.root, self.target)['ok'])
            observations.append([path.read_bytes() for path in paths] + [self.target.read_bytes(), self.receipt().read_bytes()])
            with actual_acquire(root, *args, **kwargs):
                yield
        with mock.patch.object(install, 'TOOL_ROOT', self.base / 'another tool'), \
                mock.patch.object(install.workspace_lock, 'acquire', side_effect=acquire_with_concurrent_connection):
            with self.assertRaisesRegex(ValueError, 'disconnect instruction links'):
                install.install(self.root, None, 'local')
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0], [path.read_bytes() for path in paths] + [self.target.read_bytes(), self.receipt().read_bytes()])
        self.assertFalse((self.root / '.ai-knowledge/backups').exists())

    def test_repeated_disconnect_preserves_new_user_text(self):
        link.connect(self.root, self.target)
        self.assertTrue(link.disconnect(self.root, self.target)['ok'])
        self.target.write_text('New unrelated instructions')
        self.assertTrue(link.disconnect(self.root, self.target)['repeated'])
        self.assertEqual(self.target.read_text(), 'New unrelated instructions')
        self.assertTrue(link.connect(self.root, self.target)['ok'])

    def test_disconnect_interrupted_before_removal_has_durable_intent_and_retries(self):
        original = b'Keep these personal project rules exactly.\r\n'
        self.target.write_bytes(original)
        link.connect(self.root, self.target)
        before = self.target.read_bytes()
        self.assertFalse(self.interrupt_disconnect()['ok'])
        receipt = json.loads(self.receipt().read_text())
        self.assertEqual(receipt['connection'], 'pending')
        self.assertEqual(receipt['pending_disconnect']['before_hash'], link.digest(before))
        self.assertEqual(receipt['pending_disconnect']['target_hash'], link.digest(original))
        self.assertNotIn(original.decode().strip(), self.receipt().read_text())
        self.assertEqual(self.target.read_bytes(), before)
        for action in (link.status, link.connect):
            result = action(self.root, self.target)
            self.assertFalse(result['ok'])
            self.assertEqual(result['connection'], 'pending-disconnect')
            self.assertEqual(result['disconnect_phase'], 'block-present')
        self.assertEqual(self.target.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'disconnect connected instruction files'):
            install.uninstall(self.root)
        with mock.patch.object(install, 'TOOL_ROOT', self.base / 'new tool'):
            with self.assertRaisesRegex(ValueError, 'disconnect instruction links'):
                install.install(self.root, None, 'local')
        result = link.disconnect(self.root, self.target)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['recovered'])
        self.assertEqual(self.target.read_bytes(), original)
        self.assertNotIn('pending_disconnect', json.loads(self.receipt().read_text()))
        self.assertTrue(link.disconnect(self.root, self.target)['repeated'])

    def test_disconnect_interrupted_after_removal_finalizes_without_rewriting_target(self):
        original = b'Original rules\r\n'
        self.target.write_bytes(original)
        link.connect(self.root, self.target)
        self.assertFalse(self.interrupt_disconnect(after_removal=True)['ok'])
        self.assertEqual(self.target.read_bytes(), original)
        result = link.status(self.root, self.target)
        self.assertFalse(result['ok'])
        self.assertEqual(result['disconnect_phase'], 'block-removed')
        self.assertEqual(result['connection'], 'pending-disconnect')
        self.assertEqual(len(link.connections(self.root)), 1)
        with self.assertRaisesRegex(ValueError, 'disconnect connected instruction files'):
            install.uninstall(self.root)
        actual_write = link._atomic_write
        def receipt_writes_only(path, content, expected=None):
            self.assertNotEqual(path, self.target)
            actual_write(path, content, expected)
        with mock.patch.object(link, '_atomic_write', side_effect=receipt_writes_only):
            self.assertTrue(link.disconnect(self.root, self.target)['ok'])
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(link.connections(self.root), [])
        self.assertTrue(install.uninstall(self.root)['ok'])

    def test_disconnect_final_receipt_written_before_error_is_already_complete(self):
        self.target.write_text('Keep these rules')
        link.connect(self.root, self.target)
        actual_save = link._save_receipt
        def save_then_interrupt(path, receipt):
            actual_save(path, receipt)
            if receipt['connection'] == 'disconnected':
                raise OSError('interruption after final receipt publication')
        with mock.patch.object(link, '_save_receipt', side_effect=save_then_interrupt):
            self.assertFalse(link.disconnect(self.root, self.target)['ok'])
        self.assertEqual(link.status(self.root, self.target)['connection'], 'disconnected')
        self.assertTrue(link.disconnect(self.root, self.target)['repeated'])
        self.assertEqual(self.target.read_text(), 'Keep these rules')

    def test_pending_disconnect_refuses_edits_before_removal_without_overwriting(self):
        link.connect(self.root, self.target)
        self.assertFalse(self.interrupt_disconnect()['ok'])
        before = self.target.read_bytes()
        for changed, reason in ((before + b'\nNew outside instructions', 'pending-disconnect-content-changed'),
                                (before.replace(b'recall QUERY', b'custom recall'), 'managed-block-edited')):
            with self.subTest(reason=reason):
                self.target.write_bytes(changed)
                receipt = self.receipt().read_bytes()
                for action in (link.disconnect, link.connect, link.status):
                    result = action(self.root, self.target)
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['reason'], reason)
                    self.assertEqual(self.target.read_bytes(), changed)
                    self.assertEqual(self.receipt().read_bytes(), receipt)

    def test_pending_disconnect_refuses_edits_after_removal_without_claiming_completion(self):
        original = b'Original instructions\r\n'
        self.target.write_bytes(original)
        link.connect(self.root, self.target)
        self.assertFalse(self.interrupt_disconnect(after_removal=True)['ok'])
        changed = original + b'New user content after removal\r\n'
        self.target.write_bytes(changed)
        before_receipt = self.receipt().read_bytes()
        for action in (link.disconnect, link.connect, link.status):
            result = action(self.root, self.target)
            self.assertFalse(result['ok'])
            self.assertEqual(result['reason'], 'pending-disconnect-content-changed')
            self.assertEqual(self.target.read_bytes(), changed)
            self.assertEqual(self.receipt().read_bytes(), before_receipt)

    def test_concurrent_edit_during_disconnect_is_not_overwritten(self):
        self.target.write_text('Existing instructions')
        link.connect(self.root, self.target)
        before = self.target.read_bytes()
        changed = before + b'\nConcurrent user edit'
        actual_write = link._atomic_write
        def concurrent_write(path, content, expected=None):
            if path == self.target:
                self.target.write_bytes(changed)
            actual_write(path, content, expected)
        with mock.patch.object(link, '_atomic_write', side_effect=concurrent_write):
            result = link.disconnect(self.root, self.target)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'instruction-file-changed-during-write')
        self.assertEqual(self.target.read_bytes(), changed)
        self.assertIn('pending_disconnect', json.loads(self.receipt().read_text()))
        self.assertEqual(link.status(self.root, self.target)['reason'], 'pending-disconnect-content-changed')

    def test_modified_managed_block_stops_every_operation_with_recovery(self):
        link.connect(self.root, self.target)
        self.target.write_text(self.target.read_text().replace('recall QUERY', 'custom edited retrieval'))
        before = self.target.read_bytes(), self.receipt().read_bytes()
        for action in (link.connect, link.disconnect, link.status):
            result = action(self.root, self.target)
            self.assertFalse(result['ok'])
            self.assertEqual(result['reason'], 'managed-block-edited')
            self.assertIn('restore the exact managed_block', result['recovery'])
        self.assertEqual(before, (self.target.read_bytes(), self.receipt().read_bytes()))

    def test_user_can_restore_owned_block_then_disconnect_without_losing_edits(self):
        link.connect(self.root, self.target)
        receipt = json.loads(self.receipt().read_text())
        self.target.write_text('Custom instructions moved outside.\n' + receipt['managed_block'] + '\n')
        self.assertTrue(link.disconnect(self.root, self.target)['ok'])
        self.assertEqual(self.target.read_text(), 'Custom instructions moved outside.\n\n')

    def test_foreign_block_without_receipt_is_never_adopted(self):
        self.target.write_text(link.BEGIN + '\nForeign connection\n' + link.END)
        before = self.target.read_bytes()
        for action in (link.connect, link.disconnect, link.status):
            result = action(self.root, self.target)
            self.assertEqual(result['reason'], 'foreign-managed-block')
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(self.receipt().exists())

    def test_two_knowledge_roots_cannot_claim_one_instruction_file(self):
        other = self.base / 'another knowledge'
        install.install(other, None, 'local')
        self.assertTrue(link.connect(self.root, self.target)['ok'])
        before = self.target.read_bytes()
        result = link.connect(other, self.target)
        self.assertEqual(result['reason'], 'foreign-managed-block')
        self.assertEqual(self.target.read_bytes(), before)
        self.assertTrue(link.disconnect(self.root, self.target)['ok'])
        self.assertTrue(link.connect(other, self.target)['ok'])

    def test_duplicate_or_incomplete_markers_are_rejected(self):
        for text in (link.BEGIN, link.END, link.END + link.BEGIN, link.BEGIN * 2 + link.END):
            with self.subTest(text=text):
                self.target.write_text(text)
                self.assertEqual(link.connect(self.root, self.target)['reason'], 'ambiguous-instruction-markers')
                self.assertEqual(self.target.read_text(), text)

    def test_binary_invalid_encoding_and_directory_are_rejected(self):
        for content, reason in ((b'\0binary', 'instruction-file-not-text'), (b'\xff\xfe', 'instruction-file-not-utf8')):
            with self.subTest(reason=reason):
                self.target.write_bytes(content)
                self.assertEqual(link.connect(self.root, self.target)['reason'], reason)
                self.assertEqual(self.target.read_bytes(), content)
        self.target.unlink()
        self.target.mkdir()
        self.assertEqual(link.connect(self.root, self.target)['reason'], 'instruction-file-not-small-text')

    def test_symlink_target_and_parent_and_receipt_are_rejected(self):
        real = self.project / 'real.txt'
        real.write_text('Preserve the original')
        self.target.symlink_to(real)
        self.assertEqual(link.connect(self.root, self.target)['reason'], 'symlink-path')
        self.target.unlink()
        alias = self.base / 'linked project'
        alias.symlink_to(self.project, target_is_directory=True)
        self.assertEqual(link.connect(self.root, alias / 'instructions.md')['reason'], 'symlink-path')
        receipt_dir = self.root / '.ai-knowledge/instruction-links'
        receipt_dir.symlink_to(self.project, target_is_directory=True)
        self.assertEqual(link.connect(self.root, self.target)['reason'], 'symlink-path')
        self.assertEqual(real.read_text(), 'Preserve the original')

    def test_missing_root_and_project_parent_are_not_created(self):
        missing = self.base / 'missing knowledge'
        result = link.connect(missing, self.target)
        self.assertEqual(result['reason'], 'knowledge-root-unavailable')
        self.assertFalse(missing.exists())
        missing_target = self.base / 'missing project' / 'rules.txt'
        result = link.connect(self.root, missing_target)
        self.assertEqual(result['reason'], 'instructions-parent-unavailable')
        self.assertFalse(missing_target.parent.exists())

    def test_same_target_busy_is_retryable_without_overwrite(self):
        self.target.write_text('Keep this')
        lock = self.target.with_name('.' + self.target.name + '.agent-knowledge.lock')
        lock.write_text('another operation')
        self.assertEqual(link.connect(self.root, self.target)['reason'], 'instruction-file-busy')
        self.assertEqual(self.target.read_text(), 'Keep this')
        self.assertEqual(lock.read_text(), 'another operation')

    def test_interrupted_receipt_finalization_resumes_without_duplicate_block(self):
        actual_save = link._save_receipt
        calls = []
        def interrupted(path, receipt):
            calls.append(receipt['connection'])
            if len(calls) == 2:
                raise OSError('fictional interruption')
            actual_save(path, receipt)
        with mock.patch.object(link, '_save_receipt', side_effect=interrupted):
            self.assertFalse(link.connect(self.root, self.target)['ok'])
        self.assertEqual(json.loads(self.receipt().read_text())['connection'], 'pending')
        self.assertTrue(link.connect(self.root, self.target)['ok'])
        self.assertEqual(self.target.read_text().count(link.BEGIN), 1)
        self.assertEqual(json.loads(self.receipt().read_text())['connection'], 'connected')
        self.assertFalse(list(self.project.glob('*.lock')))
        self.assertFalse(list(self.project.glob('.*.tmp')))

    def test_uninstall_requires_disconnect_and_then_preserves_knowledge(self):
        knowledge = self.root / 'profile/fictional.md'
        knowledge.write_text('A fictional fact to preserve')
        link.connect(self.root, self.target)
        with self.assertRaisesRegex(ValueError, 'disconnect connected instruction files'):
            install.uninstall(self.root)
        self.assertTrue((self.root / '.ai-knowledge/install.json').exists())
        self.assertTrue(link.disconnect(self.root, self.target)['ok'])
        self.assertTrue(install.uninstall(self.root)['ok'])
        self.assertEqual(knowledge.read_text(), 'A fictional fact to preserve')

    def test_cross_device_never_inherits_connection_or_activation(self):
        link.connect(self.root, self.target)
        before = self.target.read_bytes()
        with mock.patch.object(device_identity, 'resolve_device_id', return_value='device-b'), mock.patch.object(device_identity, 'platform_fingerprint', return_value='b' * 64):
            self.assertFalse(link.status(self.root, self.target)['ok'])
            install.install(self.root, None, 'local')
            for action in (link.status, link.connect, link.disconnect):
                result = action(self.root, self.target)
                self.assertEqual(result['reason'], 'link-binding-changed')
                self.assertEqual(result['persistent_activation'], 'not-verified')
        self.assertEqual(self.target.read_bytes(), before)

    def test_moved_knowledge_root_cannot_claim_old_connection(self):
        link.connect(self.root, self.target)
        moved = self.base / 'moved knowledge'
        shutil.copytree(self.root, moved)
        install.install(moved, None, 'local')
        result = link.status(moved, self.target)
        self.assertEqual(result['reason'], 'link-binding-changed')
        self.assertEqual(result['persistent_activation'], 'not-verified')
        self.assertEqual(len(link.connections(moved)), 1)

    def test_changed_tool_path_does_not_reuse_activation(self):
        link.connect(self.root, self.target)
        with mock.patch.object(link, 'TOOL_ROOT', self.base / 'different tool'):
            result = link.status(self.root, self.target)
        self.assertEqual(result['reason'], 'installation-binding-changed')
        self.assertEqual(result['persistent_activation'], 'not-verified')

    def test_deleted_managed_block_is_not_silently_reinstalled(self):
        link.connect(self.root, self.target)
        self.target.write_text('User intentionally replaced the instructions')
        self.assertEqual(link.connect(self.root, self.target)['reason'], 'managed-block-missing')
        self.assertEqual(self.target.read_text(), 'User intentionally replaced the instructions')

    def test_unsupported_schema_and_uninstalled_root_cannot_connect(self):
        manifest_path = self.root / 'MANIFEST.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['schema_version'] = '2'
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual(link.connect(self.root, self.target)['reason'], 'unsupported-schema')
        (self.root / '.ai-knowledge/install.json').unlink()
        self.assertEqual(link.connect(self.root, self.target)['reason'], 'installation-required')


if __name__ == '__main__':
    unittest.main()
