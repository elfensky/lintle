"""Tests for thresholds.py — --max-quarantined parsing and the quarantine exit gate."""

from fractions import Fraction

import pytest

from lintle import cli, report, thresholds


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

    def test_max_quarantined_at_threshold_passes(self, tmp_path, line1, line2):
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

    def test_max_quarantined_rejects_negative_value(
        self, tmp_path, line1, line2, capsys
    ):
        src = self._write_one_bad_record(tmp_path, line1, line2)

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(tmp_path / "out"),
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

    def test_pct_at_threshold_passes(self, tmp_path, line1, line2):
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

    def test_pct_malformed_returns_2(self, tmp_path, line1, line2, capsys):
        src = self._write_one_bad_record(tmp_path, line1, line2)

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(tmp_path / "out"),
                "--jobs",
                "1",
                "--max-quarantined",
                "1.2.3%",
            ]
        )

        assert rc == 2
        assert "invalid percentage" in capsys.readouterr().err

    def test_pct_out_of_range_returns_2(self, tmp_path, line1, line2, capsys):
        src = self._write_one_bad_record(tmp_path, line1, line2)

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(tmp_path / "out"),
                "--jobs",
                "1",
                "--max-quarantined",
                "150%",
            ]
        )

        assert rc == 2
        assert "percentage must be in 0..100" in capsys.readouterr().err


class TestQuarantineExitCodeExactBoundary:
    """The percentage gate must compare exactly: exactly-at-threshold passes
    (the strictly-greater contract). A float threshold drifts at awkward
    percentages like 0.29%, flipping an at-boundary run to a spurious failure.
    """

    @staticmethod
    def _stats(clean, quarantined):
        return [
            report.FileStats(
                src_name="t", clean_count=clean, quarantined_count=quarantined
            )
        ]

    def test_pct_exact_boundary_passes_at_awkward_percentage(self):
        # 290 quarantined of 100_000 routed = exactly 0.29%. float("0.29") is
        # 0.28999999999999998, so 100*290 (=29000) > 0.29*100000
        # (=28999.999999999996) spuriously fails. The contract is
        # strictly-greater, so exactly 0.29% must pass.
        mode, threshold = thresholds.parse_quarantine_threshold("0.29%")
        stats = self._stats(clean=99_710, quarantined=290)
        assert thresholds.quarantine_exit_code(stats, mode, threshold) == 0

    def test_pct_just_over_awkward_boundary_fails(self):
        # 291 of 100_000 = 0.291% > 0.29% → fail.
        mode, threshold = thresholds.parse_quarantine_threshold("0.29%")
        stats = self._stats(clean=99_709, quarantined=291)
        assert thresholds.quarantine_exit_code(stats, mode, threshold) == 1

    def test_parsed_pct_is_exact_rational(self):
        # The parsed percentage is an exact Fraction, not a lossy float.
        assert thresholds.parse_quarantine_threshold("0.29%") == (
            "pct",
            Fraction(29, 100),
        )


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
