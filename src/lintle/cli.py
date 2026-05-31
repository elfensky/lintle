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

from rich import box
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
from rich.table import Table

from lintle import __version__, diff, explain, fsutil, pipeline, report, resume, stem

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
    if os.path.exists(path):
        return None
    if using_default:
        return (
            f"default input directory {_DEFAULT_SOURCE!r} does not exist.\n"
            f"  pass a file or directory on the command line,\n"
            f"  or create {_DEFAULT_SOURCE}/ and put your tle*.txt files there.\n"
            f"  run 'lintle --help' for usage and examples."
        )
    return f"no such file or directory: {path!r}"


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


def _is_interactive():
    """A run is interactive iff stdin is a TTY (the prompt answer is read there)
    and no CI/NONINTERACTIVE env var forces non-interactive — which prevents a
    CI runner that allocates a pseudo-TTY from hanging on the prompt (spec §2.2)."""
    if os.environ.get("CI") or os.environ.get("NONINTERACTIVE"):
        return False
    try:
        return sys.stdin.isatty()
    except AttributeError:
        return False
    except ValueError:
        return False


def _prompt_yes_no(message, *, default):
    """Ask a y/n question on stderr, reading the answer from stdin (spec §2.4).
    Enter takes ``default``; up to 3 unrecognised answers then give up; EOF/Ctrl-D
    gives up. Returns True/False, or None when the operator gave no usable answer
    (caller treats None as abort)."""
    for _ in range(3):
        print(message, end="", file=sys.stderr, flush=True)
        line = sys.stdin.readline()
        if line == "":  # EOF / Ctrl-D
            print(file=sys.stderr)
            return None
        token = line.strip().lower()
        if token == "":
            return default
        if token in ("y", "yes"):
            return True
        if token in ("n", "no"):
            return False
        print("  please answer y or n.", file=sys.stderr)
    return None


def _check_disk_space(out_dir, input_bytes):
    """Return a ``(severity, message)`` tuple when ``out_dir``'s free space
    is at or near the 2× input-size guard, else ``None``. Severity is
    ``"error"`` when free is below 2× input (caller aborts with exit 2);
    ``"warn"`` when free sits in the borderline band 2× to 2.5× (caller
    proceeds but surfaces the warning so the user knows they're cutting
    it close). Cleaned + broken output is ~1× input; the 2× guard leaves
    transient headroom for ``.partial`` files coexisting with their final
    renames mid-run. ``input_bytes`` is the total source size, stat'd once
    by the caller and shared with the roster and byte-bar denominators.
    """
    needed = input_bytes * 2
    free = shutil.disk_usage(out_dir).free
    if free < needed:
        return (
            "error",
            f"insufficient disk space in {out_dir}: "
            f"need ~{needed:,} bytes, have {free:,}",
        )
    if free < int(needed * 1.25):
        return (
            "warn",
            f"free space in {out_dir} is close to the 2× safety guard: "
            f"{free:,} bytes free of ~{needed:,} recommended; "
            f"the run will proceed but may exhaust the disk",
        )
    return None


def _scrub_outputs(out_dir):
    """Clear the cleaned/, broken/, and .shards/ trees so a fresh run starts from
    a clean slate and never leaves orphaned outputs from a prior, differently
    scoped input set (spec §3.4). Idempotent — missing trees are ignored."""
    for sub in ("cleaned", "broken", ".shards"):
        shutil.rmtree(os.path.join(out_dir, sub), ignore_errors=True)


