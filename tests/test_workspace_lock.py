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
            root = Path(directory) / "AI Knowledge"
            path = module.lock_path(root)
            path.parent.mkdir(parents=True)
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys\n"
                        "from pathlib import Path\n"
                        "sys.path.insert(0, sys.argv[1])\n"
                        "import workspace_lock\n"
                        "with workspace_lock.acquire(Path(sys.argv[2])):\n"
                        "    print('locked', flush=True)\n"
                        "    sys.stdin.read(1)\n"
                    ),
                    str(ROOT / "scripts"),
                    str(root),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                ready = holder.stdout.readline().strip()
                if ready != "locked":
                    _, errors = holder.communicate(timeout=5)
                    self.fail("lock holder did not start: %s" % errors)
                with self.assertRaises(module.WorkspaceBusy):
                    with module.acquire(root, blocking=False):
                        self.fail("contended lock must never be acquired")
            finally:
                try:
                    holder.communicate("x" if holder.poll() is None else None, timeout=5)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.communicate()
            self.assertEqual(holder.returncode, 0)
            with module.acquire(root, blocking=False):
                pass


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
