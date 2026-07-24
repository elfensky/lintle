"""Tests for lintle.summary — pure helpers, responsive renderer, and run entry."""

import io

from rich.console import Console

from lintle import REPORT_DIRNAME, report, summary


def _report_json_path(tmp_path):
    """The path summary.run reads: ``<out_dir>/03-report/report.json``."""
    d = tmp_path / REPORT_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d / "report.json"


def _console(width, *, terminal):
    return Console(
        file=io.StringIO(),
        width=width,
        force_terminal=terminal,
        color_system=None,
        legacy_windows=False,
    )


def _demo_envelope():
    return {
        "schema_version": "3",
        "run": {
            "command": "clean",
            "timestamp": "2026-05-31T12:00:00Z",
            "elapsed_seconds": 124.0,
            "failed_files": [],
        },
        "environment": {"tool_version": "0.5.0", "python_version": "3.14.0"},
        "summary": {
            "files_processed": 3,
            "paired_records": 232378271,
            "orphan_entries": 0,
            "input_lines_seen": 463615084,
            "clean_count": 232275043,
            "quarantined_count": 103228,
            "failed_count": 0,
            "fix_counts": {
                "reconstructed-checksum": 108304512,
                "trailing-backslash": 167594304,
                "crlf": 1805,
            },
            "quarantine_counts": {
                "TLE-COL-004": 48481,
                "TLE-CHK-001": 47465,
                "TLE-PAIR-001": 4572,
                "TLE-PAIR-002": 2283,
                "TLE-COL-001": 424,
                "TLE-COL-002": 4,
                "TLE-PAIR-003": 1,
            },
        },
        "files": [],
    }


class TestHelpers:
    def test_humanize_duration(self):
        assert summary._humanize_duration(45.2) == "45 seconds"
        assert summary._humanize_duration(124.0) == "2 minutes and 4 seconds"
        assert summary._humanize_duration(3661.0) == "1 hour, 1 minute and 1 second"

    def test_format_pct_honest_tiny_rate(self):
        assert summary._format_pct(0, 1000) == "0%"
        assert summary._format_pct(4, 1000000) == "<0.01%"
        assert summary._format_pct(103228, 232378271) == "0.04%"
        assert summary._format_pct(5, 0) == "—"

    def test_can_encode(self):
        assert summary._can_encode("utf-8", "█") is True
        assert summary._can_encode(None, "█") is True
        assert summary._can_encode("ascii", "█") is False

    def test_pick_tier(self):
        pt = summary._pick_tier
        assert pt(is_terminal=False, width=200, unicode_ok=True) == "plain"
        assert pt(is_terminal=True, width=60, unicode_ok=True) == "plain"
        assert pt(is_terminal=True, width=120, unicode_ok=False) == "plain"
        assert pt(is_terminal=True, width=80, unicode_ok=True) == "medium"
        assert pt(is_terminal=True, width=120, unicode_ok=True) == "wide"

    def test_bar_caps_and_fallback(self):
        assert summary._bar(10, 10, width=10, use_unicode=True) == "█" * 10
        assert summary._bar(10, 10, width=10, use_unicode=False) == "#" * 10
        assert summary._bar(1, 2, width=10, use_unicode=False) == "#####     "
        assert summary._bar(3, 0, width=4, use_unicode=False) == "    "


class TestRender:
    def test_wide_has_bars_and_totals(self):
        con = _console(120, terminal=True)
        summary.render(_demo_envelope(), console=con)
        out = con.file.getvalue()
        assert "clean" in out and "quarantined" in out
        assert "█" in out

    def test_medium_has_no_bars(self):
        con = _console(80, terminal=True)
        summary.render(_demo_envelope(), console=con)
        assert "█" not in con.file.getvalue()

    def test_plain_when_piped_is_ascii(self):
        con = _console(120, terminal=False)
        summary.render(_demo_envelope(), console=con)
        out = con.file.getvalue()
        assert "█" not in out and "─" not in out and "→" not in out
        assert "clean" in out


