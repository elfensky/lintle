"""Single-run resume: a durable per-run checkpoint for ``clean --resume`` (issue #56).

``clean`` writes a ``.clean-state.json`` to its ``--out-dir`` as files complete
and deletes it on full success, so the checkpoint's *presence* marks an
interrupted run. ``--resume`` consults it: validate (refuse on any change to the
lintle version or the input set's identity), skip files already committed, and
finish the job. The checkpoint is scoped to *completing one run* — not a
cross-run skip cache (contrast the rejected manifest, design §13). Pure standard
library.
"""

import contextlib
import hashlib
import json
import os

from lintle import __version__, fsutil

CHECKPOINT_NAME = ".clean-state.json"
SCHEMA_VERSION = 2
# Head+tail window hashed for input identity — large enough that any append
# (tail changes) or truncation (size changes) is caught in one seek, small
# enough to stay O(1) regardless of file size. A one-time correctness gate on
# resume, not a per-run skip cache (issue #56; contrast the rejected §13 manifest).
_HASH_WINDOW = 65536


def input_fingerprint(path):
    """Return a cheap identity for ``path``: size, integer ``mtime_ns``,
    ``ctime_ns``, inode number, and SHA-256 of its first and last 64 KB.
    Integer nanosecond timestamps avoid JSON round-trip precision loss and
    cross-filesystem granularity skew. ``ctime_ns`` + inode catch
    metadata-preserving copies (``cp -p``, ``rsync -t``, ``touch -r``) and
    replace-by-rename; residual: an interior edit that also preserves
    size+mtime+ctime+inode is not detected (spec §3.5/§7). Files at or below
    the window hash their whole content for both windows. Constant memory —
    the interior is never read.
    """
    st = os.stat(path)
    with open(path, "rb") as handle:
        head = handle.read(_HASH_WINDOW)
        if st.st_size > _HASH_WINDOW:
            handle.seek(-_HASH_WINDOW, os.SEEK_END)
            tail = handle.read(_HASH_WINDOW)
        else:
            tail = head
    return {
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "inode": st.st_ino,
        "head_sha256": hashlib.sha256(head).hexdigest(),
        "tail_sha256": hashlib.sha256(tail).hexdigest(),
    }


def build_checkpoint(*, inputs, completed, run_identity):
    """Assemble the checkpoint payload, pinning schema, lintle version, and the
    run identity (spec §3.1). ``inputs`` maps each discovered input path to its
    :func:`input_fingerprint`; ``completed`` maps each fully-processed path to a
    ``{"summary": summary_dict, "outputs": {name: size}}`` record (output sizes
    back the integrity re-verification of a resumed run). ``run_identity`` pins
    output-affecting configuration beyond version+inputs.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "lintle_version": __version__,
        "run_identity": run_identity,
        "inputs": inputs,
        "completed": completed,
    }


def _checkpoint_path(out_dir):
    return os.path.join(out_dir, CHECKPOINT_NAME)


def write_checkpoint(out_dir, checkpoint):
    """Write ``checkpoint`` to ``<out_dir>/.clean-state.json`` atomically and
    durably via a ``.partial`` temp + :func:`fsutil.durable_replace`, so a
    reader — or a crash mid-write — never sees a half-written file, and the
    committed checkpoint survives a hard power loss (issue #58). Returns the
    destination path.
    """
    dest = _checkpoint_path(out_dir)
    tmp = dest + ".partial"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(checkpoint, handle, separators=(",", ":"), sort_keys=True)
    fsutil.durable_replace(tmp, dest)
    return dest


def load_checkpoint(out_dir):
    """Return the parsed checkpoint from ``out_dir``, or ``None`` if it is absent
    or unparseable. A corrupt checkpoint is treated as no checkpoint — the safe
    default is to redo work, never to resume against garbage.
    """
    try:
        with open(_checkpoint_path(out_dir), encoding="utf-8") as handle:
            return json.load(handle)
    except OSError, json.JSONDecodeError:
        return None


def delete_checkpoint(out_dir):
    """Remove the checkpoint if present; a no-op when absent. Called on a fully
    successful run so a completed run leaves no resumable state behind.
    """
    with contextlib.suppress(FileNotFoundError):
        os.remove(_checkpoint_path(out_dir))


def validate_run_identity(checkpoint, current_inputs, current_run_identity):
    """Return a human-readable reason the checkpoint cannot be resumed against the
    current run, or None if it can. Refuse-on-change (spec §3.1, all-or-nothing):
    schema, lintle version, output-affecting configuration, or any input identity
    drift invalidates the whole checkpoint.
    """
    schema = checkpoint.get("schema_version")
    if schema != SCHEMA_VERSION:
        return (
            f"checkpoint schema_version {schema!r} is not supported "
            f"(this lintle writes schema {SCHEMA_VERSION})"
        )
    recorded_version = checkpoint.get("lintle_version")
    if recorded_version != __version__:
        return (
            f"lintle version changed since the interrupted run "
            f"({recorded_version} -> {__version__})"
        )
    if checkpoint.get("run_identity") != current_run_identity:
        return "run configuration changed since the interrupted run"
    recorded = checkpoint.get("inputs", {})
    added = sorted(set(current_inputs) - set(recorded))
    if added:
        return f"new input file(s) not in the interrupted run: {', '.join(added)}"
    removed = sorted(set(recorded) - set(current_inputs))
    if removed:
        return f"input file(s) missing since the interrupted run: {', '.join(removed)}"
    for path in sorted(current_inputs):
        if current_inputs[path] != recorded[path]:
            return f"input changed since the interrupted run: {path}"
    return None
