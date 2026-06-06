"""Tests for lintle.cli — argument parsing, path discovery, exit codes."""

import json
import os
import signal

import pytest

from lintle import (
    cli,
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

        with pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args(["--version"])
        assert exc.value.code == 0
        assert "lintle" in capsys.readouterr().out

    def test_parser_help_includes_examples_and_exit_codes(self, capsys):

        with pytest.raises(SystemExit) as exc:
            cli.build_parser().parse_args(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Examples:" in out
        assert "Exit codes:" in out

    def test_parser_rejects_multiple_positional_inputs(self):
        # Single-input contract: only one positional allowed.

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
