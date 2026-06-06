"""Tests for cli_progress.py — the live progress display, roster, and spinner."""

import contextlib
import io
import queue

from rich.console import Console

from lintle import cli_progress, pipeline, report


class TestFormatSize:
    """cli_progress._format_size — human-readable byte counts for the roster (#53)."""

    def test_bytes_below_one_kib(self):
        assert cli_progress._format_size(0) == "0 B"
        assert cli_progress._format_size(512) == "512 B"

    def test_kilobytes(self):
        assert cli_progress._format_size(1024) == "1.0 KB"
        assert cli_progress._format_size(1536) == "1.5 KB"

    def test_gigabytes(self):
        assert cli_progress._format_size(1024**3) == "1.0 GB"
        assert cli_progress._format_size(3 * 1024**3) == "3.0 GB"


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
        assert "1.5 KB" in out
        assert "512 B" in out
        assert "total" in out
        assert "2.0 KB" in out  # 1536 + 512 = 2048 bytes

    def test_renders_the_sizes_it_is_given(self, tmp_path):
        # The roster renders the caller-supplied sizes verbatim — it never
        # reads file contents (the caller stats once; the roster just displays).
        f1 = tmp_path / "tle2001.txt"
        f1.write_bytes(b"ignored")  # 7 bytes on disk, irrelevant
        console = Console(file=io.StringIO(), width=80)

        cli_progress.render_roster(console, {str(f1): 2048})

        # 2.0 KB proves the passed size (2048) was used, not the 7-byte content.
        assert "2.0 KB" in console.file.getvalue()


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


class TestFormatElapsed:
    def test_format_elapsed_renders_minutes_and_hours(self):
        assert cli_progress._format_elapsed(0) == "0:00"
        assert cli_progress._format_elapsed(9) == "0:09"
        assert cli_progress._format_elapsed(75) == "1:15"
        assert cli_progress._format_elapsed(3661) == "1:01:01"


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
            assert "a" in disp._tasks
            assert disp._records == 9

            q.put(pipeline.FileEnded("a"))
            disp._drain()
            assert "a" not in disp._tasks


class TestProgressColumns:
    """Per-file ETA + throughput and an overall files-done/total counter, gated
    by task ``kind`` so byte-only columns never render on the file-count overall
    row and the count-only column never renders raw bytes on a per-file row."""

    class _Inner:
        def render(self, task):
            from rich.text import Text

            return Text("INNER")

    def test_for_kind_renders_inner_only_for_the_matching_kind(self):
        col = cli_progress._ForKind("file", self._Inner())

        class _Task:
            def __init__(self, kind):
                self.fields = {"kind": kind}

        assert col.render(_Task("file")).plain == "INNER"
        assert col.render(_Task("overall")).plain == ""

    def test_overall_and_per_file_tasks_are_kind_tagged(self):
        q = queue.Queue()
        console = Console(file=io.StringIO(), force_terminal=True, width=120)
        disp = cli_progress.ProgressDisplay(1, q, console, sizes={"a": 1000})
        with disp:
            disp._stop.set()
            disp._thread.join()
            assert {t.fields.get("kind") for t in disp._progress.tasks} == {"overall"}
            q.put(pipeline.FileStarted("a"))
            disp._drain()
            assert {t.fields.get("kind") for t in disp._progress.tasks} == {
                "overall",
                "file",
            }

    def test_progress_wires_speed_eta_per_file_and_mofn_overall(self):
        console = Console(file=io.StringIO(), force_terminal=True, width=120)
        disp = cli_progress.ProgressDisplay(1, queue.Queue(), console, sizes={})
        with disp:
            disp._stop.set()
            disp._thread.join()
            wrapped = {
                (c._kind, type(c._inner).__name__)
                for c in disp._progress.columns
                if isinstance(c, cli_progress._ForKind)
            }
        assert ("file", "TransferSpeedColumn") in wrapped
        assert ("file", "TimeRemainingColumn") in wrapped
        assert ("overall", "MofNCompleteColumn") in wrapped


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
