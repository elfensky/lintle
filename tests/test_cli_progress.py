"""Tests for cli_progress.py — the live progress display, roster, and spinner."""

import contextlib
import io
import queue
import types

from rich.console import Console

from lintle import cli_progress, pipeline, report


class TestFormatSize:
    """cli_progress._format_size — human-readable byte counts for the roster (#53)."""

    def test_bytes_below_one_kib(self):
        assert cli_progress._format_size(0) == "0B"
        assert cli_progress._format_size(512) == "512B"

    def test_kilobytes(self):
        assert cli_progress._format_size(1024) == "1.0K"
        assert cli_progress._format_size(1536) == "1.5K"

    def test_gigabytes(self):
        assert cli_progress._format_size(1024**3) == "1.0G"
        assert cli_progress._format_size(3 * 1024**3) == "3.0G"


class TestRenderRoster:
    """cli_progress.render_roster — the size-only pre-run roster (#53 §2.1)."""

    def test_lists_files_with_sizes_and_total(self, tmp_path):
        f1 = tmp_path / "tle2001.txt"
        f1.write_bytes(b"x" * 1536)  # 1.5 KB
        f2 = tmp_path / "tle2002.txt"
        f2.write_bytes(b"y" * 512)  # 512 B
        console = Console(file=io.StringIO(), width=80)

        cli_progress.render_roster(console, {str(f1): 1536, str(f2): 512})

        out = console.file.getvalue()
        assert "tle2001.txt" in out
        assert "tle2002.txt" in out
        assert "1.5K" in out
        assert "512B" in out
        assert "total" in out
        assert "2.0K" in out  # 1536 + 512 = 2048 bytes

    def test_renders_the_sizes_it_is_given(self, tmp_path):
        # The roster renders the caller-supplied sizes verbatim — it never
        # reads file contents (the caller stats once; the roster just displays).
        f1 = tmp_path / "tle2001.txt"
        f1.write_bytes(b"ignored")  # 7 bytes on disk, irrelevant
        console = Console(file=io.StringIO(), width=80)

        cli_progress.render_roster(console, {str(f1): 2048})

        # 2.0K proves the passed size (2048) was used, not the 7-byte content.
        assert "2.0K" in console.file.getvalue()


class TestProgressDisplayDrain:
    """_ProgressDisplay folds the queue into running state (issue #53 §6).

    These exercise the mode-independent tally/lifecycle logic (not rich's
    rendering), so they run with a non-terminal console.
    """

    def _display(self, q):
        console = Console(file=io.StringIO(), force_terminal=False, width=100)
        return cli_progress.ProgressDisplay(
            total_files=2,
            progress_queue=q,
            console=console,
            sizes={"a": 1000, "b": 500},
        )

    def test_progress_accumulates_overall_and_per_file_records(self):
        q = queue.Queue()
        disp = self._display(q)
        for msg in [
            pipeline.FileStarted("a"),
            pipeline.FileProgress("a", 100, 5),
            pipeline.FileProgress("a", 50, 3),
        ]:
            q.put(msg)
        disp._drain()
        assert disp._records == 8
        assert disp._rows["a"].records == 8

    def test_records_sum_across_files(self):
        q = queue.Queue()
        disp = self._display(q)
        for msg in [
            pipeline.FileStarted("a"),
            pipeline.FileStarted("b"),
            pipeline.FileProgress("a", 10, 4),
            pipeline.FileProgress("b", 20, 6),
        ]:
            q.put(msg)
        disp._drain()
        assert disp._records == 10
        assert disp._rows["a"].records == 4
        assert disp._rows["b"].records == 6

    def test_end_keeps_the_row_and_the_overall_tally(self):
        # The row is not removed — it stays for the rest of the run, and its
        # exact counts arrive later with the worker's FileStats.
        q = queue.Queue()
        disp = self._display(q)
        for msg in [
            pipeline.FileStarted("a"),
            pipeline.FileProgress("a", 10, 4),
            pipeline.FileEnded("a"),
        ]:
            q.put(msg)
        disp._drain()
        assert disp._rows["a"].records == 4
        assert disp._records == 4  # overall tally survives the file ending

    def test_late_deltas_never_inflate_a_finished_row(self):
        # A worker's last progress message can arrive after its future
        # resolved; adding it on top would push the row past its own totals.
        q = queue.Queue()
        disp = self._display(q)
        stats = report.FileStats(src_name="a")
        stats.paired_records, stats.clean_count = 40, 40
        disp.file_done(stats)
        q.put(pipeline.FileProgress("a", 500, 15))
        disp._drain()
        assert disp._rows["a"].records == 40  # not 55
        assert disp._rows["a"].bytes_done == disp._rows["a"].size
        assert disp._records == 15  # the corpus tally still counts every delta

    def test_file_done_counts_and_logs_in_non_tty(self):
        q = queue.Queue()
        console = Console(file=io.StringIO(), force_terminal=False, width=100)
        disp = cli_progress.ProgressDisplay(2, q, console, sizes={})
        stats = report.FileStats(src_name="tle2001.txt")
        stats.clean_count = 7
        stats.quarantined_count = 2

        disp.file_done(stats)

        out = console.file.getvalue()
        assert disp._files_done == 1
        assert "tle2001.txt" in out
        assert "7" in out and "2" in out


