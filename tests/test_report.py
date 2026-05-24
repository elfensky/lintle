"""Tests for lintle.report — statistics, the quarantine sidecar, and summaries."""

import json

from lintle import report
from lintle.categories import FixClass
from lintle.diagnostics import RepairTier, RuleID, diagnostic


def _diag(rule_id, src=1, **kwargs):
    """Build a Diagnostic with sane defaults for tests."""
    return diagnostic(rule_id, source_line_nos=(src,), **kwargs)


def _stats_with_counts():
    stats = report.FileStats(src_name="tle2022.txt")
    stats.paired_records = 100
    stats.orphan_entries = 0
    stats.input_lines_seen = 200
    stats.clean_count = 98
    stats.quarantined_count = 2
    stats.fix_counts = {
        FixClass.TRAILING_BACKSLASH: 50,
        FixClass.RECONSTRUCTED_CHECKSUM: 7,
    }
    stats.reject_counts = {RuleID.CHECKSUM_MISMATCH: 2}
    return stats


def _two_file_stats():
    a = report.FileStats(src_name="tle2004.txt")
    a.paired_records = 1000
    a.orphan_entries = 0
    a.input_lines_seen = 2000
    a.clean_count = 990
    a.quarantined_count = 10
    a.fix_counts = {FixClass.TRAILING_BACKSLASH: 990}
    a.reject_counts = {RuleID.CHECKSUM_MISMATCH: 10}
    b = report.FileStats(src_name="tle2005.txt")
    b.paired_records = 3000
    b.orphan_entries = 0
    b.input_lines_seen = 6000
    b.clean_count = 3000
    b.quarantined_count = 0
    b.fix_counts = {
        FixClass.TRAILING_BACKSLASH: 1000,
        FixClass.RECONSTRUCTED_CHECKSUM: 500,
    }
    return [a, b]