class TestRun:
    def _write(self, tmp_path):
        from lintle import report

        report.write_run_json(str(_report_json_path(tmp_path)), _demo_envelope())

    def test_text_renders_to_stdout(self, tmp_path, capsys):
        self._write(tmp_path)
        rc = summary.run(str(tmp_path), "text")
        assert rc == 0
        out = capsys.readouterr().out
        assert "clean" in out and "quarantined" in out

    def test_json_emits_bytes_verbatim(self, tmp_path, capsys):
        self._write(tmp_path)
        raw = _report_json_path(tmp_path).read_text(encoding="utf-8")
        rc = summary.run(str(tmp_path), "json")
        assert rc == 0
        assert capsys.readouterr().out == raw

    def test_missing_report_is_exit_2(self, tmp_path, capsys):
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        assert "no run found" in capsys.readouterr().err

    def test_bad_schema_is_exit_2(self, tmp_path, capsys):
        _report_json_path(tmp_path).write_text(
            '{"schema_version": "99"}', encoding="utf-8"
        )
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        assert "schema" in capsys.readouterr().err.lower()

    def test_schema_v2_report_is_rejected_as_unsupported(self, tmp_path, capsys):
        # A schema-2 report.json (missing failed_files / failed_count) must
        # fail the schema_version check in summary.run with a clear message —
        # the intended behaviour after the "2" -> "3" bump.
        _report_json_path(tmp_path).write_text(
            '{"schema_version": "2", "run": {"timestamp": "x",'
            ' "elapsed_seconds": 1.0}}',
            encoding="utf-8",
        )
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        err = capsys.readouterr().err
        assert "unsupported schema_version" in err
        assert "expected" in err

    def test_invalid_json_is_exit_2(self, tmp_path, capsys):
        _report_json_path(tmp_path).write_text("{not json", encoding="utf-8")
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        assert "invalid report.json" in capsys.readouterr().err

    def test_invalid_utf8_report_json_is_exit_2(self, tmp_path, capsys):
        # Issue #92: UnicodeDecodeError from report.json must be caught, not
        # propagated — `lintle report` must return 2 with a clear message.
        _report_json_path(tmp_path).write_bytes(b"\xff\xfe")
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        err = capsys.readouterr().err
        assert "invalid report.json" in err or "no run found" in err

    def test_non_object_report_json_is_exit_2(self, tmp_path, capsys):
        # Well-formed JSON that is not an object (null / array / scalar) must
        # exit 2 with a clear message, not crash with an AttributeError.
        for doc in ("null", "[]", "42", '"hi"'):
            _report_json_path(tmp_path).write_text(doc, encoding="utf-8")
            rc = summary.run(str(tmp_path), "text")
            assert rc == 2
            assert "not a JSON object" in capsys.readouterr().err

    def test_schema_constant_tracks_report_envelope_version(self):
        # summary's accepted schema must track the writer's stamped version, or
        # a future bump would make `lintle report` reject fresh report.json.
        assert summary._SCHEMA == report._ENVELOPE_SCHEMA_VERSION


class TestEnvelopeValidation:
    """Issue #97(a): summary.run must validate the envelope shape before calling
    render(), so a schema-'3' report.json missing keys returns 2, not KeyError.
    """

    def _write_raw(self, tmp_path, doc):
        _report_json_path(tmp_path).write_text(doc, encoding="utf-8")

    def test_missing_summary_key_is_exit_2(self, tmp_path, capsys):
        self._write_raw(
            tmp_path,
            '{"schema_version": "3", "run": {"timestamp": "x",'
            ' "elapsed_seconds": 1.0, "failed_files": []}}',
        )
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        err = capsys.readouterr().err
        assert "invalid report.json" in err

    def test_missing_run_key_is_exit_2(self, tmp_path, capsys):
        self._write_raw(
            tmp_path,
            '{"schema_version": "3", "summary": {}}',
        )
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        err = capsys.readouterr().err
        assert "invalid report.json" in err

    def test_run_not_dict_is_exit_2(self, tmp_path, capsys):
        self._write_raw(
            tmp_path,
            '{"schema_version": "3", "run": "bad", "summary": {}}',
        )
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        capsys.readouterr()

    def test_summary_not_dict_is_exit_2(self, tmp_path, capsys):
        self._write_raw(
            tmp_path,
            '{"schema_version": "3", "run": {}, "summary": []}',
        )
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        capsys.readouterr()

    def test_missing_elapsed_seconds_is_exit_2(self, tmp_path, capsys):
        # render() calls run["elapsed_seconds"] — must be caught before render.
        s = (
            '{"schema_version": "3",'
            ' "run": {"timestamp": "x", "failed_files": []},'
            ' "summary": {"files_processed": 0, "paired_records": 0,'
            ' "orphan_entries": 0, "input_lines_seen": 0, "clean_count": 0,'
            ' "quarantined_count": 0, "failed_count": 0,'
            ' "fix_counts": {}, "quarantine_counts": {}}}'
        )
        self._write_raw(tmp_path, s)
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        capsys.readouterr()

    def test_missing_failed_count_is_exit_2(self, tmp_path, capsys):
        # schema-3: failed_count is required in summary; absent → shape violation.
        s = (
            '{"schema_version": "3",'
            ' "run": {"timestamp": "x", "elapsed_seconds": 1.0, "failed_files": []},'
            ' "summary": {"files_processed": 0, "paired_records": 0,'
            ' "orphan_entries": 0, "input_lines_seen": 0, "clean_count": 0,'
            ' "quarantined_count": 0, "fix_counts": {}, "quarantine_counts": {}}}'
        )
        self._write_raw(tmp_path, s)
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        err = capsys.readouterr().err
        assert "invalid report.json" in err

    def test_missing_run_failed_files_is_exit_2(self, tmp_path, capsys):
        # schema-3: run.failed_files is required; absent → shape violation.
        s = (
            '{"schema_version": "3",'
            ' "run": {"timestamp": "x", "elapsed_seconds": 1.0},'
            ' "summary": {"files_processed": 0, "paired_records": 0,'
            ' "orphan_entries": 0, "input_lines_seen": 0, "clean_count": 0,'
            ' "quarantined_count": 0, "failed_count": 0,'
            ' "fix_counts": {}, "quarantine_counts": {}}}'
        )
        self._write_raw(tmp_path, s)
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        err = capsys.readouterr().err
        assert "invalid report.json" in err