class TestProgressDisplayRendering:
    """_ProgressDisplay output and TTY/non-TTY modes (issue #53)."""

    def test_file_failed_counts_and_logs_error(self):
        console = Console(file=io.StringIO(), force_terminal=False, width=100)
        disp = cli_progress.ProgressDisplay(1, queue.Queue(), console, sizes={})

        disp.file_failed("bad.txt", RuntimeError("boom"))

        out = console.file.getvalue()
        assert disp._files_done == 1
        assert "[1/1]" in out and "bad.txt" in out and "boom" in out

    def test_non_tty_emits_no_ansi(self):
        # Off a TTY the live block is suppressed and the per-file completion
        # line is plain text — no ANSI escape sequences.
        console = Console(file=io.StringIO(), force_terminal=False, width=100)
        disp = cli_progress.ProgressDisplay(1, queue.Queue(), console, sizes={"a": 100})
        assert disp._live_mode is False
        stats = report.FileStats(src_name="a")
        stats.clean_count = 1
        stats.quarantined_count = 0

        disp.file_done(stats)

        out = console.file.getvalue()
        assert "\x1b[" not in out  # no ANSI escapes
        assert "a — 1 clean, 0 quarantined" in out

    def test_live_mode_moves_a_row_through_its_states(self):
        # On a (forced) TTY, entering starts the rich live block. The row exists
        # from construction and changes state in place — pending, running, and
        # (via file_done) done. The drain thread is halted for determinism.
        q = queue.Queue()
        console = Console(file=io.StringIO(), force_terminal=True, width=100)
        disp = cli_progress.ProgressDisplay(1, q, console, sizes={"a": 1000})
        with disp:
            disp._stop.set()  # halt the drain thread; drive _drain ourselves
            disp._thread.join()
            assert disp._rows["a"].state == "pending"

            q.put(pipeline.FileStarted("a"))
            q.put(pipeline.FileProgress("a", 200, 9))
            disp._drain()
            assert disp._rows["a"].state == "running"
            assert disp._records == 9

            stats = report.FileStats(src_name="a")
            stats.paired_records, stats.clean_count = 9, 9
            disp.file_done(stats)
            assert disp._rows["a"].state == "done"


class TestStatusSpinner:
    """_status wraps an otherwise-silent finalization phase (shard concat) in a
    rich spinner on a TTY, and is a no-op context off a TTY so nothing leaks to a
    pipe/structured output."""

    def test_status_is_a_spinner_on_a_tty(self, monkeypatch):
        from rich.status import Status

        monkeypatch.setattr(
            "lintle.term.stderr_console",
            Console(file=io.StringIO(), force_terminal=True),
        )
        assert isinstance(cli_progress.status("working…"), Status)

    def test_status_is_a_noop_context_off_a_tty(self, monkeypatch):

        monkeypatch.setattr(
            "lintle.term.stderr_console",
            Console(file=io.StringIO(), force_terminal=False),
        )
        assert isinstance(cli_progress.status("working…"), contextlib.nullcontext)