def _run_started_stamp():
    """ISO-8601 UTC timestamp for archive/lock naming. Isolated so the rest of the
    resume logic stays clock-free and testable."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def _output_sizes(out_dir, stats):
    """Map each output basename this file produced to its on-disk size, captured
    at completion for the resume integrity check (spec §3.6). The broken sidecar
    is present only when something was quarantined."""
    sizes = {}
    cleaned = stem(stats.src_name) + ".cleaned.txt"
    with contextlib.suppress(OSError):
        sizes[cleaned] = os.path.getsize(os.path.join(out_dir, "cleaned", cleaned))
    if stats.quarantined_count:
        broken = stem(stats.src_name) + ".broken.txt"
        with contextlib.suppress(OSError):
            sizes[broken] = os.path.getsize(os.path.join(out_dir, "broken", broken))
    return sizes


def _signal_exit_code(signo):
    """Conventional 128 + signal number (spec §2.7): 130 SIGINT, 143 SIGTERM,
    129 SIGHUP."""
    return 128 + int(signo)


def _format_cancel_message(*, done, total):
    return (
        f"interrupted — workers stopped ({done}/{total} files done).\n"
        "Re-run the same command (same --out-dir) to continue where it stopped; "
        "inputs must be unchanged.\n"
        "Pass --no-resume to start over."
    )


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
    """Live multi-file progress for a parallel run, driven by ``rich``.

    On a TTY a ``rich.progress.Progress`` shows an overall line (files done /
    total, elapsed, total records, rec/s) plus one row per in-flight file (a
    byte-progress bar + that file's running record count). Off a TTY the live
    block is suppressed and one plain line — with exact clean/quarantined
    counts — is printed per completed file. A daemon thread drains the worker
    progress queue; ``rich`` owns all terminal control (cursor, resize,
    ``NO_COLOR``, clear-on-exit). Used as a context manager.
    """

    _REFRESH = 0.1  # seconds between queue drains

    def __init__(self, total_files, progress_queue, console, sizes, already_done=0):
        self._total_files = total_files
        self._queue = progress_queue
        self._console = console
        self._sizes = sizes
        self._live = console.is_terminal
        self._records = 0
        self._files_done = already_done
        self._start = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._file_records = {}  # name -> running record count
        self._tasks = {}  # name -> rich TaskID (live mode only)
        self._progress = None
        self._overall = None

    def __enter__(self):
        if self._live:
            self._progress = Progress(
                TextColumn("{task.fields[label]}"),
                BarColumn(bar_width=None),
                TaskProgressColumn(),
                TextColumn("{task.fields[detail]}"),
                console=self._console,
                transient=True,
            )
            self._progress.start()
            self._overall = self._progress.add_task(
                "overall", label="overall", total=self._total_files, detail=""
            )
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join()
        if self._progress is not None:
            self._progress.stop()
        return False

    def file_done(self, stats):
        """Count a finished file and print its one-line summary with exact
        clean/quarantined counts (above the live block on a TTY)."""
        self._complete(
            f"{stats.src_name} — {stats.clean_count:,} clean, "
            f"{stats.quarantined_count:,} quarantined"
        )

    def file_failed(self, path, exc):
        """Count a file that could not be processed and print the error."""
        self._complete(f"error processing {path}: {exc!r}")

    def _complete(self, summary):
        with self._lock:
            self._files_done += 1
            done = self._files_done
            if self._live:
                self._progress.update(self._overall, completed=self._files_done)
        target = self._progress.console if self._live else self._console
        target.print(f"[{done}/{self._total_files}] {summary}", markup=False)

    def _run(self):
        # The display is cosmetic: a broken queue (its manager gone at
        # shutdown) must never crash this thread with a traceback.
        with contextlib.suppress(Exception):
            while not self._stop.is_set():
                self._drain()
                self._stop.wait(self._REFRESH)
            self._drain()

    def _drain(self):
        """Fold queued messages into running state. Shapes: ``("start", name)``
        / ``("end", name)`` lifecycle and ``("progress", name, bytes_delta,
        records_delta)`` per-file deltas (issue #53 §6)."""
        msgs = []
        with contextlib.suppress(queue.Empty):
            while True:
                msgs.append(self._queue.get_nowait())
        if not msgs:
            return
        with self._lock:
            for msg in msgs:
                kind = msg[0]
                if kind == "progress":
                    _, name, byte_delta, record_delta = msg
                    self._records += record_delta
                    self._file_records[name] = (
                        self._file_records.get(name, 0) + record_delta
                    )
                    if self._live and name in self._tasks:
                        self._progress.update(
                            self._tasks[name],
                            advance=byte_delta,
                            detail=f"{self._file_records[name]:,} rec",
                        )
                elif kind == "start":
                    name = msg[1]
                    self._file_records.setdefault(name, 0)
                    if self._live:
                        self._tasks[name] = self._progress.add_task(
                            name,
                            label=name,
                            total=self._sizes.get(name),
                            detail="0 rec",
                        )
                elif kind == "end":
                    name = msg[1]
                    if self._live and name in self._tasks:
                        self._progress.remove_task(self._tasks.pop(name))
                    self._file_records.pop(name, None)
            if self._live:
                self._update_overall()

    def _update_overall(self):
        elapsed = time.monotonic() - self._start
        rps = int(self._records / elapsed) if elapsed >= 1.0 else 0
        self._progress.update(
            self._overall,
            completed=self._files_done,
            detail=f"{_format_elapsed(elapsed)} · {self._records:,} rec · {rps:,}/s",
        )


def resolve_jobs(explicit, cpu_count, n_files):
    """Resolve the worker count for a run. An explicit ``--jobs`` is the user's
    deliberate choice and is returned unchanged; otherwise default to one fewer
    than the CPU count — reserving a core for the OS during a long run — capped
    at the number of files and floored at one."""
    if explicit is not None:
        return explicit
    return max(1, min((cpu_count or 1) - 1, n_files))


def _format_size(n_bytes):
    """Render a byte count as a short human-readable string (e.g. ``2.9 GB``),
    using binary (1024) units."""
    size = float(n_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def _render_roster(console, file_sizes):
    """Print a one-shot, size-only roster of the files to be processed — index,
    basename, and size — with a final total row. ``file_sizes`` is an ordered
    ``path -> size`` map the caller stat'd once (no contents are read), so the
    roster, the disk-space guard, and the byte-bar denominators all agree.
    Rendered via ``rich`` so it degrades to plain text off a TTY (issue #53
    §2.1)."""
    table = Table(box=box.SIMPLE, pad_edge=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("file")
    table.add_column("size", justify="right")
    total = 0
    for index, (path, size) in enumerate(file_sizes.items(), start=1):
        total += size
        table.add_row(str(index), os.path.basename(path), _format_size(size))
    table.add_section()
    table.add_row("", "total", _format_size(total))
    console.print(table)


def main(argv=None):
    """Entry point for the ``lintle`` console script.

    Returns the process exit code: ``0`` = quarantine count (or rate) is at
    or below ``--max-quarantined``; ``1`` = quarantine threshold exceeded
    (default ``0`` — any quarantine fails); ``2`` = operational/usage error
    (bad args, no input files, disk shortfall, a file that failed to process,
    or a stale/corrupt/declined resume); ``130``/``143``/``129`` = terminated
    by SIGINT/SIGTERM/SIGHUP. The threshold accepts either an integer record
    count (``--max-quarantined 100``) or a percentage of routed records
    (``--max-quarantined 1%``); see :func:`parse_quarantine_threshold`.
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

    if args.jobs is not None and args.jobs < 1:
        print(f"error: --jobs must be >= 1 (got {args.jobs})", file=sys.stderr)
        return 2

    try:
        threshold_mode, quarantine_threshold = parse_quarantine_threshold(
            args.max_quarantined
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    path_error = check_paths(path, using_default=using_default)
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

    # Stat every input exactly once — the single source of size truth shared by
    # the disk-space guard, the pre-run roster, and the live byte-bar
    # denominators, so those three readouts can never silently diverge. Ordered
    # by discovery so the roster lists files in a stable order.
    file_sizes = {p: os.path.getsize(p) for p in files}

    # Defaults for the shared dispatch below; ``clean --resume`` narrows them.
    # ``inputs`` and ``completed`` drive the single-run resume checkpoint
    # (issue #56) and stay empty for ``validate``.
    files_to_process = files
    reused_stats = []
    inputs = {}
    completed = {}
    # ExitStack holds the out-dir lock for a clean run. Closed in the finally
    # block below — so every exit path (LockHeldError aside, which returns
    # before entering the try, leaving the stack empty) releases the lock.
    # For validate the stack stays empty and close() is a no-op.
    _lock_stack = contextlib.ExitStack()

    if args.command == "clean":
        os.makedirs(args.out_dir, exist_ok=True)
        try:
            _lock_stack.enter_context(
                fsutil.out_dir_lock(args.out_dir, started=_run_started_stamp())
            )
        except fsutil.LockHeldError as exc:
            # Stack is still empty — no lock to release.
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # The try/finally guarantees _lock_stack.close() runs on every exit path
    # that reaches here: disk-error return, ABORT return, interrupt return,
    # failed-files return, and normal success — so the lock file is always
    # removed.  For validate the stack is empty; close() is a no-op.
    try:
        if args.command == "clean":
            disk_status = _check_disk_space(args.out_dir, sum(file_sizes.values()))
            if disk_status is not None:
                severity, msg = disk_status
                if severity == "error":
                    print(f"error: {msg}", file=sys.stderr)
                    return 2
                print(f"warning: {msg}", file=sys.stderr)
            # Per-input identity for the resume checkpoint (spec §3.1): computed
            # once, up front, for every discovered file. Cheap and constant-memory
            # (a head+tail window hash), it is written into the checkpoint as files
            # complete so an interrupted run can be finished later.
            inputs = {path: resume.input_fingerprint(path) for path in files}
            # Output-affecting configuration pinned into the checkpoint identity
            # (spec §3.1). Today only the input set + version affect output content;
            # this is the explicit, future-proof hook so a new output-affecting flag
            # cannot validate-through and mix policies.
            run_identity = {"max_quarantined": args.max_quarantined}

            classification = resume.classify_checkpoint(
                args.out_dir, inputs, run_identity
            )
            decision = resume.resolve_resume_action(
                classification,
                resume=args.resume,
                no_resume=args.no_resume,
                interactive=_is_interactive(),
                prompt=_prompt_yes_no,
            )
            if decision.action is resume.ResumeAction.ABORT:
                print(f"error: {decision.message}", file=sys.stderr)
                return decision.exit_code
            if decision.action is resume.ResumeAction.RESUME:
                checkpoint = classification.checkpoint
                completed = dict(checkpoint["completed"])
                # Integrity re-verification (spec §3.6): drop any completed entry
                # whose outputs are missing or truncated, so they are reprocessed.
                for bad_path in resume.verify_completed_outputs(
                    completed, args.out_dir
                ):
                    completed.pop(bad_path, None)
                reused_stats = [
                    report.stats_from_summary(e["summary"]) for e in completed.values()
                ]
                files_to_process = [f for f in files if f not in completed]
                print(
                    f"resuming: {len(completed)}/{len(files)} files already complete, "
                    f"processing {len(files_to_process)}"
                    " — pass --no-resume for a fresh run",
                    file=sys.stderr,
                    flush=True,
                )
            else:  # FRESH
                # True-fresh slate (spec §3.4): archive any checkpoint (never
                # delete a recoverable run), then scrub output trees so no
                # orphans linger.
                resume.archive_checkpoint(args.out_dir, timestamp=_run_started_stamp())
                _scrub_outputs(args.out_dir)
        # Resolve the worker count now that files_to_process is final: an
        # explicit --jobs is honoured as-is; the default is CPU count - 1,
        # capped at the file count and floored at one (issue #53 §2.3).
        jobs = resolve_jobs(args.jobs, os.cpu_count(), len(files_to_process))

        # One rich Console on stderr drives both the roster and the live progress
        # block; off a TTY each degrades to plain text. Byte-bar denominators come
        # from os.stat (issue #53 §2.1/§2.2) — no pre-read of the corpus.
        console = Console(stderr=True)
        sizes = {os.path.basename(p): file_sizes[p] for p in files_to_process}
        if args.command == "clean":
            _render_roster(console, {p: file_sizes[p] for p in files_to_process})

        if not reused_stats:
            print(
                f"processing {len(files_to_process)} file(s) with {jobs} worker(s)...",
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
        run_started_iso = datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        run_monotonic_start = time.monotonic()
        # Files reused from a resume checkpoint are already done — seed the report
        # with their reconstructed stats so corpus totals cover the whole run.
        all_stats = list(reused_stats)
        failed_files = []
        interrupted = False
        interrupted_signo = signal.SIGINT

        # The executor runs without a `with` block deliberately: that block's
        # __exit__ calls shutdown(wait=True), which on Ctrl-C would block until
        # every in-flight corpus file finished. Instead the workers ignore
        # SIGINT (so only this process sees it) and, on interrupt, we terminate
        # them outright. A manager queue carries record counts back for display.
        with multiprocessing.Manager() as manager:
            progress_queue = manager.Queue()
            executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=jobs, initializer=_ignore_sigint
            )
            caught = {"signo": signal.SIGINT}

            def _raise_interrupt(signo, _frame):
                caught["signo"] = signo
                raise KeyboardInterrupt

            prev_term = signal.signal(signal.SIGTERM, _raise_interrupt)
            prev_hup = signal.signal(signal.SIGHUP, _raise_interrupt)
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
                with _ProgressDisplay(
                    len(files),
                    progress_queue,
                    console,
                    sizes,
                    already_done=len(completed),
                ) as progress:
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
                                completed[path] = {
                                    "summary": report.summary_dict(stats),
                                    "outputs": _output_sizes(args.out_dir, stats),
                                }
                                resume.write_checkpoint(
                                    args.out_dir,
                                    resume.build_checkpoint(
                                        inputs=inputs,
                                        completed=completed,
                                        run_identity=run_identity,
                                    ),
                                )
            except KeyboardInterrupt:
                # Ignore any further Ctrl-C so the shutdown itself cannot be
                # interrupted half-way (which is what left it hung before).
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                interrupted = True
                interrupted_signo = caught["signo"]
                _terminate_workers(executor)
                executor.shutdown(wait=False, cancel_futures=True)
                print(
                    _format_cancel_message(done=len(completed), total=len(files)),
                    file=sys.stderr,
                    flush=True,
                )
            else:
                executor.shutdown(wait=True)
            finally:
                signal.signal(signal.SIGTERM, prev_term)
                signal.signal(signal.SIGHUP, prev_hup)

        if interrupted:
            return _signal_exit_code(interrupted_signo)

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

        # A file that could not be processed is an operational failure (spec §2.7
        # / §10): exit 2 (operational error). Exit 1 is the quarantine quality
        # gate (threshold exceeded); exit 2 covers all other operational failures.
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
    finally:
        _lock_stack.close()
