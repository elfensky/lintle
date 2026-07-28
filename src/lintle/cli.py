"""Command-line interface: ``lintle clean``, ``lintle diff``, ``lintle explain``."""

import argparse
import contextlib
import json
import os
import shutil
import signal
import sys
import time
import traceback
from pathlib import Path

from lintle import (
    BROKEN_SUFFIX,
    CLEANED_SUFFIX,
    EXTRACT_DIRNAME,
    SHARDS_DIRNAME,
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
from lintle import (
    config as user_config,
)
from lintle.chunking import CHUNK_RECORDS_DEFAULT

_DEFAULT_SOURCE = "data/source"
_DEFAULT_OUTPUT = "data/output"


def _chunk_records_type(value):
    """argparse type for ``--chunk-records``: a non-negative int (0 = never roll,
    a single ``.00001`` chunk). Rejects negatives so a typo fails loudly."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0 (0 = never roll)")
    return n


def _add_chunk_records_arg(parser):
    """Add the shared ``--chunk-records N`` flag (clean/dedup/verify) that sizes
    the fixed-count output chunks. ``0`` writes a single ``.00001`` chunk."""
    parser.add_argument(
        "--chunk-records",
        type=_chunk_records_type,
        default=CHUNK_RECORDS_DEFAULT,
        metavar="N",
        help=(
            "records per output chunk file (default: "
            f"{CHUNK_RECORDS_DEFAULT:,}); every record/line stream is split into "
            "<stem>.NNNNN.<suffix> chunks of this size. 0 = never roll (a single "
            ".00001 chunk)"
        ),
    )


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
    ``[]`` — callers should validate inputs with :func:`input_path_error` first.
    """
    directory = Path(path)
    if directory.is_dir():
        return [
            str(directory / name)
            for name in sorted(entry.name for entry in directory.iterdir())
            if (
                name.startswith("tle")
                and name.endswith(".txt")
                and not name.endswith(CLEANED_SUFFIX)
                and not name.endswith(BROKEN_SUFFIX)
                and (directory / name).is_file()  # excludes dangling symlinks + dirs
            )
        ]
    if Path(path).is_file():
        return [path]
    return []


def input_path_error(path, using_default):
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
    _add_chunk_records_arg(sub)
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
        default=None,
        metavar="OUT-DIR",
        help=(
            "clean run output directory "
            f"(default: stored config, else {_DEFAULT_OUTPUT})"
        ),
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


def _add_verify_subparser(subparsers):
    """Add the ``verify`` subparser: audit a clean run's output for
    cleaning-corruption (goal 1) and structural contradictions (goal 3). Reads
    ``<out-dir>/01-cleaned`` and the source tree; writes only
    ``<out-dir>/04-verify``."""
    verify_parser = subparsers.add_parser(
        "verify",
        help="audit a clean run's cleaned output for corruption and contradictions",
        description=(
            "Post-run correctness auditing: re-validate every cleaned record, "
            "flag any (catalog, epoch) contradiction, and — when the original "
            "source is available — confirm every cleaned line is a sanctioned "
            "edit of a real source line (no interior mutation). Writes a suspects "
            "report under <out-dir>/04-verify. Exit 1 if any hard suspect is found."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    verify_parser.add_argument(
        "out_dir",
        nargs="?",
        default=None,
        metavar="OUT-DIR",
        help=(
            "clean run output directory to verify "
            f"(default: stored config, else {_DEFAULT_OUTPUT})"
        ),
    )
    verify_parser.add_argument(
        "--source",
        default=None,
        metavar="DIR",
        help=(
            "original source directory for the byte-diff "
            f"(default: stored config, else {_DEFAULT_SOURCE})"
        ),
    )
    verify_parser.add_argument(
        "--no-source-diff",
        action="store_true",
        help=(
            "skip the source byte-diff (goal 1); only re-validate and "
            "check contradictions"
        ),
    )
    verify_parser.add_argument(
        "--orbit",
        action="store_true",
        help=(
            "run the sampled sgp4 orbit-consistency pass (goal 2): flag "
            "position-residual outliers across each satellite's track"
        ),
    )
    verify_parser.add_argument(
        "--sample",
        type=int,
        default=3000,
        metavar="N",
        help="(--orbit) satellites to sample (default: 3000; ignored with --all)",
    )
    verify_parser.add_argument(
        "--all",
        dest="all_sats",
        action="store_true",
        help="(--orbit) check every satellite, not a sample",
    )
    verify_parser.add_argument(
        "--sensitivity",
        choices=("sensitive", "strict"),
        default="sensitive",
        help=(
            "(--orbit) outlier threshold tier: 'sensitive' (default; 100 km floor, "
            "10·MAD) or 'strict' (200 km, 20·MAD) for fewer, higher-confidence hits"
        ),
    )
    _add_chunk_records_arg(verify_parser)


def _add_dedup_subparser(subparsers):
    """Add the ``dedup`` subparser: collapse a clean run's re-issued records into
    a single 'latest only' import list. Reads ``<out-dir>/01-cleaned`` (and a prior
    ``verify`` run's ``suspects.jsonl`` if present); writes only
    ``<out-dir>/05-dedup``. ``01-cleaned/`` is never modified."""
    dedup_parser = subparsers.add_parser(
        "dedup",
        help="collapse re-issued records into a 'latest only' import list",
        description=(
            "Emit a de-duplicated, latest-re-issue-only import list from a clean "
            "run's cleaned output: one card per (catalog, epoch), keeping the "
            "highest element-set number. Benign re-issues collapse silently; a "
            "genuine same-epoch orbit contradiction is kept-latest but flagged "
            "(exit 1). When a verify run's suspects.jsonl exists, hard suspects "
            "are excluded first. Writes <out-dir>/05-dedup/import.txt; 01-cleaned/ is "
            "never modified."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dedup_parser.add_argument(
        "out_dir",
        nargs="?",
        default=None,
        metavar="OUT-DIR",
        help=(
            "clean run output directory to de-duplicate "
            f"(default: stored config, else {_DEFAULT_OUTPUT})"
        ),
    )
    _add_chunk_records_arg(dedup_parser)


def _add_extract_subparser(subparsers):
    """Add the ``extract`` subparser: one satellite's complete deduped TLE
    history from a prior dedup run — ``<id>.txt`` (pure 2-line records,
    epoch-ascending) plus a ``<id>.json`` stats sidecar, per requested id."""
    extract_parser = subparsers.add_parser(
        "extract",
        help="extract one satellite's complete TLE history from a dedup run",
        description=(
            "Extract each NORAD id's complete deduped history from "
            "<out-dir>/05-dedup into <dest>/<id>.txt (pure TLE lines, "
            "epoch-ascending) and <dest>/<id>.json (stats). Read-only and "
            "local; requires a prior 'lintle dedup' run."
        ),
    )
    extract_parser.add_argument(
        "norad_ids",
        metavar="NORAD-ID",
        nargs="+",
        type=int,
        help="catalog number(s) to extract (1-99999)",
    )
    extract_parser.add_argument(
        "--out-dir",
        metavar="DIR",
        default=_DEFAULT_OUTPUT,
        help="pipeline output tree holding 05-dedup/ (default: %(default)s)",
    )
    extract_parser.add_argument(
        "--dest",
        metavar="DIR",
        default=None,
        help=("where <id>.txt / <id>.json are written (default: <out-dir>/06-extract)"),
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
        required=False,
        metavar="{clean,dedup,diff,explain,extract,report,verify}",
        title="commands",
    )
    _add_clean_subparser(subparsers)
    _add_dedup_subparser(subparsers)
    _add_diff_subparser(subparsers)
    _add_explain_subparser(subparsers)
    _add_extract_subparser(subparsers)
    _add_report_subparser(subparsers)
    _add_verify_subparser(subparsers)
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
    resumed_names=frozenset(),
    print_results=True,
):
    """Finish a non-interrupted run: sort stats, write the clean-run artifacts,
    emit the text/JSON summary, tear down resumable state on success, and return
    the process exit code. Split out of :func:`main` so the orchestration there
    stays at the level of phases (check inputs -> plan -> dispatch -> finalize).
    ``resumed_names`` is the basenames carried over from a previous run, dimmed
    in the results table because their numbers came from that earlier run.
    ``print_results`` prints that table; the live display already ends on the
    same per-file numbers, so the caller suppresses it unless the table was
    windowed or never rendered."""
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
            args.out_dir,
            all_stats,
            envelope,
            failed_files=failed_files,
            chunk_records=args.chunk_records,
        )

    if args.report == "json":
        print(json.dumps(envelope, indent=2))
    elif all_stats:
        # Phase 3 of the display: the per-file results table, then the human
        # aggregate panel. Both go to stderr (styled ephemera) — per-file detail
        # also lives durably in report.md. Off a TTY they degrade to plain ASCII.
        # Text-mode stdout stays empty so a pipe sees nothing the report.json
        # artifact doesn't already carry. This revisits the old "no per-file
        # dump" rule deliberately: that rule was about polluting *stdout*.
        if print_results:
            summary.render_files(
                envelope, console=term.stderr_console, resumed=resumed_names
            )
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
        shard_dir = Path(args.out_dir) / SHARDS_DIRNAME
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


def _flag_present(argv, flag):
    """True if ``flag`` was passed on the command line (``--x`` or ``--x=…``)."""
    return any(a == flag or a.startswith(flag + "=") for a in argv)


def _apply_config_paths(args, argv, config):
    """Fill path arguments left at their defaults from the stored project config,
    so ``clean``/``verify``/``report`` can run without repeating paths. Precedence
    is always explicit CLI arg > stored config > built-in default — an explicit
    argument is never overridden. Mutates ``args`` in place."""
    match args.command:
        case "clean":
            if args.path is None and config.get("source"):
                args.path = config["source"]
            if not _flag_present(argv, "--out-dir") and config.get("output"):
                args.out_dir = config["output"]
        case "verify":
            args.out_dir = args.out_dir or config.get("output") or _DEFAULT_OUTPUT
            args.source = args.source or config.get("source") or _DEFAULT_SOURCE
        case "report" | "dedup":
            args.out_dir = args.out_dir or config.get("output") or _DEFAULT_OUTPUT
        case "extract":
            if not _flag_present(argv, "--out-dir") and config.get("output"):
                args.out_dir = config["output"]


def _locked_postrun(out_dir, name, action):
    """Run a post-run consumer (``verify``/``dedup``) under the out-dir lock
    with the same exit-2 operational backstop as ``clean`` (issue #89).

    Both consumers stream ``<out-dir>/01-cleaned`` and write their own
    subtree, so a concurrent ``clean`` scrubbing the out-dir mid-read would
    corrupt them — the advisory flock serializes them against it. A missing
    out-dir skips the lock (nothing to protect); the consumer's own "no
    cleaned output" error is friendlier than a lock failure would be.
    """
    try:
        if not Path(out_dir).is_dir():
            return action()
        with fsutil.out_dir_lock(out_dir, started=resume.run_started_stamp()):
            return action()
    except fsutil.LockHeldError as exc:
        term.error(str(exc))
        return 2
    except Exception as exc:
        # Catch-all backstop: any unhandled exception maps to a clean exit-2
        # operational error so exit 1 stays unambiguous as the findings signal.
        term.error(f"unexpected error during {name}: {exc}")
        _debug_traceback()
        return 2


def _debug_traceback() -> None:
    """Emit the full traceback to stderr when ``LINTLE_DEBUG`` is set — the
    opt-in escape hatch behind the clean single-line operational errors."""
    if os.environ.get("LINTLE_DEBUG"):
        term.error(traceback.format_exc())


def main(argv=None):
    """Entry point for the ``lintle`` console script — :func:`_dispatch` under a
    Ctrl-C backstop, so *every* subcommand exits 130 with one cancellation line
    instead of a traceback. ``clean`` normally catches its own SIGINT inside the
    worker pool and reports resume guidance there; this covers the windows
    outside it and the single-process consumers (``verify``/``dedup``/
    ``extract``), whose subtrees are committed only through atomic durable
    writes at the end — cancelling one leaves the prior tree intact, and the
    out-dir lock is released on the way out."""
    try:
        return _dispatch(argv)
    except KeyboardInterrupt:
        term.warning("cancelled.")
        return process_control.signal_exit_code(signal.SIGINT)


def _dispatch(argv=None):
    """The CLI body: parse, dispatch the subcommand, orchestrate ``clean``.

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

    # No subcommand: launch the interactive wizard on a TTY; off a TTY (scripts,
    # CI, pipes) keep the old "pick a command" behaviour by printing help and
    # exiting 2, so nothing that pipes `lintle` ever blocks on a prompt.
    if args.command is None:
        if term.is_interactive():
            # Lazy so the wizard's rich menu never loads for scripted runs; the
            # wizard itself never imports cli (main is injected), so the
            # dependency edge is strictly one-way.
            from lintle import wizard

            return wizard.run(main)
        build_parser().print_help(sys.stderr)
        return 2

    # Fill unset path arguments from the stored project config before any command
    # dispatches (explicit CLI arg > stored config > built-in default).
    argv_list = list(sys.argv[1:] if argv is None else argv)
    _apply_config_paths(args, argv_list, user_config.load())

    # Dispatch every non-`clean` subcommand first — none of them shares the
    # `clean` argument surface (paths, jobs, threshold), so they never reach
    # the clean orchestration below.
    match args.command:
        # `diff` is a read-only consumer of two report.jsonl files.
        case "diff":
            return diff.run(args.run_a, args.run_b)

        # `explain` is a read-only documentation lookup — no input files, no
        # output tree. An unknown tag is an operational error (exit 2) with
        # the valid tags listed so the operator can correct it.
        case "explain":
            try:
                _print_doc(explain.render(args.tag))
            except explain.UnknownTag:
                term.error(
                    f"unknown tag {args.tag!r}.\n"
                    f"  valid tags: {', '.join(explain.known_tags())}"
                )
                return 2
            return 0

        # `report` is a read-only render of a prior clean run's report.json —
        # text renders the aggregate panel to stdout, json echoes the file.
        case "report":
            return summary.run(args.out_dir, args.report)

        # `verify` is a post-run auditor of a clean run's cleaned output (plus
        # the source tree for the byte-diff). It never touches 01-cleaned/ but
        # does write <out-dir>/04-verify, so it runs under the out-dir lock like
        # every other writer.
        case "verify":
            # Lazy: keeps lintle.verify out of the clean path's module-level
            # import closure (the sgp4/verify wall — TestImportGuard).
            from lintle import verify

            source = None if args.no_source_diff else args.source
            return _locked_postrun(
                args.out_dir,
                "verify",
                lambda: verify.run(
                    args.out_dir,
                    source,
                    orbit=args.orbit,
                    sample=args.sample,
                    all_sats=args.all_sats,
                    sensitivity=args.sensitivity,
                    chunk_records=args.chunk_records,
                ),
            )

        # `dedup` consumes 01-cleaned/ (plus a prior verify run's suspects set);
        # it never touches 01-cleaned/ but does write <out-dir>/05-dedup, so it too
        # runs under the out-dir lock.
        case "dedup":
            # Lazy for the same wall reason: dedup imports lintle.verify.
            from lintle import dedup

            return _locked_postrun(
                args.out_dir,
                "dedup",
                lambda: dedup.run(args.out_dir, args.chunk_records),
            )

        # `extract` reads a prior dedup run's import chunk set (read-only) and
        # writes only <dest>, which defaults to <out-dir>/06-extract but runs
        # under the same out-dir lock as dedup/verify since a concurrent
        # `clean` scrubbing the out-dir mid-read would corrupt it.
        case "extract":
            # Lazy for the wall: extract imports lintle.verify parsers.
            from lintle import extract as extract_mod

            dest = args.dest or str(Path(args.out_dir) / EXTRACT_DIRNAME)
            return _locked_postrun(
                args.out_dir,
                "extract",
                lambda: extract_mod.run(
                    args.out_dir,
                    args.norad_ids,
                    dest,
                    write_readme=args.dest is None,
                ),
            )

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

    path_error = input_path_error(path, using_default=using_default)
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
    except OSError as exc:
        # A non-contention error acquiring the lock (e.g. ENOLCK, a read-only
        # out-dir). out_dir_lock has already closed its fd; surface it as a clean
        # exit 2 rather than an unhandled traceback.
        term.error(f"cannot lock output directory {args.out_dir!r}: {exc}")
        return 2

    # The try/finally guarantees _lock_stack.close() runs on every exit path
    # that reaches here: disk-error return, ABORT return, interrupt return,
    # failed-files return, and normal success — so the advisory flock is always
    # released (the kernel would drop it on process death regardless; the file
    # itself is intentionally left in place — see out_dir_lock).
    try:
        # Build the typed config snapshot from args ONCE so the two leaves
        # (resolve_clean_plan, run_workers) receive named, statically-typed
        # fields rather than a raw argparse Namespace.  A flag rename that
        # breaks from_args surfaces at this single construction site rather
        # than mid-run as an AttributeError inside a leaf (issue #121).
        config = run_planning.CleanConfig.from_args(args)
        try:
            plan = run_planning.resolve_clean_plan(config, files, file_sizes)
        except OSError as exc:
            term.error(f"preflight error: {exc}")
            return 2
        if plan.exit_code is not None:
            return plan.exit_code
        # Resolve the worker count now that files_to_process is final: an
        # explicit --jobs is honoured as-is; the default is CPU count - 1,
        # capped at the file count and floored at one (issue #53 §2.3).
        jobs = resolve_jobs(config.jobs, os.cpu_count(), len(plan.files_to_process))

        # The shared rich Console on stderr (term.stderr_console) drives both the
        # roster and the live progress block; off a TTY each degrades to plain
        # text. Byte-bar denominators come from os.stat (issue #53 §2.1/§2.2) —
        # no pre-read of the corpus.
        console = term.stderr_console
        # Every discovered file, in input order — not just the ones left to do.
        # On a resumed run the carried-over files still get rows (filled in from
        # the checkpoint), so the live table is the whole corpus and its
        # "N/M files" count matches the rows under it.
        sizes = {Path(p).name: file_sizes[p] for p in files}
        # On a TTY the live table opens on the same rows and fills them in, so
        # printing a separate roster first would be the same list twice. Off a
        # TTY there is no live table, and this static roster is the only
        # discovery view a piped run gets.
        if not console.is_terminal:
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
        run_started_iso = report.utc_stamp()
        run_monotonic_start = time.monotonic()

        result = worker_pool.run_workers(config, files, plan, jobs, console, sizes)

        if result.operational_error is not None:
            # Issue #89: parent-side bookkeeping failure (e.g. ENOSPC from
            # write_checkpoint). The pool has already been torn down via the
            # KI path; surface a clean error and return 2 (operational error).
            # Exit 1 is reserved exclusively for the quarantine quality gate.
            term.error(
                f"run aborted due to an operational error: {result.operational_error}\n"
                "  if some files completed, re-run with --resume to finish."
            )
            return 2

        if result.interrupted:
            return process_control.signal_exit_code(result.interrupted_signo)

        return _finalize_run(
            args,
            result.all_stats,
            result.failed_files,
            run_started_iso=run_started_iso,
            run_monotonic_start=run_monotonic_start,
            threshold_mode=threshold_mode,
            quarantine_threshold=quarantine_threshold,
            resumed_names=frozenset(s.src_name for s in plan.reused_stats),
            # The live table already ended on the per-file results, unless it
            # had to window them away or never ran at all (off a TTY).
            print_results=result.display_windowed or not console.is_terminal,
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
        _debug_traceback()
        return 2
    finally:
        _lock_stack.close()