class TestDrainThreadSurvivesTransientError:
    """Issue #84 — a transient render error must not permanently stop the drain
    thread; the queue must keep being consumed across failures."""

    def _display(self, q):
        console = Console(file=io.StringIO(), force_terminal=False, width=100)
        return cli_progress.ProgressDisplay(
            total_files=3,
            progress_queue=q,
            console=console,
            sizes={"a": 1000},
        )

    def test_transient_drain_error_does_not_kill_thread(self, monkeypatch):
        # Arrange: make _drain raise on the first call only.
        q = queue.Queue()
        disp = self._display(q)
        call_count = {"n": 0}
        original_drain = disp._drain

        def flaky_drain():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient render glitch")
            original_drain()

        monkeypatch.setattr(disp, "_drain", flaky_drain)

        # Put messages on the queue; they should be consumed on the 2nd+ drain.
        q.put(pipeline.FileStarted("a"))
        q.put(pipeline.FileProgress("a", 100, 7))

        with disp:
            # Give the drain thread at least two iterations.
            import time

            time.sleep(0.4)

        # The queue was drained and records tallied — thread did NOT die.
        assert disp._records == 7, (
            f"Expected 7 records consumed, got {disp._records}; "
            "thread died on first transient error"
        )

    def test_genuine_shutdown_error_breaks_cleanly(self, monkeypatch):
        # An EOFError (manager gone) inside _drain should not propagate out of _run.
        q = queue.Queue()
        disp = self._display(q)

        def eof_drain():
            raise EOFError("manager gone")

        monkeypatch.setattr(disp, "_drain", eof_drain)

        # _run must complete without raising — daemon thread exits cleanly.
        import threading

        errors = []

        def run_and_capture():
            try:
                disp._run()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=run_and_capture)
        t.start()
        # Signal stop so the thread terminates quickly.
        disp._stop.set()
        t.join(timeout=2.0)
        assert not t.is_alive(), "Thread hung — did not exit"
        assert not errors, f"_run propagated exception: {errors}"


class TestBracketedFilenamesDoNotCrash:
    """Issue #114 — filenames containing rich markup brackets must not raise
    MarkupError in the progress display or the pre-run roster.

    BRACKET_NAME uses ``[red]`` (an open tag) — no ``/`` so it is a valid
    POSIX filename component. A closing-tag pattern is exercised via the
    synthetic roster test (passing the name directly as a dict key, no real
    file needed).
    """

    # Valid filename on POSIX — square brackets, no embedded slash.
    BRACKET_NAME = "tle[red]1.txt"

    def test_render_roster_with_bracketed_filename(self):
        # render_roster does not read the file — it just displays the given
        # path -> size map. The "file" column must not parse the name as rich
        # markup, so [red] must appear literally, not as a style tag.
        fake_path = "/data/source/" + self.BRACKET_NAME
        console = Console(file=io.StringIO(), width=80)
        # Must not raise rich.errors.MarkupError, and brackets must survive.
        cli_progress.render_roster(console, {fake_path: 512})
        out = console.file.getvalue()
        assert "[red]" in out, f"brackets eaten by markup: {out!r}"

    def test_render_roster_with_closing_tag_filename(self):
        # Also exercise a closing-tag pattern via a synthetic path (no real
        # file needed — render_roster only reads the passed dict).
        # Path.name of a bare name (no directory separator) is the name itself.
        fake_name = "tle[bold].txt"
        console = Console(file=io.StringIO(), width=80)
        cli_progress.render_roster(console, {fake_name: 512})
        out = console.file.getvalue()
        assert "[bold]" in out, f"brackets eaten by markup: {out!r}"

    def test_add_task_with_bracketed_filename_does_not_crash(self):
        # Drive ProgressDisplay through FileStarted with a bracket-laden name;
        # neither add_task nor __exit__ (Progress.stop) should raise.
        q = queue.Queue()
        console = Console(file=io.StringIO(), force_terminal=True, width=100)
        disp = cli_progress.ProgressDisplay(
            total_files=1,
            progress_queue=q,
            console=console,
            sizes={self.BRACKET_NAME: 512},
        )
        q.put(pipeline.FileStarted(self.BRACKET_NAME))
        q.put(pipeline.FileProgress(self.BRACKET_NAME, 100, 3))
        q.put(pipeline.FileEnded(self.BRACKET_NAME))
        # __exit__ calls Progress.stop() on the main thread — that's the
        # crash site when markup=True.
        with disp:
            disp._stop.set()
            disp._thread.join()
            disp._drain()

    def test_bracketed_label_rendered_literally_in_live_mode(self):
        # Verify the label text is not silently eaten as markup.
        q = queue.Queue()
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, width=100)
        disp = cli_progress.ProgressDisplay(
            total_files=1,
            progress_queue=q,
            console=console,
            sizes={self.BRACKET_NAME: 512},
        )
        q.put(pipeline.FileStarted(self.BRACKET_NAME))
        with disp:
            disp._stop.set()
            disp._thread.join()
            disp._drain()
            disp._refresh()
            # markup=False: the brackets render verbatim instead of being
            # parsed (and silently eaten, or raising MarkupError). Assert
            # inside the `with` — the transient display is erased on exit.
            assert "[red]" in buf.getvalue()


