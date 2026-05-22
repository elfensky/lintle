"""Tests for tlekit.cli — argument parsing, path discovery, exit codes."""

import json
import os
import queue
import signal
import time

from tlekit import cli, report


class TestDiscoverPaths:
    def test_discover_expands_directory(self, tmp_path):
        (tmp_path / "tle2001.txt").write_text("x")
        (tmp_path / "tle2002.txt").write_text("x")
        (tmp_path / "tle2001.cleaned.txt").write_text("x")  # tool output — excluded
        (tmp_path / "tle2001.broken.txt").write_text("x")  # tool output — excluded
        (tmp_path / "notes.md").write_text("x")  # not a TLE file

        found = cli.discover_paths([str(tmp_path)])

        names = sorted(os.path.basename(p) for p in found)
        assert names == ["tle2001.txt", "tle2002.txt"]

    def test_discover_passes_through_explicit_files(self, tmp_path):
        explicit = tmp_path / "tle2001.txt"
        explicit.write_text("x")
        assert cli.discover_paths([str(explicit)]) == [str(explicit)]


class TestBuildParser:
    def test_parser_defaults(self):
        args = cli.build_parser().parse_args(["validate"])
        assert args.command == "validate"
        assert args.paths == ["data/source"]
        assert args.out_dir == "data/output"
        assert args.report == "text"

    def test_parser_accepts_jobs_and_paths(self):
        args = cli.build_parser().parse_args(
            ["clean", "a.txt", "b.txt", "--jobs", "4", "--report", "json"]
        )
        assert args.command == "clean"
        assert args.paths == ["a.txt", "b.txt"]
        assert args.jobs == 4
        assert args.report == "json"


