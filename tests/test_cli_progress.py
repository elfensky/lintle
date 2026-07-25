"""Tests for cli_progress.py — the live progress display, roster, and spinner."""

import contextlib
import io
import queue

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
        assert disp._file_records["a"] == 8

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
        assert disp._file_records == {"a": 4, "b": 6}

    def test_end_clears_per_file_state_but_keeps_overall(self):
        q = queue.Queue()
        disp = self._display(q)
        for msg in [
            pipeline.FileStarted("a"),
            pipeline.FileProgress("a", 10, 4),
            pipeline.FileEnded("a"),
        ]:
            q.put(msg)
        disp._drain()
        assert "a" not in disp._file_records
        assert disp._records == 4  # overall tally survives the file ending

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
        assert disp._live is False
        stats = report.FileStats(src_name="a")
        stats.clean_count = 1
        stats.quarantined_count = 0

        disp.file_done(stats)

        out = console.file.getvalue()
        assert "\x1b[" not in out  # no ANSI escapes
        assert "a — 1 clean, 0 quarantined" in out

    def test_live_mode_tracks_per_file_tasks(self):
        # On a (forced) TTY, entering starts the rich live block; a per-file
        # task appears on FileStarted, advances on FileProgress, and is removed
        # on FileEnded. The drain thread is halted so assertions are deterministic.
        q = queue.Queue()
        console = Console(file=io.StringIO(), force_terminal=True, width=100)
        disp = cli_progress.ProgressDisplay(1, q, console, sizes={"a": 1000})
        with disp:
            disp._stop.set()  # halt the drain thread; drive _drain ourselves
            disp._thread.join()

            q.put(pipeline.FileStarted("a"))
            q.put(pipeline.FileProgress("a", 200, 9))
            disp._drain()
            assert "a" in disp._rows
            assert disp._records == 9

            q.put(pipeline.FileEnded("a"))
            disp._drain()
            assert "a" not in disp._rows


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


class TestPhaseBar:
    """cli_progress.phase_bar — the single-task bar for the post-run phases."""

    def test_off_tty_renders_nothing_but_still_updates(self, monkeypatch):
        # Off a TTY the bar is disabled so nothing leaks into a pipe, yet the
        # yielded callable must stay usable — callers advance it unconditionally.
        buf = io.StringIO()
        monkeypatch.setattr(
            cli_progress.term, "stderr_console", Console(file=buf, force_terminal=False)
        )
        with cli_progress.phase_bar("verifying", 2) as progress:
            progress(description="verifying tle2000")
            progress(advance=1)
        assert buf.getvalue() == ""

    def test_on_tty_renders_the_description(self, monkeypatch):
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, width=80)
        monkeypatch.setattr(cli_progress.term, "stderr_console", console)
        with cli_progress.phase_bar("verifying", 2) as progress:
            progress(description="verifying tle2000", advance=1)
            console.print()  # force a frame while the live block is open
        assert "verifying tle2000" in buf.getvalue()

    def test_indeterminate_total_is_allowed(self, monkeypatch):
        # dedup's write phase has no known group count up front (total=None).
        buf = io.StringIO()
        monkeypatch.setattr(
            cli_progress.term, "stderr_console", Console(file=buf, force_terminal=True)
        )
        with cli_progress.phase_bar("writing", None) as progress:
            progress(completed=10_000)


class TestPhaseTwoTable:
    """Phase 2 renders in-flight files plus a pinned summary row in one table,
    bounded so terminal height and resize can never strand or crop it."""

    @staticmethod
    def _display(width, *, sizes, total_files=3, done=0):
        console = Console(file=io.StringIO(), force_terminal=True, width=width)
        return cli_progress.ProgressDisplay(
            total_files, queue.Queue(), console, sizes, already_done=done
        )

    @staticmethod
    def _render(disp, table):
        buf = io.StringIO()
        Console(file=buf, force_terminal=True, width=disp._console.width).print(table)
        return buf.getvalue()

    def test_row_carries_index_size_and_records(self):
        disp = self._display(120, sizes={"a.txt": 1000, "b.txt": 2000})
        disp._rows["b.txt"] = cli_progress._InFlight(
            index=2, name="b.txt", total=2000, started=0.0, done=500, records=42
        )
        out = self._render(disp, disp._table())
        # index and size are the identity link back to the phase-1 roster row.
        assert "b.txt" in out and "2.0K" in out and "42" in out
        assert "25%" in out  # 500 of 2000 bytes

    def test_summary_row_is_pinned_and_counts_files(self):
        disp = self._display(120, sizes={"a.txt": 1000}, total_files=29, done=3)
        out = self._render(disp, disp._table())
        assert "3/29 files" in out

    def test_height_is_bounded_by_in_flight_count(self):
        # The invariant that makes phase 2 safe at terminal height 24: rows
        # never exceed the in-flight files plus the one summary row, however
        # many files the run has.
        sizes = {f"f{i}.txt": 1000 for i in range(29)}
        disp = self._display(120, sizes=sizes, total_files=29)
        for i in range(4):  # four workers in flight
            name = f"f{i}.txt"
            disp._rows[name] = cli_progress._InFlight(
                index=i + 1, name=name, total=1000, started=0.0
            )
        assert disp._table().row_count == 5

    def test_narrow_drops_size_and_medium_drops_rate_columns(self):
        sizes = {"a.txt": 1000}
        wide = self._display(120, sizes=sizes)._table()
        medium = self._display(90, sizes=sizes)._table()
        narrow = self._display(70, sizes=sizes)._table()
        headers = lambda t: [c.header for c in t.columns]  # noqa: E731
        assert headers(wide) == [
            "#",
            "file",
            "size",
            "progress",
            "%",
            "records",
            "MB/s",
            "ETA",
        ]
        assert headers(medium) == ["#", "file", "size", "progress", "%", "records"]
        assert headers(narrow) == ["#", "file", "progress", "%", "records"]

    def test_drain_folds_bytes_into_the_overall_row(self):
        q = queue.Queue()
        disp = self._display(120, sizes={"a.txt": 1000})
        disp._rows["a.txt"] = cli_progress._InFlight(
            index=1, name="a.txt", total=1000, started=0.0
        )
        q.put(pipeline.FileProgress("a.txt", 400, 7))
        disp._queue = q
        disp._drain()
        assert disp._bytes_done == 400
        assert disp._rows["a.txt"].done == 400 and disp._rows["a.txt"].records == 7