class TestLiveTable:
    """The one live table: every discovered file has a row from the first frame,
    work updates rows in place, and the frame never outgrows the terminal."""

    @staticmethod
    def _display(width, *, sizes, total_files=3, completed=(), height=40):
        console = Console(
            file=io.StringIO(), force_terminal=True, width=width, height=height
        )
        return cli_progress.ProgressDisplay(
            total_files, queue.Queue(), console, sizes, completed=completed
        )

    @staticmethod
    def _render(disp, table):
        buf = io.StringIO()
        Console(file=buf, force_terminal=True, width=disp._console.width).print(table)
        return buf.getvalue()

    def test_first_frame_is_the_roster(self):
        # Before any work: a row per discovered file with its size, and no
        # invented zeroes in the columns nothing has measured yet.
        disp = self._display(120, sizes={"a.txt": 1024, "b.txt": 2048})
        out = self._render(disp, disp._table())
        assert "a.txt" in out and "b.txt" in out
        assert "1.0K" in out and "2.0K" in out  # sizes, humanized
        # Pending rows are blank, not zeroed: only the summary row shows 0%.
        assert out.count("0%") == 1

    def test_row_updates_in_place_through_its_states(self):
        disp = self._display(120, sizes={"a.txt": 1000, "b.txt": 2000})
        row = disp._rows["b.txt"]
        row.state, row.bytes_done, row.records = "running", 500, 42
        out = self._render(disp, disp._table())
        assert "b.txt" in out and "25%" in out and "42" in out
        assert out.count("b.txt") == 1  # updated, never duplicated

        row.state, row.records, row.clean, row.quarantined = "done", 90, 88, 2
        row.bytes_done, row.elapsed = 2000, 75.0
        out = self._render(disp, disp._table())
        assert "100%" in out and "88" in out and "1:15" in out
        assert out.count("b.txt") == 1

    def test_failed_row_says_so_without_inventing_counts(self):
        disp = self._display(120, sizes={"a.txt": 1000})
        disp._rows["a.txt"].state = "failed"
        assert "failed" in self._render(disp, disp._table())

    def test_summary_row_is_pinned_and_counts_files(self):
        carried = [report.FileStats(src_name=f"prior{i}.txt") for i in range(3)]
        disp = self._display(
            120, sizes={"a.txt": 1000}, total_files=29, completed=carried
        )
        assert "3/29 files" in self._render(disp, disp._table())

    def test_all_rows_show_when_they_fit(self):
        sizes = {f"f{i}.txt": 1000 for i in range(10)}
        disp = self._display(120, sizes=sizes, total_files=10, height=40)
        # 10 rows + summary row; nothing hidden, so no ellipsis marker.
        assert disp._table().row_count == 11
        assert disp.windowed is False

    def test_window_follows_the_work_when_rows_exceed_the_height(self):
        sizes = {f"f{i}.txt": 1000 for i in range(29)}
        disp = self._display(120, sizes=sizes, total_files=29, height=24)
        for i in range(5):  # first five finished
            disp._rows[f"f{i}.txt"].state = "done"
        disp._rows["f5.txt"].state = "running"
        out = self._render(disp, disp._table())
        assert disp.windowed is True
        # The window starts at the first unfinished row and carries the marker.
        assert "f5.txt" in out and "more" in out
        assert "f0.txt" not in out  # scrolled out, not dropped
        # Bounded by the terminal: rows + ellipsis + summary fit the height.
        assert (
            disp._table().row_count
            <= 24 - cli_progress.ProgressDisplay._CHROME_LINES + 2
        )

    def test_window_never_slides_past_the_end(self):
        sizes = {f"f{i}.txt": 1000 for i in range(29)}
        disp = self._display(120, sizes=sizes, total_files=29, height=24)
        for row in disp._rows.values():  # everything finished
            row.state = "done"
        out = self._render(disp, disp._table())
        assert "f28.txt" in out  # the last row is visible at the end of a run

    def test_window_still_windows_on_a_terminal_shorter_than_the_chrome(self):
        # Height <= chrome used to invert the guard and return ALL rows — a
        # 200-row live region rich crops from the top, with `windowed` left
        # False so the complete results table was never printed either (#G).
        rows = [types.SimpleNamespace(state="done") for _ in range(200)]
        visible, hidden = cli_progress.window(rows, 8, 8)
        assert len(visible) == 1
        assert hidden == 199
        visible, hidden = cli_progress.window(rows, 1, 8)
        assert len(visible) == 1
        assert hidden == 199

    def test_zero_denominator_dash_respects_console_encoding(self):
        # A zero-byte corpus renders the dash cell; an ASCII-only console must
        # get "-" (the #97 rule), never a raw em dash it cannot encode.
        assert cli_progress._percent(0, 0, "-") == "-"
        assert cli_progress._percent(50, 100, "-") == "50%"
        assert cli_progress._dash(types.SimpleNamespace(encoding="ascii")) == "-"
        assert cli_progress._dash(types.SimpleNamespace(encoding="utf-8")) == "—"
        assert cli_progress._dash(types.SimpleNamespace(encoding=None)) == "—"

    def test_short_terminal_marks_the_display_windowed(self):
        # `windowed` drives the complete-table reprint on exit; at height 8 it
        # must be True, so run results are still shown after the live frame.
        sizes = {f"f{i}.txt": 1000 for i in range(20)}
        disp = self._display(120, sizes=sizes, total_files=20, height=8)
        self._render(disp, disp._table())
        assert disp.windowed is True

    def test_tiers_drop_columns_whole(self):
        sizes = {"a.txt": 1000}
        headers = lambda t: [c.header for c in t.columns]  # noqa: E731
        assert headers(self._display(120, sizes=sizes)._table()) == [
            "#",
            "file",
            "size",
            "progress",
            "%",
            "records",
            "clean",
            "quarantined",
            "time",
        ]
        assert headers(self._display(90, sizes=sizes)._table()) == [
            "#",
            "file",
            "progress",
            "%",
            "records",
            "clean",
            "quarantined",
        ]
        # Narrow keeps the results and drops the bar — the percent carries it.
        assert headers(self._display(70, sizes=sizes)._table()) == [
            "#",
            "file",
            "%",
            "records",
            "clean",
            "quarantined",
        ]

    def test_drain_folds_bytes_into_the_row_and_the_overall(self):
        q = queue.Queue()
        disp = self._display(120, sizes={"a.txt": 1000})
        disp._queue = q
        q.put(pipeline.FileStarted("a.txt"))
        q.put(pipeline.FileProgress("a.txt", 400, 7))
        disp._drain()
        assert disp._bytes_done == 400
        assert disp._rows["a.txt"].bytes_done == 400
        assert disp._rows["a.txt"].records == 7

    def test_no_completion_line_on_a_tty(self):
        # The row carries the outcome; a printed line would be the new output
        # the single-table model exists to avoid.
        console = Console(file=io.StringIO(), force_terminal=True, width=120)
        disp = cli_progress.ProgressDisplay(1, queue.Queue(), console, {"a.txt": 10})
        stats = report.FileStats(src_name="a.txt")
        stats.clean_count, stats.paired_records = 5, 5
        disp.file_done(stats)
        assert "5 clean" not in console.file.getvalue()

    def test_resumed_files_get_complete_rows_from_the_checkpoint(self):
        # Regression: a resumed run showed rows only for the files left to do,
        # so the summary said "2/3 files" above a table with one row in it, and
        # the carried-over records/clean/quarantined vanished from every total.
        carried = report.FileStats(src_name="a.txt")
        carried.paired_records, carried.clean_count = 900, 850
        carried.quarantined_count, carried.elapsed_seconds = 50, 12.0
        sizes = {"a.txt": 1000, "b.txt": 1000, "c.txt": 1000}
        disp = self._display(120, sizes=sizes, total_files=3, completed=[carried])

        row = disp._rows["a.txt"]
        assert row.state == "resumed" and row.bytes_done == row.size
        assert (row.records, row.clean, row.quarantined) == (900, 850, 50)
        # Its numbers join the run totals rather than restarting from zero.
        assert (disp._records, disp._clean, disp._quarantined) == (900, 850, 50)
        assert disp._bytes_done == 1000 and disp._files_done == 1

        out = self._render(disp, disp._table())
        assert "a.txt" in out and "b.txt" in out and "c.txt" in out
        assert "1/3 files" in out
        # Complete, so it shows its counts — the pending rows stay blank.
        assert "850" in out and "900" in out

    def test_resumed_row_for_a_file_no_longer_in_the_input_set_is_ignored(self):
        # The checkpoint can name a file the current invocation did not glob.
        gone = report.FileStats(src_name="dropped.txt")
        gone.paired_records = 5
        disp = self._display(120, sizes={"a.txt": 10}, total_files=1, completed=[gone])
        assert "dropped.txt" not in disp._rows
        assert disp._files_done == 1 and disp._records == 0

    def test_failure_still_prints_its_error_on_a_tty(self):
        # A failure is not routine progress and the row cannot carry the reason.
        console = Console(file=io.StringIO(), force_terminal=True, width=120)
        disp = cli_progress.ProgressDisplay(1, queue.Queue(), console, {"a.txt": 10})
        disp.file_failed("/src/a.txt", RuntimeError("boom"))
        out = console.file.getvalue()
        assert "boom" in out and disp._rows["a.txt"].state == "failed"


