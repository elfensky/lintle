"""Tests for lintle.cli — argument parsing, path discovery, exit codes."""

import io
import json
import os
import queue
import signal

import pytest
from rich.console import Console

from lintle import (
    cli,
    cli_progress,
    pipeline,
    process_control,
    report,
    resume,
    run_planning,
    term,
    thresholds,
    worker_pool,
)


class TestDiscoverPaths:
    def test_discover_expands_directory(self, tmp_path):
        (tmp_path / "tle2001.txt").write_text("x")
        (tmp_path / "tle2002.txt").write_text("x")
        (tmp_path / "tle2001.cleaned.txt").write_text("x")  # tool output — excluded
        (tmp_path / "tle2001.broken.txt").write_text("x")  # tool output — excluded
        (tmp_path / "notes.md").write_text("x")  # not a TLE file

        found = cli.discover_paths(str(tmp_path))

        names = sorted(os.path.basename(p) for p in found)
        assert names == ["tle2001.txt", "tle2002.txt"]

    def test_discover_passes_through_explicit_file(self, tmp_path):
        explicit = tmp_path / "tle2001.txt"
        explicit.write_text("x")
        assert cli.discover_paths(str(explicit)) == [str(explicit)]


class TestResolveJobs:
    """cli.resolve_jobs — default worker count (issue #53 §2.3/§3.4)."""

    def test_default_reserves_one_core(self):
        # Plenty of files, 8 cores: reserve one for the OS during the long run.
        assert cli.resolve_jobs(None, 8, 100) == 7

    def test_default_caps_at_file_count(self):
        # 16 cores but only 4 files: no point spawning idle workers.
        assert cli.resolve_jobs(None, 16, 4) == 4

    def test_default_floor_is_one(self):
        # Few cores must never resolve below a single worker.
        assert cli.resolve_jobs(None, 2, 30) == 1
        assert cli.resolve_jobs(None, 1, 30) == 1

    def test_explicit_jobs_passthrough_not_capped(self):
        # An explicit --jobs is the user's deliberate choice; never capped.
        assert cli.resolve_jobs(16, 16, 4) == 16
        assert cli.resolve_jobs(4, 8, 100) == 4


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


class TestBuildParser:
    def test_parser_defaults(self):
        args = cli.build_parser().parse_args(["validate"])
        assert args.command == "validate"
        # path defaults to None so main() can tell "user passed nothing"
        # apart from "user explicitly passed the default" for error wording.
        assert args.path is None
        assert args.out_dir == "data/output"
        assert args.report == "text"

    def test_parser_accepts_jobs_and_path(self):
        args = cli.build_parser().parse_args(
            ["clean", "a.txt", "--jobs", "4", "--report", "json"]
        )
        assert args.command == "clean"
        assert args.path == "a.txt"
        assert args.jobs == 4
        assert args.report == "json"

    def test_parser_version_flag_exits_zero(self, capsys):
        import pytest

        with pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args(["--version"])
        assert exc.value.code == 0
        assert "lintle" in capsys.readouterr().out

    def test_parser_help_includes_examples_and_exit_codes(self, capsys):
        import pytest

        with pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Examples:" in out
        assert "Exit codes:" in out

    def test_parser_rejects_multiple_positional_inputs(self):
        # Single-input contract: only one positional allowed.
        import pytest

        with pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args(["clean", "a.txt", "b.txt"])
        assert exc.value.code == 2  # argparse usage error


