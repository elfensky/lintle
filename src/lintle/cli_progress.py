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
class _Row:
    """One discovered file's row — the whole run's state for that file, from
    ``pending`` through ``running`` to ``done``/``failed``. The row exists from
    the first frame; work updates it in place rather than adding a line."""

    index: int
    name: str
    size: int
    state: str = "pending"
    started: float = 0.0
    bytes_done: int = 0
    records: int = 0
    clean: int = 0
    quarantined: int = 0
    elapsed: float = 0.0


class ProgressDisplay:
    """The one live table a ``clean`` run renders, from discovery to results.

    Every discovered file gets a row at construction, so the first frame *is*
    the roster: index, basename, size, and an empty progress cell. Work updates
    rows in place — a file starts, its bar fills, and on completion the same row
    switches to its final records/clean/quarantined/time. Nothing is appended
    while the run proceeds, and the final frame stays on screen as the results
    view (``transient=False``), with the aggregate panel printed under it.

    A ``rich.live.Live`` region cannot scroll, so when the rows outnumber the
    terminal's height the table renders a **window** around the active files
    plus an ``… N more`` marker; the rows outside it keep their state and scroll
    back in, they are not dropped. :attr:`windowed` records whether that ever
    happened, so the caller can print the complete static results table for the
    rows the window could not show.

    Off a TTY there is no live block: the caller prints the static roster, one
    plain completion line per file lands here, and the static results table
    closes the run. A daemon thread drains the worker progress queue; ``rich``
    owns all terminal control (cursor, resize, ``NO_COLOR``). Used as a context
    manager.
    """

    _REFRESH = 0.1  # seconds between queue drains
    # Lines the frame needs beyond its rows: header, its rule, the section rule,
    # the summary row, table padding, and one spare so a redraw never collides
    # with the shell prompt.
    _CHROME_LINES = 8

    def __init__(self, total_files, progress_queue, console, sizes, already_done=0):
        self._total_files = total_files
        self._queue = progress_queue
        self._console = console
        self._live = console.is_terminal
        self._records = 0
        self._bytes_done = 0
        self._clean = 0
        self._quarantined = 0
        self._files_done = already_done
        self._start = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._display = None
        self.windowed = False
        # `sizes` is the ordered name -> size map the caller stat'd once, so row
        # N is file N everywhere. Column widths are pinned from these
        # pre-dispatch bounds — with auto-width the `#` and count columns
        # visibly reflow mid-run, re-laying out the table under the row being
        # read.
        self._rows = {
            name: _Row(index=i, name=name, size=size)
            for i, (name, size) in enumerate(sizes.items(), start=1)
        }
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
            # transient=False: the final frame is the results view. auto_refresh
            # off: every frame is driven by a state change, never by a timer.
            self._display = Live(
                self._table(),
                console=self._console,
                transient=False,
                auto_refresh=False,
            )
            self._display.start()
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join()
        if self._display is not None:
            self._refresh()  # commit the final state before the frame freezes
            self._display.stop()
        return False

    def file_done(self, stats):
        """Fold a finished file's exact counts into its row. Off a TTY — where
        there is no table to update — the same facts print as one plain line,
        the only progress record a piped run gets."""
        with self._lock:
            self._files_done += 1
            done = self._files_done
            row = self._rows.get(stats.src_name)
            if row is not None:
                row.state = "done"
                row.bytes_done = row.size  # the bar closes; no partial tail
                row.records = stats.paired_records
                row.clean = stats.clean_count
                row.quarantined = stats.quarantined_count
                row.elapsed = stats.elapsed_seconds
            self._clean += stats.clean_count
            self._quarantined += stats.quarantined_count
        if not self._live:
            self._console.print(
                f"[{done}/{self._total_files}] {stats.src_name} — "
                f"{stats.clean_count:,} clean, {stats.quarantined_count:,} quarantined",
                markup=False,
            )
        self._refresh()

    def file_failed(self, path, exc):
        """Mark a file that could not be processed. The error text prints on
        every stream, TTY included: a failure is not routine progress, and the
        row alone cannot carry the reason."""
        name = Path(path).name
        with self._lock:
            self._files_done += 1
            done = self._files_done
            if (row := self._rows.get(name)) is not None:
                row.state = "failed"
        self._console.print(
            f"[{done}/{self._total_files}] error processing {path}: {exc!r}",
            markup=False,
        )
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
        """Fold queued messages into the rows. Consumes the typed worker
        protocol: :class:`pipeline.FileStarted` / :class:`pipeline.FileEnded`
        lifecycle events and :class:`pipeline.FileProgress` per-file deltas
        (issue #53 §6). Rows are never created or removed here — every file has
        had a row since construction; these messages only change its state."""
        msgs = []
        with contextlib.suppress(queue.Empty):
            while True:
                msgs.append(self._queue.get_nowait())
        if not msgs:
            return
        with self._lock:
            for msg in msgs:
                row = self._rows.get(msg.name)
                match msg:
                    case pipeline.FileProgress():
                        self._records += msg.records_delta
                        self._bytes_done += msg.bytes_delta
                        # A row that already has its exact FileStats counts is
                        # left alone: the worker's last deltas can arrive after
                        # its future resolved, and adding them on top would push
                        # a finished file past its own totals.
                        if row is not None and row.state != "done":
                            row.bytes_done += msg.bytes_delta
                            row.records += msg.records_delta
                    case pipeline.FileStarted():
                        if row is not None:
                            row.state = "running"
                            row.started = time.monotonic()
                    case pipeline.FileEnded():
                        pass  # the exact counts arrive with `file_done`
        self._refresh()

    def _refresh(self):
        """Redraw the live table. A render failure is cosmetic — the drain loop's
        handler keeps the queue draining regardless."""
        if self._display is not None:
            self._display.update(self._table(), refresh=True)

    def _visible(self, rows):
        """Return the rows this frame can show, plus the number it cannot.

        All of them when they fit the terminal. When they do not, a window that
        follows the work: it starts at the first unfinished row so the active
        files and everything still to come stay on screen, and slides no further
        than the end of the list. Rows outside the window keep their state and
        come back into it as the window moves — the alternative, a live region
        taller than the viewport, cannot scroll and strands its overflow."""
        budget = self._console.size.height - self._CHROME_LINES
        if budget < 1 or len(rows) <= budget:
            return rows, 0
        self.windowed = True
        active = [i for i, r in enumerate(rows) if r.state in ("pending", "running")]
        start = min(active) if active else len(rows) - budget
        start = max(0, min(start, len(rows) - budget))
        return rows[start : start + budget], len(rows) - budget

    def _table(self):
        """Build the current frame: a row per discovered file (windowed to the
        terminal's height) plus the pinned summary row, with columns selected
        for the console's width. One column set serves every row state — a
        pending row is empty, a running row carries its bar, a finished row its
        counts — so a row never changes shape under the reader."""
        tier = summary.display_tier(self._console.width)
        table = Table(box=box.SIMPLE, pad_edge=False, expand=True)
        table.add_column("#", justify="right", style="dim", width=self._w_index)
        table.add_column("file", width=self._w_name, no_wrap=True)
        if tier == "wide":
            table.add_column("size", justify="right", width=self._w_size)
        if tier != "narrow":
            table.add_column("progress", ratio=1)
        table.add_column("%", justify="right", width=4)
        table.add_column("records", justify="right", width=self._w_records)
        table.add_column("clean", justify="right", width=self._w_records)
        table.add_column("quarantined", justify="right", width=11)
        if tier == "wide":
            table.add_column("time", justify="right", width=7)

        elapsed = time.monotonic() - self._start
        with self._lock:
            rows = sorted(self._rows.values(), key=lambda r: r.index)
            visible, hidden = self._visible(rows)
            for row in visible:
                table.add_row(str(row.index), Text(row.name), *self._cells(row, tier))
            if hidden:
                table.add_row("", f"… {hidden} more", style="dim")

            table.add_section()
            cells = [_format_size(self._corpus_bytes)] if tier == "wide" else []
            if tier != "narrow":
                cells.append(
                    ProgressBar(
                        total=max(self._corpus_bytes, 1), completed=self._bytes_done
                    )
                )
            cells += [
                _percent(self._bytes_done, self._corpus_bytes),
                f"{self._records:,}",
                f"{self._clean:,}",
                f"{self._quarantined:,}",
            ]
            if tier == "wide":
                cells.append(summary.format_clock(elapsed))
            table.add_row(
                "",
                f"{self._files_done}/{self._total_files} files",
                *cells,
                style="bold",
            )
        return table

    def _cells(self, row, tier):
        """Render one row's cells for the current tier. A pending file shows
        blanks rather than zeroes — nothing has been measured about it yet, and
        a column of zeroes reads as a result."""
        pending = row.state == "pending"
        failed = row.state == "failed"
        # Never past its own size: a worker's byte deltas are counted as they
        # are read, and the last one can overshoot the stat'd length by a
        # partial buffer.
        filled = min(row.bytes_done, row.size)
        cells = [_format_size(row.size)] if tier == "wide" else []
        if tier != "narrow":
            cells.append(
                "" if pending else ProgressBar(total=max(row.size, 1), completed=filled)
            )
        cells += [
            "" if pending else _percent(filled, row.size),
            "" if pending else f"{row.records:,}",
        ]
        if failed:
            cells += ["failed", ""]
        else:
            done = row.state == "done"
            cells += [
                f"{row.clean:,}" if done else "",
                f"{row.quarantined:,}" if done else "",
            ]
        if tier == "wide":
            cells.append(summary.format_clock(row.elapsed) if row.elapsed else "")
        return cells


def _percent(part, whole):
    """Render ``part / whole`` as a whole-number percentage cell."""
    return f"{int(100 * part / whole)}%" if whole > 0 else "—"


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
