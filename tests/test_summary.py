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
        assert summary.can_encode("utf-8", "█") is True
        assert summary.can_encode(None, "█") is True
        assert summary.can_encode("ascii", "█") is False

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
    def test_pick_tier_boundary_80(self):
        pt = summary._pick_tier
        assert pt(is_terminal=True, width=79, unicode_ok=True) == "plain"
        assert pt(is_terminal=True, width=80, unicode_ok=True) == "medium"

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


def _file_entry(name, **over):
    """One `files[]` entry in the envelope shape `summary_dict` produces."""
    entry = {
        "src_name": name,
        "elapsed_seconds": 75.0,
        "bytes": 2048,
        "records_per_sec": 100.0,
        "paired_records": 1000,
        "orphan_entries": 0,
        "input_lines_seen": 2000,
        "clean_count": 990,
        "quarantined_count": 10,
        "fix_counts": {"crlf": 4},
        "quarantine_counts": {},
    }
    return entry | over


class TestFormatClock:
    """summary.format_clock — the compact column duration shared with the live
    phase-2 display, as opposed to the panel's prose _humanize_duration."""

    def test_renders_minutes_and_hours(self):
        assert summary.format_clock(0) == "0:00"
        assert summary.format_clock(9) == "0:09"
        assert summary.format_clock(75) == "1:15"
        assert summary.format_clock(3661) == "1:01:01"


class TestDisplayTier:
    """summary.display_tier — the shared phase-2/phase-3 column boundaries; the
    three bands must partition every width with no gap."""

    def test_boundaries(self):
        assert summary.display_tier(79) == "narrow"
        assert summary.display_tier(80) == "medium"
        assert summary.display_tier(99) == "medium"
        assert summary.display_tier(100) == "wide"


class TestRenderFiles:
    """Phase 3 — the per-file results table printed after a run and by `report`."""

    def _render(self, envelope, *, width=120, **kwargs):
        console = _console(width, terminal=True)
        summary.render_files(envelope, console=console, **kwargs)
        return console.file.getvalue()

    def test_row_per_file_with_counts_and_total(self):
        env = _demo_envelope()
        env["files"] = [_file_entry("a.txt"), _file_entry("b.txt", clean_count=5)]
        out = self._render(env)
        assert "a.txt" in out and "b.txt" in out
        assert "total" in out
        assert "2,000" in out  # summed records across the two files

    def test_total_time_is_wall_clock_not_the_column_sum(self):
        # Two files at 75s each under parallel workers finished in 124s wall
        # clock; summing the column would claim 150s (CLAUDE.md forbids it).
        env = _demo_envelope()
        env["files"] = [_file_entry("a.txt"), _file_entry("b.txt")]
        out = self._render(env)
        assert "2:04" in out  # run.elapsed_seconds = 124.0
        assert "2:30" not in out

    def test_failed_file_row_is_all_dashes(self):
        env = _demo_envelope()
        env["files"] = [_file_entry("a.txt")]
        env["run"]["failed_files"] = [{"file": "bad.txt", "error": "boom"}]
        out = self._render(env)
        assert "bad.txt" in out
        # The failed row contributes no numbers — the total still reflects a.txt.
        assert "—" in out

    def test_resumed_files_are_marked_dim(self):
        env = _demo_envelope()
        env["files"] = [_file_entry("old.txt"), _file_entry("new.txt")]
        console = Console(file=io.StringIO(), width=120, force_terminal=True)
        summary.render_files(env, console=console, resumed=frozenset({"old.txt"}))
        out = console.file.getvalue()
        assert "\x1b[2m" in out  # dim style emitted for the carried-over row

    def test_tiers_drop_columns_whole(self):
        env = _demo_envelope()
        env["files"] = [_file_entry("a.txt")]
        wide = self._render(env, width=120)
        medium = self._render(env, width=90)
        narrow = self._render(env, width=70)
        assert "repaired" in wide and "time" in wide and "size" in wide
        assert "repaired" not in medium and "size" in medium
        assert "size" not in narrow and "records" in narrow

    def test_empty_run_prints_nothing(self):
        assert self._render(_demo_envelope()) == ""


class TestResultsTable:
    """summary.results_table — the chrome every phase-3 table shares, so no two
    commands' results can drift apart visually."""

    def test_index_is_dim_and_right_justified_name_is_left_rest_are_right(self):
        table = summary.results_table("#", "file", "records", "hard")
        assert [c.justify for c in table.columns] == ["right", "left", "right", "right"]
        assert table.columns[0].style == "dim"
        assert not any(c.style for c in table.columns[1:])  # only # is dim

    def test_render_files_uses_the_shared_chrome(self):
        env = _demo_envelope()
        env["files"] = [_file_entry("a.txt")]
        console = _console(120, terminal=True)
        summary.render_files(env, console=console)
        out = console.file.getvalue()
        assert " # " in out and "a.txt" in out

    def test_optional_justify_overrides_selected_columns(self):
        table = summary.results_table(
            "#",
            "file",
            "rule",
            "change",
            justify={"rule": "left", "change": "left"},
        )
        assert [c.justify for c in table.columns] == ["right", "left", "left", "left"]
        assert table.columns[0].style == "dim"


class TestRuleMeanings:
    """The panel glosses every code it prints — a bare `TLE-COL-001` makes the
    reader go and look it up, and the text already exists in the registries
    `explain` and `diff` read."""

    def test_meaning_covers_rules_and_fixes(self):
        assert "69 columns" in summary._meaning("TLE-COL-001")
        assert "checksum" in summary._meaning("TLE-CHK-001")
        assert "carriage return" in summary._meaning("crlf")

    def test_unknown_code_renders_without_a_gloss(self):
        # A retired ID from an older run must still render, just unglossed.
        assert summary._meaning("TLE-GONE-999") == ""
        assert summary._meaning("") == ""

    def test_panel_shows_the_gloss_next_to_the_code(self):
        env = _demo_envelope()
        console = _console(120, terminal=True)
        summary.render(env, console=console)
        out = console.file.getvalue()
        assert "TLE-COL-004" in out and "what it means" in out
        assert "column layout" in out  # the quarantine gloss
        assert "trailing backslash" in out  # the fix gloss
