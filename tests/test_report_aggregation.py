"""Tests for report_aggregation.py — corpus totals and per-NORAD rollups."""

from lintle import report
from lintle.diagnostics import RuleID


class TestPerNoradBreakdown:
    """The ``## Per-NORAD breakdown`` section appended to ``report.md``.

    Covers the rollup, ordering, top-N cap, file-list truncation, and the
    italicised "...and N more" footer — exercising the renderer with
    hand-crafted ``FileStats.quarantined_norad_ids`` :class:`NoradTracker`
    instances rather than routing through the pipeline so each test
    isolates a single property.
    """

    def test_empty_when_no_quarantines(self):
        # Default ``quarantined_norad_ids`` is an empty NoradTracker, so
        # the section renders the italicised placeholder rather than an
        # empty table — keeps the report parseable when a corpus run
        # is fully clean.
        a = report.FileStats(src_name="tle2099.txt")
        out = report.format_run_report([a])
        assert "## Per-NORAD breakdown" in out
        assert "_None — no records quarantined._" in out
        # No table header in the empty case.
        assert "| NORAD ID |" not in out

    def test_single_norad_single_category_single_file(self):
        a = report.FileStats(
            src_name="tle2022.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={25544: {RuleID.CHECKSUM_MISMATCH: 3}}
            ),
        )
        out = report.format_run_report([a])
        assert "## Per-NORAD breakdown" in out
        assert "| 25544 | 3 | TLE-CHK-001 (3) | tle2022.txt |" in out

    def test_categories_sorted_by_count_then_name(self):
        # The Defect categories column must rank dominant defects first
        # so the operator's eye lands on the biggest contributor; ties
        # break alphabetically for determinism across runs.
        a = report.FileStats(
            src_name="tle2022.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={
                    25544: {
                        RuleID.CHECKSUM_MISMATCH: 5,
                        RuleID.NON_ASCII_BYTE: 2,
                    }
                }
            ),
        )
        out = report.format_run_report([a])
        # Rule IDs sort by stable wire token after count-desc — TLE-CHK-001
        # (count 5) before TLE-COL-003 (count 2).
        assert "TLE-CHK-001 (5), TLE-COL-003 (2)" in out

    def test_aggregates_across_files(self):
        # The same NORAD ID quarantined in two files surfaces with a
        # corpus-wide total and a comma-separated alphabetical file list.
        a = report.FileStats(
            src_name="tle2008.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={25544: {RuleID.CHECKSUM_MISMATCH: 1}}
            ),
        )
        b = report.FileStats(
            src_name="tle2009.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={25544: {RuleID.CHECKSUM_MISMATCH: 2}}
            ),
        )
        out = report.format_run_report([a, b])
        assert ("| 25544 | 3 | TLE-CHK-001 (3) | tle2008.txt, tle2009.txt |") in out

    def test_aggregates_categories_across_files_for_same_norad(self):
        # Different defects in different files for the same NORAD must
        # merge into a single per-category total — the renderer treats
        # each file's contribution as additive.
        a = report.FileStats(
            src_name="tle2008.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={25544: {RuleID.CHECKSUM_MISMATCH: 2}}
            ),
        )
        b = report.FileStats(
            src_name="tle2009.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={25544: {RuleID.NON_ASCII_BYTE: 1}}
            ),
        )
        out = report.format_run_report([a, b])
        # Total: 3 ; rules sorted by count desc: TLE-CHK-001 (2) before
        # TLE-COL-003 (1).
        assert (
            "| 25544 | 3 | TLE-CHK-001 (2), TLE-COL-003 (1) | "
            "tle2008.txt, tle2009.txt |"
        ) in out

    def test_rows_sorted_by_count_desc_then_norad_asc(self):
        # Primary key: quarantined-record count descending (worst
        # offenders first). Secondary key: NORAD ID ascending so ties
        # produce a deterministic order across runs.
        a = report.FileStats(
            src_name="x.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={
                    100: {RuleID.CHECKSUM_MISMATCH: 5},
                    200: {RuleID.CHECKSUM_MISMATCH: 5},
                    300: {RuleID.CHECKSUM_MISMATCH: 10},
                }
            ),
        )
        out = report.format_run_report([a])
        idx_300 = out.index("| 300 |")
        idx_100 = out.index("| 100 |")
        idx_200 = out.index("| 200 |")
        # 300 (count=10) first; 100 (count=5) before 200 (count=5) by ID asc.
        assert idx_300 < idx_100 < idx_200

    def test_top_n_truncates_and_emits_remainder_footer(self):
        # With three NORAD IDs and top_n=2, the table shows only the two
        # worst and a footer points at the NDJSON for the long tail.
        a = report.FileStats(
            src_name="x.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={
                    1: {RuleID.CHECKSUM_MISMATCH: 1},
                    2: {RuleID.CHECKSUM_MISMATCH: 2},
                    3: {RuleID.CHECKSUM_MISMATCH: 3},
                }
            ),
        )
        out = report.format_run_report([a], top_n=2)
        assert "| 3 |" in out
        assert "| 2 |" in out
        assert "| 1 |" not in out  # capped out
        assert (
            "_...and 1 more — see broken-noradids.ndjson for the full list._"
        ) in out

    def test_top_n_none_disables_cap(self):
        # ``top_n=None`` renders every row regardless of count — the
        # opt-out used by tests asserting the long-tail tail.
        a = report.FileStats(
            src_name="x.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={i: {RuleID.CHECKSUM_MISMATCH: 1} for i in range(1, 6)}
            ),
        )
        out = report.format_run_report([a], top_n=None)
        for nid in range(1, 6):
            assert f"| {nid} |" in out
        assert "...and" not in out  # no footer when nothing is truncated

    def test_no_footer_when_count_equals_top_n(self):
        # Exactly top_n entries → table is complete, footer must be absent.
        a = report.FileStats(
            src_name="x.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={
                    1: {RuleID.CHECKSUM_MISMATCH: 1},
                    2: {RuleID.CHECKSUM_MISMATCH: 2},
                }
            ),
        )
        out = report.format_run_report([a], top_n=2)
        assert "...and" not in out

    def test_top_n_zero_renders_header_only_with_full_remainder_footer(self):
        # ``top_n=0`` is a degenerate edge case: the table header still
        # emits (the rollup is non-empty) but no data rows are written,
        # and the "...and N more" footer counts every NORAD ID as
        # remaining. Locked-in behaviour rather than a raised ValueError
        # so callers building reports programmatically with a dynamic
        # cap (e.g. ``top_n = min(100, user_arg)``) don't crash on an
        # unexpected zero.
        a = report.FileStats(
            src_name="x.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={
                    1: {RuleID.CHECKSUM_MISMATCH: 1},
                    2: {RuleID.CHECKSUM_MISMATCH: 1},
                }
            ),
        )
        out = report.format_run_report([a], top_n=0)
        # Header present.
        assert "| NORAD ID | Quarantined records | Defect categories | Files |" in out
        # No data rows.
        assert "| 1 |" not in out
        assert "| 2 |" not in out
        # Footer accounts for the entire (untruncated) rollup.
        assert (
            "_...and 2 more — see broken-noradids.ndjson for the full list._"
        ) in out

    def test_files_column_truncates_after_five_with_plus_more(self):
        # A satellite quarantined across 7 files should show the first 5
        # alphabetically then ", +2 more" — keeps the Files column bounded
        # without coupling to the corpus's filename convention.
        all_stats = [
            report.FileStats(
                src_name=f"tle{year}.txt",
                quarantined_norad_ids=report.NoradTracker(
                    counts={25544: {RuleID.CHECKSUM_MISMATCH: 1}}
                ),
            )
            for year in range(2008, 2015)
        ]
        out = report.format_run_report(all_stats)
        assert (
            "tle2008.txt, tle2009.txt, tle2010.txt, tle2011.txt, tle2012.txt, +2 more"
        ) in out

    def test_files_column_no_truncation_when_within_preview(self):
        # Five or fewer files: the full list is shown without a "+M more"
        # suffix — the truncation marker only appears when it earns its
        # keep.
        all_stats = [
            report.FileStats(
                src_name=f"tle{year}.txt",
                quarantined_norad_ids=report.NoradTracker(
                    counts={25544: {RuleID.CHECKSUM_MISMATCH: 1}}
                ),
            )
            for year in range(2008, 2013)  # 5 files exactly
        ]
        out = report.format_run_report(all_stats)
        # The row's files column lists all five.
        assert "tle2008.txt, tle2009.txt, tle2010.txt, tle2011.txt, tle2012.txt" in out
        # No truncation marker.
        assert "+0 more" not in out
        assert "more |" not in out

    def test_section_appears_after_per_file_breakdown(self):
        # The per-NORAD section is the report's drilled-in tail — placed
        # below per-file so the file-locator table the operator scans
        # first stays at the top.
        a = report.FileStats(
            src_name="x.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={25544: {RuleID.CHECKSUM_MISMATCH: 1}}
            ),
        )
        out = report.format_run_report([a])
        idx_per_file = out.index("## Per-file breakdown")
        idx_per_norad = out.index("## Per-NORAD breakdown")
        assert idx_per_file < idx_per_norad

    def test_round_trip_through_write_run_report(self, tmp_path):
        a = report.FileStats(
            src_name="x.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={25544: {RuleID.CHECKSUM_MISMATCH: 1}}
            ),
        )
        out = tmp_path / "report.md"
        report.write_run_report(str(out), [a])
        text = out.read_text(encoding="utf-8")
        assert "## Per-NORAD breakdown" in text
        assert "| 25544 |" in text

    def test_ids_with_empty_category_dicts_are_skipped(self):
        # Defensive: an outer-key-only entry (no categories recorded) is
        # treated as no data and the section is empty — matches the
        # rendering contract that a NORAD must have at least one defect
        # to show up in the table.
        a = report.FileStats(
            src_name="x.txt",
            quarantined_norad_ids=report.NoradTracker(counts={25544: {}}),
        )
        out = report.format_run_report([a])
        assert "_None — no records quarantined._" in out