class TestMain:
    def test_main_clean_returns_zero_on_clean_corpus(self, tmp_path, line1, line2):
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        assert rc == 0
        assert (out / "cleaned" / "tle2099.cleaned.txt").exists()
        assert (out / "broken" / "tle2099.broken.txt").exists()
        # A clean run writes a Markdown run report to the out-dir root.
        report_md = (out / "report.md").read_text()
        assert "# tlekit clean run report" in report_md
        assert "tle2099.txt" in report_md
        assert "Records:" in report_md

    def test_main_returns_one_when_records_quarantined(self, tmp_path, line1, line2):
        src = tmp_path / "src"
        src.mkdir()
        bad_line1 = line1[:68] + "9"
        (src / "tle2099.txt").write_bytes(
            (bad_line1 + "\n" + line2 + "\n").encode("ascii")
        )
        out = tmp_path / "out"

        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        assert rc == 1

    def test_main_returns_two_when_no_input_files(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        rc = cli.main(["validate", str(empty)])
        assert rc == 2

    def test_main_validate_prints_summary(self, tmp_path, line1, line2, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

        rc = cli.main(["validate", str(src), "--jobs", "1"])

        assert rc == 0
        assert "tle2099.txt" in capsys.readouterr().out

    def test_main_returns_two_when_a_file_fails_to_process(self, tmp_path):
        # An explicit path to a missing file is passed through to a worker,
        # which raises when it cannot open it — an operational error.
        missing = tmp_path / "tle_missing.txt"  # never created
        rc = cli.main(["validate", str(missing), "--jobs", "1"])
        assert rc == 2

    def test_main_returns_two_on_disk_shortfall(
        self, tmp_path, line1, line2, monkeypatch
    ):
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        class _Usage:
            free = 1  # far below the doubled input size

        monkeypatch.setattr(cli.shutil, "disk_usage", lambda _path: _Usage())
        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 2

    def test_main_prints_progress_to_stderr(self, tmp_path, line1, line2, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

        cli.main(["clean", str(src), "--out-dir", str(tmp_path / "out"), "--jobs", "1"])

        err = capsys.readouterr().err
        assert "processing 1 file(s)" in err  # start line
        # capsys makes stderr a non-TTY, so the live spinner is suppressed and
        # the progress display logs one plain line per completed file instead.
        assert "[1/1]" in err

    def test_main_json_report_prints_json(self, tmp_path, line1, line2, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

        rc = cli.main(["validate", str(src), "--jobs", "1", "--report", "json"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data[0]["src_name"] == "tle2099.txt"
        assert data[0]["clean_count"] == 1

    def test_main_validate_lists_reject_locations(self, tmp_path, line1, line2, capsys):
        src = tmp_path / "src"
        src.mkdir()
        bad_line1 = line1[:68] + "9"  # wrong checksum — the record is quarantined
        (src / "tle2099.txt").write_bytes(
            (bad_line1 + "\n" + line2 + "\n").encode("ascii")
        )

        rc = cli.main(["validate", str(src), "--jobs", "1"])

        assert rc == 1
        # validate mode lists each quarantined record's location and reason.
        assert "checksum" in capsys.readouterr().out

    def test_main_returns_130_on_keyboard_interrupt(
        self, tmp_path, line1, line2, monkeypatch
    ):
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

        def _interrupt(_futures):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli.concurrent.futures, "as_completed", _interrupt)
        # main()'s Ctrl-C handler sets SIGINT to SIG_IGN and never restores it;
        # save and restore it so later tests can still be interrupted.
        original_sigint = signal.getsignal(signal.SIGINT)
        try:
            rc = cli.main(
                ["clean", str(src), "--out-dir", str(tmp_path / "out"), "--jobs", "1"]
            )
        finally:
            signal.signal(signal.SIGINT, original_sigint)

        assert rc == 130


class TestFormatElapsed:
    def test_format_elapsed_renders_minutes_and_hours(self):
        assert cli._format_elapsed(0) == "0:00"
        assert cli._format_elapsed(9) == "0:09"
        assert cli._format_elapsed(75) == "1:15"
        assert cli._format_elapsed(3661) == "1:01:01"


class TestShutdownHelpers:
    def test_ignore_sigint_sets_handler_to_ignore(self):
        original = signal.getsignal(signal.SIGINT)
        try:
            cli._ignore_sigint()
            assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
        finally:
            signal.signal(signal.SIGINT, original)

    def test_terminate_workers_terminates_every_process(self):
        class _FakeProc:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

        class _FakeExecutor:
            def __init__(self, processes):
                self._processes = processes

        procs = {1: _FakeProc(), 2: _FakeProc()}
        cli._terminate_workers(_FakeExecutor(procs))
        assert all(proc.terminated for proc in procs.values())

    def test_terminate_workers_tolerates_missing_processes(self):
        class _Bare:
            pass

        # An executor with no `_processes` attribute must not raise.
        cli._terminate_workers(_Bare())


class TestProgressDisplay:
    def test_drain_folds_queued_deltas_into_total(self):
        progress = queue.Queue()
        progress.put(10)
        progress.put(5)
        display = cli._ProgressDisplay(total_files=3, progress_queue=progress)

        display._drain()

        assert display._records == 15

    def test_render_writes_a_status_line(self, capsys):
        display = cli._ProgressDisplay(total_files=3, progress_queue=queue.Queue())
        display._live = True
        display._records = 1234

        display._render()

        err = capsys.readouterr().err
        assert "0/3 files" in err
        assert "1,234 records" in err

    def test_log_prints_the_message(self, capsys):
        display = cli._ProgressDisplay(total_files=1, progress_queue=queue.Queue())
        display.log("hello")
        assert "hello" in capsys.readouterr().err

    def test_log_clears_the_live_line_first(self, capsys):
        display = cli._ProgressDisplay(total_files=1, progress_queue=queue.Queue())
        display._live = True  # pretend stderr is a TTY
        display.log("hello")
        err = capsys.readouterr().err
        assert "\x1b[K" in err  # the live line is cleared before the message
        assert "hello" in err

    def test_file_done_logs_a_summary_off_a_tty(self, capsys):
        display = cli._ProgressDisplay(total_files=2, progress_queue=queue.Queue())
        stats = report.FileStats(src_name="tle2099.txt")
        stats.clean_count = 5
        stats.quarantined_count = 1

        display.file_done(stats)

        err = capsys.readouterr().err
        assert "[1/2] tle2099.txt" in err
        assert "5 clean" in err and "1 quarantined" in err

    def test_file_done_is_silent_on_a_tty(self, capsys):
        display = cli._ProgressDisplay(total_files=2, progress_queue=queue.Queue())
        display._live = True  # on a TTY the spinner shows progress instead
        display.file_done(report.FileStats(src_name="tle2099.txt"))

        assert "tle2099.txt" not in capsys.readouterr().err
        assert display._files_done == 1

    def test_file_failed_logs_the_error(self, capsys):
        display = cli._ProgressDisplay(total_files=1, progress_queue=queue.Queue())
        display.file_failed("bad.txt", RuntimeError("boom"))
        err = capsys.readouterr().err
        assert "[1/1]" in err and "bad.txt" in err and "boom" in err

    def test_context_manager_runs_a_thread_and_clears_the_line(self, capsys):
        progress = queue.Queue()
        display = cli._ProgressDisplay(total_files=1, progress_queue=progress)
        display._live = True  # exercise the repaint thread and the exit clear

        with display:
            progress.put(7)
            time.sleep(0.15)  # let the repaint thread tick at least once

        err = capsys.readouterr().err
        assert "\x1b[K" in err  # the live line was painted, then cleared on exit
        assert display._records == 7  # the queued delta was drained
