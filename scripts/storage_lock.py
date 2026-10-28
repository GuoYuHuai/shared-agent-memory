#!/usr/bin/env python3
"""A repository-local gate preventing cold-data jobs from racing migration."""

from contextlib import contextmanager
from pathlib import Path
import sys
from typing import Iterator, TextIO

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import file_lock


class StorageBusy(RuntimeError):
    pass


@contextmanager
def acquire(root: Path, exclusive: bool, blocking: bool = False) -> Iterator[TextIO]:
    path = root / "generated" / "storage-operation.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    acquired = False
    try:
        try:
            file_lock.acquire(handle, exclusive=exclusive, blocking=blocking)
            acquired = True
        except BlockingIOError as error:
            raise StorageBusy("storage-operation-already-running") from error
        yield handle
    finally:
        if acquired:
            file_lock.release(handle)
        handle.close()