class TestCheckPaths:
    def test_missing_default_yields_friendly_hint(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no data/source here
        err = cli.check_paths("data/source", using_default=True)
        assert err is not None
        assert "data/source" in err
        assert "lintle --help" in err
        assert "or create" in err  # pins the multi-line hint branch

    def test_missing_explicit_path_yields_plain_message(self, tmp_path):
        err = cli.check_paths(str(tmp_path / "nope.txt"), using_default=False)
        assert err is not None
        assert "no such file or directory" in err
        assert "data/source" not in err  # not the default-hint variant

    def test_existing_path_returns_none(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("x")
        assert cli.check_paths(str(f), using_default=False) is None

    def test_os_access_false_negative_does_not_refuse_run(self, tmp_path, monkeypatch):
        # os.access() consults POSIX mode bits and is a false-negative on
        # filesystems that grant read via ACLs (NFSv4, SMB, FUSE). The
        # preflight must not refuse a run on os.access() alone — the
        # authoritative answer is whatever the worker's open() returns.
        f = tmp_path / "readable.txt"
        f.write_text("x")
        monkeypatch.setattr(cli.os, "access", lambda _p, _m: False)
        assert cli.check_paths(str(f), using_default=False) is None


class TestDiscoverPathsEdgeCases:
    def test_nonexistent_path_yields_empty(self, tmp_path):
        # main() validates first, but discover_paths must be robust on its own.
        assert cli.discover_paths(str(tmp_path / "missing")) == []


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
        assert "# lintle clean run report" in report_md
        assert "tle2099.txt" in report_md
        assert "Records:" in report_md
        # broken-noradids.ndjson is always emitted on clean — empty when
        # nothing was quarantined, so downstream sees a stable artifact.
        assert (out / "broken-noradids.ndjson").read_bytes() == b""

    def test_main_routes_default_jobs_through_resolve_jobs(
        self, tmp_path, monkeypatch, line1, line2
    ):
        # With no --jobs, main() resolves the worker count via resolve_jobs,
        # passing explicit=None and the count of files to process (issue #53).
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        seen = {}
        real = cli.resolve_jobs

        def spy(explicit, cpu_count, n_files):
            seen["explicit"] = explicit
            seen["n_files"] = n_files
            return real(explicit, cpu_count, n_files)

        monkeypatch.setattr(cli, "resolve_jobs", spy)
        rc = cli.main(["clean", str(src), "--out-dir", str(out)])

        assert rc == 0
        assert seen == {"explicit": None, "n_files": 1}

    def test_main_clean_writes_norad_ids_for_quarantined_records(
        self, tmp_path, line1, line2
    ):
        # A wrong-checksum record is quarantined but its NORAD ID is
        # recoverable from line 1, so it lands in broken-noradids.ndjson.
        src = tmp_path / "src"
        src.mkdir()
        bad_line1 = line1[:68] + "9"
        (src / "tle2099.txt").write_bytes(
            (bad_line1 + "\n" + line2 + "\n").encode("ascii")
        )
        out = tmp_path / "out"

        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        assert rc == 1
        # NORAD 00005 (Vanguard 1) — the canonical fixture's catalog number.
        assert (out / "broken-noradids.ndjson").read_bytes() == b'{"noradId":5}\n'

    def test_main_validate_does_not_write_norad_ids_ndjson(
        self, tmp_path, line1, line2
    ):
        # validate is read-only — no NDJSON, no run report, no out-dir.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        cli.main(["validate", str(src), "--out-dir", str(out), "--jobs", "1"])

        assert not (out / "broken-noradids.ndjson").exists()

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

    def test_main_returns_two_when_a_file_fails_to_process(self, tmp_path, capsys):
        # An explicit path to a missing file is now caught upfront by the
        # input-validation step, before any worker is spawned — friendlier
        # than the old "worker raises on open" path, same exit code.
        missing = tmp_path / "tle_missing.txt"  # never created
        rc = cli.main(["validate", str(missing), "--jobs", "1"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "no such file or directory" in err
        assert "Traceback" not in err  # no stack trace leaks to the user

    def test_main_returns_two_when_a_worker_raises(
        self, tmp_path, line1, line2, monkeypatch
    ):
        # A worker exception (e.g. I/O error mid-stream) is an operational
        # failure → exit 2 (spec §2.7). Exit 1 is the quarantine quality gate.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

        _sentinel = object()

        class _RaisingFuture:
            def result(self):
                raise RuntimeError("worker boom")

        class _FakeExecutor:
            def __init__(self, *_args, **_kwargs):
                self._processes = {}
                self._f = _RaisingFuture()

            def submit(self, fn, *args, **kwargs):
                return self._f

            def shutdown(self, **_kwargs):
                pass

        fake_executor = None

        def _capture_executor(*args, **kwargs):
            nonlocal fake_executor
            fake_executor = _FakeExecutor(*args, **kwargs)
            return fake_executor

        monkeypatch.setattr(
            worker_pool.concurrent.futures, "ProcessPoolExecutor", _capture_executor
        )

        def _as_completed_one(futures):
            # Yield the one fake future so the collection loop can call result()
            yield list(futures.keys())[0]

        monkeypatch.setattr(
            worker_pool.concurrent.futures, "as_completed", _as_completed_one
        )

        original_sigint = signal.getsignal(signal.SIGINT)
        try:
            rc = cli.main(
                ["clean", str(src), "--out-dir", str(tmp_path / "out"), "--jobs", "1"]
            )
        finally:
            signal.signal(signal.SIGINT, original_sigint)

        assert rc == 2

    def test_main_friendly_error_when_default_source_missing(
        self, tmp_path, monkeypatch, capsys
    ):
        # Run with no paths from a directory that has no data/source — the
        # original bug: cli.py crashed with FileNotFoundError from
        # os.path.getsize. Now it should exit cleanly with a hint.
        monkeypatch.chdir(tmp_path)
        rc = cli.main(["clean", "--jobs", "1"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "data/source" in err
        assert "lintle --help" in err
        assert "Traceback" not in err

    def test_main_friendly_error_when_directory_has_no_tle_files(
        self, tmp_path, capsys
    ):
        empty = tmp_path / "empty"
        empty.mkdir()
        (empty / "notes.md").write_text("not a tle file")
        rc = cli.main(["validate", str(empty), "--jobs", "1"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "no tle*.txt files found" in err

    def test_main_rejects_zero_jobs(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        rc = cli.main(["validate", str(src), "--jobs", "0"])
        assert rc == 2
        assert "--jobs must be >= 1" in capsys.readouterr().err

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

    def test_main_warns_on_disk_borderline(
        self, tmp_path, line1, line2, monkeypatch, capsys
    ):
        # Free space between 2x and 2.5x input lands in the borderline band:
        # the run proceeds (exit 0 on a clean corpus) but lintle prints a
        # warning to stderr so the user knows they are close to the guard.
        src = tmp_path / "src"
        src.mkdir()
        input_file = src / "tle2099.txt"
        input_file.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        input_size = input_file.stat().st_size

        class _Usage:
            # 2.25x input — between the 2x abort floor and the 2.5x warn
            # ceiling. Sits squarely in the borderline band.
            free = int(input_size * 2.25)

        monkeypatch.setattr(cli.shutil, "disk_usage", lambda _path: _Usage())
        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "warning" in err.lower()
        assert "free space" in err.lower()
        assert "error" not in err.lower().split("\n")[0]  # not the abort path

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

    def test_main_json_report_prints_envelope(self, tmp_path, line1, line2, capsys):
        # Issue #20: --report json is now a top-level envelope object,
        # not a flat array. The per-file entries live under ``files``;
        # the run, environment, and corpus summary live alongside.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

        rc = cli.main(["validate", str(src), "--jobs", "1", "--report", "json"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        # Top-level envelope shape (issue #20 spec §3).
        assert isinstance(data, dict)
        assert data["schema_version"] == "2"
        assert data["run"]["command"] == "validate"
        assert data["run"]["timestamp"].endswith("Z")
        assert isinstance(data["run"]["elapsed_seconds"], float)
        assert "tool_version" in data["environment"]
        assert "python_version" in data["environment"]
        assert data["summary"]["files_processed"] == 1
        assert data["summary"]["paired_records"] == 1
        assert data["summary"]["clean_count"] == 1
        # Per-file entries preserve summary_dict shape under ``files``.
        assert data["files"][0]["src_name"] == "tle2099.txt"
        assert data["files"][0]["clean_count"] == 1
        # Timing fields are real floats — gate R2 (never null).
        assert isinstance(data["files"][0]["elapsed_seconds"], float)
        assert isinstance(data["files"][0]["records_per_sec"], float)
        assert data["files"][0]["bytes"] > 0

    def test_main_validate_lists_quarantine_locations(
        self, tmp_path, line1, line2, capsys
    ):
        src = tmp_path / "src"
        src.mkdir()
        bad_line1 = line1[:68] + "9"  # wrong checksum — the record is quarantined
        (src / "tle2099.txt").write_bytes(
            (bad_line1 + "\n" + line2 + "\n").encode("ascii")
        )

        rc = cli.main(["validate", str(src), "--jobs", "1"])

        assert rc == 1
        # validate mode lists each quarantined record's location and rule ID.
        assert "TLE-CHK-001" in capsys.readouterr().out

    def test_main_validate_renders_grouped_exemplars(
        self, tmp_path, line1, line2, capsys
    ):
        # Two distinct defect rules in one file: a checksum mismatch
        # (TLE-CHK-001) and a stray line that isn't a TLE (TLE-PAIR-002).
        src = tmp_path / "src"
        src.mkdir()
        bad_line1 = line1[:68] + "9"  # wrong checksum
        (src / "tle2099.txt").write_bytes(
            (bad_line1 + "\n" + line2 + "\n" + "garbage\n").encode("ascii")
        )

        rc = cli.main(["validate", str(src), "--jobs", "1"])

        out = capsys.readouterr().out
        assert rc == 1
        # The grouped rule heading (2-space indent, count parenthesized).
        assert "  TLE-CHK-001 (" in out
        # The 4-space-indented exemplar line under it.
        assert "    line " in out
        # The other rule is grouped under its own heading.
        assert "  TLE-PAIR-002 (" in out

    def test_main_returns_130_on_keyboard_interrupt(
        self, tmp_path, line1, line2, monkeypatch
    ):
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

        def _interrupt(_futures):
            raise KeyboardInterrupt

        monkeypatch.setattr(worker_pool.concurrent.futures, "as_completed", _interrupt)
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

    def test_main_interrupt_writes_no_report_or_ndjson(
        self, tmp_path, line1, line2, monkeypatch
    ):
        # An interrupted `clean` run must not publish corpus-wide artifacts:
        # `report.md` and `broken-noradids.ndjson` are only written after the
        # results loop completes, so the early `return 130` path must leave
        # the out-dir free of those files (issue #25). Worker-side `.partial`
        # cleanup is covered by `test_failed_run_does_not_leak_temp_file` in
        # test_pipeline.py — here we fake the executor outright so no worker
        # ever runs, making the parent-side assertion fully deterministic.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        class _NoopFuture:
            def result(self):  # pragma: no cover — must not be awaited
                raise AssertionError("fake future must not be awaited")

        class _FakeExecutor:
            def __init__(self, *_args, **_kwargs):
                self._processes = {}  # _terminate_workers iterates this

            def submit(self, *_args, **_kwargs):
                return _NoopFuture()

            def shutdown(self, **_kwargs):
                pass

        monkeypatch.setattr(
            worker_pool.concurrent.futures, "ProcessPoolExecutor", _FakeExecutor
        )

        def _interrupt(_futures):
            raise KeyboardInterrupt

        monkeypatch.setattr(worker_pool.concurrent.futures, "as_completed", _interrupt)

        original_sigint = signal.getsignal(signal.SIGINT)
        try:
            rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        finally:
            signal.signal(signal.SIGINT, original_sigint)

        assert rc == 130
        assert not (out / "report.md").exists()
        assert not (out / "broken-noradids.ndjson").exists()
        # No worker ran, so cleaned/ and broken/ must either be absent or empty
        # — in particular, no published `.cleaned.txt` and no stray `.partial`.
        assert not list(out.rglob("*.cleaned.txt"))
        assert not list(out.rglob("*.broken.txt"))
        assert not list(out.rglob("*.partial"))


class TestMaxQuarantinedThreshold:
    """Issue #13: ``--max-quarantined N`` allows CI to tolerate up to N
    quarantined records before the exit code flips to non-zero. Default
    ``N=0`` preserves the legacy "any quarantine fails" behaviour. Also
    covers the trailing-``%`` rate form: ``--max-quarantined 1%`` fails the
    run when more than 1% of routed records were quarantined.
    """

    def _write_one_bad_record(self, tmp_path, line1, line2):
        src = tmp_path / "src"
        src.mkdir()
        bad_line1 = line1[:68] + "9"
        (src / "tle2099.txt").write_bytes(
            (bad_line1 + "\n" + line2 + "\n").encode("ascii")
        )
        return src

    def _write_n_good_and_one_bad(self, tmp_path, line1, line2, n_good):
        # n_good copies of a valid 2-line record + one wrong-checksum pair.
        # The bad pair is quarantined under TLE-CHK-001; the n_good pairs
        # route to clean. Total routed = n_good + 1; quarantined = 1; rate
        # = 1 / (n_good + 1).
        src = tmp_path / "src"
        src.mkdir()
        bad_line1 = line1[:68] + "9"
        body = (line1 + "\n" + line2 + "\n") * n_good
        body += bad_line1 + "\n" + line2 + "\n"
        (src / "tle2099.txt").write_bytes(body.encode("ascii"))
        return src

    def test_max_quarantined_one_allows_single_quarantined_record(
        self, tmp_path, line1, line2
    ):
        src = self._write_one_bad_record(tmp_path, line1, line2)
        out = tmp_path / "out"

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out),
                "--jobs",
                "1",
                "--max-quarantined",
                "1",
            ]
        )

        assert rc == 0

    def test_max_quarantined_uses_strictly_greater_than_semantics(
        self, tmp_path, line1, line2
    ):
        # Two quarantined records, --max-quarantined 1 — count is > 1 so fail.
        src = tmp_path / "src"
        src.mkdir()
        bad_line1 = line1[:68] + "9"
        (src / "tle2099.txt").write_bytes(
            (bad_line1 + "\n" + line2 + "\n" + bad_line1 + "\n" + line2 + "\n").encode(
                "ascii"
            )
        )
        out = tmp_path / "out"

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out),
                "--jobs",
                "1",
                "--max-quarantined",
                "1",
            ]
        )

        assert rc == 1

    def test_max_quarantined_default_is_zero_legacy_behavior(
        self, tmp_path, line1, line2
    ):
        # No --max-quarantined flag: a single quarantined record must still
        # flip the exit code to 1. The new flag's default is 0, matching the
        # historical "any quarantine fails" contract.
        src = self._write_one_bad_record(tmp_path, line1, line2)
        out = tmp_path / "out"

        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        assert rc == 1

    def test_max_quarantined_applies_to_validate_too(self, tmp_path, line1, line2):
        src = self._write_one_bad_record(tmp_path, line1, line2)

        rc = cli.main(["validate", str(src), "--jobs", "1", "--max-quarantined", "1"])

        assert rc == 0

    def test_max_quarantined_rejects_negative_value(
        self, tmp_path, line1, line2, capsys
    ):
        src = self._write_one_bad_record(tmp_path, line1, line2)

        rc = cli.main(
            [
                "validate",
                str(src),
                "--jobs",
                "1",
                "--max-quarantined",
                "-1",
            ]
        )

        assert rc == 2
        assert "--max-quarantined must be >= 0" in capsys.readouterr().err

    def test_pct_under_threshold_passes(self, tmp_path, line1, line2):
        # 1 bad of 100 routed records = 1.0%. `--max-quarantined 5%` is
        # well above that, so the run exits 0.
        src = self._write_n_good_and_one_bad(tmp_path, line1, line2, n_good=99)
        out = tmp_path / "out"

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out),
                "--jobs",
                "1",
                "--max-quarantined",
                "5%",
            ]
        )

        assert rc == 0

    def test_pct_over_threshold_fails(self, tmp_path, line1, line2):
        # 1 bad of 100 routed = 1.0%. `--max-quarantined 0.5%` is below
        # that, so the run exits 1.
        src = self._write_n_good_and_one_bad(tmp_path, line1, line2, n_good=99)
        out = tmp_path / "out"

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out),
                "--jobs",
                "1",
                "--max-quarantined",
                "0.5%",
            ]
        )

        assert rc == 1

    def test_pct_at_exact_boundary_passes(self, tmp_path, line1, line2):
        # 1 bad of 100 routed = exactly 1.0%. Strictly-greater semantics
        # (matching count mode) mean exactly-at-boundary passes.
        src = self._write_n_good_and_one_bad(tmp_path, line1, line2, n_good=99)
        out = tmp_path / "out"

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out),
                "--jobs",
                "1",
                "--max-quarantined",
                "1%",
            ]
        )

        assert rc == 0

    def test_pct_hundred_percent_never_fails(self, tmp_path, line1, line2):
        # 100% is the upper bound. The cross-multiplied comparison
        # `100*q > 100*r` reduces to `q > r`, which is structurally
        # impossible (quarantined <= routed). Even an all-bad input
        # passes a 100% gate.
        src = self._write_one_bad_record(tmp_path, line1, line2)
        out = tmp_path / "out"

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out),
                "--jobs",
                "1",
                "--max-quarantined",
                "100%",
            ]
        )

        assert rc == 0

    def test_pct_applies_to_validate(self, tmp_path, line1, line2):
        src = self._write_n_good_and_one_bad(tmp_path, line1, line2, n_good=99)

        rc = cli.main(["validate", str(src), "--jobs", "1", "--max-quarantined", "5%"])

        assert rc == 0

    def test_pct_malformed_returns_2(self, tmp_path, line1, line2, capsys):
        src = self._write_one_bad_record(tmp_path, line1, line2)

        rc = cli.main(
            ["validate", str(src), "--jobs", "1", "--max-quarantined", "1.2.3%"]
        )

        assert rc == 2
        assert "invalid percentage" in capsys.readouterr().err

    def test_pct_out_of_range_returns_2(self, tmp_path, line1, line2, capsys):
        src = self._write_one_bad_record(tmp_path, line1, line2)

        rc = cli.main(
            ["validate", str(src), "--jobs", "1", "--max-quarantined", "150%"]
        )

        assert rc == 2
        assert "percentage must be in 0..100" in capsys.readouterr().err


