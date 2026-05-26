"""Command-line interface: ``lintle validate``, ``lintle clean``, ``lintle diff``."""

import argparse
import concurrent.futures
import contextlib
import datetime
import json
import multiprocessing
import os
import queue
import shutil
import signal
import sys
import threading
import time

from lintle import __version__, diff, pipeline, report

_DEFAULT_SOURCE = "data/source"
_DEFAULT_OUTPUT = "data/output"

_EPILOG = """\
Examples:
  lintle validate                         audit data/source/ (read-only)
  lintle clean                            clean data/source/ -> data/output/
  lintle validate file.txt                audit a single file
  lintle clean dir1 dir2 --jobs 4         clean multiple directories
  lintle clean data/raw --out-dir build   write to a custom location
  lintle validate --report json           emit a machine-readable summary
  lintle diff run-a/ run-b/               compare two runs' findings

Exit codes:
  0    no records quarantined — every defect repaired (or under --max-quarantined)
  1    more than --max-quarantined records were quarantined (default threshold: 0)
  2    operational error (missing input, disk shortfall, file failure)
  130  interrupted (Ctrl-C)

See `lintle <command> --help` for command-specific options.
"""


def discover_paths(paths):
    """Expand each entry in ``paths``: a directory becomes its sorted
    ``tle*.txt`` files (excluding ``*.cleaned.txt`` / ``*.broken.txt`` tool
    output); a file is passed through unchanged. Nonexistent entries are
    dropped — callers should validate inputs with :func:`check_paths` first.
    Duplicates (same canonical path via ``os.path.realpath``) are collapsed,
    so e.g. ``lintle clean dirA dirA/foo.txt`` processes ``foo.txt`` once.
    """
    result = []
    seen = set()

    def _add(candidate):
        canonical = os.path.realpath(candidate)
        if canonical in seen:
            return
        seen.add(canonical)
        result.append(candidate)

    for path in paths:
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if (
                    name.startswith("tle")
                    and name.endswith(".txt")
                    and not name.endswith(".cleaned.txt")
                    and not name.endswith(".broken.txt")
                ):
                    _add(os.path.join(path, name))
        elif os.path.isfile(path):
            _add(path)
    return result


def _detect_basename_collisions(files):
    """Return a user-facing error string if any two ``files`` share a
    basename, else ``None``. Cleaned and broken outputs are keyed by
    basename, so colliding inputs would silently overwrite each other —
    safer to refuse the run than to publish a wrong-but-valid-looking
    output (spec §1, "correctness over recovery").
    """
    groups = {}
    for path in files:
        groups.setdefault(os.path.basename(path), []).append(path)
    collisions = {name: ps for name, ps in groups.items() if len(ps) > 1}
    if not collisions:
        return None
    lines = [
        "output collision: inputs share a basename and would overwrite each other:"
    ]
    for name in sorted(collisions):
        lines.append(f"  '{name}':")
        for path in collisions[name]:
            lines.append(f"    - {path}")
    lines.append("  rename the inputs or process them in separate runs (--out-dir).")
    return "\n".join(lines)


def check_paths(paths, using_default):
    """Return a user-facing error string if any ``paths`` entry is missing,
    else ``None``. ``using_default`` tailors the message for the case where
    the user passed no paths at all and the default (``data/source``) is
    what's missing.

    Readability is *not* checked here — :func:`os.access` consults the
    POSIX mode bits only and is a false-negative on filesystems that grant
    read through ACLs (NFSv4, SMB, FUSE). The authoritative answer is
    whatever the worker's :func:`open` returns; a genuine permission error
    surfaces through the per-file failure path in :func:`main` with the
    same exit code 2.
    """
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        if using_default:
            return (
                f"default input directory {_DEFAULT_SOURCE!r} does not exist.\n"
                f"  pass one or more files or directories on the command line,\n"
                f"  or create {_DEFAULT_SOURCE}/ and put your tle*.txt files there.\n"
                f"  run 'lintle --help' for usage and examples."
            )
        if len(missing) == 1:
            return f"no such file or directory: {missing[0]!r}"
        joined = ", ".join(repr(p) for p in missing)
        return f"no such files or directories: {joined}"
    return None


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
        metavar="{validate,clean,diff}",
        title="commands",
    )
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
            "paths",
            nargs="*",
            default=None,
            metavar="PATH",
            help=(
                f"files or directories to process "
                f"(default: {_DEFAULT_SOURCE}). "
                "Directories are globbed for tle*.txt."
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
            default=os.cpu_count() or 1,
            metavar="N",
            help="number of files to process in parallel (default: CPU count)",
        )
        sub.add_argument(
            "--report",
            choices=["text", "json"],
            default="text",
            help="summary output format (default: text)",
        )
        sub.add_argument(
            "--max-quarantined",
            type=int,
            default=0,
            metavar="N",
            help=(
                "exit non-zero only if MORE than N records were quarantined "
                "(default: 0 — any quarantine fails)"
            ),
        )

    # `diff` has a different shape from validate/clean — two positional run
    # directories, no --out-dir / --jobs / --report / --max-quarantined. It is
    # read-only: it consumes each run's report.jsonl and writes nothing.
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
    return parser


