"""Clean-run preflight planning and resume resolution."""

import contextlib
import dataclasses
import shutil
from pathlib import Path

from lintle import (
    BROKEN_DIRNAME,
    CLEANED_DIRNAME,
    SHARDS_DIRNAME,
    fsutil,
    report,
    resume,
    term,
)
from lintle.chunking import CHUNK_RECORDS_DEFAULT

# Marker written into the out-dir on the first fresh run.  Its presence (or the
# presence of a checkpoint / stale-checkpoint archive) is the ownership signal
# that lets scrub_outputs proceed safely.  Any other non-empty directory that
# lacks all three signals is treated as user-owned and scrub refuses (issue #93).
_OUTPUT_MARKER = ".lintle-output"

# Exact prefix of a stale-checkpoint archive (see resume.archive_checkpoint:
# ``<checkpoint>.stale-<timestamp>``).  Matching this — rather than the bare
# checkpoint name — keeps a user file like ``.clean-state.json.bak`` from being
# mistaken for a lintle-ownership signal (issue #93).
_STALE_CHECKPOINT_PREFIX = resume.CHECKPOINT_NAME + ".stale-"

# Report artifacts written by output_artifacts.write_clean_artifacts.  Removed
# during a fresh-run scrub so an interrupted fresh run leaves no stale reports
# for ``lintle report`` to render (issue #102).
_REPORT_ARTIFACTS = (
    "report.md",
    "report.json",
    "report.jsonl",
    "broken-noradids.ndjson",
)


@dataclasses.dataclass(slots=True, frozen=True)
class CleanConfig:
    """Typed snapshot of the ``clean`` command's configuration (issue #121).

    Built once in ``cli.main`` right after ``parse_args`` via
    :meth:`from_args` and passed to :func:`resolve_clean_plan` and
    ``worker_pool.run_workers`` instead of the raw argparse ``Namespace``.
    Centralising the attribute names here means a flag rename surfaces as
    an ``AttributeError`` at the single ``from_args`` construction site —
    before the out-dir lock is taken — rather than mid-run in a leaf.
    ``jobs`` is included so callers never need to reach back to the
    ``Namespace`` after the config is built.
    """

    out_dir: str
    command: str
    max_quarantined: str
    reconstruct_checksum: bool
    resume: bool
    no_resume: bool
    jobs: int | None
    chunk_records: int = CHUNK_RECORDS_DEFAULT

    @classmethod
    def from_args(cls, args) -> CleanConfig:
        """Construct a CleanConfig from a parsed argparse Namespace."""
        return cls(
            out_dir=args.out_dir,
            command=args.command,
            max_quarantined=args.max_quarantined,
            reconstruct_checksum=args.reconstruct_checksum,
            resume=args.resume,
            no_resume=args.no_resume,
            jobs=args.jobs,
            chunk_records=getattr(args, "chunk_records", CHUNK_RECORDS_DEFAULT),
        )


@dataclasses.dataclass(slots=True)
class RunPlan:
    """The resolved pre-flight plan for a ``clean``/``validate`` run."""

    files_to_process: list[str] = dataclasses.field(default_factory=list)
    reused_stats: list[report.FileStats] = dataclasses.field(default_factory=list)
    inputs: dict[str, object] = dataclasses.field(default_factory=dict)
    completed: dict[str, object] = dataclasses.field(default_factory=dict)
    run_identity: dict[str, object] = dataclasses.field(default_factory=dict)
    exit_code: int | None = None


