"""Command-line interface: ``lintle validate``, ``lintle clean``, ``lintle diff``."""

import argparse
import contextlib
import datetime
import json
import os
import shutil
import time
from pathlib import Path

from lintle import (
    __version__,
    cli_progress,
    diff,
    explain,
    fsutil,
    output_artifacts,
    process_control,
    report,
    resume,
    run_planning,
    term,
    thresholds,
    worker_pool,
)

_DEFAULT_SOURCE = "data/source"
_DEFAULT_OUTPUT = "data/output"

_EPILOG = """\
Examples:
  lintle validate                         audit data/source/ (read-only)
  lintle clean                            clean data/source/ -> data/output/
  lintle validate file.txt                audit a single file
  lintle clean data/raw --jobs 4          clean with 4 parallel workers
  lintle clean data/raw --out-dir build   write to a custom location
  lintle validate --report json           emit a machine-readable summary
  lintle diff run-a/ run-b/               compare two runs' findings

Exit codes:
  0    quarantine count (or rate) is at or below --max-quarantined
  1    quarantine count (or rate) exceeded --max-quarantined
       (default: 0 — any quarantine fails)
  2    operational/usage error: bad args, no input files, disk shortfall,
       a file that failed to process, lock held, or a stale/corrupt/
       declined resume (including EOF at the prompt)
  129  terminated by SIGHUP (128+1)
  130  terminated by SIGINT / Ctrl-C (128+2)
  143  terminated by SIGTERM (128+15)

See `lintle <command> --help` for command-specific options.
"""


def discover_paths(path):
    """Expand ``path``: a directory becomes its sorted ``tle*.txt`` files
    (excluding ``*.cleaned.txt`` / ``*.broken.txt`` tool output); a file is
    returned as a single-element list. A nonexistent entry yields ``[]`` —
    callers should validate inputs with :func:`check_paths` first.
    """
    directory = Path(path)
    if directory.is_dir():
        return [
            str(directory / name)
            for name in sorted(entry.name for entry in directory.iterdir())
            if (
                name.startswith("tle")
                and name.endswith(".txt")
                and not name.endswith(".cleaned.txt")
                and not name.endswith(".broken.txt")
            )
        ]
    if Path(path).is_file():
        return [path]
    return []


def check_paths(path, using_default):
    """Return a user-facing error string if ``path`` does not exist, else
    ``None``. ``using_default`` tailors the message for the case where the
    user passed nothing and the default (``data/source``) is what's missing.

    Readability is *not* checked here — :func:`os.access` consults the
    POSIX mode bits only and is a false-negative on filesystems that grant
    read through ACLs (NFSv4, SMB, FUSE). The authoritative answer is
    whatever the worker's :func:`open` returns; a genuine permission error
    surfaces through the per-file failure path in :func:`main` with the
    same exit code 2.
    """
    if Path(path).exists():
        return None
    if using_default:
        return (
            f"default input directory {_DEFAULT_SOURCE!r} does not exist.\n"
            f"  pass a file or directory on the command line,\n"
            f"  or create {_DEFAULT_SOURCE}/ and put your tle*.txt files there.\n"
            f"  run 'lintle --help' for usage and examples."
        )
    return f"no such file or directory: {path!r}"


