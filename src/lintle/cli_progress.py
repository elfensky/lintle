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


@dataclasses.dataclass(slots=True)
class _Row:
    """One discovered file's row — the whole run's state for that file, from
    ``pending`` through ``running`` to ``done``/``failed``. The row exists from
    the first frame; work updates it in place rather than adding a line. A
    resumed run's carried-over files start at ``resumed``: complete like
    ``done``, but dimmed, because an earlier run measured those numbers."""

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

    def __init__(self, total_files, progress_queue, console, sizes, completed=()):
        self._total_files = total_files
        self._queue = progress_queue
        self._console = console
        self._live = console.is_terminal
        self._records = 0
        self._bytes_done = 0
        self._clean = 0
        self._quarantined = 0
        self._files_done = len(completed)
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
        # A resumed run's already-complete files get their rows filled in from
        # the checkpoint before the first frame, so the roster is the whole
        # corpus and the summary row's totals cover it. Showing only the
        # remaining files would leave the count ("2/29 files") describing a
        # table with 27 rows in it, and would drop the earlier run's records
        # and bytes out of every total. Matches `summary.render_files`, which
        # already carries resumed files as dimmed rows.
        for stats in completed:
            self._adopt_completed(stats)
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

    def _adopt_completed(self, stats):
        """Fill a row finished by an earlier run from its checkpointed stats, at
        construction, so it is already complete in the first frame. Unlike
        :meth:`file_done` this prints nothing and does not bump the done count —
        that came from the checkpoint, and this file's completion was reported
        when it actually happened."""
        row = self._rows.get(stats.src_name)
        if row is None:
            return  # checkpointed under a name no longer in the input set
        row.state = "resumed"
        row.bytes_done = row.size
        row.records = stats.paired_records
        row.clean = stats.clean_count
        row.quarantined = stats.quarantined_count
        row.elapsed = stats.elapsed_seconds
        self._records += stats.paired_records
        self._bytes_done += row.size
        self._clean += stats.clean_count
        self._quarantined += stats.quarantined_count

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
                # Redraw every cycle, not only when messages arrived: the
                # summary row carries the run's wall clock, and a frozen clock
                # during a stall is exactly when it most needs to be moving.
                self._refresh()
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

    def _refresh(self):
        """Redraw the live table. A render failure is cosmetic — the drain loop's
        handler keeps the queue draining regardless."""
        if self._display is not None:
            self._display.update(self._table(), refresh=True)

    def _visible(self, rows):
        """The rows this frame can show, plus the number it cannot — see
        :func:`window`. Records that windowing happened so the caller knows the
        final frame is partial."""
        visible, hidden = window(rows, self._console.size.height, self._CHROME_LINES)
        self.windowed = self.windowed or bool(hidden)
        return visible, hidden

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
                table.add_row(
                    str(row.index),
                    Text(row.name),
                    *self._cells(row, tier),
                    # Dimmed like the results table's resumed rows: complete,
                    # but measured by the earlier run, not this one.
                    style="dim" if row.state == "resumed" else None,
                )
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
            done = row.state in ("done", "resumed")
            cells += [
                f"{row.clean:,}" if done else "",
                f"{row.quarantined:,}" if done else "",
            ]
        if tier == "wide":
            cells.append(summary.format_clock(row.elapsed) if row.elapsed else "")
        return cells


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# One frame per tick, ten a second — the rate rich's own spinners turn at, and
# the rate the clean display already drains its queue at.
_TICK = 0.1


def bar(completed, total):
    """A progress bar cell for a results-table row — the same renderable the
    ``clean`` display uses, so a filling bar means the same thing everywhere. A
    percentage alone reads as static text on a file that takes minutes."""
    return ProgressBar(total=max(total, 1), completed=min(completed, total))


def window(rows, height, chrome_lines):
    """Return the rows a frame of ``height`` lines can show, plus the number it
    cannot.

    All of them when they fit. When they do not, a window that follows the work:
    it starts at the first unfinished row so the active unit and everything
    still to come stay on screen, and slides no further than the end of the
    list. Rows outside the window keep their state and come back into it as the
    window moves — the alternative, a live region taller than the viewport,
    cannot scroll and strands its overflow."""
    budget = height - chrome_lines
    if budget < 1 or len(rows) <= budget:
        return rows, 0
    active = [i for i, r in enumerate(rows) if r.state in ("pending", "running")]
    start = min(active) if active else len(rows) - budget
    start = max(0, min(start, len(rows) - budget))
    return rows[start : start + budget], len(rows) - budget


@dataclasses.dataclass(slots=True)
class _UnitRow:
    """One unit's row in a :class:`UnitTable` — its state plus whatever cells the
    command has filled in so far, keyed by column header."""

    index: int
    name: str
    state: str = "pending"
    cells: dict = dataclasses.field(default_factory=dict)


