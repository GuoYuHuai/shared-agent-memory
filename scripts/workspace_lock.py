#!/usr/bin/env python3
"""Serialize cooperative mutations of the repository and canonical knowledge."""

from contextlib import contextmanager
from pathlib import Path
import sys
from typing import Iterator, TextIO

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import file_lock


class WorkspaceBusy(RuntimeError):
    pass


def lock_path(root: Path) -> Path:
    """Keep the host-local lock outside Git's index and outside cold storage."""

    return root / "generated" / "workspace-mutation.lock"


@contextmanager
def acquire(root: Path, blocking: bool = False) -> Iterator[TextIO]:
    # The installer creates a knowledge root explicitly. Runtime locking must
    # never recreate a removed directory or an unplugged portable checkout.
    if not root.is_dir():
        raise FileNotFoundError("knowledge-root-unavailable: %s" % root)
    path = lock_path(root)
    # No parents=True: if the root disappears after the check, fail rather
    # than rebuilding the missing root/volume path on the internal disk.
    path.parent.mkdir(exist_ok=True)
    handle = path.open("a+")
    acquired = False
    try:
        try:
            file_lock.acquire(handle, exclusive=True, blocking=blocking)
            acquired = True
        except BlockingIOError as error:
            raise WorkspaceBusy("workspace-mutation-already-running") from error
        yield handle
    finally:
        if acquired:
            file_lock.release(handle)
        handle.close()