class TestWriteBrokenFile:
    def test_write_broken_file(self, tmp_path):
        stats = report.FileStats(src_name="tle2099.txt")
        stats.paired_records = 5
        stats.quarantined_count = 1
        stats.reject_exemplars.setdefault(RuleID.BAD_PREFIX, []).append(
            report.RejectEntry(
                raw_lines=[b"1 garbage"],
                source_lines=[42],
                primary=_diag(
                    RuleID.BAD_PREFIX,
                    src=42,
                    note="line does not start with '1 ' or '2 '",
                ),
            )
        )
        out = tmp_path / "tle2099.broken.txt"

        report.write_broken_file(str(out), "tle2099.txt", stats)

        text = out.read_bytes()
        assert b"# source: tle2099.txt" in text
        # Denominator is paired_records + orphan_entries — what the file's
        # quarantine count is measured against. With 0 orphans here, that
        # equals paired_records (5).
        assert b"1 quarantined of 5 entries" in text
        assert b"source line 42" in text
        assert b"rule: TLE-PAIR-002" in text  # BAD_PREFIX
        assert b"1 garbage" in text

    def test_broken_file_is_byte_faithful(self, tmp_path):
        # A line quarantined for a non-ASCII byte must appear verbatim.
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.reject_exemplars.setdefault(RuleID.NON_ASCII_BYTE, []).append(
            report.RejectEntry(
                raw_lines=[b"1 \xff\xfe non-ascii"],
                source_lines=[7],
                primary=_diag(RuleID.NON_ASCII_BYTE, src=7),
            )
        )
        out = tmp_path / "x.broken.txt"

        report.write_broken_file(str(out), "x.txt", stats)

        assert b"\xff\xfe" in out.read_bytes()

    def test_two_line_record_location(self, tmp_path):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.reject_exemplars.setdefault(RuleID.CHECKSUM_MISMATCH, []).append(
            report.RejectEntry(
                raw_lines=[b"1 aaa", b"2 bbb"],
                source_lines=[14820, 14821],
                primary=diagnostic(
                    RuleID.CHECKSUM_MISMATCH,
                    source_line_nos=(14820, 14821),
                    tier_attempted=RepairTier.NORMALIZATION,
                    column_range=(69, 69),
                    observed="7",
                    expected="3",
                ),
            )
        )
        out = tmp_path / "x.broken.txt"

        report.write_broken_file(str(out), "x.txt", stats)

        text = out.read_bytes()
        assert b"source lines 14820-14821" in text
        # New format surfaces structured fields:
        assert b"rule: TLE-CHK-001 (tier-1)" in text
        assert b"col 69" in text
        assert b"observed='7'" in text
        assert b"expected='3'" in text

    def test_related_diagnostics_render_as_continuation_lines(self, tmp_path):
        # When a record has both a primary and a related diagnostic (e.g. both
        # lines failed), the related ones fold onto indented "and: ..." lines.
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.reject_exemplars.setdefault(RuleID.CHECKSUM_MISMATCH, []).append(
            report.RejectEntry(
                raw_lines=[b"1 aaa", b"2 bbb"],
                source_lines=[5, 6],
                primary=diagnostic(
                    RuleID.CHECKSUM_MISMATCH,
                    source_line_nos=(5,),
                    column_range=(69, 69),
                ),
                related=(
                    diagnostic(
                        RuleID.LINE_LENGTH,
                        source_line_nos=(6,),
                        observed="68",
                        expected="68 or 69",
                    ),
                ),
            )
        )
        out = tmp_path / "x.broken.txt"
        report.write_broken_file(str(out), "x.txt", stats)
        text = out.read_bytes()
        assert b"rule: TLE-CHK-001" in text
        assert b"    and: rule: TLE-COL-001" in text

    def test_write_broken_file_flattens_multiple_rules(self, tmp_path):
        stats = report.FileStats(src_name="x.txt")
        stats.paired_records = 6
        stats.quarantined_count = 6
        # Three rules, two entries each, source lines interleaved
        # (10/40, 20/50, 30/60) so a correct sort by source_lines[0] yields
        # the order 10, 20, 30, 40, 50, 60.
        for rule, srcs in (
            (RuleID.CHECKSUM_MISMATCH, [10, 40]),
            (RuleID.BAD_PREFIX, [20, 50]),
            (RuleID.NON_ASCII_BYTE, [30, 60]),
        ):
            for s in srcs:
                stats.reject_exemplars.setdefault(rule, []).append(
                    report.RejectEntry(
                        raw_lines=[f"row-{s}".encode("ascii")],
                        source_lines=[s],
                        primary=_diag(rule, src=s),
                    )
                )

        out = tmp_path / "x.broken.txt"
        report.write_broken_file(str(out), "x.txt", stats)

        text = out.read_bytes()
        for s in (10, 20, 30, 40, 50, 60):
            assert f"row-{s}".encode("ascii") in text
        for i in range(1, 7):
            assert f"[{i}] source line".encode("ascii") in text

    def test_write_broken_file_orders_by_source_line(self, tmp_path):
        stats = report.FileStats(src_name="x.txt")
        stats.paired_records = 6
        stats.quarantined_count = 6
        for rule, srcs in (
            (RuleID.CHECKSUM_MISMATCH, [10, 40]),
            (RuleID.BAD_PREFIX, [20, 50]),
            (RuleID.NON_ASCII_BYTE, [30, 60]),
        ):
            for s in srcs:
                stats.reject_exemplars.setdefault(rule, []).append(
                    report.RejectEntry(
                        raw_lines=[f"row-{s}".encode("ascii")],
                        source_lines=[s],
                        primary=_diag(rule, src=s),
                    )
                )

        out = tmp_path / "x.broken.txt"
        report.write_broken_file(str(out), "x.txt", stats)
        text = out.read_text("ascii")

        # Order of appearance must follow source_lines, not dict insertion
        # order or rule grouping.
        positions = [text.index(f"row-{s}") for s in (10, 20, 30, 40, 50, 60)]
        assert positions == sorted(positions)