class UnitTable:
    """The one live table a single-process command renders, updated in place.

    The post-run commands (``verify``, ``dedup``) know their units — the cleaned
    stems — before they start, so every unit gets a row up front and the first
    frame is the roster. Work then fills cells in place: :meth:`start` marks a
    unit running, :meth:`update` merges cells as it streams, :meth:`finish`
    writes its final numbers. :meth:`phase` relabels the pinned summary row for
    the stages that follow the per-unit loop (sorting, writing), so those
    stages need no spinner and no new line.

    Same rules as the ``clean`` display, for the same reasons: the frame windows
    when the rows outnumber the terminal (a live region cannot scroll), it is
    not transient so the finished table is the results view, and off a TTY it
    degrades to two static prints — the roster on entry, the results on exit.
    A windowed frame is followed by the complete static table, so no row the
    window hid is lost. ``headers[0]`` indexes and ``headers[1]`` names, matching
    ``summary.results_table``; the rest are the command's own columns.
    """

    _CHROME_LINES = 8

    def __init__(
        self, names, headers, *, console, unit="files", drop=None, justify=None
    ):
        self._console = console
        self._live_mode = console.is_terminal
        self._headers = list(headers)
        self._unit = unit
        # Columns this console is too narrow for, by tier — dropped whole, never
        # truncated, exactly as the clean table does it.
        self._drop = frozenset(
            (drop or {}).get(summary.display_tier(console.width), ())
        )
        self._justify = dict(justify or {})
        self._rows = [
            _UnitRow(index=i, name=name) for i, name in enumerate(names, start=1)
        ]
        self._by_name = {r.name: r for r in self._rows}
        self._done = 0
        self._label = None
        self._totals = {}
        self._display = None
        self._finished = False
        self.windowed = False
        self._start = time.monotonic()
        # The heartbeat has to keep moving between work reports, so a timer
        # redraws the frame as well as the work does. That makes `_table()` a
        # shared read across two threads, hence the lock.
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._animate, daemon=True)

    def __enter__(self):
        if self._live_mode:
            self._display = Live(
                self._table(),
                console=self._console,
                transient=False,
                auto_refresh=False,
            )
            self._display.start()
            self._thread.start()
        else:
            self._console.print(self._table())  # the roster, statically
        return self

    def __exit__(self, *_exc):
        self._finished = True
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        if self._display is not None:
            self._refresh()
            self._display.stop()
        if not self._live_mode or self.windowed:
            # Off a TTY there was no live frame to end on; a windowed frame
            # ended on only part of the table. Either way the complete results
            # are still owed.
            self._console.print(self._table(complete=True))
        return False

    def start(self, name):
        """Mark a unit as the one being worked."""
        with self._lock:
            if (row := self._by_name.get(name)) is not None:
                row.state = "running"
        self._refresh()

    def update(self, name, **cells):
        """Merge cells into a running unit's row. Callers throttle this — one
        update per record would cost more than the work being reported."""
        with self._lock:
            if (row := self._by_name.get(name)) is not None:
                row.cells.update(cells)
        self._refresh()

    def finish(self, name, **cells):
        """Write a unit's final cells and count it done."""
        with self._lock:
            if (row := self._by_name.get(name)) is not None:
                row.state = "done"
                row.cells.update(cells)
                self._done += 1
        self._refresh()

    def phase(self, label):
        """Relabel the summary row for a stage that is not per-unit (sorting the
        stream, writing the output tree). The table stays put and keeps every
        row it has — the stage reports itself without printing a line."""
        with self._lock:
            self._label = label
        self._refresh()

    def totals(self, **cells):
        """Set the summary row's cells."""
        with self._lock:
            self._totals.update(cells)
        self._refresh()

    def _animate(self):
        """Redraw on a timer so the heartbeat keeps moving through a long
        stretch of work that reports rarely — the sort, the orbit pass, a big
        stem. Nothing here reads or writes row state; it only asks for the
        frame that :meth:`_refresh` would have drawn anyway."""
        while not self._stop.wait(_TICK):
            self._refresh()

    def _refresh(self):
        if self._display is not None:
            with self._lock:
                table = self._table()
            self._display.update(table, refresh=True)

    def _table(self, *, complete=False):
        """Build the current frame. ``complete`` renders every row regardless of
        the terminal's height — the static fallback for a windowed run."""
        headers = [h for h in self._headers if h not in self._drop]
        table = summary.results_table(*headers, justify=self._justify)
        if complete:
            visible, hidden = self._rows, 0
        else:
            visible, hidden = window(
                self._rows, self._console.size.height, self._CHROME_LINES
            )
            self.windowed = self.windowed or bool(hidden)
        for row in visible:
            table.add_row(
                str(row.index),
                Text(row.name),
                # Cells pass through as given: a string, or a renderable such as
                # a ProgressBar, which a str() would flatten to its repr.
                *(row.cells.get(h, "") for h in headers[2:]),
                style="dim" if row.state == "pending" else None,
            )
        if hidden:
            table.add_row("", f"… {hidden} more", style="dim")
        table.add_section()
        table.add_row(
            "",
            self._heartbeat(complete)
            + (self._label or f"{self._done}/{len(self._rows)} {self._unit}"),
            *(self._totals.get(h, "") for h in headers[2:]),
            style="bold",
        )
        return table

    def _heartbeat(self, complete):
        """A spinner frame for the summary row, picked from the wall clock so it
        turns at a steady rate for as long as the table is open. Deriving it
        from the redraw count instead made it stutter along with the work —
        smooth while records streamed, frozen through the stages that report
        once a minute, which reads as a hang rather than as progress. Absent
        from the final frame and from the static off-a-TTY prints, so finished
        output is stable text."""
        if complete or self._finished or not self._live_mode:
            return ""
        frame = int((time.monotonic() - self._start) / _TICK) % len(_SPINNER)
        return _SPINNER[frame] + " "


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
