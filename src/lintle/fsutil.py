"""Durable atomic file commit (issue #58).

``durable_replace`` is the one place ``os.replace`` is wrapped with the fsyncs
that make a committed file survive a hard power loss, not just a clean process
exit. ``os.replace`` alone guarantees *atomicity* — a reader sees the old name
or the new one, never a half-written file — but not *durability*: the rename
can reach disk while the file's data has not. This helper closes that gap, and
is the sole sanctioned commit path so the guarantee holds uniformly. Pure
standard library.

On macOS, ``os.fsync`` flushes to the drive but not the drive's own write
cache, so it is *not* a true power-loss barrier; ``fcntl(fd, F_FULLFSYNC)`` is.
On Linux and other platforms ``os.fsync`` is the real barrier. We pick the
right one per platform at import time.
"""

import fcntl
import os
import sys

# True power-loss durability on macOS requires F_FULLFSYNC, not plain fsync
# (issue #58). Elsewhere os.fsync is the barrier.
_USE_FULLFSYNC = sys.platform == "darwin" and hasattr(fcntl, "F_FULLFSYNC")


def _fsync(fd):
    """Flush ``fd`` to stable storage, using the platform's true durability
    barrier (``F_FULLFSYNC`` on macOS, ``os.fsync`` otherwise).
    """
    if _USE_FULLFSYNC:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


def durable_replace(tmp, dest):
    """Atomically and durably commit ``tmp`` to ``dest``: flush ``tmp``'s data to
    stable storage, ``os.replace`` it onto ``dest``, then flush the containing
    directory so the rename itself is durable. Returns ``dest``.
    """
    fd = os.open(tmp, os.O_RDONLY)
    try:
        _fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dest)
    dir_fd = os.open(os.path.dirname(dest) or ".", os.O_RDONLY)
    try:
        _fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return dest
