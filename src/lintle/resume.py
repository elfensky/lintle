"""Single-run resume: a durable per-run checkpoint for ``clean --resume`` (issue #56).

``clean`` writes a ``.clean-state.json`` to its ``--out-dir`` as files complete
and deletes it on full success, so the checkpoint's *presence* marks an
interrupted run. ``--resume`` consults it: validate (refuse on any change to the
lintle version or the input set's identity), skip files already committed, and
finish the job. The checkpoint is scoped to *completing one run* — not a
cross-run skip cache (contrast the declined manifest, design §13). Pure standard
library.
"""

import contextlib
import dataclasses
import datetime
import enum
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

from lintle import (
    BROKEN_DIRNAME,
    BROKEN_SUFFIX,
    CLEANED_DIRNAME,
    CLEANED_SUFFIX,
    FINDINGS_SUFFIX,
    SHARDS_DIRNAME,
    __version__,
    chunking,
    fsutil,
    stem,
)

CHECKPOINT_NAME = ".clean-state.json"
SCHEMA_VERSION = 4
# Head+tail window hashed for input identity — large enough that any append
# (tail changes) or truncation (size changes) is caught in one seek, small
# enough to stay O(1) regardless of file size. A one-time correctness gate on
# resume, not a per-run skip cache (issue #56; contrast the declined §13 manifest).
_HASH_WINDOW = 65536


def input_fingerprint(path: str) -> dict[str, int | str]:
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
    st = Path(path).stat()
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


