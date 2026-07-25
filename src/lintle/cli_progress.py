"""Live terminal presentation for a ``clean`` run: the pre-run file roster, a
transient status spinner, and the multi-file progress display that consumes the
worker progress protocol. ``rich``-only rendering, split out of ``cli`` so the
composition root keeps a single responsibility (issue #53)."""

import contextlib
import dataclasses
import queue
import threading
import time
from pathlib import Path

from rich import box
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
)
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from lintle import pipeline, summary, term


def status(message):
    """A transient ``rich`` spinner on stderr for an otherwise-silent phase (e.g.
    concatenating the per-worker findings shards into ``report.jsonl``), or a
    no-op context off a TTY so nothing leaks to a pipe. MUST NOT be entered
    inside the live progress block — both wrap ``rich.live.Live``, which cannot
    nest — but report finalization runs after that block has exited."""
    if term.stderr_console.is_terminal:
        return term.stderr_console.status(message)
    return contextlib.nullcontext()


@contextlib.contextmanager
def phase_bar(description, total):
    """A single-task ``rich`` bar on stderr for the single-process post-run
    phases (``verify``/``dedup`` streaming their stems), yielding an ``update``
    callable — ``update(advance=1)``, ``update(description=...)``. Disabled off a
    TTY so nothing leaks into a pipe, and transient so the finished run leaves
    only its verdict line. Like :func:`status` it wraps ``rich.live.Live`` and so
    must not nest inside another live block."""
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=term.stderr_console,
        transient=True,
        disable=not term.stderr_console.is_terminal,
    ) as progress:
        task = progress.add_task(description, total=total)
        yield lambda **fields: progress.update(task, **fields)


@dataclasses.dataclass(slots=True)
class _InFlight:
    """One file currently being processed — the phase-2 row state. ``total`` is
    the stat'd input size (the bar denominator); ``done`` and ``records`` are the
    running deltas folded in from the worker progress protocol."""

    index: int
    name: str
    total: int
    started: float
    done: int = 0
    records: int = 0