class _AssertingLock:
    """A stand-in for the tables' lock that RAISES on a nested acquire instead
    of blocking. threading.Lock is not reentrant, so the real deadlock shows up
    as a hung test run — useless as a signal. This turns it into a fast, named
    failure."""

    def __init__(self):
        self.held = False

    def __enter__(self):
        assert not self.held, (
            "_refresh must not hold the lock _table takes — threading.Lock is "
            "not reentrant, so this nesting deadlocks the first frame"
        )
        self.held = True
        return self

    def __exit__(self, *_exc):
        self.held = False
        return False


class TestLockConvention:
    """_LiveTable's rule: _table() acquires the lock, _refresh() must not. It is
    easy to 'tidy' the lock up into _refresh — and that hangs the whole run, so
    both subclasses are pinned here."""

    def test_unit_table_refresh_does_not_nest_the_lock(self):
        console = Console(file=io.StringIO(), force_terminal=True, width=120, height=40)
        table = cli_progress.UnitTable(["a", "b"], ("#", "file", "n"), console=console)
        table._lock = _AssertingLock()
        with table:
            table.start("a")
            table.update("a", n="1")
            table.finish("a", n="2")
            table.totals(n="2")
            table.phase("writing…")

    def test_progress_display_refresh_does_not_nest_the_lock(self):
        console = Console(file=io.StringIO(), force_terminal=True, width=120, height=40)
        disp = cli_progress.ProgressDisplay(
            1, queue.Queue(), console, sizes={"a.txt": 100}
        )
        disp._lock = _AssertingLock()
        stats = report.FileStats(src_name="a.txt")
        stats.clean_count = 1
        with disp:
            disp.file_done(stats)