def run_started_stamp() -> str:
    """ISO-8601 UTC timestamp for archive/lock naming. Isolated so the rest of the
    resume logic stays clock-free and testable."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def output_sizes(out_dir: str, stats) -> dict[str, int]:
    """Map each output *chunk* basename this file produced to its on-disk size,
    captured at completion for the resume integrity check (spec §3.6). The cleaned
    and broken streams are chunk sets (``<stem>.NNNNN.<suffix>``); every chunk is
    enumerated and recorded by its own basename, so the check validates the whole
    set — a truncated or missing chunk on resume forces reprocessing. Clean mode
    always writes at least one cleaned chunk and one broken-sidecar chunk (a
    header-only ``.00001`` when nothing is quarantined), so a non-validate run
    always records both sets. The findings shard in ``.shards/`` stays a single
    intermediate file and is recorded by name (issue #117). Suffix/dirname
    constants come from ``lintle.__init__`` — the single naming authority."""
    sizes = {}
    out = Path(out_dir)
    file_stem = stem(stats.src_name)
    for sub, suffix in (
        (CLEANED_DIRNAME, CLEANED_SUFFIX),
        (BROKEN_DIRNAME, BROKEN_SUFFIX),
    ):
        reader = chunking.ChunkedReader(out / sub, file_stem, suffix)
        for chunk in reader.chunk_paths():
            with contextlib.suppress(OSError):
                sizes[chunk.name] = chunk.stat().st_size
    shard = file_stem + FINDINGS_SUFFIX
    with contextlib.suppress(OSError):
        sizes[shard] = (out / SHARDS_DIRNAME / shard).stat().st_size
    return sizes


@dataclasses.dataclass(slots=True, frozen=True)
class CompletedEntry:
    """Typed record for one file's per-run checkpoint entry (issue #118).

    Owns the ``{"summary": ..., "outputs": ...}`` shape that worker_pool
    builds after each file completes and that resume readers consume.
    ``summary`` holds the :func:`report.summary_dict` result; ``outputs``
    holds the :func:`output_sizes` result — both built by the caller
    (worker_pool), so resume stays an actual leaf: it never imports
    ``report``. ``as_dict`` serialises to the exact wire shape consumed by
    ``build_checkpoint`` and the resume readers. Both fields are plain dicts
    so the checkpoint JSON is byte-identical to the pre-refactor form —
    ``build_checkpoint`` passes the whole ``completed`` mapping through
    ``json.dumps(sort_keys=True)``, which sorts ``"outputs"`` before
    ``"summary"`` regardless of field order.
    """

    summary: dict
    outputs: dict

    def as_dict(self) -> dict:
        """Serialise to the ``{"summary": ..., "outputs": ...}`` wire shape.

        The key order here is irrelevant: ``build_checkpoint`` calls
        ``json.dumps(sort_keys=True)``, which sorts to ``outputs`` before
        ``summary`` regardless. The dict is what ``plan.completed`` stores
        so the resume readers (``run_planning`` and ``verify_completed_outputs``)
        continue to see a plain dict — no reader changes needed.
        """
        return {"summary": self.summary, "outputs": self.outputs}


def build_checkpoint(*, inputs: dict, completed: dict, run_identity: dict) -> dict:
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


def _checkpoint_path(out_dir: str) -> str:
    return str(Path(out_dir) / CHECKPOINT_NAME)


def write_checkpoint(out_dir: str, checkpoint: dict) -> str:
    """Write ``checkpoint`` to ``<out_dir>/.clean-state.json`` atomically and
    durably via a ``.partial`` temp + :func:`fsutil.durable_replace`, so a
    reader — or a crash mid-write — never sees a half-written file, and the
    committed checkpoint survives a hard power loss (issue #58). Returns the
    destination path.
    """
    dest = _checkpoint_path(out_dir)
    fsutil.durable_write_text(
        dest,
        json.dumps(checkpoint, separators=(",", ":"), sort_keys=True),
    )
    return dest


def load_checkpoint(out_dir: str) -> dict | None:
    """Return the parsed checkpoint from ``out_dir``, or ``None`` if it is absent,
    unparseable, or not a JSON object. A corrupt checkpoint is treated as no
    checkpoint — the safe default is to redo work, never to resume against garbage.
    Routes through :func:`fsutil.read_json_or_none` so ``UnicodeDecodeError`` on
    invalid-UTF-8 bytes and non-dict payloads are both caught (issues #91, #92).
    """
    return fsutil.read_json_or_none(_checkpoint_path(out_dir))


class CheckpointStatus(enum.Enum):
    """Outcome of inspecting an on-disk checkpoint: absent, corrupt (unreadable
    or malformed), valid (matches the current run), or stale (present but no
    longer matching this run's identity)."""

    ABSENT = "absent"
    CORRUPT = "corrupt"
    VALID = "valid"
    STALE = "stale"


@dataclasses.dataclass(slots=True)
class Classification:
    """Result of :func:`classify_checkpoint` — bundles status, optional reason string
    (populated for STALE), and the parsed payload (populated for VALID and STALE).
    """

    status: CheckpointStatus
    reason: str | None = None
    checkpoint: dict | None = None


def classify_checkpoint(
    out_dir: str, current_inputs: dict, current_run_identity: dict
) -> Classification:
    """Classify the checkpoint in ``out_dir`` against the current run (spec §2.3).
    Distinguishes ABSENT (no file), CORRUPT (present but unparseable or structurally
    malformed — never treated as absent, so a damaged interrupted run is surfaced,
    not silently discarded), VALID, and STALE(reason). A corrupt ``completed`` block
    is routed to CORRUPT rather than STALE because the payload cannot be consumed at
    all, regardless of identity (issue #91): ``_validate_completed_shape`` is checked
    before ``validate_run_identity`` so the stricter structural gate takes precedence.
    """
    if not Path(_checkpoint_path(out_dir)).exists():
        return Classification(CheckpointStatus.ABSENT)
    checkpoint = load_checkpoint(out_dir)
    if checkpoint is None:
        return Classification(CheckpointStatus.CORRUPT)
    if _validate_completed_shape(checkpoint) is not None:
        return Classification(CheckpointStatus.CORRUPT)
    reason = validate_run_identity(checkpoint, current_inputs, current_run_identity)
    if reason is not None:
        return Classification(
            CheckpointStatus.STALE, reason=reason, checkpoint=checkpoint
        )
    return Classification(CheckpointStatus.VALID, checkpoint=checkpoint)


_STALE_ARCHIVE_KEEP = 3  # how many stale-checkpoint archives to retain


def archive_checkpoint(out_dir: str, *, timestamp: str) -> str | None:
    """Rename the checkpoint to ``.clean-state.json.stale-<timestamp>`` so a fresh
    run never silently destroys a recoverable interrupted run (spec §2.3): the
    operator can downgrade/revert and recover it. Returns the archived basename,
    or None if there was no checkpoint. ``timestamp`` is supplied by the caller
    (clock access is forbidden in pure helpers). After archiving, prunes older
    archives keeping only the newest ``_STALE_ARCHIVE_KEEP`` (default 3) — the
    stamp is lexicographically sortable (``YYYYmmddTHHMMSSZ``), so ``sorted()``
    orders them chronologically; each unlink is individually suppressed so a race
    or permission error on one does not abort the others."""
    src = _checkpoint_path(out_dir)
    if not Path(src).exists():
        return None
    archived = f"{CHECKPOINT_NAME}.stale-{timestamp}"
    os.replace(src, Path(out_dir) / archived)
    # Prune old stale archives — keep only the newest _STALE_ARCHIVE_KEEP.
    prefix = CHECKPOINT_NAME + ".stale-"
    out = Path(out_dir)
    archives = sorted(p for p in out.iterdir() if p.name.startswith(prefix))
    for old in archives[:-_STALE_ARCHIVE_KEEP]:
        with contextlib.suppress(OSError):
            old.unlink()
    return archived


def delete_checkpoint(out_dir: str) -> None:
    """Remove the checkpoint if present; a no-op when absent. Called on a fully
    successful run so a completed run leaves no resumable state behind.
    """
    with contextlib.suppress(FileNotFoundError):
        Path(_checkpoint_path(out_dir)).unlink()


# The output trees a `clean` run writes into, under ``--out-dir``. Suffix and
# dirname constants live in ``lintle.__init__`` — the single naming-convention
# authority (pipeline._clean_output_paths, resume.output_sizes, cli.discover_paths,
# and report_writers.concat_findings_shards all import from there).
_OUTPUT_DIRS = (
    CLEANED_DIRNAME,
    BROKEN_DIRNAME,
    SHARDS_DIRNAME,
)


def _locate_output(out_dir: str, name: str) -> Path | None:
    """Return the on-disk path of output basename ``name`` under ``out_dir``'s
    output trees, or None if it is in none of them. Searching the known trees
    (rather than inferring the directory from the filename suffix) keeps the
    output-naming convention in one place — ``lintle.__init__`` — not duplicated
    here. The shards directory is included so findings shards recorded in the
    checkpoint are located on resume (issue #117)."""
    for sub in _OUTPUT_DIRS:
        candidate = Path(out_dir) / sub / name
        if candidate.exists():
            return candidate
    return None


def verify_completed_outputs(completed: dict, out_dir: str) -> list[str]:
    """Return the list of input paths whose recorded outputs are missing or do
    not match their recorded size (spec §3.6). A checkpoint entry is trusted only
    when every output file it named still exists on disk at the exact byte size
    captured at completion — guarding against a SIGKILL/disk-full truncation that
    ``os.stat``-existence alone would not catch. Flagged files are reprocessed."""
    reprocess = []
    for path, entry in completed.items():
        for name, expected_size in entry.get("outputs", {}).items():
            actual = _locate_output(out_dir, name)
            if actual is None or actual.stat().st_size != expected_size:
                reprocess.append(path)
                break
    return reprocess


class ResumeAction(enum.Enum):
    """The action :func:`resolve_resume_action` chose for a run: start FRESH,
    RESUME the existing checkpoint, or ABORT (with an exit code)."""

    FRESH = "fresh"
    RESUME = "resume"
    ABORT = "abort"


@dataclasses.dataclass(slots=True)
class Decision:
    """Result of :func:`resolve_resume_action` — bundles the chosen action, an
    optional human-readable message for the caller to surface, and the process
    exit code (set only for ABORT).
    """

    action: ResumeAction
    message: str | None = None
    exit_code: int | None = None


def resolve_resume_action(
    classification: Classification,
    *,
    resume: bool,
    no_resume: bool,
    interactive: bool,
    prompt: Callable[..., bool | None],
) -> Decision:
    """Pure decision for the §2.3 matrix. ``resume``/``no_resume`` are the explicit
    flags (authoritative); ``interactive`` is the detected mode; ``prompt`` is a
    callable ``(message, *, default) -> bool | None`` (None = EOF/no-answer) used
    only when a decision needs the operator. Returns a Decision."""
    status = classification.status
    St = CheckpointStatus
    if status is St.ABSENT:
        if resume:
            return Decision(ResumeAction.ABORT, "no interrupted run to resume", 2)
        return Decision(ResumeAction.FRESH)
    if status is St.CORRUPT:
        if no_resume:
            return Decision(ResumeAction.FRESH)
        return Decision(
            ResumeAction.ABORT,
            "checkpoint is unreadable; pass --no-resume to start fresh",
            2,
        )
    if status is St.VALID:
        if resume:
            return Decision(ResumeAction.RESUME)
        if no_resume:
            return Decision(ResumeAction.FRESH)
        if not interactive:
            return Decision(ResumeAction.RESUME)
        answer = prompt("Resume interrupted run? [Y/n] ", default=True)
        if answer is None:
            return Decision(ResumeAction.ABORT, "aborted", 2)
        return Decision(ResumeAction.RESUME if answer else ResumeAction.FRESH)
    # STALE
    reason = classification.reason or "inputs changed"
    if no_resume:
        return Decision(ResumeAction.FRESH)
    if resume:
        return Decision(ResumeAction.ABORT, f"cannot resume: {reason}", 2)
    if not interactive:
        return Decision(
            ResumeAction.ABORT,
            f"cannot resume: {reason}. Pass --no-resume to start fresh",
            2,
        )
    answer = prompt(
        f"Can't resume ({reason}). Reprocess all from scratch? [y/N] ", default=False
    )
    if answer:
        return Decision(ResumeAction.FRESH)
    return Decision(ResumeAction.ABORT, "aborted", 2)


def validate_run_identity(
    checkpoint: dict, current_inputs: dict, current_run_identity: dict
) -> str | None:
    """Return a human-readable reason the checkpoint cannot be resumed against the
    current run, or None if it can. Refuse-on-change (spec §3.1, all-or-nothing):
    schema, lintle version, output-affecting configuration, or any input identity
    drift invalidates the whole checkpoint. Also validates the ``completed`` block
    shape (issue #91): entries missing ``summary`` or ``outputs`` dicts cause a
    non-None return so callers see the checkpoint as unusable.
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
    return _validate_completed_shape(checkpoint)


def _validate_completed_shape(checkpoint: dict) -> str | None:
    """Return a human-readable reason the ``completed`` block is structurally
    corrupt, or ``None`` if it is well-formed. Checked independently of identity
    so :func:`classify_checkpoint` can route structural violations to CORRUPT
    (not STALE) — a malformed ``completed`` block means the checkpoint cannot be
    consumed at all, not merely that the run configuration has changed (issue #91).
    """
    completed = checkpoint.get("completed")
    if not isinstance(completed, dict):
        return "checkpoint completed block is missing or not a JSON object"
    for entry_path, entry in completed.items():
        if not isinstance(entry, dict):
            return f"checkpoint completed entry for {entry_path!r} is not a JSON object"
        if not isinstance(entry.get("summary"), dict):
            return f"checkpoint completed entry for {entry_path!r} has no summary dict"
        if not isinstance(entry.get("outputs"), dict):
            return f"checkpoint completed entry for {entry_path!r} has no outputs dict"
    return None