class ProgressDisplay:
    """Phase 2 of the three-phase ``clean`` display: live progress, bounded.

    On a TTY a ``rich.live.Live`` redraws a ``rich.table.Table`` holding one row
    per *in-flight* file — index, basename, size, byte bar, percent, records,
    MB/s, ETA — plus a pinned summary row (files done/total, corpus size, overall
    percent, total records, aggregate rate, elapsed). The index and size columns
    are the identity link back to the phase-1 roster, which is ordered by the
    same sorted basename.

    Bounded on purpose: a live region cannot scroll, so a table holding every
    file breaks at terminal height 24 and strands rows on resize. In-flight rows
    are capped by the worker count, so correctness here is independent of
    terminal height; the full picture is the static phase-3 results table.

    Off a TTY the live block is suppressed and one plain line — with exact
    clean/quarantined counts — is printed per completed file; those lines print
    above the live block on a TTY too, as the durable scrollback record of
    completion order. A daemon thread drains the worker progress queue; ``rich``
    owns all terminal control (cursor, resize, ``NO_COLOR``, clear-on-exit).
    Used as a context manager.
    """

    _REFRESH = 0.1  # seconds between queue drains

    def __init__(self, total_files, progress_queue, console, sizes, already_done=0):
        self._total_files = total_files
        self._queue = progress_queue
        self._console = console
        self._sizes = sizes
        self._live = console.is_terminal
        self._records = 0
        self._bytes_done = 0
        self._files_done = already_done
        self._start = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._file_records = {}  # name -> running record count
        self._rows = {}  # name -> _InFlight (live mode only)
        self._display = None
        # Roster order is the identity link: `sizes` is the same ordered map the
        # phase-1 roster was rendered from, so index N means the same file in
        # both. Column widths are pinned from these pre-dispatch bounds — with
        # auto-width the `#` and count columns visibly reflow mid-run, which
        # re-lays out the table under the row being read.
        self._index = {name: i for i, name in enumerate(sizes, start=1)}
        self._corpus_bytes = sum(sizes.values())
        self._w_index = len(str(max(total_files, 1)))
        # The pinned name width must also hold the summary row's own label, or
        # the no-wrap column truncates it.
        widest_label = len(f"{total_files}/{total_files} files")
        self._w_name = max(max((len(n) for n in sizes), default=4), widest_label)
        self._w_size = max((len(_format_size(s)) for s in sizes.values()), default=4)
        # The record count has no pre-dispatch bound, so it is pinned wide
        # enough for a billion-record file rather than allowed to reflow.
        self._w_records = 13

    def __enter__(self):
        if self._live:
            # auto_refresh off: every frame is driven by _drain, so a redraw
            # only happens when the state it renders actually changed.
            self._display = Live(
                self._table(),
                console=self._console,
                transient=True,
                auto_refresh=False,
            )
            self._display.start()
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join()
        if self._display is not None:
            self._display.stop()
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

    def _complete(self, line):
        with self._lock:
            self._files_done += 1
            done = self._files_done
        self._console.print(f"[{done}/{self._total_files}] {line}", markup=False)
        self._refresh()

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
                        self._bytes_done += msg.bytes_delta
                        self._file_records[msg.name] = (
                            self._file_records.get(msg.name, 0) + msg.records_delta
                        )
                        if (row := self._rows.get(msg.name)) is not None:
                            row.done += msg.bytes_delta
                            row.records = self._file_records[msg.name]
                    case pipeline.FileStarted():
                        self._file_records.setdefault(msg.name, 0)
                        if self._live:
                            self._rows[msg.name] = _InFlight(
                                index=self._index.get(msg.name, 0),
                                name=msg.name,
                                total=self._sizes.get(msg.name, 0),
                                started=time.monotonic(),
                            )
                    case pipeline.FileEnded():
                        self._rows.pop(msg.name, None)
                        self._file_records.pop(msg.name, None)
        self._refresh()

    def _refresh(self):
        """Redraw the live table. A render failure is cosmetic — the drain loop's
        handler keeps the queue draining regardless."""
        if self._display is not None:
            self._display.update(self._table(), refresh=True)

    def _table(self):
        """Build the current frame: the in-flight rows plus the pinned summary
        row, with columns selected for the console's width."""
        tier = summary.display_tier(self._console.width)
        table = Table(box=box.SIMPLE, pad_edge=False, expand=True)
        table.add_column("#", justify="right", style="dim", width=self._w_index)
        table.add_column("file", width=self._w_name, no_wrap=True)
        if tier != "narrow":
            table.add_column("size", justify="right", width=self._w_size)
        table.add_column("progress", ratio=1)
        table.add_column("%", justify="right", width=4)
        table.add_column("records", justify="right", width=self._w_records)
        if tier == "wide":
            table.add_column("MB/s", justify="right", width=9)
            table.add_column("ETA", justify="right", width=7)

        elapsed = time.monotonic() - self._start
        with self._lock:
            rows = sorted(self._rows.values(), key=lambda r: r.index)
            for row in rows:
                cells = [] if tier == "narrow" else [_format_size(row.total)]
                cells += [
                    ProgressBar(total=max(row.total, 1), completed=row.done),
                    _percent(row.done, row.total),
                    f"{row.records:,}",
                ]
                if tier == "wide":
                    rate = _rate(row.done, time.monotonic() - row.started)
                    cells += [_format_rate(rate), _eta(row.total - row.done, rate)]
                table.add_row(str(row.index), Text(row.name), *cells)

            table.add_section()
            cells = [] if tier == "narrow" else [_format_size(self._corpus_bytes)]
            cells += [
                ProgressBar(
                    total=max(self._corpus_bytes, 1), completed=self._bytes_done
                ),
                _percent(self._bytes_done, self._corpus_bytes),
                f"{self._records:,}",
            ]
            if tier == "wide":
                # The summary row's last cell is elapsed, not an ETA: the run's
                # remaining time is the slowest worker's, not a corpus average.
                cells += [
                    _format_rate(_rate(self._bytes_done, elapsed)),
                    summary.format_clock(elapsed),
                ]
            table.add_row(
                "",
                f"{self._files_done}/{self._total_files} files",
                *cells,
                style="bold",
            )
        return table


def _percent(part, whole):
    """Render ``part / whole`` as a whole-number percentage cell."""
    return f"{int(100 * part / whole)}%" if whole > 0 else "—"


def _rate(n_bytes, seconds):
    """Bytes per second, or ``0`` before a second of evidence has accumulated."""
    return n_bytes / seconds if seconds >= 1.0 else 0


def _format_rate(rate):
    """Render a byte rate in the same gnu units the size column uses."""
    return "—" if rate <= 0 else f"{_format_size(rate)}/s"


def _eta(remaining, rate):
    """Render the time to finish ``remaining`` bytes at ``rate``."""
    return "—" if rate <= 0 else summary.format_clock(remaining / rate)


def _format_size(n_bytes):
    """Render a byte count compactly for the roster (e.g. ``"3.0G"``), via
    humanize's gnu units — fixes the prior binary-math/decimal-label mismatch."""
    return summary.format_size(n_bytes)


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
        table.add_row(str(index), Text(Path(path).name), _format_size(size))
    table.add_section()
    table.add_row("", "total", _format_size(total))
    console.print(table)