class TestSummaries:
    def test_format_summary_shows_counts(self):
        out = report.format_summary(_stats_with_counts())
        assert "tle2022.txt" in out
        assert "100" in out  # paired_records — the anchor field of the header
        assert "98" in out
        assert "trailing-backslash 50" in out
        assert "reconstructed-checksum 7" in out
        assert "TLE-CHK-001 2" in out

    def test_format_summary_distinguishes_paired_from_orphan(self):
        stats = report.FileStats(src_name="tle2099.txt")
        stats.paired_records = 7
        stats.orphan_entries = 2
        stats.input_lines_seen = 17  # 7*2 + 2 + 1 blank, say
        stats.clean_count = 6
        stats.quarantined_count = 3  # 1 paired-failure + 2 orphans
        out = report.format_summary(stats)
        # The summary must surface the three independent counters from issue #5.
        assert "7" in out  # paired records
        assert "2" in out  # orphan lines
        assert "17" in out  # lines read

    def test_summary_dict_is_json_friendly(self):
        data = report.summary_dict(_stats_with_counts())
        assert data["src_name"] == "tle2022.txt"
        assert data["paired_records"] == 100
        assert data["orphan_entries"] == 0
        assert data["input_lines_seen"] == 200
        assert data["fix_counts"]["trailing-backslash"] == 50
        # reject_counts is keyed by stable rule IDs — TLE-CHK-001, not the
        # old free-form "checksum-mismatch" string.
        assert data["reject_counts"]["TLE-CHK-001"] == 2
        json.dumps(data)  # must not raise — cli.py serialises this in json mode

    def test_summary_dict_surfaces_quarantined_norad_ids(self):
        # The per-NORAD breakdown is part of the JSON contract — programmatic
        # consumers should see the same per-satellite data the Markdown
        # report does, with integer NORAD keys auto-stringified by json
        # and RuleID members coerced to their stable wire token.
        stats = report.FileStats(
            src_name="tle2099.txt",
            quarantined_norad_ids={
                25544: {
                    RuleID.CHECKSUM_MISMATCH: 3,
                    RuleID.NON_ASCII_BYTE: 1,
                },
                42: {RuleID.ORPHAN_LINE: 1},
            },
        )
        data = report.summary_dict(stats)
        assert data["quarantined_norad_ids"][25544][RuleID.CHECKSUM_MISMATCH] == 3
        # Shallow-copy: mutating the returned dict must not leak back to
        # the live FileStats (mirrors fix_counts / reject_counts).
        data["quarantined_norad_ids"].pop(42)
        assert 42 in stats.quarantined_norad_ids
        # JSON round-trip: int keys auto-stringify, RuleID keys coerce to
        # their stable wire token (TLE-CHK-001, TLE-COL-003, TLE-PAIR-001).
        rendered = json.loads(json.dumps(report.summary_dict(stats)))
        assert rendered["quarantined_norad_ids"]["25544"]["TLE-CHK-001"] == 3
        assert rendered["quarantined_norad_ids"]["25544"]["TLE-COL-003"] == 1
        assert rendered["quarantined_norad_ids"]["42"]["TLE-PAIR-001"] == 1

    def test_summary_dict_handles_empty_quarantined_norad_ids(self):
        # A fully clean run produces an empty per-NORAD dict, not a missing
        # key — JSON consumers can rely on the field always being present.
        data = report.summary_dict(_stats_with_counts())
        assert data["quarantined_norad_ids"] == {}