class TestReportJsonl:
    """Issue #9 spec §8.5: ``clean`` mode emits ``<out_dir>/report.jsonl``
    after every successful run; validate mode does not.
    """

    def test_clean_emits_report_jsonl(self, tmp_path, line1, line2, capsys):
        # A wrong-checksum record produces a quarantine + a JSONL line.
        src = tmp_path / "src"
        src.mkdir()
        bad_line2 = line2[:-1] + ("9" if line2[-1] != "9" else "0")
        (src / "tle2099.txt").write_bytes(
            (line1 + "\n" + bad_line2 + "\n").encode("ascii")
        )
        out = tmp_path / "out"

        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        jsonl_path = out / "report.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["schema_version"] == "1"
        assert parsed["outcome"] == "quarantined"
        assert parsed["file"] == "tle2099.txt"
        assert parsed["rule_id"] == "TLE-CHK-001"
        # .shards/ is cleaned up by concat.
        assert not (out / ".shards").exists()

    def test_clean_jsonl_empty_when_zero_quarantines(self, tmp_path, line1, line2):
        # An all-clean run still produces report.jsonl, just empty —
        # matches broken-noradids.ndjson's contract that the artifact
        # is always present after a successful clean.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        jsonl_path = out / "report.jsonl"
        assert jsonl_path.exists()
        assert jsonl_path.read_text(encoding="utf-8") == ""

    def test_clean_summary_announces_findings_path(
        self, tmp_path, line1, line2, capsys
    ):
        # The post-run summary block includes a "findings: <path>" line
        # so operators see the artifact location alongside report.md.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        stdout = capsys.readouterr().out
        assert "findings: " in stdout
        assert "report.jsonl" in stdout

    def test_validate_does_not_emit_jsonl(self, tmp_path, line1, line2):
        # Validate mode owns no --out-dir artifacts; no report.jsonl.
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"

        cli.main(["validate", str(src), "--out-dir", str(out), "--jobs", "1"])

        assert not (out / "report.jsonl").exists()
        # Validate mode never even creates the out_dir.
        assert not out.exists() or not list(out.iterdir())


