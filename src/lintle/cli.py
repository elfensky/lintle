"""Command-line interface: ``lintle clean``, ``lintle diff``, ``lintle explain``."""

import argparse
import signal
import sys
from pathlib import Path

from lintle import (
    EXTRACT_DIRNAME,
    __version__,
    clean_run,
    diff,
    explain,
    fsutil,
    process_control,
    resume,
    summary,
    term,
    tle,
)
from lintle import (
    config as user_config,
)
from lintle.chunking import CHUNK_RECORDS_DEFAULT

# Re-exported for compatibility: the clean orchestration (and these helpers)
# moved to clean_run.py, but ``cli.discover_paths`` et al. remain the public
# spellings used by callers and tests.
from lintle.clean_run import (
    _DEFAULT_OUTPUT,
    _DEFAULT_SOURCE,
    _debug_traceback,
    discover_paths,  # noqa: F401
    input_path_error,  # noqa: F401
    resolve_jobs,  # noqa: F401
)


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


def _norad_id_type(value):
    """argparse type for ``extract``'s NORAD-ID: the plain decimal catalog
    number or, since the SATCAT passed 99,999, the Alpha-5 wire spelling
    (``E8493``), decoded to the integer every downstream artifact speaks.
    Routed through ``tle.decode_catalog`` so the CLI and the cleaned-tree
    reader cannot disagree about which satellite the caller means."""
    catalog = tle.decode_catalog(value)
    if catalog is None or catalog < 1:
        raise argparse.ArgumentTypeError(
            f"not a NORAD id: {value!r} (expected a catalog number like 25544 "
            "or an Alpha-5 id like E8493)"
        )
    return catalog


def _add_out_dir_positional(parser, purpose):
    """Add the shared optional ``OUT-DIR`` positional (report/verify/dedup).
    ``purpose`` completes "clean run output directory ..." in the help text.
    ``default=None`` is load-bearing: :func:`_apply_config_paths` distinguishes
    "omitted" from an explicitly-given empty path by identity, not truthiness."""
    parser.add_argument(
        "out_dir",
        nargs="?",
        default=None,
        metavar="OUT-DIR",
        help=(
            f"clean run output directory{purpose} "
            f"(default: stored config, else {_DEFAULT_OUTPUT})"
        ),
    )


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
    _add_out_dir_positional(report_parser, "")
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
    _add_out_dir_positional(verify_parser, " to verify")
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
    _add_out_dir_positional(dedup_parser, " to de-duplicate")
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
        type=_norad_id_type,
        help="catalog number(s) to extract, decimal or Alpha-5 (e.g. 25544, E8493)",
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


def _flag_present(argv, flag):
    """True if ``flag`` was passed on the command line (``--x`` or ``--x=…``)."""
    return any(a == flag or a.startswith(flag + "=") for a in argv)


def _resolved_path(explicit, configured, fallback):
    """One spelling of "explicit CLI arg > stored config > built-in default".
    ``explicit is None`` means the argument was omitted (the positionals' argparse
    default); an empty string was *given*, so it wins. A configured-but-empty
    value is meaningless and falls through to ``fallback``."""
    if explicit is not None:
        return explicit
    return configured or fallback


def _apply_config_paths(args, argv, config):
    """Fill path arguments left at their defaults from the stored project config,
    so ``clean``/``verify``/``report`` can run without repeating paths. Precedence
    is always explicit CLI arg > stored config > built-in default — an explicit
    argument is never overridden. Mutates ``args`` in place.

    "Not given" is tested as ``is None`` (the positionals' argparse default), never
    for truthiness: an explicitly-passed empty path is still explicit. The `or`
    chain this replaced silently swapped it for the stored config — quietly
    operating on a *different* tree than the one asked for, which is exactly the
    guarantee above."""
    match args.command:
        case "clean":
            if args.path is None and config.get("source"):
                args.path = config["source"]
            if not _flag_present(argv, "--out-dir") and config.get("output"):
                args.out_dir = config["output"]
        case "verify":
            args.out_dir = _resolved_path(
                args.out_dir, config.get("output"), _DEFAULT_OUTPUT
            )
            args.source = _resolved_path(
                args.source, config.get("source"), _DEFAULT_SOURCE
            )
        case "report" | "dedup":
            args.out_dir = _resolved_path(
                args.out_dir, config.get("output"), _DEFAULT_OUTPUT
            )
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
    """The CLI body: parse, dispatch the subcommand, hand ``clean`` to
    :func:`clean_run.run`.

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

            # `is None`, never truthiness: an explicitly-passed empty dest is
            # still explicit (same rule as `_resolved_path`) — `or` sent it to
            # the default 06-extract while `write_readme` below said otherwise.
            if args.dest is not None:
                dest = args.dest
            else:
                dest = str(Path(args.out_dir) / EXTRACT_DIRNAME)
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

    # `clean` is the one subcommand with real orchestration; it lives in
    # clean_run.py (discovery, preflight, lock, worker pool, finalize, and
    # the exit-code join).
    return clean_run.run(args)
