"""Durable atomic file commit (issue #58) and host-aware out-dir lock (spec §3.3).

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

``out_dir_lock`` is an exclusive lock over the out-dir for the duration of a
run, held as an advisory ``fcntl.flock`` on a ``".clean.lock"`` sidecar. It
prevents two concurrent ``clean`` runs from corrupting a shared ``--out-dir``.
The kernel owns the lock's lifetime — it is released the instant the holder
closes its fd, exits, is killed, or the host reboots — so there is no stale-lock
reclaim to race (issue #87) and no PID-liveness or boot-id guesswork that could
wedge the directory after a crash (issue #99). POSIX-only (the project already
is); pure standard library.
"""

import contextlib
import fcntl
import json
import os
import socket
import sys
from pathlib import Path

# True power-loss durability on macOS requires F_FULLFSYNC, not plain fsync
# (issue #58). Elsewhere os.fsync is the barrier.
_USE_FULLFSYNC = sys.platform == "darwin" and hasattr(fcntl, "F_FULLFSYNC")

# Per-process flag: once F_FULLFSYNC proves unsupported (e.g. SMB/NFS/exFAT),
# skip straight to os.fsync for all subsequent calls (avoids repeated ENOTSUP
# syscalls). Starts as None — "untried" — so the first call always attempts
# F_FULLFSYNC on macOS and only latches False on failure (issue #98).
_fullfsync_works: bool | None = None


def _fsync(fd):
    """Flush ``fd`` to stable storage, using the platform's true durability
    barrier (``F_FULLFSYNC`` on macOS, ``os.fsync`` otherwise). On macOS,
    if ``F_FULLFSYNC`` raises ``OSError`` (e.g. SMB/NFS/exFAT volumes that
    don't implement it), fall back to ``os.fsync`` and remember the failure so
    subsequent calls skip the unsupported syscall (issue #98).
    """
    global _fullfsync_works
    if _USE_FULLFSYNC and _fullfsync_works is not False:
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            _fullfsync_works = True
            return
        except OSError:
            _fullfsync_works = False
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
    dir_fd = os.open(Path(dest).parent, os.O_RDONLY)
    try:
        _fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return dest


def durable_write_text(path, text, *, encoding="utf-8"):
    """Write ``text`` to ``path`` atomically, durably, and with pinned LF
    line endings (``newline="\\n"``), so the artifact is byte-identical across
    platforms (Critical Rules #1/#2). Owns the ``.partial`` suffix, the open,
    the write, and the :func:`durable_replace` call, deduplicating the
    boilerplate from bounded text-mode writers (issue #85). NOT for streaming
    writers, which cannot buffer their whole output as a single string.
    """
    tmp = path + ".partial"
    with open(tmp, "w", encoding=encoding, newline="\n") as handle:
        handle.write(text)
    durable_replace(tmp, path)


LOCK_NAME = ".clean.lock"


class LockHeldError(RuntimeError):
    """Raised when the out-dir lock is held by another live run."""


def _host_id():
    """Hostname, recorded as informational holder identity in the lock file.

    Used only to make a ``LockHeldError`` message legible — exclusion itself is
    the kernel's ``flock``, so this carries no boot-id or PID-liveness component
    (those were the source of the reboot-wedge and PID-reuse hostage, #99) and
    deliberately stays stable across reboots."""
    return socket.gethostname()


def read_json_or_none(path):
    """Open ``path`` as UTF-8 JSON and return its parsed value if it is a
    ``dict``; return ``None`` on any read or parse error (``OSError``,
    ``json.JSONDecodeError``, ``UnicodeDecodeError``) and also when the
    parsed value is not a dict (array, string, number, null). The dict guard
    means callers can index the result directly without an isinstance check."""
    try:
        with open(path, encoding="utf-8") as h:
            data = json.load(h)
    except OSError, json.JSONDecodeError, UnicodeDecodeError:
        return None
    return data if isinstance(data, dict) else None


@contextlib.contextmanager
def out_dir_lock(out_dir, *, started="unknown"):
    """Exclusive lock over ``out_dir`` for the duration of a run (spec §3.3).

    Held as an advisory ``fcntl.flock`` on the ``.clean.lock`` sidecar. The
    kernel ties the lock to the open fd: it is released automatically when this
    process closes the fd, exits, is killed, or the host reboots — so a live
    holder is detected by the kernel (no PID-liveness or boot-id guesswork, #99)
    and a dead holder's lock is freed without any reclaim step to race (#87).
    Raises :class:`LockHeldError` when another live run already holds it.

    The lock file is deliberately never unlinked. ``flock`` binds to the inode,
    not the path; unlinking a locked file would let a concurrent opener take the
    lock on the now-orphaned inode while a fresh file is created in its place,
    silently breaking mutual exclusion. Release is therefore the bare
    ``os.close`` of *our* fd — a run can only ever drop its own lock, never a
    successor's (the blind-release cascade of #87 is gone by construction). The
    leftover file is empty-of-meaning and reused next run; ``run_planning``
    already treats it as scrub noise.

    Concurrent runs on a single host are fully serialized. A shared out-dir
    written from multiple hosts over a network filesystem relies on ``flock``
    propagating server-side (modern NFSv4) and is not a tested configuration —
    give each host its own ``--out-dir``. ``started`` is an ISO timestamp
    recorded as informational holder metadata (no clock here)."""
    path = Path(out_dir) / LOCK_NAME
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = read_json_or_none(path)  # best-effort, for the message only
            raise LockHeldError(
                f"another lintle clean is using {out_dir!r} "
                f"(held by {holder}; lock file {os.fspath(path)!r}); wait for it "
                f"to finish, or remove that file if no run is active"
            ) from None
        # We hold the lock — record informational holder identity for any peer
        # that finds itself blocked on this live hold.
        os.ftruncate(fd, 0)
        os.write(
            fd,
            json.dumps(
                {"host": _host_id(), "pid": os.getpid(), "started": started}
            ).encode("utf-8"),
        )
        yield
    finally:
        os.close(fd)  # releases the advisory flock; the file is intentionally kept