class TestPreRunShardScrub:
    """Issue #9 spec §4.6 / §8.10: the pre-run shard-dir scrub removes
    any leftover ``.shards/`` from a prior aborted run so this run's
    ``report.jsonl`` cannot inherit stale entries.
    """

    def test_pre_run_scrub_purges_stale_shards(self, tmp_path, line1, line2):
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        out.mkdir()
        # Preseed a finalized shard from a previous run for a file that
        # the current run does NOT process.
        stale_shard_dir = out / ".shards"
        stale_shard_dir.mkdir()
        stale = stale_shard_dir / "tle1999.findings.jsonl"
        stale.write_text('{"bogus": "from-prior-run"}\n', encoding="utf-8")

        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        # The scrub removed .shards/ before workers wrote anything;
        # the concat then runs and either removes the dir again or
        # leaves no trace either way.
        assert not stale.exists()

    def test_pre_run_scrub_purges_partials(self, tmp_path, line1, line2):
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
        out = tmp_path / "out"
        out.mkdir()
        stale_shard_dir = out / ".shards"
        stale_shard_dir.mkdir()
        stale = stale_shard_dir / "tle1999.findings.jsonl.partial"
        stale.write_text("incomplete-write\n", encoding="utf-8")

        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        assert not stale.exists()


class TestFormatElapsed:
    def test_format_elapsed_renders_minutes_and_hours(self):
        assert cli_progress._format_elapsed(0) == "0:00"
        assert cli_progress._format_elapsed(9) == "0:09"
        assert cli_progress._format_elapsed(75) == "1:15"
        assert cli_progress._format_elapsed(3661) == "1:01:01"


