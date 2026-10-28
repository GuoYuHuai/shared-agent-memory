"""Create-only publication without hard links; no filesystem mounting in CI."""
import errno
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
import atomic_publish
import kb


class AtomicPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='knowledge-atomic-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def unsupported_links(self):
        return mock.patch.object(atomic_publish.os, 'link',
                                 side_effect=OSError(errno.ENOTSUP, 'fixture: no hard links'))

    def test_no_hardlinks_publishes_complete_unicode_json(self):
        destination = self.root / '知识 库' / 'record.json'
        payload = {'id': 'fictional', 'value': 'exact\r\n中文\r\n' + 'x' * 50000}
        with self.unsupported_links():
            kb.atomic_json_create(destination, payload)
        self.assertEqual(json.loads(destination.read_text(encoding='utf-8')), payload)
        self.assertEqual(list(destination.parent.glob('*.tmp-*')), [])

    def test_no_hardlinks_preserves_existing_file_and_directory(self):
        for is_directory in (False, True):
            with self.subTest(is_directory=is_directory):
                destination = self.root / ('folder' if is_directory else 'record.json')
                if is_directory:
                    destination.mkdir()
                else:
                    destination.write_bytes(b'original exact bytes\r\n')
                with self.unsupported_links(), self.assertRaises((kb.ConflictError, OSError)):
                    kb.atomic_json_create(destination, {'id': 'replacement'})
                if is_directory:
                    self.assertTrue(destination.is_dir())
                else:
                    self.assertEqual(destination.read_bytes(), b'original exact bytes\r\n')
                self.assertEqual(list(self.root.glob('*.tmp-*')), [])

    def test_no_hardlinks_preserves_dangling_symlink(self):
        destination = self.root / 'link.json'
        try:
            destination.symlink_to(self.root / 'missing')
        except OSError as error:
            self.skipTest('host cannot create the symlink fixture: ' + type(error).__name__)
        with self.unsupported_links(), self.assertRaises((kb.ConflictError, OSError)):
            kb.atomic_json_create(destination, {'id': 'replacement'})
        self.assertTrue(destination.is_symlink())
        self.assertFalse(destination.exists())

    def test_no_hardlinks_durability_failure_is_not_success(self):
        destination = self.root / 'record.json'
        with self.unsupported_links(), mock.patch.object(kb, 'fsync_directory', side_effect=OSError('fixture flush failed')):
            with self.assertRaises(OSError):
                kb.atomic_json_create(destination, {'id': 'saved-before-flush'})
        self.assertEqual(json.loads(destination.read_text())['id'], 'saved-before-flush')
        self.assertEqual(list(self.root.glob('*.tmp-*')), [])

    def test_non_capability_link_failures_are_not_retried_as_rename(self):
        for number in (errno.EACCES, errno.EROFS, errno.ENOSPC, errno.EIO, errno.EXDEV, errno.EEXIST):
            with self.subTest(errno=number):
                with mock.patch.object(atomic_publish.os, 'link', side_effect=OSError(number, 'fixture')), mock.patch.object(atomic_publish, 'rename_noreplace') as rename:
                    with self.assertRaises(OSError) as caught:
                        atomic_publish.publish_noreplace(self.root / 'from', self.root / 'to')
                    self.assertEqual(caught.exception.errno, number)
                    rename.assert_not_called()

    def test_missing_native_api_fails_without_weaker_replace(self):
        source, destination = self.root / 'from', self.root / 'to'
        source.write_text('complete source', encoding='utf-8')
        with self.unsupported_links(), mock.patch.object(atomic_publish.sys, 'platform', 'unsupported-fixture'), mock.patch.object(atomic_publish.os, 'replace') as replace:
            with self.assertRaises(OSError) as caught:
                atomic_publish.publish_noreplace(source, destination)
            self.assertEqual(caught.exception.errno, errno.ENOTSUP)
            self.assertIn('atomic', str(caught.exception))
            replace.assert_not_called()
        self.assertEqual(source.read_text(), 'complete source')
        self.assertFalse(destination.exists())

    def test_native_unsupported_error_keeps_completed_source(self):
        source, destination = self.root / 'from', self.root / 'to'
        source.write_text('complete source', encoding='utf-8')
        library = mock.Mock()
        def unsupported(*args):
            atomic_publish.ctypes.set_errno(errno.ENOTSUP)
            return -1
        library.renamex_np = mock.Mock(side_effect=unsupported)
        with mock.patch.object(atomic_publish.sys, 'platform', 'darwin'), mock.patch.object(atomic_publish.ctypes, 'CDLL', return_value=library):
            with self.assertRaisesRegex(OSError, 'atomic no-replace'):
                atomic_publish.rename_noreplace(source, destination)
        self.assertEqual(source.read_text(), 'complete source')
        self.assertFalse(destination.exists())

    def test_probe_removes_only_its_files_and_checks_no_hardlink_storage(self):
        marker = self.root / 'existing-user-file'
        marker.write_bytes(b'existing exact bytes')
        with self.unsupported_links():
            report = atomic_publish.probe_directory(self.root)
        self.assertEqual(report, {'ok': True, 'publication': 'exclusive-rename',
                                  'overwrite_protection': 'verified'})
        self.assertEqual(list(self.root.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b'existing exact bytes')

    def test_probe_cleans_up_after_unsupported_filesystem(self):
        marker = self.root / 'existing-user-file'
        marker.write_text('preserve')
        with self.unsupported_links(), mock.patch.object(atomic_publish, 'rename_noreplace', side_effect=OSError(errno.ENOTSUP, 'storage unsupported')):
            with self.assertRaisesRegex(OSError, 'storage unsupported'):
                atomic_publish.probe_directory(self.root)
        self.assertEqual(list(self.root.iterdir()), [marker])
        self.assertEqual(marker.read_text(), 'preserve')

    def test_probe_rejects_a_backend_that_silently_overwrites(self):
        def unsafe(source, destination):
            os.replace(source, destination)
            return True
        with mock.patch.object(atomic_publish, 'publish_noreplace', side_effect=unsafe):
            with self.assertRaisesRegex(OSError, 'did not reject replacement'):
                atomic_publish.probe_directory(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_concurrent_no_hardlink_writers_have_exactly_one_complete_winner(self):
        destination = self.root / 'shared.json'
        code = '''
import errno,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import atomic_publish,kb
def unsupported(*args,**kwargs):
    raise OSError(errno.ENOTSUP,'fixture: no hard links')
atomic_publish.os.link=unsupported
try:
    kb.atomic_json_create(Path(sys.argv[2]),{'writer':sys.argv[3],'value':sys.argv[3]*30000})
except kb.ConflictError:
    raise SystemExit(1)
'''
        children = []
        try:
            for writer in range(8):
                children.append(subprocess.Popen([sys.executable, '-B', '-c', code, str(ROOT / 'scripts'), str(destination), str(writer)], stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            results = []
            for child in children:
                out, error = child.communicate(timeout=30)
                self.assertIn(child.returncode, (0, 1), error.decode(errors='replace'))
                results.append(child.returncode)
            self.assertEqual(results.count(0), 1)
            self.assertEqual(results.count(1), 7)
            payload = json.loads(destination.read_text())
            self.assertEqual(payload['value'], payload['writer'] * 30000)
            self.assertEqual(list(self.root.glob('*.tmp-*')), [])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()


if __name__ == '__main__':
    unittest.main()