def _check_disk_space(out_dir, files):
    """Return an error string if ``out_dir`` lacks room for cleaned +
    broken output (roughly twice the total input size), else ``None``.
    """
    needed = sum(os.path.getsize(f) for f in files) * 2
    free = shutil.disk_usage(out_dir).free
    if free < needed:
        return (
            f"insufficient disk space in {out_dir}: "
            f"need ~{needed:,} bytes, have {free:,}"
        )
    return None


def _ignore_sigint():
    """Worker-process initializer: ignore Ctrl-C in the worker.

    Without this, a terminal Ctrl-C is delivered to every worker too, and
    each one prints its own ``KeyboardInterrupt`` traceback. Making the
    parent the only process that sees the interrupt keeps shutdown tidy —
    it catches it once and terminates the workers itself.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _terminate_workers(executor):
    """SIGTERM every pool worker so Ctrl-C need not wait on in-flight files.

    ``ProcessPoolExecutor`` offers no public "stop now": leaving its
    ``with`` block runs ``shutdown(wait=True)``, which blocks until every
    running task finishes — and one TLE corpus file can take minutes.
    Terminating the worker processes directly makes Ctrl-C feel immediate.

    The fast path reaches into the private ``_processes`` mapping (no
    public equivalent exists on CPython 3.11–3.13). If a future runtime
    removes or renames it, fall back to ``shutdown(cancel_futures=True)`` —
    slower (waits for running tasks) but always available — and tell the
    operator why Ctrl-C felt sluggish.
    """
    try:
        processes = executor._processes
    except AttributeError:
        print(
            "lintle: ProcessPoolExecutor._processes unavailable; "
            "falling back to shutdown(cancel_futures=True) — "
            "Ctrl-C may wait for in-flight tasks.",
            file=sys.stderr,
        )
        executor.shutdown(cancel_futures=True)
        return
    for proc in list(processes.values()):
        proc.terminate()


def _format_elapsed(seconds):
    """Render an elapsed duration as ``M:SS`` (or ``H:MM:SS`` past an hour)."""
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class _ProgressDisplay:
    """A live, single-line progress indicator for a parallel run.

    On a terminal, a background thread repaints one self-overwriting line
    — spinner, elapsed time, files completed, and the running total of
    records processed (workers stream their counts in over the progress
    queue). When stderr is not a TTY (a pipe or a log file) there is no
    spinner: one plain line is printed per completed file instead, so
    redirected output stays readable.

    Used as a context manager: the thread starts on entry and is stopped,
    with the live line cleared, on exit.
    """

    _SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _REFRESH = 0.1  # seconds between repaints — ~10 fps: smooth but cheap

    def __init__(self, total_files, progress_queue):
        self._total_files = total_files
        self._queue = progress_queue
        self._live = sys.stderr.isatty()
        self._records = 0
        self._files_done = 0
        self._frame = 0
        self._start = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        # Files currently in flight, keyed by basename in insertion order so
        # the earliest entry is the candidate "slow" file once peers finish.
        # Values are start-times — kept for symmetry with the worker events,
        # not currently rendered.
        self._active = {}

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join()
        if self._live:
            sys.stderr.write("\r\x1b[K")
            sys.stderr.flush()
        return False

    def file_done(self, stats):
        """Count a finished file; off a TTY, log its one-line summary."""
        done = self._advance()
        if not self._live:
            self.log(
                f"[{done}/{self._total_files}] {stats.src_name} — "
                f"{stats.clean_count:,} clean, "
                f"{stats.quarantined_count:,} quarantined"
            )

    def file_failed(self, path, exc):
        """Count a file that could not be processed and log the error."""
        done = self._advance()
        self.log(f"[{done}/{self._total_files}] error processing {path}: {exc!r}")

    def log(self, message):
        """Print a line without the live status line swallowing it."""
        with self._lock:
            if self._live:
                sys.stderr.write("\r\x1b[K")
            sys.stderr.write(message + "\n")
            sys.stderr.flush()

    def _advance(self):
        with self._lock:
            self._files_done += 1
            return self._files_done

    def _run(self):
        # The display is cosmetic: never let a broken progress queue (e.g.
        # its manager gone mid-shutdown) crash this thread with a traceback.
        with contextlib.suppress(Exception):
            while not self._stop.is_set():
                self._drain()
                if self._live:
                    self._render()
                self._stop.wait(self._REFRESH)
            self._drain()

    def _drain(self):
        """Fold every queued message into the running state. Two kinds
        share one queue: ``int`` deltas (records processed since the last
        tick) and ``(kind, name)`` lifecycle events (``"start"`` / ``"end"``)
        from each worker.
        """
        batch = 0
        started = []
        ended = []
        with contextlib.suppress(queue.Empty):
            while True:
                msg = self._queue.get_nowait()
                if isinstance(msg, int):
                    batch += msg
                else:
                    kind, name = msg
                    (started if kind == "start" else ended).append(name)
        if batch or started or ended:
            with self._lock:
                self._records += batch
                for name in started:
                    self._active[name] = time.monotonic()
                for name in ended:
                    self._active.pop(name, None)

    def _render(self):
        with self._lock:
            frame = self._SPINNER[self._frame % len(self._SPINNER)]
            self._frame += 1
            elapsed = time.monotonic() - self._start
            # Guard against the first frame's near-zero elapsed: integer
            # division by 0 raises; floor it to 0 rec/s until time accrues.
            rps = int(self._records / elapsed) if elapsed >= 1.0 else 0
            line = (
                f"{frame} {_format_elapsed(elapsed)} · "
                f"{self._files_done}/{self._total_files} files · "
                f"{self._records:,} records · "
                f"{rps:,} rec/s"
            )
            if self._active:
                # The oldest entry is the longest-running file — the one
                # that surfaces alone once peers finish, which is the whole
                # point of showing names at all.
                names = list(self._active)
                line += f" · {names[0]}"
                if len(names) > 1:
                    line += f" +{len(names) - 1} more"
            sys.stderr.write("\r\x1b[K" + line)
            sys.stderr.flush()


def main(argv=None):
    """Entry point for the ``lintle`` console script.

    Returns the process exit code: ``0`` = total quarantined is at or below
    ``--max-quarantined`` (default 0); ``1`` = more than ``--max-quarantined``
    records quarantined; ``2`` = operational error (no input files, disk
    shortfall, or a file that failed to process); ``130`` = interrupted with
    Ctrl-C.
    """
    args = build_parser().parse_args(argv)

    # `diff` is a read-only consumer of two report.jsonl files; it shares none
    # of the validate/clean argument surface (paths, jobs, out-dir, threshold),
    # so dispatch it before any of that logic runs.
    if args.command == "diff":
        return diff.run(args.run_a, args.run_b)

    # `args.paths` is None when the user passed nothing — fall back to the
    # default source dir, and remember it so we can give a tailored error if
    # that default doesn't exist on this machine.
    using_default = not args.paths
    paths = args.paths or [_DEFAULT_SOURCE]

    if args.jobs < 1:
        print(f"error: --jobs must be >= 1 (got {args.jobs})", file=sys.stderr)
        return 2

    if args.max_quarantined < 0:
        print(
            f"error: --max-quarantined must be >= 0 (got {args.max_quarantined})",
            file=sys.stderr,
        )
        return 2

    path_error = check_paths(paths, using_default=using_default)
    if path_error:
        print(f"error: {path_error}", file=sys.stderr)
        return 2

    files = discover_paths(paths)
    if not files:
        dirs = [p for p in paths if os.path.isdir(p)]
        if dirs:
            joined = ", ".join(repr(d) for d in dirs)
            print(
                f"error: no tle*.txt files found in {joined}.\n"
                "  expected one or more files named tle*.txt "
                "(excluding *.cleaned.txt / *.broken.txt).",
                file=sys.stderr,
            )
        else:
            print("error: no input files found", file=sys.stderr)
        return 2

    collision_error = _detect_basename_collisions(files)
    if collision_error:
        print(f"error: {collision_error}", file=sys.stderr)
        return 2

    if args.command == "clean":
        os.makedirs(args.out_dir, exist_ok=True)
        disk_error = _check_disk_space(args.out_dir, files)
        if disk_error:
            print(f"error: {disk_error}", file=sys.stderr)
            return 2
        # Pre-run shard-dir scrub (issue #9, spec §4.6). Workers write
        # per-file findings shards under ``<out_dir>/.shards/`` which the
        # post-run concat consumes. Leftover shards from a prior aborted
        # run (SIGINT terminates workers outright, bypassing context-
        # manager cleanup) would contaminate this run's ``report.jsonl``
        # if not purged before any new shard is written. The scrub is
        # required, not best-effort — relying on ``os.makedirs(exist_ok=True)``
        # alone preserves prior contents.
        shard_dir = os.path.join(args.out_dir, ".shards")
        if os.path.exists(shard_dir):
            shutil.rmtree(shard_dir)

    print(
        f"processing {len(files)} file(s) with {args.jobs} worker(s)...",
        file=sys.stderr,
        flush=True,
    )
    # Run-level timing for the v1 envelope (issue #20). The wall-clock
    # start captures NOW (just before worker dispatch); the corresponding
    # stop happens after the executor drains. Two separate measurements:
    # ``run_started_iso`` is an ISO 8601 UTC string for human-readable
    # ``run.timestamp``; ``run_monotonic_start`` feeds the elapsed_seconds
    # subtraction using a monotonic clock so NTP jitter mid-run cannot
    # produce a negative duration. Per spec §4, this aggregate is
    # intentionally NOT the sum of per-file worker durations.
    run_started_iso = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_monotonic_start = time.monotonic()
    all_stats = []
    failed_files = []
    interrupted = False

    # The executor runs without a `with` block deliberately: that block's
    # __exit__ calls shutdown(wait=True), which on Ctrl-C would block until
    # every in-flight corpus file finished. Instead the workers ignore
    # SIGINT (so only this process sees it) and, on interrupt, we terminate
    # them outright. A manager queue carries record counts back for display.
    with multiprocessing.Manager() as manager:
        progress_queue = manager.Queue()
        executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=args.jobs, initializer=_ignore_sigint
        )
        try:
            futures = {
                executor.submit(
                    pipeline.process_file,
                    path,
                    args.out_dir,
                    args.command,
                    progress_queue,
                ): path
                for path in files
            }
            with _ProgressDisplay(len(files), progress_queue) as progress:
                for future in concurrent.futures.as_completed(futures):
                    path = futures[future]
                    try:
                        stats = future.result()
                    except Exception as exc:
                        progress.file_failed(path, exc)
                        failed_files.append(path)
                    else:
                        all_stats.append(stats)
                        progress.file_done(stats)
        except KeyboardInterrupt:
            # Ignore any further Ctrl-C so the shutdown itself cannot be
            # interrupted half-way (which is what left it hung before).
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            interrupted = True
            _terminate_workers(executor)
            executor.shutdown(wait=False, cancel_futures=True)
            print("interrupted — workers stopped", file=sys.stderr, flush=True)
        else:
            executor.shutdown(wait=True)

    if interrupted:
        return 130

    all_stats.sort(key=lambda stats: stats.src_name)

    # A `clean` run writes a Markdown run report, a corpus-wide NDJSON of
    # NORAD IDs whose records were quarantined anywhere, and (issue #9) a
    # corpus-wide ``report.jsonl`` of structured findings concatenated
    # from the per-worker shards. All three are always written on a
    # successful clean run — empty when nothing was quarantined — so
    # downstream consumers see a stable artifact set.
    report_path = None
    noradids_path = None
    findings_path = None
    if args.command == "clean" and all_stats:
        report_path = os.path.join(args.out_dir, "report.md")
        report.write_run_report(report_path, all_stats)
        noradids_path = os.path.join(args.out_dir, "broken-noradids.ndjson")
        report.write_broken_noradids_ndjson(noradids_path, all_stats)
        findings_path = os.path.join(args.out_dir, "report.jsonl")
        report.concat_findings_shards(args.out_dir, findings_path, all_stats)

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
            if args.command == "validate" and stats.reject_sample.buckets:
                print(report.format_reject_lines(stats))
        if report_path:
            print(f"\nrun report: {report_path}")
        if noradids_path:
            print(f"broken NORAD IDs: {noradids_path}")
        if findings_path:
            print(f"findings: {findings_path}")

    # A file that could not be processed is an operational error (spec §10),
    # and that outranks the quarantined-record signal.
    if failed_files:
        return 2
    total_quarantined = sum(s.quarantined_count for s in all_stats)
    return 1 if total_quarantined > args.max_quarantined else 0
