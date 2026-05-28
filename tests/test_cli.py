"""Tests for lintle.cli — argument parsing, path discovery, exit codes."""

import json
import os
import queue
import signal
import time

import pytest

from lintle import cli, pipeline, report, resume


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


class TestDetectBasenameCollisions:
    def test_no_collisions_returns_none(self, tmp_path):
        a = tmp_path / "tle2001.txt"
        b = tmp_path / "tle2002.txt"
        assert cli._detect_basename_collisions([str(a), str(b)]) is None

    def test_returns_error_with_each_colliding_path(self, tmp_path):
        # Two inputs with the same basename would write to the same
        # cleaned/broken sidecar — silently overwriting each other.
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        a = dir_a / "tle2022.txt"
        b = dir_b / "tle2022.txt"
        err = cli._detect_basename_collisions([str(a), str(b)])
        assert err is not None
        assert "tle2022.txt" in err
        assert str(a) in err and str(b) in err

    def test_lists_all_collision_groups(self, tmp_path):
        # Multiple distinct basename collisions all surface in one message.
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        files = [
            str(dir_a / "tle2001.txt"),
            str(dir_b / "tle2001.txt"),
            str(dir_a / "tle2002.txt"),
            str(dir_b / "tle2002.txt"),
        ]
        err = cli._detect_basename_collisions(files)
        assert err is not None
        assert "tle2001.txt" in err and "tle2002.txt" in err


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
        assert data["schema_version"] == "1"
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

    def test_main_validate_lists_reject_locations(self, tmp_path, line1, line2, capsys):
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
            cli.concurrent.futures, "ProcessPoolExecutor", _FakeExecutor
        )

        def _interrupt(_futures):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli.concurrent.futures, "as_completed", _interrupt)

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
        cli._terminate_workers(executor)
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

        cli._terminate_workers(_NoPrivateExecutor())
        err = capsys.readouterr().err
        assert "_processes" in err


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

    def test_render_includes_records_per_second(self, capsys, monkeypatch):
        # Long runs are easier to monitor with a throughput number. Stubbing
        # cli.time.monotonic pins elapsed to exactly 4 seconds so the rate
        # is exactly 2,500 rec/s, not 2,499 (real-clock drift between the
        # _start assignment and the _render call would otherwise floor it).
        display = cli._ProgressDisplay(total_files=1, progress_queue=queue.Queue())
        display._live = True
        display._records = 10_000
        display._start = 0.0
        monkeypatch.setattr(cli.time, "monotonic", lambda: 4.0)

        display._render()

        err = capsys.readouterr().err
        assert "2,500 rec/s" in err

    def test_render_rps_handles_zero_elapsed_without_dividing(
        self, capsys, monkeypatch
    ):
        # On the first frame elapsed is sub-second — never raise
        # ZeroDivisionError, just show 0 rec/s until a second has passed.
        display = cli._ProgressDisplay(total_files=1, progress_queue=queue.Queue())
        display._live = True
        display._records = 100
        display._start = 0.0
        monkeypatch.setattr(cli.time, "monotonic", lambda: 0.0)

        display._render()

        err = capsys.readouterr().err
        assert "0 rec/s" in err

    def test_render_shows_the_active_filename(self, capsys):
        display = cli._ProgressDisplay(total_files=2, progress_queue=queue.Queue())
        display._live = True
        display._active["tle2024.txt"] = 0.0

        display._render()

        assert "tle2024.txt" in capsys.readouterr().err

    def test_render_collapses_multiple_active_files(self, capsys):
        # With --jobs N, several files run in parallel. Show the
        # earliest-started one (the candidate slow file once peers finish)
        # plus a count of the others so the line stays readable.
        display = cli._ProgressDisplay(total_files=3, progress_queue=queue.Queue())
        display._live = True
        # Insertion order doubles as start-order: tle_first is the oldest.
        display._active["tle_first.txt"] = 0.0
        display._active["tle_second.txt"] = 1.0
        display._active["tle_third.txt"] = 2.0

        display._render()

        err = capsys.readouterr().err
        assert "tle_first.txt" in err
        assert "+2 more" in err
        # The other two names aren't spelled out — only the oldest is shown.
        assert "tle_second.txt" not in err
        assert "tle_third.txt" not in err

    def test_drain_handles_start_event(self):
        progress = queue.Queue()
        progress.put(("start", "tle2024.txt"))
        display = cli._ProgressDisplay(total_files=1, progress_queue=progress)

        display._drain()

        assert "tle2024.txt" in display._active

    def test_drain_handles_end_event(self):
        progress = queue.Queue()
        progress.put(("end", "tle2024.txt"))
        display = cli._ProgressDisplay(total_files=1, progress_queue=progress)
        display._active["tle2024.txt"] = 0.0

        display._drain()

        assert "tle2024.txt" not in display._active

    def test_drain_handles_mixed_int_and_event_messages(self):
        # The queue interleaves record deltas with lifecycle events; drain
        # must fold all of them in one pass without dropping any.
        progress = queue.Queue()
        progress.put(("start", "tle_a.txt"))
        progress.put(100)
        progress.put(("start", "tle_b.txt"))
        progress.put(50)
        progress.put(("end", "tle_a.txt"))
        display = cli._ProgressDisplay(total_files=2, progress_queue=progress)

        display._drain()

        assert display._records == 150
        assert "tle_a.txt" not in display._active
        assert "tle_b.txt" in display._active

    def test_drain_preserves_active_insertion_order(self):
        # Showing the earliest-still-active file relies on dict insertion
        # order — verify it survives a drain.
        progress = queue.Queue()
        progress.put(("start", "tle_first.txt"))
        progress.put(("start", "tle_second.txt"))
        display = cli._ProgressDisplay(total_files=2, progress_queue=progress)

        display._drain()

        assert list(display._active) == ["tle_first.txt", "tle_second.txt"]

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