class TestFormatRejectLines:
    def test_format_reject_lines_groups_by_rule(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 4
        stats.reject_counts = {
            RuleID.CHECKSUM_MISMATCH: 2,
            RuleID.BAD_PREFIX: 2,
        }
        stats.reject_exemplars.setdefault(RuleID.CHECKSUM_MISMATCH, []).append(
            report.RejectEntry(
                raw_lines=[b"1 a", b"2 b"],
                source_lines=[10, 11],
                primary=diagnostic(
                    RuleID.CHECKSUM_MISMATCH,
                    source_line_nos=(10, 11),
                    column_range=(69, 69),
                ),
            )
        )
        stats.reject_exemplars.setdefault(RuleID.BAD_PREFIX, []).append(
            report.RejectEntry(
                raw_lines=[b"x"],
                source_lines=[20],
                primary=_diag(RuleID.BAD_PREFIX, src=20, note="line does not start"),
            )
        )

        out = report.format_reject_lines(stats)

        # Two rule-heading blocks appear, each with their count.
        assert "TLE-CHK-001 (2):" in out
        assert "TLE-PAIR-002 (2):" in out
        # Exemplars appear under their headings — rule_id embedded in the
        # diagnostic body line.
        assert "line 10-11: rule: TLE-CHK-001" in out
        assert "line 20: rule: TLE-PAIR-002" in out

    def test_format_reject_lines_sorts_by_descending_count(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 115
        stats.reject_counts = {
            RuleID.NON_ASCII_BYTE: 5,
            RuleID.CHECKSUM_MISMATCH: 100,
            RuleID.BAD_PREFIX: 10,
        }
        for rule in (
            RuleID.NON_ASCII_BYTE,
            RuleID.CHECKSUM_MISMATCH,
            RuleID.BAD_PREFIX,
        ):
            stats.reject_exemplars.setdefault(rule, []).append(
                report.RejectEntry(
                    raw_lines=[b"x"],
                    source_lines=[1],
                    primary=_diag(rule, src=1),
                )
            )

        out = report.format_reject_lines(stats)

        # CHECKSUM (100) → BAD_PREFIX (10) → NON_ASCII (5)
        # rule_ids: TLE-CHK-001, TLE-PAIR-002, TLE-COL-003
        pos_chk = out.index("TLE-CHK-001 (100)")
        pos_pair = out.index("TLE-PAIR-002 (10)")
        pos_col = out.index("TLE-COL-003 (5)")
        assert pos_chk < pos_pair < pos_col

    def test_format_reject_lines_ties_break_alphabetically(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 14
        # Same count — alphabetic tiebreak on rule_id string value.
        # TLE-CHK-001 < TLE-PAIR-002 alphabetically.
        stats.reject_counts = {
            RuleID.BAD_PREFIX: 7,  # "TLE-PAIR-002"
            RuleID.CHECKSUM_MISMATCH: 7,  # "TLE-CHK-001"
        }
        for rule in (RuleID.BAD_PREFIX, RuleID.CHECKSUM_MISMATCH):
            stats.reject_exemplars.setdefault(rule, []).append(
                report.RejectEntry(
                    raw_lines=[b"x"],
                    source_lines=[1],
                    primary=_diag(rule, src=1),
                )
            )

        out = report.format_reject_lines(stats)

        assert out.index("TLE-CHK-001") < out.index("TLE-PAIR-002")

    def test_format_reject_lines_emits_per_rule_remainder(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1003
        stats.reject_counts = {
            RuleID.CHECKSUM_MISMATCH: 1000,
            RuleID.BAD_PREFIX: 3,
        }
        # Full bucket of 5 for the noisy rule.
        for i in range(5):
            stats.reject_exemplars.setdefault(RuleID.CHECKSUM_MISMATCH, []).append(
                report.RejectEntry(
                    raw_lines=[b"x"],
                    source_lines=[i],
                    primary=_diag(RuleID.CHECKSUM_MISMATCH, src=i),
                )
            )
        # Bucket equal to the rule's total count — no remainder.
        for i in range(3):
            stats.reject_exemplars.setdefault(RuleID.BAD_PREFIX, []).append(
                report.RejectEntry(
                    raw_lines=[b"x"],
                    source_lines=[100 + i],
                    primary=_diag(RuleID.BAD_PREFIX, src=100 + i),
                )
            )

        out = report.format_reject_lines(stats)

        assert "...and 995 more" in out
        # Only the noisy rule has a remainder; "...and" appears exactly once.
        assert out.count("...and") == 1

    def test_format_reject_lines_empty_when_no_rejects(self):
        stats = report.FileStats(src_name="x.txt")
        assert report.format_reject_lines(stats) == ""

    def test_format_reject_lines_indentation_contract(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.reject_counts = {RuleID.CHECKSUM_MISMATCH: 1}
        stats.reject_exemplars.setdefault(RuleID.CHECKSUM_MISMATCH, []).append(
            report.RejectEntry(
                raw_lines=[b"x"],
                source_lines=[10, 11],
                primary=diagnostic(
                    RuleID.CHECKSUM_MISMATCH,
                    source_line_nos=(10,),
                    column_range=(69, 69),
                ),
                related=(
                    diagnostic(
                        RuleID.NON_ASCII_BYTE,
                        source_line_nos=(11,),
                        note="line 2: non-ascii",
                    ),
                ),
            )
        )

        out = report.format_reject_lines(stats)
        lines = out.splitlines()

        # Rule heading is 2-space indented.
        assert lines[0].startswith("  TLE-CHK-001")
        assert not lines[0].startswith("   ")
        # Exemplar line is 4-space indented (one nest deeper).
        assert lines[1].startswith("    line ")
        assert not lines[1].startswith("     ")
        # Related diagnostic is 6-space indented (one nest deeper still).
        assert lines[2].startswith("      and: ")
        assert not lines[2].startswith("       ")

    def test_format_reject_lines_surfaces_related_diagnostics(self):
        # Dual-failure record: primary + related, both must render.
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.reject_counts = {RuleID.CHECKSUM_MISMATCH: 1}
        stats.reject_exemplars.setdefault(RuleID.CHECKSUM_MISMATCH, []).append(
            report.RejectEntry(
                raw_lines=[b"1 a", b"2 b"],
                source_lines=[10, 11],
                primary=diagnostic(
                    RuleID.CHECKSUM_MISMATCH,
                    source_line_nos=(10,),
                    column_range=(69, 69),
                ),
                related=(
                    diagnostic(
                        RuleID.NON_ASCII_BYTE,
                        source_line_nos=(11,),
                        note="line 2: non-ascii byte",
                    ),
                ),
            )
        )
        out = report.format_reject_lines(stats)
        assert "TLE-CHK-001" in out
        assert "      and:" in out  # 6-space indent for related under a group
        assert "TLE-COL-003" in out


class TestRunReport:
    def test_format_run_report_aggregates_corpus(self):
        out = report.format_run_report(_two_file_stats())
        assert "# lintle clean run report" in out
        assert "Files processed: 2" in out
        assert "Records: 4,000" in out  # paired: 1000 + 3000
        assert "Orphan lines: 0" in out
        assert "Input lines: 8,000" in out  # 2000 + 6000
        assert "Cleaned: 3,990" in out  # 990 + 3000
        assert "Quarantined: 10" in out
        assert "99.7500%" in out  # 3990 / 4000
        assert "trailing-backslash | 1,990" in out  # 990 + 1000, summed
        assert "reconstructed-checksum | 500" in out
        assert "TLE-CHK-001 | 10" in out
        # Per-file rows present.
        assert "tle2004.txt" in out and "tle2005.txt" in out

    def test_format_run_report_includes_rule_reference_section(self):
        # Every rule ID that fired in the run gets a Rule reference entry
        # so report.md is self-explanatory without a separate docs page.
        out = report.format_run_report(_two_file_stats())
        assert "## Rule reference" in out
        assert "`TLE-CHK-001`" in out

    def test_format_run_report_surfaces_orphans_per_file(self):
        # Two files: one with orphans, one without. The per-file breakdown
        # must distinguish paired records from orphan lines so the reader can
        # see WHICH file contributed the orphans.
        a = report.FileStats(src_name="messy.txt")
        a.paired_records = 100
        a.orphan_entries = 5
        a.input_lines_seen = 210
        a.clean_count = 99
        a.quarantined_count = 6  # 1 paired-failure + 5 orphans
        a.reject_counts = {
            RuleID.ORPHAN_LINE: 5,
            RuleID.CHECKSUM_MISMATCH: 1,
        }
        b = report.FileStats(src_name="clean.txt")
        b.paired_records = 50
        b.orphan_entries = 0
        b.input_lines_seen = 100
        b.clean_count = 50
        b.quarantined_count = 0
        out = report.format_run_report([a, b])
        # Per-file breakdown must show orphan counts; an "0" is ambiguous on
        # its own so we anchor on the row that actually has orphans.
        assert "messy.txt" in out
        assert "5" in out  # the orphan count for messy.txt
        # The corpus-level orphan total surfaces too.
        assert "Orphan lines: 5" in out

    def test_write_run_report(self, tmp_path):
        out = tmp_path / "report.md"
        report.write_run_report(str(out), _two_file_stats())
        text = out.read_text(encoding="utf-8")
        assert text.startswith("# lintle clean run report")
        assert "Per-file breakdown" in text


class TestBrokenNoradIdsNdjson:
    def test_format_emits_empty_when_nothing_quarantined(self):
        # NDJSON has no header; an empty corpus produces an empty file,
        # not a blank line — consumers reading line-by-line just see zero
        # records, which is the same shape as a non-quarantine run.
        stats = report.FileStats(src_name="tle2099.txt", quarantined_norad_ids={})
        out = report.format_broken_noradids_ndjson([stats])
        assert out == ""

    def test_format_sorts_ids_ascending(self):
        # The NDJSON emitter cares only about which NORAD IDs exist (the
        # outer dict keys); per-category counts inside each entry feed the
        # report.md per-NORAD breakdown but are irrelevant here.
        a = report.FileStats(
            src_name="tle2008.txt", quarantined_norad_ids={26125: {}, 5: {}}
        )
        b = report.FileStats(src_name="tle2009.txt", quarantined_norad_ids={42: {}})
        out = report.format_broken_noradids_ndjson([a, b])
        assert out == '{"noradId":5}\n{"noradId":42}\n{"noradId":26125}\n'

    def test_format_uses_compact_json(self):
        # Compact separators (no space after the colon) keep the wire
        # format tight — kilobytes matter less than predictability for
        # downstream diffs and byte-exact CI assertions.
        stats = report.FileStats(src_name="x.txt", quarantined_norad_ids={5: {}})
        assert report.format_broken_noradids_ndjson([stats]) == '{"noradId":5}\n'

    def test_format_dedupes_across_files(self):
        # A NORAD ID seen in two files appears once — the deduplication is
        # the entire point of a corpus-wide artifact.
        a = report.FileStats(
            src_name="tle2008.txt", quarantined_norad_ids={1234: {}, 5678: {}}
        )
        b = report.FileStats(
            src_name="tle2009.txt", quarantined_norad_ids={5678: {}, 9999: {}}
        )
        out = report.format_broken_noradids_ndjson([a, b])
        assert out == '{"noradId":1234}\n{"noradId":5678}\n{"noradId":9999}\n'

    def test_format_each_line_is_parseable_json(self):
        # Every line must round-trip through json.loads independently —
        # the defining NDJSON guarantee.
        stats = report.FileStats(
            src_name="x.txt", quarantined_norad_ids={5: {}, 42: {}, 26125: {}}
        )
        out = report.format_broken_noradids_ndjson([stats])
        ids = [json.loads(line)["noradId"] for line in out.splitlines()]
        assert ids == [5, 42, 26125]

    def test_write_emits_lf_line_endings(self, tmp_path):
        # CRLF on Windows would break consumers expecting plain LF; the
        # writer must pin to ``\n`` regardless of platform default.
        stats = report.FileStats(src_name="x.txt", quarantined_norad_ids={1: {}, 2: {}})
        out = tmp_path / "broken-noradids.ndjson"
        report.write_broken_noradids_ndjson(str(out), [stats])
        assert out.read_bytes() == b'{"noradId":1}\n{"noradId":2}\n'

    def test_write_emits_empty_file_when_nothing_quarantined(self, tmp_path):
        stats = report.FileStats(src_name="x.txt", quarantined_norad_ids={})
        out = tmp_path / "broken-noradids.ndjson"
        report.write_broken_noradids_ndjson(str(out), [stats])
        assert out.read_bytes() == b""

    def test_aggregate_returns_sorted_unique_ids(self):
        a = report.FileStats(src_name="a.txt", quarantined_norad_ids={3: {}, 1: {}})
        b = report.FileStats(src_name="b.txt", quarantined_norad_ids={2: {}, 1: {}})
        assert report.aggregate_broken_norad_ids([a, b]) == [1, 2, 3]


class TestPerNoradBreakdown:
    """The ``## Per-NORAD breakdown`` section appended to ``report.md``.

    Covers the rollup, ordering, top-N cap, file-list truncation, and the
    italicised "...and N more" footer — exercising the renderer with
    hand-crafted ``FileStats.quarantined_norad_ids`` dicts rather than
    routing through the pipeline so each test isolates a single property.
    """

    def test_empty_when_no_quarantines(self):
        # Default ``quarantined_norad_ids`` is an empty dict, so the
        # section renders the italicised placeholder rather than an
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
            quarantined_norad_ids={25544: {RuleID.CHECKSUM_MISMATCH: 3}},
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
            quarantined_norad_ids={
                25544: {
                    RuleID.CHECKSUM_MISMATCH: 5,
                    RuleID.NON_ASCII_BYTE: 2,
                }
            },
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
            quarantined_norad_ids={25544: {RuleID.CHECKSUM_MISMATCH: 1}},
        )
        b = report.FileStats(
            src_name="tle2009.txt",
            quarantined_norad_ids={25544: {RuleID.CHECKSUM_MISMATCH: 2}},
        )
        out = report.format_run_report([a, b])
        assert ("| 25544 | 3 | TLE-CHK-001 (3) | tle2008.txt, tle2009.txt |") in out

    def test_aggregates_categories_across_files_for_same_norad(self):
        # Different defects in different files for the same NORAD must
        # merge into a single per-category total — the renderer treats
        # each file's contribution as additive.
        a = report.FileStats(
            src_name="tle2008.txt",
            quarantined_norad_ids={25544: {RuleID.CHECKSUM_MISMATCH: 2}},
        )
        b = report.FileStats(
            src_name="tle2009.txt",
            quarantined_norad_ids={25544: {RuleID.NON_ASCII_BYTE: 1}},
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
            quarantined_norad_ids={
                100: {RuleID.CHECKSUM_MISMATCH: 5},
                200: {RuleID.CHECKSUM_MISMATCH: 5},
                300: {RuleID.CHECKSUM_MISMATCH: 10},
            },
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
            quarantined_norad_ids={
                1: {RuleID.CHECKSUM_MISMATCH: 1},
                2: {RuleID.CHECKSUM_MISMATCH: 2},
                3: {RuleID.CHECKSUM_MISMATCH: 3},
            },
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
            quarantined_norad_ids={
                i: {RuleID.CHECKSUM_MISMATCH: 1} for i in range(1, 6)
            },
        )
        out = report.format_run_report([a], top_n=None)
        for nid in range(1, 6):
            assert f"| {nid} |" in out
        assert "...and" not in out  # no footer when nothing is truncated

    def test_no_footer_when_count_equals_top_n(self):
        # Exactly top_n entries → table is complete, footer must be absent.
        a = report.FileStats(
            src_name="x.txt",
            quarantined_norad_ids={
                1: {RuleID.CHECKSUM_MISMATCH: 1},
                2: {RuleID.CHECKSUM_MISMATCH: 2},
            },
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
            quarantined_norad_ids={
                1: {RuleID.CHECKSUM_MISMATCH: 1},
                2: {RuleID.CHECKSUM_MISMATCH: 1},
            },
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
                quarantined_norad_ids={25544: {RuleID.CHECKSUM_MISMATCH: 1}},
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
                quarantined_norad_ids={25544: {RuleID.CHECKSUM_MISMATCH: 1}},
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
            quarantined_norad_ids={25544: {RuleID.CHECKSUM_MISMATCH: 1}},
        )
        out = report.format_run_report([a])
        idx_per_file = out.index("## Per-file breakdown")
        idx_per_norad = out.index("## Per-NORAD breakdown")
        assert idx_per_file < idx_per_norad

    def test_round_trip_through_write_run_report(self, tmp_path):
        a = report.FileStats(
            src_name="x.txt",
            quarantined_norad_ids={25544: {RuleID.CHECKSUM_MISMATCH: 1}},
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
        a = report.FileStats(src_name="x.txt", quarantined_norad_ids={25544: {}})
        out = report.format_run_report([a])
        assert "_None — no records quarantined._" in out