class TestShutdownHelpers:
    def test_ignore_sigint_sets_handler_to_ignore(self):
        original = signal.getsignal(signal.SIGINT)
        try:
            process_control.ignore_sigint()
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
        process_control.terminate_workers(_FakeExecutor(procs))
        assert all(proc.terminated for proc in procs.values())

    def test_terminate_workers_falls_back_to_shutdown_when_processes_missing(self):
        # If a future CPython removes or renames the private `_processes`
        # attribute, we must still stop the pool — fall back to the public
        # shutdown(cancel_futures=True) API instead of silently no-op'ing.
        class _NoPrivateExecutor:
            def __init__(self):
                self.shutdown_kwargs = None

            @property
            def _processes(self):
                raise AttributeError("simulated CPython API change")

            def shutdown(self, **kwargs):
                self.shutdown_kwargs = kwargs

        executor = _NoPrivateExecutor()
        process_control.terminate_workers(executor)
        assert executor.shutdown_kwargs == {"cancel_futures": True}

    def test_terminate_workers_warns_to_stderr_when_processes_missing(self, capsys):
        # The fallback path is observable — print a one-line note so the
        # operator knows shutdown took the slow path (waits for in-flight
        # tasks to cancel) rather than the immediate-terminate path.
        class _NoPrivateExecutor:
            @property
            def _processes(self):
                raise AttributeError

            def shutdown(self, **kwargs):
                pass

        process_control.terminate_workers(_NoPrivateExecutor())
        err = capsys.readouterr().err
        assert "_processes" in err


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
        import contextlib

        monkeypatch.setattr(
            "lintle.term.stderr_console",
            Console(file=io.StringIO(), force_terminal=False),
        )
        assert isinstance(cli_progress.status("working…"), contextlib.nullcontext)


class TestExplainCommand:
    """`lintle explain <TAG>` is a read-only documentation lookup."""

    def test_parser_accepts_explain_with_a_tag(self):
        args = cli.build_parser().parse_args(["explain", "TLE-CHK-001"])
        assert args.command == "explain"
        assert args.tag == "TLE-CHK-001"

    def test_explain_rule_prints_documentation(self, capsys):
        rc = cli.main(["explain", "TLE-CHK-001"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "TLE-CHK-001" in out
        assert "checksum" in out.lower()

    def test_explain_fix_prints_documentation(self, capsys):
        rc = cli.main(["explain", "reconstructed-checksum"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "reconstructed-checksum" in out

    def test_explain_unknown_tag_errors_with_guidance(self, capsys):
        rc = cli.main(["explain", "NOT-A-REAL-TAG"])
        err = capsys.readouterr().err
        assert rc == 2
        assert "NOT-A-REAL-TAG" in err
        assert "TLE-CHK-001" in err  # the error lists valid tags


def _strip_generated(path):
    """Read a report.md, dropping the nondeterministic `- Generated:` line so
    two runs can be compared for structural equality."""
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.startswith("- Generated:")
    )


def _simulate_interrupted_clean(
    src_paths, out_dir, *, completed_count, run_identity=None
):
    """Leave `out_dir` looking exactly like a clean run interrupted partway:
    the first `completed_count` files fully processed (their cleaned/broken
    outputs and findings shards committed, as a worker leaves them — no
    end-of-run concat), plus a checkpoint that lists them complete with every
    input fingerprinted. Mirrors the real interrupted state without needing to
    actually kill a parallel run. ``run_identity`` defaults to the schema-v2
    shape used by ``main()`` (``{"max_quarantined": "0"}``)."""
    if run_identity is None:
        run_identity = {"max_quarantined": "0"}
    os.makedirs(out_dir, exist_ok=True)
    inputs = {p: resume.input_fingerprint(p) for p in src_paths}
    completed = {}
    for path in src_paths[:completed_count]:
        stats = pipeline.process_file(path, out_dir, "clean")
        cleaned_name = os.path.splitext(os.path.basename(path))[0] + ".cleaned.txt"
        sizes = {}
        cleaned_path = os.path.join(out_dir, "cleaned", cleaned_name)
        if os.path.exists(cleaned_path):
            sizes[cleaned_name] = os.path.getsize(cleaned_path)
        broken_name = os.path.splitext(os.path.basename(path))[0] + ".broken.txt"
        broken_path = os.path.join(out_dir, "broken", broken_name)
        if os.path.exists(broken_path):
            sizes[broken_name] = os.path.getsize(broken_path)
        completed[path] = {"summary": report.summary_dict(stats), "outputs": sizes}
    resume.write_checkpoint(
        out_dir,
        resume.build_checkpoint(
            inputs=inputs, completed=completed, run_identity=run_identity
        ),
    )


class TestResume:
    """Single-run resume for `clean --resume` (issue #56): checkpoint lifecycle,
    refuse-on-change validation, and a golden 'resume finishes the job' run."""

    def _two_file_src(self, tmp_path, line1, line2):
        # Each file: one clean pair + one wrong-checksum pair (quarantined),
        # so cleaned/, broken/, report.jsonl, and broken-noradids all exercise.
        src = tmp_path / "src"
        src.mkdir()
        bad1 = line1[:68] + "9"
        content = (line1 + "\n" + line2 + "\n" + bad1 + "\n" + line2 + "\n").encode(
            "ascii"
        )
        (src / "tle2098.txt").write_bytes(content)
        (src / "tle2099.txt").write_bytes(content)
        return src

    def test_completed_run_leaves_no_checkpoint(self, tmp_path, line1, line2):
        src = self._two_file_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert not (out / resume.CHECKPOINT_NAME).exists()

    def test_resume_without_checkpoint_errors(self, tmp_path, line1, line2, capsys):
        # --resume (explicit force-resume) with no checkpoint: ABSENT + resume
        # → ABORT with exit_code=2 (operational error, not the quarantine gate).
        src = self._two_file_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        out.mkdir()
        rc = cli.main(
            ["clean", str(src), "--out-dir", str(out), "--resume", "--jobs", "1"]
        )
        err = capsys.readouterr().err
        assert rc == 2
        assert "no interrupted run" in err.lower()

    def test_resume_finishes_and_matches_full_run(self, tmp_path, line1, line2):
        src = self._two_file_src(tmp_path, line1, line2)
        paths = cli.discover_paths(str(src))
        out_full = tmp_path / "full"
        rc_full = cli.main(
            ["clean", str(src), "--out-dir", str(out_full), "--jobs", "1"]
        )

        out_partial = tmp_path / "partial"
        _simulate_interrupted_clean(paths, str(out_partial), completed_count=1)
        first_cleaned = out_partial / "cleaned" / "tle2098.cleaned.txt"
        mtime_before = first_cleaned.stat().st_mtime_ns

        rc_resume = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out_partial),
                "--resume",
                "--jobs",
                "1",
            ]
        )

        assert rc_resume == rc_full
        # report.jsonl and broken-noradids are timing-free — the golden anchors.
        assert (out_partial / "report.jsonl").read_bytes() == (
            out_full / "report.jsonl"
        ).read_bytes()
        assert (out_partial / "broken-noradids.ndjson").read_bytes() == (
            out_full / "broken-noradids.ndjson"
        ).read_bytes()
        for name in ("tle2098.cleaned.txt", "tle2099.cleaned.txt"):
            assert (out_partial / "cleaned" / name).read_bytes() == (
                out_full / "cleaned" / name
            ).read_bytes()
        assert _strip_generated(out_partial / "report.md") == _strip_generated(
            out_full / "report.md"
        )
        # The already-completed file was skipped, not reprocessed.
        assert first_cleaned.stat().st_mtime_ns == mtime_before
        # A fully successful resumed run tears down BOTH the checkpoint and the
        # findings shards (they are removed together only on success, #56).
        assert not (out_partial / resume.CHECKPOINT_NAME).exists()
        assert not (out_partial / ".shards").exists()

    def test_fresh_run_clears_stale_checkpoint(self, tmp_path, line1, line2):
        # --no-resume discards an incompatible checkpoint and starts fresh
        # (archives it, scrubs output trees, reprocesses everything).
        src = self._two_file_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        out.mkdir()
        resume.write_checkpoint(
            str(out),
            resume.build_checkpoint(
                inputs={
                    "old.txt": {
                        "size": 1,
                        "mtime_ns": 1,
                        "head_sha256": "x",
                        "tail_sha256": "y",
                    }
                },
                completed={},
                run_identity={},
            ),
        )
        rc = cli.main(
            ["clean", str(src), "--out-dir", str(out), "--no-resume", "--jobs", "1"]
        )
        assert rc == 1  # quarantines present
        assert not (out / resume.CHECKPOINT_NAME).exists()
        assert (out / "cleaned" / "tle2098.cleaned.txt").exists()
        assert (out / "cleaned" / "tle2099.cleaned.txt").exists()

    def test_resume_refuses_when_input_changed(self, tmp_path, line1, line2, capsys):
        # --resume (explicit force-resume) with a stale checkpoint (input changed)
        # → STALE + resume flag → ABORT with exit_code=2.
        src = self._two_file_src(tmp_path, line1, line2)
        paths = cli.discover_paths(str(src))
        out_partial = tmp_path / "partial"
        _simulate_interrupted_clean(paths, str(out_partial), completed_count=1)
        # The not-yet-processed input changes between "office" and "home".
        with open(src / "tle2099.txt", "ab") as handle:
            handle.write(b"1 extra junk line\n")

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out_partial),
                "--resume",
                "--jobs",
                "1",
            ]
        )
        err = capsys.readouterr().err
        assert rc == 2
        assert "input changed" in err.lower()
        assert "tle2099" in err
        # A refused resume leaves the checkpoint intact for an explicit restart.
        assert (out_partial / resume.CHECKPOINT_NAME).exists()

    def test_interrupt_preserves_checkpoint_and_shards(
        self, tmp_path, line1, line2, monkeypatch
    ):
        # An interrupted run must stay resumable: BOTH the checkpoint and the
        # findings shards survive (the shards so a later --resume rebuilds a
        # complete report.jsonl). Simulate the interrupt by raising
        # KeyboardInterrupt from the parent's checkpoint write after a file
        # commits — the same path a real Ctrl-C takes through the collect loop.
        src = self._two_file_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        real_write = resume.write_checkpoint

        def interrupt_after_first(out_dir, checkpoint):
            real_write(out_dir, checkpoint)  # persist the first file's progress
            raise KeyboardInterrupt

        monkeypatch.setattr(cli.resume, "write_checkpoint", interrupt_after_first)
        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

        assert rc == 130
        # The checkpoint written before the interrupt survives → resumable.
        assert resume.load_checkpoint(str(out)) is not None
        # The end-of-run shard scrub never ran (we returned 130 first), so the
        # completed file's shard is still on disk for --resume to reuse.
        assert (out / ".shards").exists()

    def test_resume_with_corrupt_checkpoint_reports_corruption(
        self, tmp_path, line1, line2, capsys
    ):
        # A present-but-corrupt checkpoint must not be reported as "not found".
        # CORRUPT + any flag (or no flag) → ABORT with exit_code=2 unless --no-resume.
        src = self._two_file_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        out.mkdir()
        (out / resume.CHECKPOINT_NAME).write_text("{ not valid json", encoding="utf-8")
        rc = cli.main(
            ["clean", str(src), "--out-dir", str(out), "--resume", "--jobs", "1"]
        )
        err = capsys.readouterr().err
        assert rc == 2
        assert "corrupt" in err.lower() or "unreadable" in err.lower()
        assert "no interrupted run" not in err.lower()