def _add_processing_subparsers(subparsers):
    """Add the ``validate`` and ``clean`` subparsers, which share an argument
    surface (path, --out-dir, --jobs, --report, --max-quarantined); ``clean``
    additionally gets the mutually-exclusive --resume/--no-resume group."""
    for name, help_text, description in (
        (
            "validate",
            "audit files and report defects (writes nothing)",
            "Audit TLE files against the spec and report every defect "
            "(checksum mismatches, wrong length, orphan lines, etc.) "
            "without modifying anything.",
        ),
        (
            "clean",
            "write cleaned files and quarantine sidecars",
            "Apply validated repairs and write cleaned files plus a per-file "
            "quarantine sidecar to --out-dir; emit a corpus-wide report.md.",
        ),
    ):
        sub = subparsers.add_parser(
            name,
            help=help_text,
            description=description,
            epilog=_EPILOG,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        sub.add_argument(
            "path",
            nargs="?",
            default=None,
            metavar="PATH",
            help=(
                f"file or directory to process "
                f"(default: {_DEFAULT_SOURCE}). "
                "A directory is globbed for tle*.txt."
            ),
        )
        sub.add_argument(
            "--out-dir",
            default=_DEFAULT_OUTPUT,
            metavar="DIR",
            help=f"destination for cleaned/broken files (default: {_DEFAULT_OUTPUT})",
        )
        sub.add_argument(
            "--jobs",
            type=int,
            default=None,
            metavar="N",
            help=(
                "files processed in parallel "
                "(default: CPU count - 1, capped at file count)"
            ),
        )
        sub.add_argument(
            "--report",
            choices=["text", "json"],
            default="text",
            help="summary output format (default: text)",
        )
        sub.add_argument(
            "--max-quarantined",
            default="0",
            metavar="N[%]",
            help=(
                "exit non-zero only if MORE than N records were quarantined; "
                "or, with a trailing `%%`, more than N%% of routed records "
                "(default: 0 — any quarantine fails)"
            ),
        )
        if name == "clean":
            resume_group = sub.add_mutually_exclusive_group()
            resume_group.add_argument(
                "--resume",
                action="store_true",
                help=(
                    "resume an interrupted run in --out-dir without prompting "
                    "(resume is the default when an interrupted run is found)"
                ),
            )
            resume_group.add_argument(
                "--no-resume",
                action="store_true",
                help=(
                    "ignore any interrupted run and start fresh, clearing prior "
                    "outputs in --out-dir"
                ),
            )


def _add_diff_subparser(subparsers):
    """Add the read-only ``diff`` subparser: two positional run directories, no
    shared validate/clean option surface. It consumes each run's report.jsonl
    and writes nothing."""
    diff_parser = subparsers.add_parser(
        "diff",
        help="compare two run outputs and show per-rule deltas",
        description=(
            "Read report.jsonl from two clean-run output directories and print "
            "the new defect classes (in RUN-B only), fixed classes (in RUN-A "
            "only), and per-rule count deltas. Read-only; writes nothing."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    diff_parser.add_argument(
        "run_a", metavar="RUN-A", help="first run's output directory"
    )
    diff_parser.add_argument(
        "run_b", metavar="RUN-B", help="second run's output directory"
    )


def _add_explain_subparser(subparsers):
    """Add the read-only ``explain`` subparser: one positional TAG (a rule ID
    like TLE-CHK-001 or a fix tag like reconstructed-checksum). Writes nothing."""
    explain_parser = subparsers.add_parser(
        "explain",
        help="print what a rule ID or fix tag means, with examples",
        description=(
            "Explain one quarantine rule (e.g. TLE-CHK-001) or repair tag (e.g. "
            "reconstructed-checksum): its definition, a verified good/bad or "
            "before/after example, the repair tier, and the source of truth in "
            "the code. Read-only; writes nothing."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    explain_parser.add_argument(
        "tag",
        metavar="TAG",
        help="a rule ID (TLE-CHK-001) or fix tag (reconstructed-checksum)",
    )


def build_parser():
    """Build the ``lintle`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="lintle",
        description=(
            "Validate and clean Two-Line Element (TLE) corpus files exported "
            "from space-track.org."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{validate,clean,diff,explain}",
        title="commands",
    )
    _add_processing_subparsers(subparsers)
    _add_diff_subparser(subparsers)
    _add_explain_subparser(subparsers)
    return parser


def resolve_jobs(explicit, cpu_count, n_files):
    """Resolve the worker count for a run. An explicit ``--jobs`` is the user's
    deliberate choice and is returned unchanged; otherwise default to one fewer
    than the CPU count — reserving a core for the OS during a long run — capped
    at the number of files and floored at one."""
    if explicit is not None:
        return explicit
    return max(1, min((cpu_count or 1) - 1, n_files))


def _finalize_run(
    args,
    all_stats,
    failed_files,
    *,
    run_started_iso,
    run_monotonic_start,
    threshold_mode,
    quarantine_threshold,
):
    """Finish a non-interrupted run: sort stats, write the clean-run artifacts,
    emit the text/JSON summary, tear down resumable state on success, and return
    the process exit code. Split out of :func:`main` so the orchestration there
    stays at the level of phases (validate inputs -> plan -> dispatch -> finalize)."""
    all_stats.sort(key=lambda stats: stats.src_name)

    # A `clean` run writes a Markdown run report, a corpus-wide NDJSON of
    # NORAD IDs whose records were quarantined anywhere, and (issue #9) a
    # corpus-wide ``report.jsonl`` of structured findings concatenated
    # from the per-worker shards. All three are always written on a
    # successful clean run — empty when nothing was quarantined — so
    # downstream consumers see a stable artifact set.
    artifacts = output_artifacts.CleanArtifacts()
    if args.command == "clean" and all_stats:
        # A spinner over the silent finalization (the per-worker shard
        # concat into report.jsonl dominates on a large corpus); a no-op off
        # a TTY. Runs after the progress block has exited, so no Live nesting.
        artifacts = output_artifacts.write_clean_artifacts(args.out_dir, all_stats)

    if args.report == "json":
        # Issue #20: top-level versioned envelope; replaces the prior
        # flat-array output. Run wall-clock is the parent process's
        # monotonic delta, NOT the sum of per-file worker durations
        # (those are reported per-file under ``files[i].elapsed_seconds``
        # and exceed parent wall-clock under ``--jobs N``).
        run_elapsed = time.monotonic() - run_monotonic_start
        envelope = report.build_run_envelope(
            all_stats,
            command=args.command,
            started_at=run_started_iso,
            elapsed_seconds=run_elapsed,
        )
        print(json.dumps(envelope, indent=2))
    else:
        for stats in all_stats:
            print(report.format_summary(stats))
            if args.command == "validate" and stats.quarantine_sample.buckets:
                print(report.format_quarantine_lines(stats))
        if artifacts.report_path:
            print(f"\nrun report: {artifacts.report_path}")
        if artifacts.noradids_path:
            print(f"broken NORAD IDs: {artifacts.noradids_path}")
        if artifacts.findings_path:
            print(f"findings: {artifacts.findings_path}")

    # A fully successful clean run leaves no resumable state behind. The
    # checkpoint and the findings shards (`.shards`) are both in-progress run
    # state and are torn down together, here, ONLY on success: an interrupted
    # run already returned 130 above (keeping both), and a failed run keeps both
    # too, so the operator can fix the cause and `--resume` re-reads the
    # surviving shards to rebuild a complete `report.jsonl` (issue #56). The
    # `report.jsonl` was written from those shards by the report block above.
    if args.command == "clean" and not failed_files:
        resume.delete_checkpoint(args.out_dir)
        shard_dir = Path(args.out_dir) / ".shards"
        if shard_dir.exists():
            shutil.rmtree(shard_dir)

    # A file that could not be processed is an operational failure (spec §2.7
    # / §10): exit 2 (operational error). Exit 1 is the quarantine quality
    # gate (threshold exceeded); exit 2 covers all other operational failures.
    if failed_files:
        return 2
    return thresholds.quarantine_exit_code(
        all_stats, threshold_mode, quarantine_threshold
    )


def main(argv=None):
    """Entry point for the ``lintle`` console script.

    Returns the process exit code: ``0`` = quarantine count (or rate) is at
    or below ``--max-quarantined``; ``1`` = quarantine threshold exceeded
    (default ``0`` — any quarantine fails); ``2`` = operational/usage error
    (bad args, no input files, disk shortfall, a file that failed to process,
    or a stale/corrupt/declined resume); ``130``/``143``/``129`` = terminated
    by SIGINT/SIGTERM/SIGHUP. The threshold accepts either an integer record
    count (``--max-quarantined 100``) or a percentage of routed records
    (``--max-quarantined 1%``); see :func:`thresholds.parse_quarantine_threshold`.
    """
    args = build_parser().parse_args(argv)

    # `diff` is a read-only consumer of two report.jsonl files; it shares none
    # of the validate/clean argument surface (paths, jobs, out-dir, threshold),
    # so dispatch it before any of that logic runs.
    if args.command == "diff":
        return diff.run(args.run_a, args.run_b)

    # `explain` is a read-only documentation lookup — no input files, no output
    # tree. An unknown tag is an operational error (exit 2) with the valid tags
    # listed so the operator can correct it.
    if args.command == "explain":
        try:
            print(explain.render(args.tag))
        except explain.UnknownTag:
            term.error(
                f"unknown tag {args.tag!r}.\n"
                f"  valid tags: {', '.join(explain.known_tags())}"
            )
            return 2
        return 0

    # `args.path` is None when the user passed nothing — fall back to the
    # default source dir, and remember it so we can give a tailored error if
    # that default doesn't exist on this machine.
    using_default = args.path is None
    path = args.path if args.path is not None else _DEFAULT_SOURCE

    if args.jobs is not None and args.jobs < 1:
        term.error(f"--jobs must be >= 1 (got {args.jobs})")
        return 2

    try:
        threshold_mode, quarantine_threshold = thresholds.parse_quarantine_threshold(
            args.max_quarantined
        )
    except ValueError as exc:
        term.error(str(exc))
        return 2

    path_error = check_paths(path, using_default=using_default)
    if path_error:
        term.error(path_error)
        return 2

    files = discover_paths(path)
    if not files:
        if Path(path).is_dir():
            term.error(
                f"no tle*.txt files found in {path!r}.\n"
                "  expected one or more files named tle*.txt "
                "(excluding *.cleaned.txt / *.broken.txt)."
            )
        else:
            term.error("no input files found")
        return 2

    # Stat every input exactly once — the single source of size truth shared by
    # the disk-space guard, the pre-run roster, and the live byte-bar
    # denominators, so those three readouts can never silently diverge. Ordered
    # by discovery so the roster lists files in a stable order.
    file_sizes = {p: Path(p).stat().st_size for p in files}

    # ExitStack holds the out-dir lock for a clean run. Closed in the finally
    # block below — so every exit path (LockHeldError aside, which returns
    # before entering the try, leaving the stack empty) releases the lock.
    # For validate the stack stays empty and close() is a no-op.
    _lock_stack = contextlib.ExitStack()

    if args.command == "clean":
        Path(args.out_dir).mkdir(parents=True, exist_ok=True)
        try:
            _lock_stack.enter_context(
                fsutil.out_dir_lock(args.out_dir, started=resume.run_started_stamp())
            )
        except fsutil.LockHeldError as exc:
            # Stack is still empty — no lock to release.
            term.error(str(exc))
            return 2

    # The try/finally guarantees _lock_stack.close() runs on every exit path
    # that reaches here: disk-error return, ABORT return, interrupt return,
    # failed-files return, and normal success — so the lock file is always
    # removed.  For validate the stack is empty; close() is a no-op.
    try:
        if args.command == "clean":
            plan = run_planning.resolve_clean_plan(args, files, file_sizes)
            if plan.exit_code is not None:
                return plan.exit_code
        else:
            # validate processes every discovered file; no resume, no checkpoint.
            plan = run_planning.RunPlan(files_to_process=files)
        # Resolve the worker count now that files_to_process is final: an
        # explicit --jobs is honoured as-is; the default is CPU count - 1,
        # capped at the file count and floored at one (issue #53 §2.3).
        jobs = resolve_jobs(args.jobs, os.cpu_count(), len(plan.files_to_process))

        # The shared rich Console on stderr (term.stderr_console) drives both the
        # roster and the live progress block; off a TTY each degrades to plain
        # text. Byte-bar denominators come from os.stat (issue #53 §2.1/§2.2) —
        # no pre-read of the corpus.
        console = term.stderr_console
        sizes = {Path(p).name: file_sizes[p] for p in plan.files_to_process}
        if args.command == "clean":
            cli_progress.render_roster(
                console, {p: file_sizes[p] for p in plan.files_to_process}
            )

        if not plan.reused_stats:
            term.note(
                f"processing {len(plan.files_to_process)} file(s) "
                f"with {jobs} worker(s)..."
            )
        # Run-level timing for the v1 envelope (issue #20). The wall-clock
        # start captures NOW (just before worker dispatch); the corresponding
        # stop happens after the executor drains. Two separate measurements:
        # ``run_started_iso`` is an ISO 8601 UTC string for human-readable
        # ``run.timestamp``; ``run_monotonic_start`` feeds the elapsed_seconds
        # subtraction using a monotonic clock so NTP jitter mid-run cannot
        # produce a negative duration. Per spec §4, this aggregate is
        # intentionally NOT the sum of per-file worker durations.
        run_started_iso = datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        run_monotonic_start = time.monotonic()

        all_stats, failed_files, interrupted, interrupted_signo = (
            worker_pool.run_workers(args, files, plan, jobs, console, sizes)
        )

        if interrupted:
            return process_control.signal_exit_code(interrupted_signo)

        return _finalize_run(
            args,
            all_stats,
            failed_files,
            run_started_iso=run_started_iso,
            run_monotonic_start=run_monotonic_start,
            threshold_mode=threshold_mode,
            quarantine_threshold=quarantine_threshold,
        )
    finally:
        _lock_stack.close()
