"""Tests for lintle.cli — argument parsing, path discovery, exit codes."""

import json
import os
import queue
import signal
import time

from lintle import cli, report


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
        # paths defaults to None so main() can tell "user passed nothing"
        # apart from "user explicitly passed the default" for error wording.
        assert args.paths == []
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


class TestCheckPaths:
    def test_missing_default_yields_friendly_hint(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no data/source here
        err = cli.check_paths(["data/source"], using_default=True)
        assert err is not None
        assert "data/source" in err
        assert "lintle --help" in err

    def test_missing_explicit_path_yields_plain_message(self, tmp_path):
        err = cli.check_paths([str(tmp_path / "nope.txt")], using_default=False)
        assert err is not None
        assert "no such file or directory" in err
        assert "data/source" not in err  # not the default-hint variant

    def test_multiple_missing_paths_are_listed(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        err = cli.check_paths([str(a), str(b)], using_default=False)
        assert err is not None
        assert str(a) in err and str(b) in err

    def test_existing_paths_return_none(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("x")
        assert cli.check_paths([str(f), str(tmp_path)], using_default=False) is None

    def test_os_access_false_negative_does_not_refuse_run(self, tmp_path, monkeypatch):
        # os.access() consults POSIX mode bits and is a false-negative on
        # filesystems that grant read via ACLs (NFSv4, SMB, FUSE). The
        # preflight must not refuse a run on os.access() alone — the
        # authoritative answer is whatever the worker's open() returns.
        f = tmp_path / "readable.txt"
        f.write_text("x")
        monkeypatch.setattr(cli.os, "access", lambda _p, _m: False)
        assert cli.check_paths([str(f)], using_default=False) is None


class TestDiscoverPathsEdgeCases:
    def test_nonexistent_path_is_dropped(self, tmp_path):
        # main() validates first, but discover_paths must be robust on its own:
        # a missing entry no longer silently masquerades as a file.
        assert cli.discover_paths([str(tmp_path / "missing")]) == []

    def test_duplicate_explicit_paths_are_deduped(self, tmp_path):
        # Passing the same file twice on the CLI is harmless; discover_paths
        # collapses duplicates so process_file isn't invoked on the same path
        # twice (its outputs would otherwise overwrite themselves).
        f = tmp_path / "tle2099.txt"
        f.write_text("x")
        assert cli.discover_paths([str(f), str(f)]) == [str(f)]

    def test_dir_and_explicit_file_inside_it_are_deduped(self, tmp_path):
        # `lintle clean dirA dirA/tle2099.txt` should process the file once,
        # not twice. Dedup is by canonical realpath so this works for plain
        # paths and symlinks alike.
        f = tmp_path / "tle2099.txt"
        f.write_text("x")
        found = cli.discover_paths([str(tmp_path), str(f)])
        # One canonical entry, regardless of which spelling won the race.
        canonical = {os.path.realpath(p) for p in found}
        assert canonical == {os.path.realpath(f)}
        assert len(found) == 1

    def test_symlinked_path_is_deduped(self, tmp_path):
        real = tmp_path / "tle2099.txt"
        real.write_text("x")
        link = tmp_path / "tle2099-link.txt"
        link.symlink_to(real)
        found = cli.discover_paths([str(real), str(link)])
        # The link and its target are the same file; only one survives.
        assert len(found) == 1


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

    def test_main_returns_two_on_basename_collision(self, tmp_path, capsys):
        # Two input dirs each contain a file named tle2022.txt — their cleaned
        # and broken outputs would silently overwrite each other under
        # data/output/. main() must catch this upfront and refuse the run.
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        (dir_a / "tle2022.txt").write_text("x")
        (dir_b / "tle2022.txt").write_text("x")

        rc = cli.main(["validate", str(dir_a), str(dir_b), "--jobs", "1"])

        assert rc == 2
        err = capsys.readouterr().err
        assert "tle2022.txt" in err
        assert "collision" in err.lower() or "overwrite" in err.lower()
        assert "Traceback" not in err

    def test_main_does_not_collide_when_same_file_listed_twice(
        self, tmp_path, line1, line2
    ):
        # `lintle clean dirA dirA/tle.txt` resolves to one file via
        # discover_paths' realpath dedup — there's no collision to report.
        src = tmp_path / "src"
        src.mkdir()
        f = src / "tle2099.txt"
        f.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

        rc = cli.main(
            [
                "clean",
                str(src),
                str(f),
                "--out-dir",
                str(tmp_path / "out"),
                "--jobs",
                "1",
            ]
        )

        assert rc == 0

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

    def test_clean_jsonl_empty_when_zero_quarantines(
        self, tmp_path, line1, line2
    ):
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
