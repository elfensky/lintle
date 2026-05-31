"""Tests for lintle.summary — pure helpers, responsive renderer, and run entry."""

import io

from rich.console import Console

from lintle import summary


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
        "schema_version": "2",
        "run": {
            "command": "clean",
            "timestamp": "2026-05-31T12:00:00Z",
            "elapsed_seconds": 124.0,
        },
        "environment": {"tool_version": "0.5.0", "python_version": "3.14.0"},
        "summary": {
            "files_processed": 3,
            "paired_records": 232378271,
            "orphan_entries": 0,
            "input_lines_seen": 463615084,
            "clean_count": 232275043,
            "quarantined_count": 103228,
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
        assert summary._humanize_duration(45.2) == "45.2s"
        assert summary._humanize_duration(124.0) == "2m 04s"
        assert summary._humanize_duration(3661.0) == "1h 01m 01s"

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

        report.write_run_json(str(tmp_path / "report.json"), _demo_envelope())

    def test_text_renders_to_stdout(self, tmp_path, capsys):
        self._write(tmp_path)
        rc = summary.run(str(tmp_path), "text")
        assert rc == 0
        out = capsys.readouterr().out
        assert "clean" in out and "quarantined" in out

    def test_json_emits_bytes_verbatim(self, tmp_path, capsys):
        self._write(tmp_path)
        raw = (tmp_path / "report.json").read_text(encoding="utf-8")
        rc = summary.run(str(tmp_path), "json")
        assert rc == 0
        assert capsys.readouterr().out == raw

    def test_missing_report_is_exit_2(self, tmp_path, capsys):
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        assert "no run found" in capsys.readouterr().err

    def test_bad_schema_is_exit_2(self, tmp_path, capsys):
        (tmp_path / "report.json").write_text(
            '{"schema_version": "99"}', encoding="utf-8"
        )
        rc = summary.run(str(tmp_path), "text")
        assert rc == 2
        assert "schema" in capsys.readouterr().err.lower()


class TestEdgeCases:
    def test_pick_tier_boundary_72(self):
        pt = summary._pick_tier
        assert pt(is_terminal=True, width=71, unicode_ok=True) == "plain"
        assert pt(is_terminal=True, width=72, unicode_ok=True) == "medium"

    def test_zero_records_run_renders_without_sections(self):
        import io

        from rich.console import Console

        env = {
            "schema_version": "2",
            "run": {
                "command": "clean",
                "timestamp": "2026-01-01T00:00:00Z",
                "elapsed_seconds": 0.1,
            },
            "environment": {"tool_version": "0.5.0", "python_version": "3.14.0"},
            "summary": {
                "files_processed": 1,
                "paired_records": 0,
                "orphan_entries": 0,
                "input_lines_seen": 0,
                "clean_count": 0,
                "quarantined_count": 0,
                "fix_counts": {},
                "quarantine_counts": {},
            },
            "files": [],
        }
        con = Console(
            file=io.StringIO(), width=120, force_terminal=True, color_system=None
        )
        summary.render(env, console=con)  # must not raise
        out = con.file.getvalue()
        assert "clean" in out  # totals still render
        assert "—" in out  # honest pct for the 0/0 case
