"""Tests for lintle.report — statistics, the quarantine sidecar, and summaries."""

import json

from lintle import report


def _stats_with_counts():
    stats = report.FileStats(src_name="tle2022.txt")
    stats.paired_records = 100
    stats.orphan_entries = 0
    stats.input_lines_seen = 200
    stats.clean_count = 98
    stats.quarantined_count = 2
    stats.fix_counts = {"trailing-backslash": 50, "reconstructed-checksum": 7}
    stats.reject_categories = {"checksum-mismatch": 2}
    return stats


def _two_file_stats():
    a = report.FileStats(src_name="tle2004.txt")
    a.paired_records = 1000
    a.orphan_entries = 0
    a.input_lines_seen = 2000
    a.clean_count = 990
    a.quarantined_count = 10
    a.fix_counts = {"trailing-backslash": 990}
    a.reject_categories = {"checksum-mismatch": 10}
    b = report.FileStats(src_name="tle2005.txt")
    b.paired_records = 3000
    b.orphan_entries = 0
    b.input_lines_seen = 6000
    b.clean_count = 3000
    b.quarantined_count = 0
    b.fix_counts = {"trailing-backslash": 1000, "reconstructed-checksum": 500}
    return [a, b]


class TestWriteBrokenFile:
    def test_write_broken_file(self, tmp_path):
        stats = report.FileStats(src_name="tle2099.txt")
        stats.paired_records = 5
        stats.quarantined_count = 1
        stats.reject_exemplars.append(
            report.RejectEntry(
                raw_lines=[b"1 garbage"],
                source_lines=[42],
                reason="bad-prefix: line does not start with '1 ' or '2 '",
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
        assert b"1 garbage" in text

    def test_broken_file_is_byte_faithful(self, tmp_path):
        # A line quarantined for a non-ASCII byte must appear verbatim.
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.reject_exemplars.append(
            report.RejectEntry(
                raw_lines=[b"1 \xff\xfe non-ascii"],
                source_lines=[7],
                reason="non-ascii",
            )
        )
        out = tmp_path / "x.broken.txt"

        report.write_broken_file(str(out), "x.txt", stats)

        assert b"\xff\xfe" in out.read_bytes()

    def test_two_line_record_location(self, tmp_path):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.reject_exemplars.append(
            report.RejectEntry(
                raw_lines=[b"1 aaa", b"2 bbb"],
                source_lines=[14820, 14821],
                reason="line 2: checksum mismatch",
            )
        )
        out = tmp_path / "x.broken.txt"

        report.write_broken_file(str(out), "x.txt", stats)

        assert b"source lines 14820-14821" in out.read_bytes()


class TestSummaries:
    def test_format_summary_shows_counts(self):
        out = report.format_summary(_stats_with_counts())
        assert "tle2022.txt" in out
        assert "100" in out  # paired_records — the anchor field of the header
        assert "98" in out
        assert "trailing-backslash 50" in out
        assert "reconstructed-checksum 7" in out
        assert "checksum-mismatch 2" in out

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
        assert data["reject_categories"]["checksum-mismatch"] == 2
        json.dumps(data)  # must not raise — cli.py serialises this in json mode


class TestFormatRejectLines:
    def test_format_reject_lines_lists_locations(self):
        stats = report.FileStats(src_name="x.txt")
        stats.reject_exemplars.append(
            report.RejectEntry(
                raw_lines=[b"1 a", b"2 b"],
                source_lines=[10, 11],
                reason="line 2: checksum mismatch",
            )
        )
        out = report.format_reject_lines(stats)
        assert "10-11" in out and "checksum mismatch" in out

    def test_format_reject_lines_caps_long_lists(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 250
        for i in range(250):
            stats.reject_exemplars.append(
                report.RejectEntry(
                    raw_lines=[b"1 a"], source_lines=[i], reason="bad-prefix"
                )
            )
        out = report.format_reject_lines(stats, limit=100)
        assert "150 more" in out


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
        assert "checksum-mismatch | 10" in out
        # Per-file rows present.
        assert "tle2004.txt" in out and "tle2005.txt" in out

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
        a.reject_categories = {"orphan-line": 5, "checksum-mismatch": 1}
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
        stats = report.FileStats(src_name="tle2099.txt", quarantined_norad_ids=set())
        out = report.format_broken_noradids_ndjson([stats])
        assert out == ""

    def test_format_sorts_ids_ascending(self):
        a = report.FileStats(src_name="tle2008.txt", quarantined_norad_ids={26125, 5})
        b = report.FileStats(src_name="tle2009.txt", quarantined_norad_ids={42})
        out = report.format_broken_noradids_ndjson([a, b])
        assert out == '{"noradId":5}\n{"noradId":42}\n{"noradId":26125}\n'

    def test_format_uses_compact_json(self):
        # Compact separators (no space after the colon) keep the wire
        # format tight — kilobytes matter less than predictability for
        # downstream diffs and byte-exact CI assertions.
        stats = report.FileStats(src_name="x.txt", quarantined_norad_ids={5})
        assert report.format_broken_noradids_ndjson([stats]) == '{"noradId":5}\n'

    def test_format_dedupes_across_files(self):
        # A NORAD ID seen in two files appears once — the deduplication is
        # the entire point of a corpus-wide artifact.
        a = report.FileStats(src_name="tle2008.txt", quarantined_norad_ids={1234, 5678})
        b = report.FileStats(src_name="tle2009.txt", quarantined_norad_ids={5678, 9999})
        out = report.format_broken_noradids_ndjson([a, b])
        assert out == '{"noradId":1234}\n{"noradId":5678}\n{"noradId":9999}\n'

    def test_format_each_line_is_parseable_json(self):
        # Every line must round-trip through json.loads independently —
        # the defining NDJSON guarantee.
        stats = report.FileStats(src_name="x.txt", quarantined_norad_ids={5, 42, 26125})
        out = report.format_broken_noradids_ndjson([stats])
        ids = [json.loads(line)["noradId"] for line in out.splitlines()]
        assert ids == [5, 42, 26125]

    def test_write_emits_lf_line_endings(self, tmp_path):
        # CRLF on Windows would break consumers expecting plain LF; the
        # writer must pin to ``\n`` regardless of platform default.
        stats = report.FileStats(src_name="x.txt", quarantined_norad_ids={1, 2})
        out = tmp_path / "broken-noradids.ndjson"
        report.write_broken_noradids_ndjson(str(out), [stats])
        assert out.read_bytes() == b'{"noradId":1}\n{"noradId":2}\n'

    def test_write_emits_empty_file_when_nothing_quarantined(self, tmp_path):
        stats = report.FileStats(src_name="x.txt", quarantined_norad_ids=set())
        out = tmp_path / "broken-noradids.ndjson"
        report.write_broken_noradids_ndjson(str(out), [stats])
        assert out.read_bytes() == b""

    def test_aggregate_returns_sorted_unique_ids(self):
        a = report.FileStats(src_name="a.txt", quarantined_norad_ids={3, 1})
        b = report.FileStats(src_name="b.txt", quarantined_norad_ids={2, 1})
        assert report.aggregate_broken_norad_ids([a, b]) == [1, 2, 3]