class TestUnitTable:
    """The post-run commands' live table: rows exist from the first frame, work
    fills them in place, and the finished table is the results view."""

    HEADERS = ("#", "file", "size", "progress", "records", "hard")

    def _table(
        self, names, *, terminal=True, width=120, height=40, drop=None, justify=None
    ):
        console = Console(
            file=io.StringIO(), force_terminal=terminal, width=width, height=height
        )
        return cli_progress.UnitTable(
            names,
            self.HEADERS,
            console=console,
            drop=drop or {},
            justify=justify,
        )

    def test_first_frame_is_the_roster(self):
        table = self._table(["a", "b", "c"])
        with table:
            rendered = self._render(table)
        assert "a" in rendered and "c" in rendered
        assert "0/3 files" in rendered

    @staticmethod
    def _render(table):
        buf = io.StringIO()
        Console(file=buf, force_terminal=True, width=table._console.width).print(
            table._table()
        )
        return buf.getvalue()

    def test_cells_update_in_place_never_appending_a_row(self):
        table = self._table(["a", "b"])
        with table:
            table.start("a")
            table.update("a", records="1,000")
            table.finish("a", records="2,000", hard="1")
            rendered = self._render(table)
        assert rendered.count("a ") >= 1
        assert "2,000" in rendered and "1,000" not in rendered
        assert "1/2 files" in rendered

    def test_phase_relabels_the_summary_row(self):
        # The stages after the per-unit loop report themselves in the table
        # rather than by printing a line.
        table = self._table(["a"])
        with table:
            table.phase("sorting…")
            assert "sorting…" in self._render(table)
            table.phase(None)
            assert "0/1 files" in self._render(table)

    def test_window_marks_what_it_cannot_show(self):
        table = self._table([f"f{i}" for i in range(30)], height=20)
        with table:
            table.finish("f0", records="1")
            rendered = self._render(table)
        assert "more" in rendered
        assert table.windowed is True

    def test_off_a_tty_prints_the_roster_then_the_results(self):
        table = self._table(["a", "b"], terminal=False)
        with table:
            table.finish("a", records="7")
            table.finish("b", records="9")
        out = table._console.file.getvalue()
        # Two static prints, no live frames: the roster, then the results.
        assert out.count("records") == 2
        assert "7" in out and "9" in out

    def test_tier_drops_columns_whole(self):
        table = self._table(["a"], width=70, drop={"narrow": ("size", "progress")})
        headers = [c.header for c in table._table().columns]
        assert headers == ["#", "file", "records", "hard"]

    def test_can_override_column_justification(self):
        table = self._table(["a"], justify={"progress": "left", "hard": "left"})
        assert [c.justify for c in table._table().columns] == [
            "right",
            "left",
            "right",
            "left",
            "right",
            "left",
        ]


