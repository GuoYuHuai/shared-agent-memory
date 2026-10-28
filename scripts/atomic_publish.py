"""Publish a completed file without replacing an existing destination.

Hard links remain the first choice. Filesystems without them need an operating
system primitive that also excludes replacement atomically; a separate exists
check followed by rename/replace cannot provide that guarantee.
"""
import ctypes
import errno
import os
from pathlib import Path
import sys
import tempfile


LINK_UNSUPPORTED = {errno.ENOSYS, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}


def rename_noreplace(source: Path, destination: Path) -> None:
    """Rename siblings exclusively, or fail without a weaker fallback."""
    if source.parent != destination.parent:
        raise ValueError("atomic publication requires sibling paths")
    if sys.platform == "win32":
        # Python's Windows rename never replaces an existing destination.
        # Sibling paths ensure this is a same-volume move, not a file copy.
        os.rename(source, destination)
        return

    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise OSError(errno.ENOTSUP,
                      "storage requires hard links or atomic no-replace rename",
                      str(destination))

    libc = ctypes.CDLL(None, use_errno=True)
    try:
        if sys.platform == "darwin":
            rename = libc.renamex_np
            rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
            arguments = (os.fsencode(source), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
        elif sys.platform.startswith("linux"):
            rename = libc.renameat2
            rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                               ctypes.c_char_p, ctypes.c_uint)
            arguments = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE
    except AttributeError as error:
        raise OSError(errno.ENOTSUP,
                      "storage requires hard links or atomic no-replace rename",
                      str(destination)) from error
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        code = ctypes.get_errno() or errno.EIO
        message = ("storage does not support atomic no-replace publication"
                   if code in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL}
                   else os.strerror(code))
        raise OSError(code, message, str(destination))


def publish_noreplace(source: Path, destination: Path) -> bool:
    """Return whether publication moved the source rather than linking it.

    The caller owns file and directory flushing and temporary-file cleanup.
    Both publication methods expose only the completed file and preserve every
    existing destination, including a dangling symlink.
    """
    try:
        os.link(source, destination)
        return False
    except OSError as error:
        if error.errno not in LINK_UNSUPPORTED:
            raise
    rename_noreplace(source, destination)
    return True


def probe_directory(directory: Path) -> dict:
    """Check create-only publication on this volume using disposable files.

    Call before installing into a selected directory. The caller must choose an
    existing directory on the target volume; this function touches only its own
    unique temporary subdirectory and never treats a filename check as a lock.
    This verifies publication semantics, not power-loss or physical-device safety.
    """
    directory = Path(directory)
    original = b'{"probe":"original","value":"complete\\r\\n"}\n'
    replacement = b'{"probe":"replacement"}\n'
    with tempfile.TemporaryDirectory(prefix='.ak-publication-probe-', dir=str(directory)) as temporary:
        probe = Path(temporary)
        source, destination = probe / 'pending', probe / 'published'

        def write(payload):
            with source.open('xb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

        write(original)
        moved = publish_noreplace(source, destination)
        if destination.read_bytes() != original:
            raise OSError(errno.EIO, 'storage publication readback did not match the complete file')
        if not moved:
            source.unlink()
        write(replacement)
        try:
            publish_noreplace(source, destination)
        except FileExistsError:
            if destination.read_bytes() != original or source.read_bytes() != replacement:
                raise OSError(errno.EIO, 'storage did not preserve an existing immutable record')
        else:
            raise OSError(errno.EIO, 'storage did not reject replacement of an immutable record')
    return {'ok': True, 'publication': 'exclusive-rename' if moved else 'hardlink',
            'overwrite_protection': 'verified'}