class TestParseQuarantineThreshold:
    """The ``--max-quarantined`` value parser. A bare integer is an absolute
    count; a trailing ``%`` switches to a percentage of routed records. The
    two modes are mutually exclusive by construction (a single value is one
    or the other, never both).
    """

    def test_bare_integer_is_count_mode(self):
        assert thresholds.parse_quarantine_threshold("100") == ("count", 100)

    def test_zero_is_count_zero(self):
        assert thresholds.parse_quarantine_threshold("0") == ("count", 0)

    def test_trailing_percent_is_pct_mode(self):
        assert thresholds.parse_quarantine_threshold("1%") == ("pct", 1.0)

    def test_zero_percent_is_valid(self):
        assert thresholds.parse_quarantine_threshold("0%") == ("pct", 0.0)

    def test_hundred_percent_is_valid(self):
        assert thresholds.parse_quarantine_threshold("100%") == ("pct", 100.0)

    def test_fractional_percent(self):
        assert thresholds.parse_quarantine_threshold("1.5%") == ("pct", 1.5)

    def test_surrounding_whitespace_tolerated(self):
        assert thresholds.parse_quarantine_threshold("  100  ") == ("count", 100)
        assert thresholds.parse_quarantine_threshold("  1%  ") == ("pct", 1.0)

    def test_negative_count_rejected_with_legacy_message(self):
        # Preserves the issue-#13 substring required by the existing
        # negative-value integration test in TestMaxQuarantinedThreshold.
        with pytest.raises(ValueError, match=r"--max-quarantined must be >= 0"):
            thresholds.parse_quarantine_threshold("-1")

    def test_non_integer_count_rejected(self):
        # Counts are whole records; "1.5" with no `%` is not a count.
        with pytest.raises(ValueError, match="invalid value"):
            thresholds.parse_quarantine_threshold("1.5")

    def test_non_numeric_rejected(self):
        with pytest.raises(ValueError, match="invalid value"):
            thresholds.parse_quarantine_threshold("abc")

    def test_bare_percent_rejected(self):
        with pytest.raises(ValueError, match="invalid percentage"):
            thresholds.parse_quarantine_threshold("%")

    def test_pct_over_one_hundred_rejected(self):
        with pytest.raises(ValueError, match=r"percentage must be in 0\.\.100"):
            thresholds.parse_quarantine_threshold("150%")

    def test_pct_negative_rejected(self):
        with pytest.raises(ValueError, match=r"percentage must be in 0\.\.100"):
            thresholds.parse_quarantine_threshold("-1%")

    def test_pct_malformed_rejected(self):
        with pytest.raises(ValueError, match="invalid percentage"):
            thresholds.parse_quarantine_threshold("1.2.3%")

    def test_inner_whitespace_around_percent_tolerated(self):
        # A space between the number and the `%` is accepted: the helper
        # strips the inner whitespace before parsing the float, so the
        # value still resolves to the same percentage.
        assert thresholds.parse_quarantine_threshold("1 %") == ("pct", 1.0)
        assert thresholds.parse_quarantine_threshold("  1.5 %  ") == ("pct", 1.5)


