"""Command-line interface: ``lintle clean``, ``lintle diff``, ``lintle explain``."""

import argparse
import contextlib
import datetime
import json
import os
import shutil
import sys
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
    summary,
    term,
    thresholds,
    worker_pool,
)

_DEFAULT_SOURCE = "data/source"
_DEFAULT_OUTPUT = "data/output"

_EPILOG = """\
Examples:
  lintle clean                            clean data/source/ -> data/output/
  lintle clean file.txt                   clean a single file
  lintle clean data/raw --jobs 4          clean with 4 parallel workers
  lintle clean data/raw --out-dir build   write to a custom location
  lintle clean --report json              emit a machine-readable summary
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
    """Expand ``path``: a directory becomes its sorted ``tle*.txt`` regular
    files (excluding ``*.cleaned.txt`` / ``*.broken.txt`` tool output, and
    excluding dangling symlinks and directories even when named ``tle*.txt``);
    a file is returned as a single-element list. A nonexistent entry yields
    ``[]`` — callers should validate inputs with :func:`check_paths` first.
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
                and (directory / name).is_file()  # excludes dangling symlinks + dirs
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


def _add_clean_subparser(subparsers):
    """Add the ``clean`` subparser (path, --out-dir, --jobs, --report,
    --max-quarantined) plus its mutually-exclusive --resume/--no-resume group."""
    sub = subparsers.add_parser(
        "clean",
        help="write cleaned files and quarantine sidecars",
        description=(
            "Apply validated repairs and write cleaned files plus a per-file "
            "quarantine sidecar to --out-dir; emit a corpus-wide report.md."
        ),
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
            "files processed in parallel (default: CPU count - 1, capped at file count)"
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
    sub.add_argument(
        "--reconstruct-checksum",
        action="store_true",
        help=(
            "recompute and append a missing column-69 checksum for an "
            "otherwise-valid 68-char line (tier-2 repair). Off by default: a "
            "dropped data character is indistinguishable from a dropped "
            "checksum, so by default such lines are quarantined"
        ),
    )
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
    shared ``clean`` option surface. It consumes each run's report.jsonl
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


def _add_report_subparser(subparsers):
    """Add the read-only ``report`` subparser: render a prior clean run's
    aggregate summary from ``<out-dir>/report.json``. No --jobs / --out-dir
    options beyond the positional output dir; writes nothing."""
    report_parser = subparsers.add_parser(
        "report",
        help="render the last clean run's aggregate summary from report.json",
        description=(
            "Read report.json from a clean-run output directory and render the "
            "aggregate summary panel. Read-only; writes nothing."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    report_parser.add_argument(
        "out_dir",
        nargs="?",
        default=_DEFAULT_OUTPUT,
        metavar="OUT-DIR",
        help=f"clean run output directory (default: {_DEFAULT_OUTPUT})",
    )
    report_parser.add_argument(
        "--report",
        choices=["text", "json"],
        default="text",
        help=(
            "output format: text renders the panel to stdout; "
            "json emits report.json verbatim (default: text)"
        ),
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
        metavar="{clean,diff,explain,report}",
        title="commands",
    )
    _add_clean_subparser(subparsers)
    _add_diff_subparser(subparsers)
    _add_explain_subparser(subparsers)
    _add_report_subparser(subparsers)
    return parser


def resolve_jobs(explicit, cpu_count, n_files):
    """Resolve the worker count for a run. An explicit ``--jobs`` is the user's
    deliberate choice and is returned unchanged; otherwise default to one fewer
    than the CPU count — reserving a core for the OS during a long run — capped
    at the number of files and floored at one."""
    if explicit is not None:
        return explicit
    return max(1, min((cpu_count or 1) - 1, n_files))


def _print_doc(text):
    """Print read-only documentation to stdout, surviving a non-UTF-8 stdout.

    The ``explain`` text carries non-ASCII characters (a ``·`` heading
    separator, em-dashes from rule titles), so a bare ``print`` on an ASCII
    stdout (``PYTHONIOENCODING=ascii``, a C-locale session) would crash with
    ``UnicodeEncodeError``. Re-encoding through the stream's own encoding with
    ``backslashreplace`` escapes only the un-representable characters, leaving
    a fully-readable document on a capable terminal unchanged."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = text.encode(encoding, errors="backslashreplace").decode(encoding)
    print(safe)


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
    stays at the level of phases (check inputs -> plan -> dispatch -> finalize)."""
    all_stats.sort(key=lambda stats: stats.src_name)

    # Build the run envelope once, unconditionally — even an all-failed run
    # (empty all_stats) must emit a valid versioned object under --report json,
    # never ``null`` (``build_run_envelope([])`` returns a zeroed envelope). The
    # same object is persisted to report.json (when there are stats) and printed
    # to stdout under --report json — one object, no divergence.
    # Issue #20: top-level versioned envelope. Run wall-clock is the parent
    # process's monotonic delta, NOT the sum of per-file worker durations
    # (those are reported per-file under ``files[i].elapsed_seconds`` and
    # exceed parent wall-clock under ``--jobs N``).
    run_elapsed = time.monotonic() - run_monotonic_start
    envelope = report.build_run_envelope(
        all_stats,
        command=args.command,
        started_at=run_started_iso,
        elapsed_seconds=run_elapsed,
        failed_files=failed_files,
    )

    # A `clean` run writes a Markdown run report, the machine-readable
    # ``report.json`` (the byte-identical twin of the --report json stdout
    # envelope), a corpus-wide NDJSON of NORAD IDs quarantined anywhere, and
    # (issue #9) a corpus-wide ``report.jsonl`` of structured findings
    # concatenated from the per-worker shards. All are always written on a
    # successful clean run — empty when nothing was quarantined — so downstream
    # consumers see a stable artifact set. A spinner covers the silent
    # finalization (the per-worker shard concat dominates on a large corpus); a
    # no-op off a TTY, and after the progress block exits, so no Live nesting.
    if all_stats:
        output_artifacts.write_clean_artifacts(
            args.out_dir, all_stats, envelope, failed_files=failed_files
        )

    if args.report == "json":
        print(json.dumps(envelope, indent=2))
    elif all_stats:
        # The human aggregate panel goes to stderr (styled ephemera), replacing
        # the old per-file stdout dump; per-file detail lives in report.md. Off
        # a TTY it degrades to a plain ASCII block. Text-mode stdout stays empty
        # so a pipe sees nothing the report.json artifact doesn't already carry.
        summary.render(envelope, console=term.stderr_console, command_label="clean")

    # A fully successful clean run leaves no resumable state behind. The
    # checkpoint and the findings shards (`.shards`) are both in-progress run
    # state and are torn down together, here, ONLY on success: an interrupted
    # run already returned 130 above (keeping both), and a failed run keeps both
    # too, so the operator can fix the cause and `--resume` re-reads the
    # surviving shards to rebuild a complete `report.jsonl` (issue #56). The
    # `report.jsonl` was written from those shards by the report block above.
    if not failed_files:
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
    # of the `clean` argument surface (paths, jobs, out-dir, threshold), so
    # dispatch it before any of that logic runs.
    if args.command == "diff":
        return diff.run(args.run_a, args.run_b)

    # `explain` is a read-only documentation lookup — no input files, no output
    # tree. An unknown tag is an operational error (exit 2) with the valid tags
    # listed so the operator can correct it.
    if args.command == "explain":
        try:
            _print_doc(explain.render(args.tag))
        except explain.UnknownTag:
            term.error(
                f"unknown tag {args.tag!r}.\n"
                f"  valid tags: {', '.join(explain.known_tags())}"
            )
            return 2
        return 0

    # `report` is a read-only render of a prior clean run's report.json — text
    # renders the aggregate panel to stdout, json echoes the file verbatim.
    # Shares none of the `clean` surface, so dispatch it before that logic.
    if args.command == "report":
        return summary.run(args.out_dir, args.report)

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
    try:
        file_sizes = {p: Path(p).stat().st_size for p in files}
    except OSError as exc:
        term.error(f"cannot read input file: {exc}")
        return 2

    # ExitStack holds the out-dir lock for the clean run. Closed in the finally
    # block below — so every exit path (LockHeldError aside, which returns
    # before entering the try, leaving the stack empty) releases the lock.
    _lock_stack = contextlib.ExitStack()

    try:
        Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        term.error(f"cannot create output directory {args.out_dir!r}: {exc}")
        return 2

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
    # removed.
    try:
        try:
            plan = run_planning.resolve_clean_plan(args, files, file_sizes)
        except OSError as exc:
            term.error(f"preflight error: {exc}")
            return 2
        if plan.exit_code is not None:
            return plan.exit_code
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

        all_stats, failed_files, interrupted, interrupted_signo, operational_error = (
            worker_pool.run_workers(args, files, plan, jobs, console, sizes)
        )

        if operational_error is not None:
            # Issue #89: parent-side bookkeeping failure (e.g. ENOSPC from
            # write_checkpoint). The pool has already been torn down via the
            # KI path; surface a clean error and return 2 (operational error).
            # Exit 1 is reserved exclusively for the quarantine quality gate.
            term.error(
                f"run aborted due to an operational error: {operational_error}\n"
                "  if some files completed, re-run with --resume to finish."
            )
            return 2

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
    except Exception as exc:
        # Issue #89: catch-all backstop — any unhandled exception in the clean
        # orchestration (that isn't a KeyboardInterrupt, which propagates
        # naturally) maps to a clean exit-2 operational error so exit 1 stays
        # unambiguous as the quality-gate signal. A brief resume hint tells the
        # operator that partial progress may be recoverable.
        term.error(
            f"unexpected error during clean: {exc}\n"
            "  if some files completed, re-run with --resume to finish."
        )
        return 2
    finally:
        _lock_stack.close()
