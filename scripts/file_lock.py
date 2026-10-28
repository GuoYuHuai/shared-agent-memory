#!/usr/bin/env python3
"""Small cross-platform advisory file-lock primitive.

POSIX keeps the existing ``flock`` behavior.  Native Windows uses a one-byte
``msvcrt.locking`` region so importing the knowledge tools does not require the
Unix-only :mod:`fcntl` module.
"""

from __future__ import annotations

import errno
import os
import time
from typing import Any, TextIO


_WINDOWS_BUSY_ERRNOS = {
    errno.EACCES,
    errno.EAGAIN,
    getattr(errno, "EDEADLK", errno.EAGAIN),
    getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
}
_WINDOWS_BUSY_CODES = {33, 36}


def _ensure_lock_byte(handle: TextIO) -> None:
    """Ensure Windows has a real byte range to lock."""

    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write("\0")
        handle.flush()
    handle.seek(0)


def _is_windows_contention(error: OSError) -> bool:
    return bool(
        error.errno in _WINDOWS_BUSY_ERRNOS
        or getattr(error, "winerror", None) in _WINDOWS_BUSY_CODES
    )


def _acquire_windows(
    handle: TextIO,
    exclusive: bool,
    blocking: bool,
    backend: Any = None,
) -> None:
    if backend is None:
        import msvcrt as backend  # type: ignore[import-not-found]

    _ensure_lock_byte(handle)
    mode = backend.LK_NBLCK if exclusive else backend.LK_NBRLCK
    while True:
        handle.seek(0)
        try:
            backend.locking(handle.fileno(), mode, 1)
            return
        except OSError as error:
            if not _is_windows_contention(error):
                raise
            if not blocking:
                raise BlockingIOError(
                    errno.EAGAIN, "file lock is already held"
                ) from error
            time.sleep(0.05)


def _release_windows(handle: TextIO, backend: Any = None) -> None:
    if backend is None:
        import msvcrt as backend  # type: ignore[import-not-found]

    handle.seek(0)
    backend.locking(handle.fileno(), backend.LK_UNLCK, 1)


def _acquire_posix(handle: TextIO, exclusive: bool, blocking: bool) -> None:
    import fcntl

    flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        flags |= fcntl.LOCK_NB
    fcntl.flock(handle.fileno(), flags)


def _release_posix(handle: TextIO) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def acquire(handle: TextIO, exclusive: bool = True, blocking: bool = False) -> None:
    if os.name == "nt":
        _acquire_windows(handle, exclusive, blocking)
    else:
        _acquire_posix(handle, exclusive, blocking)


def release(handle: TextIO) -> None:
    if os.name == "nt":
        _release_windows(handle)
    else:
        _release_posix(handle)