class TestIsInteractive:
    def test_requires_stdin_tty_and_no_ci(self, monkeypatch):
        monkeypatch.setattr(term.sys, "stdin", io.StringIO())  # not a tty
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("NONINTERACTIVE", raising=False)
        assert term.is_interactive() is False

    def test_ci_env_forces_non_interactive(self, monkeypatch):
        class _TTY(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr(term.sys, "stdin", _TTY())
        monkeypatch.setenv("CI", "true")
        assert term.is_interactive() is False

    def test_interactive_when_stdin_tty_and_no_ci(self, monkeypatch):
        class _TTY(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr(term.sys, "stdin", _TTY())
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("NONINTERACTIVE", raising=False)
        assert term.is_interactive() is True


class TestPromptYesNo:
    def test_enter_takes_default(self, monkeypatch):
        monkeypatch.setattr(term.sys, "stdin", io.StringIO("\n"))
        assert term.prompt_yes_no("go? ", default=True) is True

    def test_explicit_no(self, monkeypatch):
        monkeypatch.setattr(term.sys, "stdin", io.StringIO("n\n"))
        assert term.prompt_yes_no("go? ", default=True) is False

    def test_eof_returns_none(self, monkeypatch):
        monkeypatch.setattr(term.sys, "stdin", io.StringIO(""))
        assert term.prompt_yes_no("go? ", default=True) is None

    def test_garbage_then_abort(self, monkeypatch):
        monkeypatch.setattr(term.sys, "stdin", io.StringIO("maybe\nhuh\nwhat\n"))
        assert term.prompt_yes_no("go? ", default=True) is None


class TestScrubOutputs:
    def test_removes_output_trees(self, tmp_path):
        out = tmp_path
        for sub in ("cleaned", "broken", ".shards"):
            d = out / sub
            d.mkdir()
            (d / "stale.txt").write_text("old")
        run_planning.scrub_outputs(str(out))
        for sub in ("cleaned", "broken", ".shards"):
            assert not (out / sub).exists()

    def test_noop_on_empty_dir(self, tmp_path):
        run_planning.scrub_outputs(str(tmp_path))  # must not raise


class TestSignalHandling:
    def test_cancel_message_some_done_skips_completed_not_continues(self):
        # With some files completed, the re-run skips them and reprocesses the
        # rest; the file interrupted mid-stream restarts. The message must not
        # promise to "continue where it stopped" — resume has no intra-file
        # granularity, and that wording read as a broken resume.
        msg = process_control.format_cancel_message(done=12, total=29)
        assert "12/29" in msg
        assert "--no-resume" in msg
        assert "same --out-dir" in msg
        assert "continue where it stopped" not in msg
        assert "restart" in msg.lower()

    def test_cancel_message_zero_done_says_it_restarts(self):
        # No file finished -> no checkpoint is written -> the re-run starts over
        # from the beginning. The message must say so rather than imply
        # resumable progress (the single-file Ctrl-C field report). It also must
        # not dangle --no-resume, since there is no checkpoint to ignore.
        msg = process_control.format_cancel_message(done=0, total=1)
        assert "0/1" in msg
        assert "continue where it stopped" not in msg
        assert "starts over" in msg.lower()

    def test_signal_exit_code(self):
        assert process_control.signal_exit_code(signal.SIGINT) == 130
        assert process_control.signal_exit_code(signal.SIGTERM) == 143

    def test_sigterm_sighup_traps_installed_and_raise(
        self, tmp_path, line1, line2, monkeypatch
    ):
        # A clean run must trap SIGTERM and SIGHUP (not just SIGINT) so a
        # scheduler/preemption kill stops gracefully and exits 128+signo. We
        # don't deliver a real signal (flaky); we capture what main() registers
        # and confirm the installed trap raises KeyboardInterrupt — which the
        # executor's except-path converts to the signal exit code (§3.2).
        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode())
        out = tmp_path / "out"

        registered = {}
        real_signal = signal.signal

        def recording_signal(signum, handler):
            registered.setdefault(signum, []).append(handler)
            return real_signal(signum, handler)

        monkeypatch.setattr(signal, "signal", recording_signal)
        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 0

        assert signal.SIGTERM in registered, "SIGTERM was never trapped"
        assert signal.SIGHUP in registered, "SIGHUP was never trapped"
        # The first handler installed for SIGTERM during the run is the trap;
        # invoking it must raise KeyboardInterrupt (the graceful-stop trigger).
        trap = registered[signal.SIGTERM][0]
        with pytest.raises(KeyboardInterrupt):
            trap(signal.SIGTERM, None)


class TestLockWiring:
    def test_refuses_when_locked(self, tmp_path, line1, line2):
        from lintle import fsutil

        src = tmp_path / "src"
        src.mkdir()
        (src / "tle2000.txt").write_bytes((line1 + "\n" + line2 + "\n").encode())
        out = tmp_path / "out"
        out.mkdir()
        with fsutil.out_dir_lock(str(out)):  # simulate a concurrent run
            rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 2  # operational refusal — lock held


class TestResumeWiring:
    """Task 11: --no-resume flag + decision-core wiring in main() (spec §2)."""

    def _make_src(self, tmp_path, line1, line2, n=2):
        src = tmp_path / "src"
        src.mkdir()
        for i in range(n):
            (src / f"tle20{i:02d}.txt").write_bytes(
                (line1 + "\n" + line2 + "\n").encode()
            )
        return src

    def test_no_resume_and_resume_are_mutually_exclusive(
        self, tmp_path, line1, line2, capsys
    ):
        src = self._make_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        with pytest.raises(SystemExit) as exc:
            cli.main(
                [
                    "clean",
                    str(src),
                    "--out-dir",
                    str(out),
                    "--resume",
                    "--no-resume",
                    "--jobs",
                    "1",
                ]
            )
        assert exc.value.code == 2  # argparse usage error

    def test_default_run_with_no_checkpoint_is_fresh_and_succeeds(
        self, tmp_path, line1, line2
    ):
        src = self._make_src(tmp_path, line1, line2)
        out = tmp_path / "out"
        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 0
        # checkpoint deleted on success
        assert not (out / ".clean-state.json").exists()

    def test_auto_resume_when_checkpoint_valid(self, tmp_path, line1, line2):
        # DEFAULT behavior (no --resume flag, no --no-resume flag, non-interactive
        # environment — pytest has no TTY): a valid checkpoint is picked up
        # automatically and the completed files are skipped.
        src = self._make_src(tmp_path, line1, line2)
        paths = cli.discover_paths(str(src))
        out_full = tmp_path / "full"
        rc_full = cli.main(
            ["clean", str(src), "--out-dir", str(out_full), "--jobs", "1"]
        )

        out_partial = tmp_path / "partial"
        _simulate_interrupted_clean(paths, str(out_partial), completed_count=1)
        # Note the mtime of the already-completed file so we can confirm it
        # was NOT reprocessed.
        first_name = os.path.basename(paths[0])
        first_stem = os.path.splitext(first_name)[0]
        first_cleaned = out_partial / "cleaned" / f"{first_stem}.cleaned.txt"
        mtime_before = first_cleaned.stat().st_mtime_ns

        # No --resume flag — auto-resume is the default in non-interactive mode.
        rc_auto = cli.main(
            ["clean", str(src), "--out-dir", str(out_partial), "--jobs", "1"]
        )

        assert rc_auto == rc_full
        # The already-completed file was skipped, not reprocessed.
        assert first_cleaned.stat().st_mtime_ns == mtime_before
        # report.jsonl output matches the full run.
        assert (out_partial / "report.jsonl").read_bytes() == (
            out_full / "report.jsonl"
        ).read_bytes()
        # A successful auto-resume tears down the checkpoint and shards.
        assert not (out_partial / resume.CHECKPOINT_NAME).exists()
        assert not (out_partial / ".shards").exists()


class TestFreshRunOrphanScrub:
    """Gap 1 (spec §3.4): a true-fresh run wipes cleaned/ + broken/ so outputs
    from a prior run whose input is no longer in the input set do not linger."""

    def test_no_resume_scrubs_orphaned_outputs(self, tmp_path, line1, line2):
        # Step 1 — run on two inputs; both cleaned outputs are written.
        src = tmp_path / "src"
        src.mkdir()
        content = (line1 + "\n" + line2 + "\n").encode("ascii")
        (src / "tle2000.txt").write_bytes(content)
        (src / "tle2001.txt").write_bytes(content)
        out = tmp_path / "out"

        rc1 = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc1 == 0
        assert (out / "cleaned" / "tle2000.cleaned.txt").exists()
        assert (out / "cleaned" / "tle2001.cleaned.txt").exists()
        # A completed run deletes the checkpoint.
        assert not (out / resume.CHECKPOINT_NAME).exists()

        # Step 2 — remove tle2001.txt from the input set; its cleaned output is
        # now an orphan in the out-dir.
        (src / "tle2001.txt").unlink()
        assert (out / "cleaned" / "tle2001.cleaned.txt").exists()  # orphan present

        # Step 3 — fresh run (--no-resume) on the now-one-file dir.
        rc2 = cli.main(
            ["clean", str(src), "--out-dir", str(out), "--no-resume", "--jobs", "1"]
        )
        assert rc2 == 0

        # The output for the surviving input must exist.
        assert (out / "cleaned" / "tle2000.cleaned.txt").exists()

        # The orphan from the prior run must be gone — spec §3.4 guarantees
        # the fresh run scrubs the whole cleaned/ tree before processing.
        assert not (out / "cleaned" / "tle2001.cleaned.txt").exists()


class TestStaleCheckpointNonInteractive:
    """Gap 2 (spec §2.3): STALE checkpoint + no flag + non-interactive mode
    must exit 2 and print the change reason plus a --no-resume hint.

    ``test_resume_refuses_when_input_changed`` in TestResume covers the
    STALE + *explicit --resume flag* cell.  This class covers the
    STALE + *no flag* + non-interactive cell — a different branch in
    resolve_resume_action — so both are independently tested."""

    def _make_stale_setup(self, tmp_path, line1, line2):
        """Return (src, out_partial) with a valid checkpoint whose fingerprint
        will not match after we mutate the input."""
        src = tmp_path / "src"
        src.mkdir()
        content = (line1 + "\n" + line2 + "\n").encode("ascii")
        (src / "tle2000.txt").write_bytes(content)
        (src / "tle2001.txt").write_bytes(content)
        paths = cli.discover_paths(str(src))
        out_partial = tmp_path / "partial"
        _simulate_interrupted_clean(paths, str(out_partial), completed_count=1)
        return src, out_partial

    def test_stale_checkpoint_non_interactive_errors(
        self, tmp_path, line1, line2, capsys, monkeypatch
    ):
        # Ensure non-interactive mode regardless of the test runner environment.
        monkeypatch.setenv("CI", "true")

        src, out_partial = self._make_stale_setup(tmp_path, line1, line2)

        # Mutate tle2001.txt so its fingerprint no longer matches the checkpoint.
        with open(src / "tle2001.txt", "ab") as fh:
            fh.write(b"extra junk\n")

        # No --resume and no --no-resume: the default path.
        rc = cli.main(["clean", str(src), "--out-dir", str(out_partial), "--jobs", "1"])
        err = capsys.readouterr().err

        # spec §2.3: STALE + no flag + non-interactive → exit 2 + guidance.
        assert rc == 2
        # The error message must name the change reason.
        assert "input changed" in err.lower() or "cannot resume" in err.lower()
        # The guidance hint directs the operator to --no-resume.
        assert "--no-resume" in err
        # The checkpoint must survive (not silently discarded) so the operator
        # can inspect what changed before choosing to discard or investigate.
        assert (out_partial / resume.CHECKPOINT_NAME).exists()