class TestHeartbeat:
    """A number that changes is not motion. The summary row carries a spinner
    turned by the clock, so it keeps moving through the stages that report
    rarely instead of freezing until the next update lands."""

    def _table(self, terminal=True):
        console = Console(
            file=io.StringIO(), force_terminal=terminal, width=100, height=40
        )
        return cli_progress.UnitTable(
            ["a", "b"], ("#", "file", "records"), console=console
        )

    @staticmethod
    def _glyph(table):
        return table._table().columns[1]._cells[-1][0]

    def test_spinner_advances_with_the_clock_not_with_work(self, monkeypatch):
        # The regression: deriving the frame from the redraw count meant a
        # stage that reports once a minute showed a frozen glyph for a minute.
        table = self._table()
        now = [table._start]
        monkeypatch.setattr(cli_progress.time, "monotonic", lambda: now[0])
        with table:
            frames = []
            for _ in range(4):  # time passes; no work is reported at all
                now[0] += cli_progress._TICK
                frames.append(self._glyph(table))
        assert len(set(frames)) == 4

    def test_spinner_holds_still_while_the_clock_does(self, monkeypatch):
        # The complement: work landing does not advance it either, so the rate
        # is the clock's and nothing else's.
        table = self._table()
        monkeypatch.setattr(cli_progress.time, "monotonic", lambda: table._start)
        with table:
            frames = []
            for _ in range(4):
                table.update("a", records="1")
                frames.append(self._glyph(table))
        assert len(set(frames)) == 1

    def test_clean_beats_too_and_at_the_same_rate(self, monkeypatch):
        # clean's table went without a heartbeat while verify's and dedup's had
        # one, which is the inconsistency the shared helper exists to prevent.
        console = Console(file=io.StringIO(), force_terminal=True, width=120)
        disp = cli_progress.ProgressDisplay(2, queue.Queue(), console, {"a.txt": 10})
        now = [disp._start]
        monkeypatch.setattr(cli_progress.time, "monotonic", lambda: now[0])
        frames = []
        for _ in range(4):
            now[0] += cli_progress._TICK
            frames.append(disp._table().columns[1]._cells[-1][0])
        assert len(set(frames)) == 4
        assert all(f in cli_progress._SPINNER for f in frames)

    def test_clean_has_no_spinner_once_finished_or_off_a_tty(self):
        console = Console(file=io.StringIO(), force_terminal=True, width=120)
        disp = cli_progress.ProgressDisplay(2, queue.Queue(), console, {"a.txt": 10})
        disp._finished = True
        assert disp._table().columns[1]._cells[-1][0] not in cli_progress._SPINNER
        piped = Console(file=io.StringIO(), force_terminal=False, width=120)
        off = cli_progress.ProgressDisplay(2, queue.Queue(), piped, {"a.txt": 10})
        assert off._table().columns[1]._cells[-1][0] not in cli_progress._SPINNER

    def test_no_spinner_once_the_run_is_over(self):
        table = self._table()
        with table:
            table.finish("a", records="1")
        assert table._table().columns[1]._cells[-1][0] not in cli_progress._SPINNER

    def test_no_spinner_off_a_tty(self):
        # Piped output must stay stable text — a spinner glyph would be noise.
        table = self._table(terminal=False)
        with table:
            table.update("a", records="1")
            assert table._table().columns[1]._cells[-1][0] not in cli_progress._SPINNER


class TestBarCell:
    """cli_progress.bar — the same renderable clean's rows use, so a filling bar
    means the same thing in every command."""

    def test_bar_is_clamped_to_its_total(self):
        from rich.progress_bar import ProgressBar

        b = cli_progress.bar(500, 1000)
        assert isinstance(b, ProgressBar) and b.completed == 500 and b.total == 1000
        assert cli_progress.bar(2000, 1000).completed == 1000  # never past 100%

    def test_renderable_cells_survive_the_table(self):
        # Cells are passed through, not str()'d — a str() would render a repr.
        console = Console(file=io.StringIO(), force_terminal=True, width=100)
        table = cli_progress.UnitTable(
            ["a"], ("#", "file", "progress"), console=console
        )
        table.update("a", progress=cli_progress.bar(1, 2))
        buf = io.StringIO()
        Console(file=buf, force_terminal=True, width=100).print(table._table())
        assert "ProgressBar" not in buf.getvalue()
        assert "━" in buf.getvalue()
