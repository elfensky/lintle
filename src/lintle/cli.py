"""Command-line interface: ``lintle validate`` and ``lintle clean``."""

import argparse
import concurrent.futures
import contextlib
import json
import multiprocessing
import os
import queue
import shutil
import signal
import sys
import threading
import time

from lintle import __version__, pipeline, report

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

Exit codes:
  0    no records quarantined — every defect repaired
  1    at least one record was quarantined
  2    operational error (missing input, disk shortfall, file failure)
  130  interrupted (Ctrl-C)

See `lintle <command> --help` for command-specific options.
"""


def discover_paths(paths):
    """Expand each entry in ``paths``: a directory becomes its sorted
    ``tle*.txt`` files (excluding ``*.cleaned.txt`` / ``*.broken.txt`` tool
    output); a file is passed through unchanged. Nonexistent entries are
    dropped — callers should validate inputs with :func:`check_paths` first.
    """
    result = []
    for path in paths:
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if (
                    name.startswith("tle")
                    and name.endswith(".txt")
                    and not name.endswith(".cleaned.txt")
                    and not name.endswith(".broken.txt")
                ):
                    result.append(os.path.join(path, name))
        elif os.path.isfile(path):
            result.append(path)
    return result


def check_paths(paths, using_default):
    """Return a user-facing error string if any ``paths`` entry is missing
    or unreadable, else ``None``. ``using_default`` tailors the message for
    the case where the user passed no paths at all and the default
    (``data/source``) is what's missing.
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
    unreadable = [p for p in paths if not os.access(p, os.R_OK)]
    if unreadable:
        joined = ", ".join(repr(p) for p in unreadable)
        return f"permission denied: {joined}"
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
        metavar="{validate,clean}",
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
    """
    processes = getattr(executor, "_processes", None) or {}
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
        """Fold every queued record-count delta into the running total."""
        batch = 0
        with contextlib.suppress(queue.Empty):
            while True:
                batch += self._queue.get_nowait()
        if batch:
            with self._lock:
                self._records += batch

    def _render(self):
        with self._lock:
            frame = self._SPINNER[self._frame % len(self._SPINNER)]
            self._frame += 1
            line = (
                f"{frame} {_format_elapsed(time.monotonic() - self._start)} · "
                f"{self._files_done}/{self._total_files} files · "
                f"{self._records:,} records"
            )
            sys.stderr.write("\r\x1b[K" + line)
            sys.stderr.flush()


def main(argv=None):
    """Entry point for the ``lintle`` console script.

    Returns the process exit code: ``0`` = no records quarantined;
    ``1`` = at least one record quarantined; ``2`` = operational error
    (no input files, disk shortfall, or a file that failed to process);
    ``130`` = interrupted with Ctrl-C.
    """
    args = build_parser().parse_args(argv)

    # `args.paths` is None when the user passed nothing — fall back to the
    # default source dir, and remember it so we can give a tailored error if
    # that default doesn't exist on this machine.
    using_default = not args.paths
    paths = args.paths or [_DEFAULT_SOURCE]

    if args.jobs < 1:
        print(f"error: --jobs must be >= 1 (got {args.jobs})", file=sys.stderr)
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

    if args.command == "clean":
        os.makedirs(args.out_dir, exist_ok=True)
        disk_error = _check_disk_space(args.out_dir, files)
        if disk_error:
            print(f"error: {disk_error}", file=sys.stderr)
            return 2

    print(
        f"processing {len(files)} file(s) with {args.jobs} worker(s)...",
        file=sys.stderr,
        flush=True,
    )
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

    # A `clean` run writes a Markdown run report to the out-dir root.
    report_path = None
    if args.command == "clean" and all_stats:
        report_path = os.path.join(args.out_dir, "report.md")
        report.write_run_report(report_path, all_stats)

    if args.report == "json":
        print(json.dumps([report.summary_dict(s) for s in all_stats], indent=2))
    else:
        for stats in all_stats:
            print(report.format_summary(stats))
            if args.command == "validate" and stats.reject_exemplars:
                print(report.format_reject_lines(stats))
        if report_path:
            print(f"\nrun report: {report_path}")

    # A file that could not be processed is an operational error (spec §10),
    # and that outranks the quarantined-record signal.
    if failed_files:
        return 2
    total_quarantined = sum(s.quarantined_count for s in all_stats)
    return 1 if total_quarantined else 0
