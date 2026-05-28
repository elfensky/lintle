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

from lintle import __version__, diff, explain, pipeline, report, resume

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
  0    quarantine count (or rate) is at or below --max-quarantined
  1    quarantine count (or rate) exceeded --max-quarantined
       (default: 0 — any quarantine fails)
  2    operational error (missing input, disk shortfall, file failure)
  130  interrupted (Ctrl-C)

See `lintle <command> --help` for command-specific options.
"""


def discover_paths(path):
    """Expand ``path``: a directory becomes its sorted ``tle*.txt`` files
    (excluding ``*.cleaned.txt`` / ``*.broken.txt`` tool output); a file is
    returned as a single-element list. A nonexistent entry yields ``[]`` —
    callers should validate inputs with :func:`check_paths` first.
    """
    if os.path.isdir(path):
        return [
            os.path.join(path, name)
            for name in sorted(os.listdir(path))
            if (
                name.startswith("tle")
                and name.endswith(".txt")
                and not name.endswith(".cleaned.txt")
                and not name.endswith(".broken.txt")
            )
        ]
    if os.path.isfile(path):
        return [path]
    return []


def parse_quarantine_threshold(raw):
    """Parse a ``--max-quarantined`` value into ``(mode, threshold)``.

    A bare integer (e.g. ``"100"``) is an absolute record count and returns
    ``("count", int)``. A value with a trailing ``%`` (e.g. ``"1%"`` or
    ``"1.5%"``) is a percentage of routed records and returns
    ``("pct", float)``; the percentage must lie in ``0..100``. Surrounding
    whitespace is tolerated. Raises :class:`ValueError` on malformed input
    or out-of-range values; the message for a negative count preserves the
    exact substring asserted by the legacy issue-#13 integration test.
    """
    raw = raw.strip()
    if raw.endswith("%"):
        body = raw[:-1].strip()
        try:
            pct = float(body)
        except ValueError:
            raise ValueError(f"--max-quarantined: invalid percentage {raw!r}") from None
        if not (0.0 <= pct <= 100.0):
            raise ValueError(
                f"--max-quarantined percentage must be in 0..100 (got {raw!r})"
            )
        return ("pct", pct)
    try:
        count = int(raw)
    except ValueError:
        raise ValueError(f"--max-quarantined: invalid value {raw!r}") from None
    if count < 0:
        raise ValueError(f"--max-quarantined must be >= 0 (got {count})")
    return ("count", count)


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
        metavar="{validate,clean,diff,explain}",
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
            default="0",
            metavar="N[%]",
            help=(
                "exit non-zero only if MORE than N records were quarantined; "
                "or, with a trailing `%%`, more than N%% of routed records "
                "(default: 0 — any quarantine fails)"
            ),
        )
        if name == "clean":
            sub.add_argument(
                "--resume",
                action="store_true",
                help=(
                    "continue an interrupted run in --out-dir: skip files already "
                    "completed (per its .clean-state.json checkpoint) and process "
                    "only the rest. Refuses if the lintle version or any input "
                    "changed since the interrupted run."
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

    # `explain` is a read-only documentation lookup: one positional TAG (a rule
    # ID like TLE-CHK-001 or a fix tag like reconstructed-checksum), no shared
    # validate/clean argument surface. Writes nothing.
    explain_parser = subparsers.add_parser(
        "explain",
        help="print what a rule ID or fix tag means, with examples",
        description=(
            "Explain one rejection rule (e.g. TLE-CHK-001) or repair tag (e.g. "
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

    Returns the process exit code: ``0`` = quarantine count (or rate) is at
    or below ``--max-quarantined``; ``1`` = it exceeded the threshold
    (default ``0`` — any quarantine fails); ``2`` = operational error (no
    input files, disk shortfall, or a file that failed to process); ``130``
    = interrupted with Ctrl-C. The threshold accepts either an integer
    record count (``--max-quarantined 100``) or a percentage of routed
    records (``--max-quarantined 1%``); see :func:`parse_quarantine_threshold`.
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
            print(
                f"error: unknown tag {args.tag!r}.\n"
                f"  valid tags: {', '.join(explain.known_tags())}",
                file=sys.stderr,
            )
            return 2
        return 0

    # `args.path` is None when the user passed nothing — fall back to the
    # default source dir, and remember it so we can give a tailored error if
    # that default doesn't exist on this machine.
    using_default = args.path is None
    path = args.path if args.path is not None else _DEFAULT_SOURCE

    if args.jobs < 1:
        print(f"error: --jobs must be >= 1 (got {args.jobs})", file=sys.stderr)
        return 2

    try:
        threshold_mode, quarantine_threshold = parse_quarantine_threshold(
            args.max_quarantined
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    path_error = check_paths([path], using_default=using_default)
    if path_error:
        print(f"error: {path_error}", file=sys.stderr)
        return 2

    files = discover_paths(path)
    if not files:
        if os.path.isdir(path):
            print(
                f"error: no tle*.txt files found in {path!r}.\n"
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

    # Defaults for the shared dispatch below; ``clean --resume`` narrows them.
    # ``inputs`` and ``completed`` drive the single-run resume checkpoint
    # (issue #56) and stay empty for ``validate``.
    files_to_process = files
    reused_stats = []
    inputs = {}
    completed = {}

    if args.command == "clean":
        os.makedirs(args.out_dir, exist_ok=True)
        disk_error = _check_disk_space(args.out_dir, files)
        if disk_error:
            print(f"error: {disk_error}", file=sys.stderr)
            return 2
        # Per-input identity for the resume checkpoint (issue #56): computed
        # once, up front, for every discovered file. Cheap and constant-memory
        # (a head+tail window hash), it is written into the checkpoint as files
        # complete so an interrupted run can be finished later.
        inputs = {path: resume.input_fingerprint(path) for path in files}
        shard_dir = os.path.join(args.out_dir, ".shards")
        if args.resume:
            checkpoint = resume.load_checkpoint(args.out_dir)
            if checkpoint is None:
                # load_checkpoint returns None for both "absent" and "present
                # but corrupt" — distinguish them so the operator can tell a
                # nothing-to-resume from a damaged checkpoint.
                ckpt_file = os.path.join(args.out_dir, resume.CHECKPOINT_NAME)
                if os.path.exists(ckpt_file):
                    print(
                        f"error: cannot resume: {resume.CHECKPOINT_NAME} in "
                        f"{args.out_dir!r} is unreadable or corrupt.\n"
                        "  re-run without --resume to do a clean full pass.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"error: no interrupted run to resume in {args.out_dir!r} "
                        f"(no {resume.CHECKPOINT_NAME} found)",
                        file=sys.stderr,
                    )
                return 2
            reason = resume.validate_resumable(checkpoint, inputs)
            if reason is not None:
                print(
                    f"error: cannot resume: {reason}.\n"
                    "  re-run without --resume to do a clean full pass.",
                    file=sys.stderr,
                )
                return 2
            # Reuse files already committed; reconstruct their stats so the
            # final report covers the whole corpus. Their findings shards
            # survived the interruption (resume does NOT scrub ``.shards``),
            # so ``report.jsonl`` stays complete.
            completed = dict(checkpoint["completed"])
            reused_stats = [report.stats_from_summary(s) for s in completed.values()]
            files_to_process = [f for f in files if f not in completed]
        else:
            # Fresh run: any leftover checkpoint describes a different attempt
            # and must not be resumable after this run partially overwrites
            # outputs; and ``.shards`` from a prior aborted run would
            # contaminate this run's ``report.jsonl`` (issue #9, spec §4.6).
            # Clear both before writing anything.
            resume.delete_checkpoint(args.out_dir)
            if os.path.exists(shard_dir):
                shutil.rmtree(shard_dir)

    reused_n = len(reused_stats)
    if reused_n:
        print(
            f"resuming: {reused_n} file(s) already complete, processing "
            f"{len(files_to_process)} of {len(files)} with {args.jobs} worker(s)...",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            f"processing {len(files_to_process)} file(s) with {args.jobs} worker(s)...",
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
    # Files reused from a resume checkpoint are already done — seed the report
    # with their reconstructed stats so corpus totals cover the whole run.
    all_stats = list(reused_stats)
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
                for path in files_to_process
            }
            with _ProgressDisplay(len(files_to_process), progress_queue) as progress:
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
                        # Record this file as completed in the always-on resume
                        # checkpoint (issue #56). Both the worker's outputs and
                        # this checkpoint write go through fsutil.durable_replace,
                        # so a checkpoint entry can only name a file whose bytes
                        # are on disk — the ordering invariant --resume's "trust
                        # without reprocessing" requires (issue #58).
                        if args.command == "clean":
                            completed[path] = report.summary_dict(stats)
                            resume.write_checkpoint(
                                args.out_dir,
                                resume.build_checkpoint(
                                    inputs=inputs, completed=completed
                                ),
                            )
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

    # A fully successful clean run leaves no resumable state behind. The
    # checkpoint and the findings shards (`.shards`) are both in-progress run
    # state and are torn down together, here, ONLY on success: an interrupted
    # run already returned 130 above (keeping both), and a failed run keeps both
    # too, so the operator can fix the cause and `--resume` re-reads the
    # surviving shards to rebuild a complete `report.jsonl` (issue #56). The
    # `report.jsonl` was written from those shards by the report block above.
    if args.command == "clean" and not failed_files:
        resume.delete_checkpoint(args.out_dir)
        shard_dir = os.path.join(args.out_dir, ".shards")
        if os.path.exists(shard_dir):
            shutil.rmtree(shard_dir)

    # A file that could not be processed is an operational error (spec §10),
    # and that outranks the quarantined-record signal.
    if failed_files:
        return 2
    total_quarantined = sum(s.quarantined_count for s in all_stats)
    if threshold_mode == "count":
        return 1 if total_quarantined > quarantine_threshold else 0
    # Rate mode: cross-multiplied (`100*q > p*r`) to avoid divide-by-zero on
    # an empty corpus and float drift at the boundary. See design §3.
    total_routed = sum(s.clean_count + s.quarantined_count for s in all_stats)
    if 100 * total_quarantined > quarantine_threshold * total_routed:
        return 1
    return 0
