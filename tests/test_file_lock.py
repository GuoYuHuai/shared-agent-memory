import errno
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts" / "file_lock.py"
    spec = importlib.util.spec_from_file_location("file_lock_under_test", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeWindowsLocking:
    LK_NBLCK = 1
    LK_NBRLCK = 2
    LK_UNLCK = 3

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def locking(self, descriptor, mode, length):
        self.calls.append((descriptor, mode, length))
        if self.error is not None:
            raise self.error


class FileLockTests(unittest.TestCase):
    def test_current_platform_nonblocking_lock_detects_contention(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with path.open("a+") as first, path.open("a+") as second:
                module.acquire(first, exclusive=True, blocking=False)
                try:
                    with self.assertRaises(BlockingIOError):
                        module.acquire(second, exclusive=True, blocking=False)
                finally:
                    module.release(first)
                module.acquire(second, exclusive=True, blocking=False)
                module.release(second)

    def test_windows_backend_locks_one_real_byte_and_unlocks(self):
        module = load_module()
        backend = FakeWindowsLocking()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with path.open("a+") as handle:
                module._acquire_windows(
                    handle, exclusive=False, blocking=False, backend=backend
                )
                module._release_windows(handle, backend=backend)
            self.assertEqual(path.stat().st_size, 1)
        self.assertEqual(backend.calls[0][1:], (backend.LK_NBRLCK, 1))
        self.assertEqual(backend.calls[1][1:], (backend.LK_UNLCK, 1))

    def test_windows_nonblocking_contention_maps_to_blocking_io_error(self):
        module = load_module()
        backend = FakeWindowsLocking(OSError(errno.EACCES, "busy"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with path.open("a+") as handle:
                with self.assertRaises(BlockingIOError):
                    module._acquire_windows(
                        handle, exclusive=True, blocking=False, backend=backend
                    )


if __name__ == "__main__":
    unittest.main()
