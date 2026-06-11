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

``out_dir_lock`` is a host-aware exclusive lock written as a JSON sidecar
``".clean.lock"`` inside the out-dir. It prevents two concurrent ``clean`` runs
from corrupting a shared ``--out-dir``. Cross-host locks are never reclaimed;
same-host dead-PID locks are. Pure standard library.
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
    dir_fd = os.open(Path(dest).parent, os.O_RDONLY)
    try:
        _fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return dest


LOCK_NAME = ".clean.lock"


class LockHeldError(RuntimeError):
    """Raised when the out-dir lock is held by another live run."""


def _host_id():
    """Stable per-host identity for the lock. Hostname + boot id where available
    (Linux); hostname alone elsewhere. Lets reclaim be same-host-only so a dead
    PID on host A is never falsely reclaimed from host B (spec §3.3)."""
    host = socket.gethostname()
    try:
        with open("/proc/sys/kernel/random/boot_id") as h:
            return f"{host}:{h.read().strip()}"
    except OSError:
        return host


def _pid_alive(pid):
    """Return True if the process with ``pid`` exists on this host."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_json_or_none(path):
    """Open ``path`` as UTF-8 JSON and return its parsed value if it is a
    ``dict``; return ``None`` on any read or parse error (``OSError``,
    ``json.JSONDecodeError``, ``UnicodeDecodeError``) and also when the
    parsed value is not a dict (array, string, number, null). The dict guard
    means callers can index the result directly without an isinstance check."""
    try:
        with open(path, encoding="utf-8") as h:
            data = json.load(h)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_lock(path):
    """Read and parse the JSON lock file at ``path``; return None on any error.
    Delegates to :func:`read_json_or_none` so UnicodeDecodeError in a corrupt
    lock file is caught rather than propagated (issue #92)."""
    return read_json_or_none(path)


@contextlib.contextmanager
def out_dir_lock(out_dir, *, started="unknown"):
    """Exclusive, host-aware lock over ``out_dir`` for the duration of a run
    (spec §3.3). Refuses (LockHeldError) when held by a live process on this host
    or by any process on a different host. Reclaims only a same-host dead-PID
    lock. ``started`` is an ISO timestamp passed in by the caller (no clock here)."""
    path = Path(out_dir) / LOCK_NAME
    host = _host_id()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = _read_lock(path)
            same_host_dead = (
                holder
                and holder.get("host") == host
                and not _pid_alive(holder.get("pid", -1))
            )
            if same_host_dead:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)
                continue
            raise LockHeldError(
                f"another lintle clean is using {out_dir!r} "
                f"(held by {holder}); wait for it to finish"
            ) from None
        else:
            with os.fdopen(fd, "w") as h:
                json.dump({"host": host, "pid": os.getpid(), "started": started}, h)
            break
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
