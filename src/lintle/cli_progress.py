"""Live terminal presentation for a ``clean``/``validate`` run: the pre-run file
roster, a transient status spinner, and the multi-file progress display that
consumes the worker progress protocol. ``rich``-only rendering, split out of
``cli`` so the composition root keeps a single responsibility (issue #53)."""

import contextlib
import queue
import threading
import time
from pathlib import Path

import humanize
from rich import box
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

from lintle import pipeline, term


def _format_elapsed(seconds):
    """Render an elapsed duration as ``M:SS`` (or ``H:MM:SS`` past an hour)."""
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class _ForKind(ProgressColumn):
    """Render the wrapped column only for tasks of a given ``kind``; every other
    task gets an empty cell. One ``Progress`` drives two task shapes — the
    overall row (``total`` = file count) and the per-file rows (``total`` =
    bytes) — so a byte column (speed, ETA) would render a misleading value on the
    count row, and the files-done/total counter would render raw byte numbers on
    a per-file row. Gating by ``kind`` keeps each column on the rows it fits."""

    def __init__(self, kind, inner):
        super().__init__()
        self._kind = kind
        self._inner = inner

    def render(self, task):
        if task.fields.get("kind") == self._kind:
            return self._inner.render(task)
        return Text("")


def status(message):
    """A transient ``rich`` spinner on stderr for an otherwise-silent phase (e.g.
    concatenating the per-worker findings shards into ``report.jsonl``), or a
    no-op context off a TTY so nothing leaks to a pipe. MUST NOT be entered
    inside the live progress block — both wrap ``rich.live.Live``, which cannot
    nest — but report finalization runs after that block has exited."""
    if term.stderr_console.is_terminal:
        return term.stderr_console.status(message)
    return contextlib.nullcontext()


class ProgressDisplay:
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
                # Overall row: files done / total. Per-file rows: byte
                # throughput + ETA, computed from the byte total set per task.
                _ForKind("overall", MofNCompleteColumn()),
                _ForKind("file", TransferSpeedColumn()),
                _ForKind("file", TimeRemainingColumn(compact=True)),
                TextColumn("{task.fields[detail]}"),
                console=self._console,
                transient=True,
            )
            self._progress.start()
            self._overall = self._progress.add_task(
                "overall",
                label="overall",
                kind="overall",
                total=self._total_files,
                detail="",
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
        # The display is cosmetic: render errors must never kill this thread
        # permanently (the queue would grow unbounded). Per-iteration errors
        # are caught and skipped so draining continues; genuine shutdown
        # errors (manager gone) break the loop cleanly.
        while not self._stop.is_set():
            try:
                self._drain()
            except EOFError, BrokenPipeError, ConnectionResetError, OSError:
                break
            except Exception:  # transient render glitch — keep draining
                pass
            self._stop.wait(self._REFRESH)
        with contextlib.suppress(Exception):
            self._drain()

    def _drain(self):
        """Fold queued messages into running state. Consumes the typed worker
        protocol: :class:`pipeline.FileStarted` / :class:`pipeline.FileEnded`
        lifecycle events and :class:`pipeline.FileProgress` per-file deltas
        (issue #53 §6)."""
        msgs = []
        with contextlib.suppress(queue.Empty):
            while True:
                msgs.append(self._queue.get_nowait())
        if not msgs:
            return
        with self._lock:
            for msg in msgs:
                match msg:
                    case pipeline.FileProgress():
                        self._records += msg.records_delta
                        self._file_records[msg.name] = (
                            self._file_records.get(msg.name, 0) + msg.records_delta
                        )
                        if self._live and msg.name in self._tasks:
                            self._progress.update(
                                self._tasks[msg.name],
                                advance=msg.bytes_delta,
                                detail=f"{self._file_records[msg.name]:,} rec",
                            )
                    case pipeline.FileStarted():
                        self._file_records.setdefault(msg.name, 0)
                        if self._live:
                            self._tasks[msg.name] = self._progress.add_task(
                                msg.name,
                                label=msg.name,
                                kind="file",
                                total=self._sizes.get(msg.name),
                                detail="0 rec",
                            )
                    case pipeline.FileEnded():
                        if self._live and msg.name in self._tasks:
                            self._progress.remove_task(self._tasks.pop(msg.name))
                        self._file_records.pop(msg.name, None)
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


def _format_size(n_bytes):
    """Render a byte count compactly for the roster (e.g. ``"3.0G"``), via
    humanize's gnu units — fixes the prior binary-math/decimal-label mismatch."""
    return humanize.naturalsize(n_bytes, gnu=True)


def render_roster(console, file_sizes):
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
        table.add_row(str(index), Path(path).name, _format_size(size))
    table.add_section()
    table.add_row("", "total", _format_size(total))
    console.print(table)