def _simulate_interrupted_clean(src_paths, out_dir, *, completed_count):
    """Leave `out_dir` looking exactly like a clean run interrupted partway:
    the first `completed_count` files fully processed (their cleaned/broken
    outputs and findings shards committed, as a worker leaves them — no
    end-of-run concat), plus a checkpoint that lists them complete with every
    input fingerprinted. Mirrors the real interrupted state without needing to
    actually kill a parallel run."""
    os.makedirs(out_dir, exist_ok=True)
    inputs = {p: resume.input_fingerprint(p) for p in src_paths}
    completed = {}
    for path in src_paths[:completed_count]:
        stats = pipeline.process_file(path, out_dir, "clean")
        completed[path] = report.summary_dict(stats)
    resume.write_checkpoint(
        out_dir, resume.build_checkpoint(inputs=inputs, completed=completed)
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
            ),
        )
        rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])
        assert rc == 1  # quarantines present
        assert not (out / resume.CHECKPOINT_NAME).exists()
        assert (out / "cleaned" / "tle2098.cleaned.txt").exists()
        assert (out / "cleaned" / "tle2099.cleaned.txt").exists()

    def test_resume_refuses_when_input_changed(self, tmp_path, line1, line2, capsys):
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
        assert cli.parse_quarantine_threshold("100") == ("count", 100)

    def test_zero_is_count_zero(self):
        assert cli.parse_quarantine_threshold("0") == ("count", 0)

    def test_trailing_percent_is_pct_mode(self):
        assert cli.parse_quarantine_threshold("1%") == ("pct", 1.0)

    def test_zero_percent_is_valid(self):
        assert cli.parse_quarantine_threshold("0%") == ("pct", 0.0)

    def test_hundred_percent_is_valid(self):
        assert cli.parse_quarantine_threshold("100%") == ("pct", 100.0)

    def test_fractional_percent(self):
        assert cli.parse_quarantine_threshold("1.5%") == ("pct", 1.5)

    def test_surrounding_whitespace_tolerated(self):
        assert cli.parse_quarantine_threshold("  100  ") == ("count", 100)
        assert cli.parse_quarantine_threshold("  1%  ") == ("pct", 1.0)

    def test_negative_count_rejected_with_legacy_message(self):
        # Preserves the issue-#13 substring required by the existing
        # negative-value integration test in TestMaxQuarantinedThreshold.
        with pytest.raises(ValueError, match=r"--max-quarantined must be >= 0"):
            cli.parse_quarantine_threshold("-1")

    def test_non_integer_count_rejected(self):
        # Counts are whole records; "1.5" with no `%` is not a count.
        with pytest.raises(ValueError, match="invalid value"):
            cli.parse_quarantine_threshold("1.5")

    def test_non_numeric_rejected(self):
        with pytest.raises(ValueError, match="invalid value"):
            cli.parse_quarantine_threshold("abc")

    def test_bare_percent_rejected(self):
        with pytest.raises(ValueError, match="invalid percentage"):
            cli.parse_quarantine_threshold("%")

    def test_pct_over_one_hundred_rejected(self):
        with pytest.raises(ValueError, match=r"percentage must be in 0\.\.100"):
            cli.parse_quarantine_threshold("150%")

    def test_pct_negative_rejected(self):
        with pytest.raises(ValueError, match=r"percentage must be in 0\.\.100"):
            cli.parse_quarantine_threshold("-1%")

    def test_pct_malformed_rejected(self):
        with pytest.raises(ValueError, match="invalid percentage"):
            cli.parse_quarantine_threshold("1.2.3%")

    def test_inner_whitespace_around_percent_tolerated(self):
        # A space between the number and the `%` is accepted: the helper
        # strips the inner whitespace before parsing the float, so the
        # value still resolves to the same percentage.
        assert cli.parse_quarantine_threshold("1 %") == ("pct", 1.0)
        assert cli.parse_quarantine_threshold("  1.5 %  ") == ("pct", 1.5)
