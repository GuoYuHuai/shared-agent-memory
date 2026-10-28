import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(script="workspace_lock"):
    path = ROOT / "scripts" / (script + ".py")
    name = "workspace_lock_test_%s" % uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkspaceLockTests(unittest.TestCase):
    def test_missing_root_is_not_created(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'missing-drive' / 'knowledge'
            with self.assertRaisesRegex(FileNotFoundError, 'knowledge-root-unavailable'):
                with module.acquire(root):
                    self.fail('must not acquire')
            self.assertFalse(root.parent.exists())

    def test_root_disappearing_after_precheck_is_not_recreated(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'knowledge'
            root.mkdir()
            original = Path.is_dir
            def disappears(path):
                result = original(path)
                if path == root:
                    root.rmdir()
                return result
            with mock.patch.object(Path, 'is_dir', disappears):
                with self.assertRaises(FileNotFoundError):
                    with module.acquire(root):
                        self.fail('must not acquire')
            self.assertFalse(root.exists())

    def test_existing_root_can_create_its_lock_directory(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with module.acquire(root) as handle:
                self.assertFalse(handle.closed)
                self.assertTrue(module.lock_path(root).is_file())
            self.assertTrue(handle.closed)

    def test_second_process_fails_fast_while_workspace_is_held(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "AI-Knowledge"
            path = module.lock_path(root)
            path.parent.mkdir(parents=True)
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import fcntl,sys; "
                        "h=open(sys.argv[1],'a+'); "
                        "fcntl.flock(h.fileno(),fcntl.LOCK_EX); "
                        "print('locked',flush=True); sys.stdin.read(1)"
                    ),
                    str(path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                with self.assertRaises(module.WorkspaceBusy):
                    with module.acquire(root, blocking=False):
                        self.fail("contended lock must never be acquired")
            finally:
                if holder.stdin:
                    holder.stdin.write("x")
                    holder.stdin.flush()
                holder.wait(timeout=5)
                for stream in (holder.stdin, holder.stdout, holder.stderr):
                    if stream:
                        stream.close()


class StorageLockTests(unittest.TestCase):
    def test_missing_root_is_not_created(self):
        module = load_module('storage_lock')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'missing-drive' / 'knowledge'
            with self.assertRaisesRegex(FileNotFoundError, 'knowledge-root-unavailable'):
                with module.acquire(root, exclusive=True):
                    self.fail('must not acquire')
            self.assertFalse(root.parent.exists())

    def test_existing_root_acquires_and_releases_storage_lock(self):
        module = load_module('storage_lock')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with module.acquire(root, exclusive=True) as handle:
                self.assertFalse(handle.closed)
            self.assertTrue(handle.closed)

    def test_root_disappearing_during_lock_setup_is_not_recreated(self):
        module = load_module('storage_lock')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'knowledge'
            root.mkdir()
            original = Path.is_dir
            def disappears(path):
                result = original(path)
                if path == root:
                    root.rmdir()
                return result
            with mock.patch.object(Path, 'is_dir', disappears):
                with self.assertRaises(FileNotFoundError):
                    with module.acquire(root, exclusive=True):
                        self.fail('must not acquire')
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