def check_disk_space(out_dir, input_bytes):
    """Return a ``(term.Severity, message)`` tuple when ``out_dir``'s free
    space is at or near the 2× input-size guard, else ``None``.
    ``term.Severity.ERROR`` when free is below 2× input (caller aborts with exit
    2); ``term.Severity.WARNING`` when free sits in the borderline band 2× to
    2.5× (caller proceeds but
    surfaces the warning so the user knows they're cutting it close). Cleaned +
    broken output is ~1× input; the 2× guard leaves transient headroom for
    ``.partial`` files coexisting with their final renames mid-run.
    ``input_bytes`` is the total source size, stat'd once by the caller and
    shared with the roster and byte-bar denominators.
    """
    needed = input_bytes * 2
    free = shutil.disk_usage(out_dir).free
    if free < needed:
        return (
            term.Severity.ERROR,
            f"insufficient disk space in {out_dir}: "
            f"need ~{needed:,} bytes, have {free:,}",
        )
    if free < int(needed * 1.25):
        return (
            term.Severity.WARNING,
            f"free space in {out_dir} is close to the 2× safety guard: "
            f"{free:,} bytes free of ~{needed:,} recommended; "
            f"the run will proceed but may exhaust the disk",
        )
    return None


def _is_safe_to_scrub(out_dir):
    """Return ``True`` when ``out_dir`` is safe to scrub without risking user
    data, ``False`` otherwise (issue #93).  A directory is safe when:

    - it does not exist;
    - its only entries are the lock file (``fsutil.LOCK_NAME``) and/or the
      ownership marker (``_OUTPUT_MARKER``) — i.e. effectively empty modulo the
      files ``clean`` itself wrote this run; or
    - it already carries a lintle-ownership signal: the ownership marker, the
      resume checkpoint (``resume.CHECKPOINT_NAME``), or a stale-checkpoint
      archive (a file named ``<checkpoint>.stale-…`` — the exact prefix
      ``archive_checkpoint`` writes, NOT the bare checkpoint name, so a user
      file like ``.clean-state.json.bak`` is never mistaken for our signal).

    The lock file is always present when this function is called (``cli.main``
    acquires the lock before calling ``resolve_clean_plan``), so it is excluded
    from the emptiness check to avoid spuriously refusing a brand-new out-dir.
    """
    p = Path(out_dir)
    if not p.exists():
        return True
    entries = {e.name for e in p.iterdir()}
    # Exclude the lock (always present this run) and the marker (written by us).
    noise = {fsutil.LOCK_NAME, _OUTPUT_MARKER}
    real_entries = entries - noise
    if not real_entries:
        return True
    # Presence of any lintle-ownership signal is sufficient.
    if _OUTPUT_MARKER in entries:
        return True
    if resume.CHECKPOINT_NAME in entries:
        return True
    return any(name.startswith(_STALE_CHECKPOINT_PREFIX) for name in entries)


def scrub_outputs(out_dir):
    """Clear the cleaned/, broken/, and .shards/ trees and remove prior-run
    report artifacts so a fresh run starts from a clean slate and never leaves
    orphaned outputs from a prior, differently-scoped input set (spec §3.4) or
    a stale report that ``lintle report`` would render as current (issue #102).
    Idempotent — missing trees/files are ignored.  Does NOT check ownership;
    callers that need the ownership gate call :func:`_is_safe_to_scrub` first."""
    out = Path(out_dir)
    for sub in (CLEANED_DIRNAME, BROKEN_DIRNAME, SHARDS_DIRNAME):
        shutil.rmtree(out / sub, ignore_errors=True)
    for name in _REPORT_ARTIFACTS:
        with contextlib.suppress(OSError):
            (out / name).unlink()