class TestPlainTierAsciiSafe:
    """Issue #97(b): the plain tier must not emit the em dash U+2014 — it is
    selected precisely when the console cannot encode Unicode, so any non-ASCII
    character causes UnicodeEncodeError.
    """

    def test_zero_denominator_plain_renders_without_emdash(self):
        # _format_pct(x, 0) returns "—" by default; the plain tier must use an
        # ASCII fallback so the same string can be written to an ASCII console.
        con = _console(80, terminal=False)
        env = {
            "schema_version": "3",
            "run": {
                "command": "clean",
                "timestamp": "2026-01-01T00:00:00Z",
                "elapsed_seconds": 0.1,
                "failed_files": [],
            },
            "environment": {"tool_version": "0.5.0", "python_version": "3.14.0"},
            "summary": {
                "files_processed": 1,
                "paired_records": 0,
                "orphan_entries": 0,
                "input_lines_seen": 0,
                "clean_count": 0,
                "quarantined_count": 0,
                "failed_count": 0,
                "fix_counts": {},
                "quarantine_counts": {},
            },
            "files": [],
        }
        summary.render(env, console=con)
        out = con.file.getvalue()
        # em dash must not appear in plain output
        assert "—" not in out
        # some ASCII replacement must appear for the zero-denominator pct field
        assert out  # rendered something

    def test_plain_tier_format_pct_returns_ascii(self):
        # _format_pct_plain (or the tier-aware branch) must return ASCII for 0/0.
        result = summary._format_pct_plain(5, 0)
        assert all(ord(c) < 128 for c in result)
        assert result  # non-empty


class TestFailedFilesRendering:
    """Issue #83: summary.render must show a Failures section when
    failed_count > 0 and omit it when failed_count == 0."""

    def _env_with_failures(self, failed_files):
        return {
            "schema_version": "3",
            "run": {
                "command": "clean",
                "timestamp": "2026-05-31T12:00:00Z",
                "elapsed_seconds": 10.0,
                "failed_files": failed_files,
            },
            "environment": {"tool_version": "0.5.0", "python_version": "3.14.0"},
            "summary": {
                "files_processed": 1,
                "paired_records": 100,
                "orphan_entries": 0,
                "input_lines_seen": 200,
                "clean_count": 100,
                "quarantined_count": 0,
                "failed_count": len(failed_files),
                "fix_counts": {},
                "quarantine_counts": {},
            },
            "files": [],
        }

    def test_failures_section_appears_when_failures(self):
        env = self._env_with_failures(
            [{"file": "tle2099.txt", "error": "OSError: disk full"}]
        )
        con = _console(120, terminal=True)
        summary.render(env, console=con)
        out = con.file.getvalue()
        assert "tle2099.txt" in out
        assert "disk full" in out

    def test_no_failures_section_when_clean(self):
        env = self._env_with_failures([])
        con = _console(120, terminal=True)
        summary.render(env, console=con)
        out = con.file.getvalue()
        # A clean run must NOT add any failures-related text. (No `or "0"`
        # escape hatch — the panel always shows "quarantined 0", which would
        # make the disjunction vacuously true and hide a regression.)
        assert "failed" not in out.lower()

    def test_failures_section_appears_in_plain_tier(self):
        env = self._env_with_failures(
            [{"file": "tle_err.txt", "error": "RuntimeError: boom"}]
        )
        con = _console(80, terminal=False)
        summary.render(env, console=con)
        out = con.file.getvalue()
        assert "tle_err.txt" in out
        assert "boom" in out


class TestEdgeCases:
    def test_pick_tier_boundary_72(self):
        pt = summary._pick_tier
        assert pt(is_terminal=True, width=71, unicode_ok=True) == "plain"
        assert pt(is_terminal=True, width=72, unicode_ok=True) == "medium"

    def test_zero_records_run_renders_without_sections(self):
        env = {
            "schema_version": "3",
            "run": {
                "command": "clean",
                "timestamp": "2026-01-01T00:00:00Z",
                "elapsed_seconds": 0.1,
                "failed_files": [],
            },
            "environment": {"tool_version": "0.5.0", "python_version": "3.14.0"},
            "summary": {
                "files_processed": 1,
                "paired_records": 0,
                "orphan_entries": 0,
                "input_lines_seen": 0,
                "clean_count": 0,
                "quarantined_count": 0,
                "failed_count": 0,
                "fix_counts": {},
                "quarantine_counts": {},
            },
            "files": [],
        }
        con = _console(120, terminal=True)
        summary.render(env, console=con)  # must not raise
        out = con.file.getvalue()
        assert "clean" in out  # totals still render
        assert "—" in out  # honest pct for the 0/0 case