def resolve_clean_plan(config: CleanConfig, files, file_sizes):
    """Resolve disk-space, resume, and fresh-run state for ``clean``.

    Execution order: build inputs + run_identity → classify checkpoint →
    resolve resume action → branch RESUME (disk guard on remaining) or FRESH
    (ownership check, scrub, disk guard on full corpus, marker write).
    """
    inputs = {path: resume.input_fingerprint(path) for path in files}
    # reconstruct_checksum changes which records are accepted vs quarantined,
    # so a resume with a flipped flag must re-run (STALE), not fold mismatched
    # outputs together (issue #82).
    # chunk_records is part of run identity: it sets the chunk boundaries, so a
    # resume with a different value would mix chunk sizes within one logical run
    # (completed stems at the old size, redone stems at the new). A mismatch
    # classifies the checkpoint STALE → the run restarts fresh rather than
    # producing a set whose concatenation a fresh run would not reproduce.
    run_identity = {
        "max_quarantined": config.max_quarantined,
        "reconstruct_checksum": config.reconstruct_checksum,
        "chunk_records": config.chunk_records,
    }

    classification = resume.classify_checkpoint(config.out_dir, inputs, run_identity)
    decision = resume.resolve_resume_action(
        classification,
        resume=config.resume,
        no_resume=config.no_resume,
        interactive=term.is_interactive(),
        prompt=term.prompt_yes_no,
    )
    if decision.action is resume.ResumeAction.ABORT:
        term.error(decision.message)
        return RunPlan(exit_code=decision.exit_code)

    if decision.action is resume.ResumeAction.RESUME:
        checkpoint = classification.checkpoint
        completed = dict(checkpoint["completed"])
        # Integrity re-verification: drop any completed entry whose outputs are
        # missing or truncated, so they are reprocessed.
        for bad_path in resume.verify_completed_outputs(completed, config.out_dir):
            completed.pop(bad_path, None)
        reused_stats = [
            report.stats_from_summary(e["summary"]) for e in completed.values()
        ]
        files_to_process = [f for f in files if f not in completed]
        term.note(
            f"resuming: {len(completed)}/{len(files)} files already complete, "
            f"processing {len(files_to_process)}"
            " — pass --no-resume for a fresh run"
        )
        # Issue #94: charge the guard against the REMAINING input only, AFTER
        # we know which files still need processing.
        remaining_bytes = sum(file_sizes[f] for f in files_to_process)
        disk_status = check_disk_space(config.out_dir, remaining_bytes)
        if disk_status is not None:
            severity, msg = disk_status
            term.emit(severity, msg)
            if severity is term.Severity.ERROR:
                return RunPlan(exit_code=2)
        return RunPlan(
            files_to_process=files_to_process,
            reused_stats=reused_stats,
            inputs=inputs,
            completed=completed,
            run_identity=run_identity,
        )

    # FRESH branch.
    # Issue #93: gate the scrub on an ownership check before destroying anything.
    if not _is_safe_to_scrub(config.out_dir):
        term.error(
            f"refusing to scrub {config.out_dir!r}: not a lintle output directory "
            f"(no {_OUTPUT_MARKER} marker); "
            f"use an empty or new --out-dir, or remove it yourself"
        )
        return RunPlan(exit_code=2)

    # Write the ownership marker BEFORE scrubbing so it persists even if a
    # later step (scrub, disk guard, or the run itself) fails — otherwise a
    # successful run whose marker write was lost would leave the dir with
    # outputs but no ownership signal, locking out the next fresh run. The
    # marker is not in the scrub set, so it survives the scrub. A write failure
    # means the out-dir is unwritable (the run could not write outputs either),
    # so surface it as an operational error rather than silently swallowing it.
    try:
        Path(config.out_dir, _OUTPUT_MARKER).write_text("", encoding="utf-8")
    except OSError as exc:
        term.error(f"cannot write the output marker in {config.out_dir!r}: {exc}")
        return RunPlan(exit_code=2)

    # Archive any prior checkpoint (preserves recoverability) then scrub outputs.
    resume.archive_checkpoint(config.out_dir, timestamp=resume.run_started_stamp())
    # Issue #102: scrub also removes prior-run report artifacts.
    scrub_outputs(config.out_dir)

    # Issue #94: disk guard runs AFTER scrub so freed prior-output space is
    # already counted as available.
    disk_status = check_disk_space(config.out_dir, sum(file_sizes.values()))
    if disk_status is not None:
        severity, msg = disk_status
        term.emit(severity, msg)
        if severity is term.Severity.ERROR:
            return RunPlan(exit_code=2)

    return RunPlan(
        files_to_process=files,
        inputs=inputs,
        run_identity=run_identity,
    )
