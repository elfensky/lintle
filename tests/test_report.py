"""Tests for lintle.report — statistics, the quarantine sidecar, and summaries."""

import dataclasses
import json
import os

import pytest

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


class TestRejectEntryConstructorContract:
    """Lock the RejectEntry field order so pipeline._record_reject's positional
    construction (pipeline.py:299) stays correct. norad_id MUST be the trailing
    field — see issue #9 spec §4.5.
    """

    def test_existing_keyword_construction_unchanged(self):
        # Locks the default-value contract for the 18 existing test-fixture
        # call sites that omit norad_id.
        entry = report.RejectEntry(
            raw_lines=[b"1 garbage"],
            source_lines=[42],
            primary=_diag(RuleID.BAD_PREFIX, src=42),
        )
        assert entry.norad_id is None
        assert entry.related == ()

    def test_positional_construction_pins_field_order(self):
        # Locks the (raw_lines, source_lines, primary, related) positional
        # contract used by pipeline._record_reject.
        primary = _diag(RuleID.CHECKSUM_MISMATCH, src=10)
        related = (_diag(RuleID.LINE_LENGTH, src=10),)
        entry = report.RejectEntry([b"1 x"], [10], primary, related)
        assert entry.raw_lines == [b"1 x"]
        assert entry.source_lines == [10]
        assert entry.primary is primary
        assert entry.related is related
        assert entry.norad_id is None  # appended trailing default

    def test_norad_id_must_be_keyword_to_avoid_corruption(self):
        # Documents the construction pattern that pipeline._record_reject
        # MUST use after adding norad_id to RejectEntry.
        primary = _diag(RuleID.CHECKSUM_MISMATCH, src=10)
        entry = report.RejectEntry(
            [b"1 x"], [10], primary, (), norad_id=12345
        )
        assert entry.norad_id == 12345


class TestEntryToJsonlDict:
    """The pure-function envelope+nested renderer that produces the wire
    shape of one ``report.jsonl`` line (issue #9, spec §4.2). Tests cover
    field presence, StrEnum coercion, tuple-to-list flattening, null
    handling, and the related-array nesting.
    """

    def test_envelope_carries_required_fields(self):
        entry = report.RejectEntry(
            raw_lines=[b"1 x", b"2 x"],
            source_lines=[12345, 12346],
            primary=diagnostic(
                RuleID.CHECKSUM_MISMATCH,
                source_line_nos=(12345, 12346),
                tier_attempted=RepairTier.NONE,
                column_range=(69, 69),
                observed="0",
                expected="3",
            ),
            norad_id=25544,
        )
        out = report.entry_to_jsonl_dict(entry, file="tle2022.txt", norad_id=25544)
        expected_keys = {
            "schema_version",
            "outcome",
            "file",
            "rule_id",
            "source_lines",
            "tier_attempted",
            "norad_id",
            "column_range",
            "observed",
            "expected",
            "note",
            "related",
        }
        assert set(out.keys()) == expected_keys
        assert out["schema_version"] == "1"
        assert out["outcome"] == "quarantined"
        assert out["file"] == "tle2022.txt"
        assert out["rule_id"] == "TLE-CHK-001"
        assert out["source_lines"] == [12345, 12346]
        assert out["tier_attempted"] == "none"
        assert out["norad_id"] == 25544
        assert out["column_range"] == [69, 69]
        assert out["observed"] == "0"
        assert out["expected"] == "3"
        assert out["note"] is None  # empty Diagnostic.note coerces to JSON null
        assert out["related"] == []

    def test_related_diagnostics_nested(self):
        primary = diagnostic(RuleID.CHECKSUM_MISMATCH, source_line_nos=(10, 11))
        related = (
            diagnostic(RuleID.LINE_LENGTH, source_line_nos=(10,), note="too long"),
            diagnostic(RuleID.NON_ASCII_BYTE, source_line_nos=(11,)),
        )
        entry = report.RejectEntry([b"1", b"2"], [10, 11], primary, related)
        out = report.entry_to_jsonl_dict(entry, file="x.txt", norad_id=None)
        assert len(out["related"]) == 2
        # Nested entries carry no envelope fields (no schema_version, outcome,
        # file, or norad_id).
        nested_keys = {
            "rule_id",
            "source_lines",
            "tier_attempted",
            "column_range",
            "observed",
            "expected",
            "note",
        }
        assert set(out["related"][0].keys()) == nested_keys
        assert out["related"][0]["rule_id"] == "TLE-COL-001"
        assert out["related"][0]["note"] == "too long"
        assert out["related"][1]["rule_id"] == "TLE-COL-003"
        assert out["related"][1]["note"] is None  # empty -> null

    def test_strenum_values_render_as_strings(self):
        # rule_id and tier_attempted are StrEnum members internally; the
        # output MUST be their stable wire token, not the enum repr.
        entry = report.RejectEntry(
            [b"1"],
            [1],
            diagnostic(
                RuleID.NON_ASCII_BYTE,
                source_line_nos=(1,),
                tier_attempted=RepairTier.NORMALIZATION,
            ),
        )
        out = report.entry_to_jsonl_dict(entry, file="x.txt", norad_id=None)
        assert out["rule_id"] == "TLE-COL-003"
        assert out["tier_attempted"] == "tier-1"
        # And these must be plain JSON-serializable strings — round-tripping
        # through json must not raise.
        assert json.dumps(out)

    def test_tuples_become_lists(self):
        # source_line_nos is a tuple internally; JSON has no tuple type, so
        # the renderer MUST coerce to list. Same for column_range.
        entry = report.RejectEntry(
            [b"1"],
            [10],
            diagnostic(
                RuleID.CHECKSUM_MISMATCH,
                source_line_nos=(10, 11),
                column_range=(69, 69),
            ),
        )
        out = report.entry_to_jsonl_dict(entry, file="x.txt", norad_id=None)
        assert isinstance(out["source_lines"], list)
        assert isinstance(out["column_range"], list)

    def test_none_fields_stay_none(self):
        # Diagnostic fields that are absent (column_range, observed, expected)
        # render as JSON null. note coerces "" -> null.
        entry = report.RejectEntry(
            [b"x"],
            [1],
            diagnostic(RuleID.BAD_PREFIX, source_line_nos=(1,)),
        )
        out = report.entry_to_jsonl_dict(entry, file="x.txt", norad_id=None)
        assert out["column_range"] is None
        assert out["observed"] is None
        assert out["expected"] is None
        assert out["note"] is None

    def test_norad_id_null_when_unreadable(self):
        entry = report.RejectEntry(
            [b"2 something"],
            [42],
            diagnostic(RuleID.ORPHAN_LINE, source_line_nos=(42,)),
            norad_id=None,
        )
        out = report.entry_to_jsonl_dict(entry, file="x.txt", norad_id=None)
        assert out["norad_id"] is None


class TestReportJsonlSchemaLock:
    """The schema contract for ``report.jsonl`` (issue #9 spec §8.6 / §5).
    Failures here force a spec revision: removing or renaming pinned
    fields requires bumping ``schema_version``. Adding new optional
    fields is non-breaking and should NOT fail these tests.
    """

    def _entry(self, src=10, norad_id=None):
        return report.RejectEntry(
            raw_lines=[b"1 x"],
            source_lines=[src],
            primary=diagnostic(RuleID.CHECKSUM_MISMATCH, source_line_nos=(src,)),
            norad_id=norad_id,
        )

    def test_schema_version_is_pinned(self, tmp_path):
        # Every line of a synthesized report.jsonl carries schema_version="1".
        path = str(tmp_path / "x.findings.jsonl")
        with report.JsonlFindingsWriter(path, src_name="x.txt") as writer:
            for src in (10, 20, 30):
                writer.write_entry(self._entry(src=src))
            writer.finalize()
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                parsed = json.loads(line)
                assert parsed["schema_version"] == "1"

    def test_outcome_field_pinned(self, tmp_path):
        # Every line of v1 report.jsonl carries outcome="quarantined".
        # Future additions of "fixed" outcomes will require updating
        # this test along with the spec.
        path = str(tmp_path / "x.findings.jsonl")
        with report.JsonlFindingsWriter(path, src_name="x.txt") as writer:
            writer.write_entry(self._entry())
            writer.finalize()
        with open(path, encoding="utf-8") as handle:
            parsed = json.loads(handle.readline())
            assert parsed["outcome"] == "quarantined"

    def test_envelope_field_set_is_locked(self):
        # The exact set of top-level keys is the spec contract; both
        # accidental additions and accidental removals fail here.
        entry = self._entry()
        out = report.entry_to_jsonl_dict(
            entry, file="x.txt", norad_id=entry.norad_id
        )
        expected = {
            "schema_version",
            "outcome",
            "file",
            "rule_id",
            "source_lines",
            "tier_attempted",
            "norad_id",
            "column_range",
            "observed",
            "expected",
            "note",
            "related",
        }
        assert set(out.keys()) == expected


class TestWriteBrokenFile:
    def test_write_broken_file(self, tmp_path):
        stats = report.FileStats(src_name="tle2099.txt")
        stats.paired_records = 5
        stats.quarantined_count = 1
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.BAD_PREFIX: [
                    report.RejectEntry(
                        raw_lines=[b"1 garbage"],
                        source_lines=[42],
                        primary=_diag(
                            RuleID.BAD_PREFIX,
                            src=42,
                            note="line does not start with '1 ' or '2 '",
                        ),
                    )
                ]
            },
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
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.NON_ASCII_BYTE: [
                    report.RejectEntry(
                        raw_lines=[b"1 \xff\xfe non-ascii"],
                        source_lines=[7],
                        primary=_diag(RuleID.NON_ASCII_BYTE, src=7),
                    )
                ]
            },
        )
        out = tmp_path / "x.broken.txt"

        report.write_broken_file(str(out), "x.txt", stats)

        assert b"\xff\xfe" in out.read_bytes()

    def test_two_line_record_location(self, tmp_path):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.CHECKSUM_MISMATCH: [
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
                ]
            },
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
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.CHECKSUM_MISMATCH: [
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
                ]
            },
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
        buckets = {}
        for rule, srcs in (
            (RuleID.CHECKSUM_MISMATCH, [10, 40]),
            (RuleID.BAD_PREFIX, [20, 50]),
            (RuleID.NON_ASCII_BYTE, [30, 60]),
        ):
            buckets[rule] = [
                report.RejectEntry(
                    raw_lines=[f"row-{s}".encode("ascii")],
                    source_lines=[s],
                    primary=_diag(rule, src=s),
                )
                for s in srcs
            ]
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5, entries_by_rule=buckets
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
        buckets = {}
        for rule, srcs in (
            (RuleID.CHECKSUM_MISMATCH, [10, 40]),
            (RuleID.BAD_PREFIX, [20, 50]),
            (RuleID.NON_ASCII_BYTE, [30, 60]),
        ):
            buckets[rule] = [
                report.RejectEntry(
                    raw_lines=[f"row-{s}".encode("ascii")],
                    source_lines=[s],
                    primary=_diag(rule, src=s),
                )
                for s in srcs
            ]
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5, entries_by_rule=buckets
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
            quarantined_norad_ids=report.NoradTracker(
                counts={
                    25544: {
                        RuleID.CHECKSUM_MISMATCH: 3,
                        RuleID.NON_ASCII_BYTE: 1,
                    },
                    42: {RuleID.ORPHAN_LINE: 1},
                }
            ),
        )
        data = report.summary_dict(stats)
        assert data["quarantined_norad_ids"][25544][RuleID.CHECKSUM_MISMATCH] == 3
        # Shallow-copy: mutating the returned dict must not leak back to
        # the live FileStats (mirrors fix_counts / reject_counts).
        data["quarantined_norad_ids"].pop(42)
        assert 42 in stats.quarantined_norad_ids.counts
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

    def test_summary_dict_surfaces_dropped_counts(self):
        # Sample with drops surfaces them in JSON under a stable
        # rule-ID key, so programmatic consumers can show "K of N
        # examples retained" without recomputing (issue #46).
        stats = _stats_with_counts()
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={},
            dropped_count={RuleID.CHECKSUM_MISMATCH: 995},
        )
        data = report.summary_dict(stats)
        assert data["dropped_counts"][RuleID.CHECKSUM_MISMATCH] == 995
        # JSON round-trip: StrEnum key coerces to its stable wire token.
        rendered = json.loads(json.dumps(data))
        assert rendered["dropped_counts"]["TLE-CHK-001"] == 995

    def test_summary_dict_dropped_counts_empty_by_default(self):
        # A run with no truncation produces an empty dict, not a missing
        # key — same contract as fix_counts / reject_counts. JSON
        # consumers can rely on the field always being present.
        data = report.summary_dict(_stats_with_counts())
        assert data["dropped_counts"] == {}

    def test_summary_dict_dropped_counts_is_shallow_copy(self):
        # Mutating the returned dict must not leak back to the live
        # FileSample (mirrors fix_counts / reject_counts contract).
        stats = report.FileStats(src_name="x.txt")
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={},
            dropped_count={RuleID.CHECKSUM_MISMATCH: 42},
        )
        data = report.summary_dict(stats)
        data["dropped_counts"].pop(RuleID.CHECKSUM_MISMATCH)
        assert RuleID.CHECKSUM_MISMATCH in stats.reject_sample.dropped_count

    def test_summary_dict_keys_unchanged_after_jsonl_feature(self):
        # Issue #9 spec §8.9: defensive test against per-finding fields
        # leaking into the --report json stdout output. The exact set of
        # keys returned by summary_dict is the contract for downstream
        # tooling consuming `lintle ... --report json`.
        data = report.summary_dict(_stats_with_counts())
        expected = {
            "src_name",
            "paired_records",
            "orphan_entries",
            "input_lines_seen",
            "clean_count",
            "quarantined_count",
            "fix_counts",
            "reject_counts",
            "dropped_counts",
            "quarantined_norad_ids",
        }
        assert set(data.keys()) == expected


class TestFormatRejectLines:
    def test_format_reject_lines_groups_by_rule(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 4
        stats.reject_counts = {
            RuleID.CHECKSUM_MISMATCH: 2,
            RuleID.BAD_PREFIX: 2,
        }
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.CHECKSUM_MISMATCH: [
                    report.RejectEntry(
                        raw_lines=[b"1 a", b"2 b"],
                        source_lines=[10, 11],
                        primary=diagnostic(
                            RuleID.CHECKSUM_MISMATCH,
                            source_line_nos=(10, 11),
                            column_range=(69, 69),
                        ),
                    )
                ],
                RuleID.BAD_PREFIX: [
                    report.RejectEntry(
                        raw_lines=[b"x"],
                        source_lines=[20],
                        primary=_diag(
                            RuleID.BAD_PREFIX, src=20, note="line does not start"
                        ),
                    )
                ],
            },
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
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                rule: [
                    report.RejectEntry(
                        raw_lines=[b"x"],
                        source_lines=[1],
                        primary=_diag(rule, src=1),
                    )
                ]
                for rule in (
                    RuleID.NON_ASCII_BYTE,
                    RuleID.CHECKSUM_MISMATCH,
                    RuleID.BAD_PREFIX,
                )
            },
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
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                rule: [
                    report.RejectEntry(
                        raw_lines=[b"x"],
                        source_lines=[1],
                        primary=_diag(rule, src=1),
                    )
                ]
                for rule in (RuleID.BAD_PREFIX, RuleID.CHECKSUM_MISMATCH)
            },
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
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                # Full bucket of 5 for the noisy rule.
                RuleID.CHECKSUM_MISMATCH: [
                    report.RejectEntry(
                        raw_lines=[b"x"],
                        source_lines=[i],
                        primary=_diag(RuleID.CHECKSUM_MISMATCH, src=i),
                    )
                    for i in range(5)
                ],
                # Bucket equal to the rule's total count — no remainder.
                RuleID.BAD_PREFIX: [
                    report.RejectEntry(
                        raw_lines=[b"x"],
                        source_lines=[100 + i],
                        primary=_diag(RuleID.BAD_PREFIX, src=100 + i),
                    )
                    for i in range(3)
                ],
            },
            dropped_count={RuleID.CHECKSUM_MISMATCH: 995},
        )

        out = report.format_reject_lines(stats)

        assert "...and 995 more" in out
        # Only the noisy rule has a remainder; "...and" appears exactly once.
        assert out.count("...and") == 1

    def test_format_reject_lines_heading_shows_drop_count_when_truncated(self):
        # When the sink had to drop entries, the rule heading switches to
        # the explicit "(N of M hits, K dropped)" form so an operator sees
        # the truncation at a glance, not just via the trailing
        # "...and X more" hint (issue #46).
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1003
        stats.reject_counts = {
            RuleID.CHECKSUM_MISMATCH: 1000,
            RuleID.BAD_PREFIX: 3,
        }
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.CHECKSUM_MISMATCH: [
                    report.RejectEntry(
                        raw_lines=[b"x"],
                        source_lines=[i],
                        primary=_diag(RuleID.CHECKSUM_MISMATCH, src=i),
                    )
                    for i in range(5)
                ],
                RuleID.BAD_PREFIX: [
                    report.RejectEntry(
                        raw_lines=[b"x"],
                        source_lines=[100 + i],
                        primary=_diag(RuleID.BAD_PREFIX, src=100 + i),
                    )
                    for i in range(3)
                ],
            },
            dropped_count={RuleID.CHECKSUM_MISMATCH: 995},
        )

        out = report.format_reject_lines(stats)

        # Truncated rule uses the explicit form.
        assert "TLE-CHK-001 (5 of 1,000 hits, 995 dropped):" in out
        # Rule with no drops keeps the simple form — heading stays readable
        # for the common, well-bounded case.
        assert "TLE-PAIR-002 (3):" in out

    def test_format_reject_lines_empty_when_no_rejects(self):
        stats = report.FileStats(src_name="x.txt")
        assert report.format_reject_lines(stats) == ""

    def test_format_reject_lines_indentation_contract(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.reject_counts = {RuleID.CHECKSUM_MISMATCH: 1}
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.CHECKSUM_MISMATCH: [
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
                ]
            },
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
        stats.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.CHECKSUM_MISMATCH: [
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
                ]
            },
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
        # Quarantined-by-rule table has a Dropped column (issue #46);
        # _two_file_stats does not set reject_sample so every rule's
        # dropped count is the zero default.
        assert "TLE-CHK-001 | 10 | 0" in out
        # Per-file rows present.
        assert "tle2004.txt" in out and "tle2005.txt" in out

    def test_format_run_report_quarantined_table_aggregates_drops_across_files(self):
        # Two files both contributing drops on the same rule; the
        # corpus-wide table sums them (issue #46). The Dropped column
        # is the operator-visible counterpart to the JSON dropped_counts
        # field, scoped to "how much evidence did the cap discard for
        # each rule across the whole run."
        a = report.FileStats(src_name="tle-a.txt")
        a.paired_records = 1000
        a.clean_count = 0
        a.quarantined_count = 1000
        a.reject_counts = {RuleID.CHECKSUM_MISMATCH: 1000}
        a.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={},
            dropped_count={RuleID.CHECKSUM_MISMATCH: 995},
        )
        b = report.FileStats(src_name="tle-b.txt")
        b.paired_records = 500
        b.clean_count = 0
        b.quarantined_count = 500
        b.reject_counts = {RuleID.CHECKSUM_MISMATCH: 500}
        b.reject_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={},
            dropped_count={RuleID.CHECKSUM_MISMATCH: 495},
        )

        out = report.format_run_report([a, b])

        # 1,000 + 500 hits, 995 + 495 dropped — table sums both.
        assert "TLE-CHK-001 | 1,500 | 1,490" in out

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
        stats = report.FileStats(
            src_name="tle2099.txt", quarantined_norad_ids=report.NoradTracker(counts={})
        )
        out = report.format_broken_noradids_ndjson([stats])
        assert out == ""

    def test_format_sorts_ids_ascending(self):
        # The NDJSON emitter cares only about which NORAD IDs exist (the
        # outer dict keys); per-category counts inside each entry feed the
        # report.md per-NORAD breakdown but are irrelevant here.
        a = report.FileStats(
            src_name="tle2008.txt",
            quarantined_norad_ids=report.NoradTracker(counts={26125: {}, 5: {}}),
        )
        b = report.FileStats(
            src_name="tle2009.txt",
            quarantined_norad_ids=report.NoradTracker(counts={42: {}}),
        )
        out = report.format_broken_noradids_ndjson([a, b])
        assert out == '{"noradId":5}\n{"noradId":42}\n{"noradId":26125}\n'

    def test_format_uses_compact_json(self):
        # Compact separators (no space after the colon) keep the wire
        # format tight — kilobytes matter less than predictability for
        # downstream diffs and byte-exact CI assertions.
        stats = report.FileStats(
            src_name="x.txt", quarantined_norad_ids=report.NoradTracker(counts={5: {}})
        )
        assert report.format_broken_noradids_ndjson([stats]) == '{"noradId":5}\n'

    def test_format_dedupes_across_files(self):
        # A NORAD ID seen in two files appears once — the deduplication is
        # the entire point of a corpus-wide artifact.
        a = report.FileStats(
            src_name="tle2008.txt",
            quarantined_norad_ids=report.NoradTracker(counts={1234: {}, 5678: {}}),
        )
        b = report.FileStats(
            src_name="tle2009.txt",
            quarantined_norad_ids=report.NoradTracker(counts={5678: {}, 9999: {}}),
        )
        out = report.format_broken_noradids_ndjson([a, b])
        assert out == '{"noradId":1234}\n{"noradId":5678}\n{"noradId":9999}\n'

    def test_format_each_line_is_parseable_json(self):
        # Every line must round-trip through json.loads independently —
        # the defining NDJSON guarantee.
        stats = report.FileStats(
            src_name="x.txt",
            quarantined_norad_ids=report.NoradTracker(
                counts={5: {}, 42: {}, 26125: {}}
            ),
        )
        out = report.format_broken_noradids_ndjson([stats])
        ids = [json.loads(line)["noradId"] for line in out.splitlines()]
        assert ids == [5, 42, 26125]

    def test_write_emits_lf_line_endings(self, tmp_path):
        # CRLF on Windows would break consumers expecting plain LF; the
        # writer must pin to ``\n`` regardless of platform default.
        stats = report.FileStats(
            src_name="x.txt",
            quarantined_norad_ids=report.NoradTracker(counts={1: {}, 2: {}}),
        )
        out = tmp_path / "broken-noradids.ndjson"
        report.write_broken_noradids_ndjson(str(out), [stats])
        assert out.read_bytes() == b'{"noradId":1}\n{"noradId":2}\n'

    def test_write_emits_empty_file_when_nothing_quarantined(self, tmp_path):
        stats = report.FileStats(
            src_name="x.txt", quarantined_norad_ids=report.NoradTracker(counts={})
        )
        out = tmp_path / "broken-noradids.ndjson"
        report.write_broken_noradids_ndjson(str(out), [stats])
        assert out.read_bytes() == b""

    def test_aggregate_returns_sorted_unique_ids(self):
        a = report.FileStats(
            src_name="a.txt",
            quarantined_norad_ids=report.NoradTracker(counts={3: {}, 1: {}}),
        )
        b = report.FileStats(
            src_name="b.txt",
            quarantined_norad_ids=report.NoradTracker(counts={2: {}, 1: {}}),
        )
        assert report.aggregate_broken_norad_ids([a, b]) == [1, 2, 3]


class TestConcatFindingsShards:
    """End-of-run concatenation of per-worker findings shards into the
    corpus-wide ``report.jsonl`` (issue #9, spec §4.6).
    """

    def _make_shard(self, shard_dir, stem_name, payload_lines):
        path = shard_dir / f"{stem_name}.findings.jsonl"
        path.write_text("".join(line + "\n" for line in payload_lines), encoding="utf-8")
        return path

    def test_concat_orders_alphabetically_by_src_name(self, tmp_path):
        shard_dir = tmp_path / ".shards"
        shard_dir.mkdir()
        # Write shards out of alphabetical order — concat must still emit
        # them in alphabetical src_name order.
        self._make_shard(shard_dir, "tle2022", ['{"file":"tle2022.txt"}'])
        self._make_shard(shard_dir, "tle2004", ['{"file":"tle2004.txt"}'])
        self._make_shard(shard_dir, "tle2013", ['{"file":"tle2013.txt"}'])
        # all_stats is already sorted by src_name in cli.py:510 before this
        # function is called; we mimic that here.
        all_stats = [
            report.FileStats(src_name="tle2004.txt"),
            report.FileStats(src_name="tle2013.txt"),
            report.FileStats(src_name="tle2022.txt"),
        ]
        dest = tmp_path / "report.jsonl"
        report.concat_findings_shards(str(tmp_path), str(dest), all_stats)
        lines = dest.read_text(encoding="utf-8").splitlines()
        files = [json.loads(line)["file"] for line in lines]
        assert files == ["tle2004.txt", "tle2013.txt", "tle2022.txt"]

    def test_concat_creates_empty_file_when_no_shards(self, tmp_path):
        # Empty .shards/ and empty all_stats -> empty report.jsonl
        # (matches broken-noradids.ndjson's zero-quarantine contract).
        (tmp_path / ".shards").mkdir()
        dest = tmp_path / "report.jsonl"
        report.concat_findings_shards(str(tmp_path), str(dest), [])
        assert dest.exists()
        assert dest.read_text(encoding="utf-8") == ""

    def test_concat_handles_missing_shard_gracefully(self, tmp_path):
        # all_stats references a file with no shard (validate-mode worker
        # or worker-crash before finalize) — concat skips it silently.
        shard_dir = tmp_path / ".shards"
        shard_dir.mkdir()
        self._make_shard(shard_dir, "tle2022", ['{"file":"tle2022.txt"}'])
        all_stats = [
            report.FileStats(src_name="tle2022.txt"),
            report.FileStats(src_name="tle2099.txt"),  # no shard for this one
        ]
        dest = tmp_path / "report.jsonl"
        report.concat_findings_shards(str(tmp_path), str(dest), all_stats)
        lines = dest.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["file"] == "tle2022.txt"

    def test_concat_removes_shard_directory(self, tmp_path):
        # After successful concat, .shards/ is gone.
        shard_dir = tmp_path / ".shards"
        shard_dir.mkdir()
        self._make_shard(shard_dir, "tle2022", ['{"file":"tle2022.txt"}'])
        report.concat_findings_shards(
            str(tmp_path),
            str(tmp_path / "report.jsonl"),
            [report.FileStats(src_name="tle2022.txt")],
        )
        assert not shard_dir.exists()

    def test_concat_atomic_rename(self, tmp_path):
        # The destination is written via .partial + os.replace, so no
        # .partial is left after success.
        shard_dir = tmp_path / ".shards"
        shard_dir.mkdir()
        self._make_shard(shard_dir, "tle2022", ['{"file":"tle2022.txt"}'])
        dest = tmp_path / "report.jsonl"
        report.concat_findings_shards(
            str(tmp_path),
            str(dest),
            [report.FileStats(src_name="tle2022.txt")],
        )
        assert dest.exists()
        assert not (tmp_path / "report.jsonl.partial").exists()

    def test_concat_failure_preserves_prior_report_jsonl(
        self, tmp_path, monkeypatch
    ):
        # If os.replace raises during concat, the destination from a
        # prior run (if any) stays unchanged and the partial is left
        # behind — next run's pre-run scrub purges. Spec §8.7.
        shard_dir = tmp_path / ".shards"
        shard_dir.mkdir()
        self._make_shard(shard_dir, "tle2022", ['{"file":"tle2022.txt"}'])
        dest = tmp_path / "report.jsonl"
        dest.write_text("from-prior-run\n", encoding="utf-8")

        def boom(*args, **kwargs):
            raise OSError("simulated concat rename failure")

        monkeypatch.setattr("os.replace", boom)
        with pytest.raises(OSError, match="simulated concat rename failure"):
            report.concat_findings_shards(
                str(tmp_path),
                str(dest),
                [report.FileStats(src_name="tle2022.txt")],
            )
        # Prior content is untouched.
        assert dest.read_text(encoding="utf-8") == "from-prior-run\n"


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


class TestFileSample:
    """The immutable per-file bounded sample (issue #19 refactor)."""

    def _stub_entries(self, count, rule=RuleID.CHECKSUM_MISMATCH):
        """Build N minimal RejectEntry stubs for cap-bound tests."""
        return [
            report.RejectEntry(
                raw_lines=[b"1 stub"],
                source_lines=[i],
                primary=_diag(rule, src=i),
            )
            for i in range(count)
        ]

    def test_from_bounded_clones_into_tuples(self):
        # Lists in -> tuples out, so the immutable invariant is structural,
        # not just a docstring promise.
        entries = self._stub_entries(2)
        sample = report.FileSample.from_bounded(
            cap=5, entries_by_rule={RuleID.CHECKSUM_MISMATCH: entries}
        )
        assert isinstance(sample.buckets[RuleID.CHECKSUM_MISMATCH], tuple)

    def test_from_bounded_rejects_over_cap(self):
        # Strict by design: an over-cap input surfaces immediately as a
        # ValueError naming the rule and the counts, so test fixtures that
        # accidentally exceed the bound fail loudly.
        with pytest.raises(ValueError, match=r"CHECKSUM_MISMATCH.*6 entries.*cap is 5"):
            report.FileSample.from_bounded(
                cap=5,
                entries_by_rule={RuleID.CHECKSUM_MISMATCH: self._stub_entries(6)},
            )

    def test_from_bounded_accepts_exactly_cap(self):
        # The boundary is inclusive — N entries at cap=N must succeed.
        sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={RuleID.CHECKSUM_MISMATCH: self._stub_entries(5)},
        )
        assert len(sample.buckets[RuleID.CHECKSUM_MISMATCH]) == 5

    def test_empty_default_has_zero_buckets(self):
        # The sentinel for files with no rejects — no None-checks needed
        # in renderers, and the cap survives so renderers can show
        # truncation against it.
        sample = report.FileSample.empty(cap=5)
        assert sample.buckets == {}
        assert sample.cap == 5

    def test_frozen(self):
        # Frozen dataclass: post-finalize mutation is structurally
        # impossible, so consumers cannot accidentally invalidate the cap.
        sample = report.FileSample.empty(cap=5)
        with pytest.raises(dataclasses.FrozenInstanceError):
            sample.cap = 99

    def test_empty_default_dropped_count_is_zero(self):
        # No rejects → no drops. The sentinel must initialise the per-rule
        # drop counter cleanly so renderers and aggregators do not need to
        # special-case the empty case.
        sample = report.FileSample.empty(cap=5)
        assert sample.dropped_count == {}

    def test_from_bounded_default_dropped_count_is_empty(self):
        # When the caller does not pass dropped_count, the field defaults
        # to an empty dict — matches how existing TestWriteBrokenFile and
        # TestFormatRejectLines fixtures invoke from_bounded (issue #46
        # backwards compat).
        sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={RuleID.CHECKSUM_MISMATCH: self._stub_entries(2)},
        )
        assert sample.dropped_count == {}

    def test_from_bounded_round_trips_dropped_count(self):
        # The drop counter passes through and is keyed by RuleID so
        # programmatic consumers can join it against reject_counts /
        # buckets without translation.
        sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={RuleID.CHECKSUM_MISMATCH: self._stub_entries(5)},
            dropped_count={RuleID.CHECKSUM_MISMATCH: 995},
        )
        assert sample.dropped_count[RuleID.CHECKSUM_MISMATCH] == 995

    def test_from_bounded_clones_dropped_count(self):
        # Caller mutations to the source dict must not leak into the frozen
        # sample — protects the invariant against accidental aliasing
        # (mirrors the tuple-clone done for buckets).
        source = {RuleID.CHECKSUM_MISMATCH: 10}
        sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={},
            dropped_count=source,
        )
        source[RuleID.CHECKSUM_MISMATCH] = 9999
        assert sample.dropped_count[RuleID.CHECKSUM_MISMATCH] == 10


class TestJsonlFindingsWriter:
    """The streaming writer for one file's findings shard (issue #9,
    spec §4.3). Mirrors ``BrokenFileWriter``'s lifecycle pattern but
    emits one JSON object per line with explicit UTF-8 / LF / sort_keys
    discipline.
    """

    def _entry(self, src=10, rule=RuleID.CHECKSUM_MISMATCH, norad_id=None):
        return report.RejectEntry(
            raw_lines=[b"1 x"],
            source_lines=[src],
            primary=diagnostic(rule, source_line_nos=(src,)),
            norad_id=norad_id,
        )

    def test_writes_one_line_per_entry(self, tmp_path):
        path = str(tmp_path / "tle2022.findings.jsonl")
        with report.JsonlFindingsWriter(path, src_name="tle2022.txt") as writer:
            writer.write_entry(self._entry(src=10))
            writer.write_entry(self._entry(src=20))
            writer.write_entry(self._entry(src=30))
            writer.finalize()
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        assert len(lines) == 3
        for line in lines:
            parsed = json.loads(line)
            assert parsed["schema_version"] == "1"
            assert parsed["outcome"] == "quarantined"
            assert parsed["file"] == "tle2022.txt"

    def test_compact_json_no_whitespace(self, tmp_path):
        # No spaces around JSON separators — grep can count lines, byte
        # count is minimal, downstream tooling can rely on one record per line.
        path = str(tmp_path / "x.findings.jsonl")
        with report.JsonlFindingsWriter(path, src_name="x.txt") as writer:
            writer.write_entry(self._entry())
            writer.finalize()
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        assert ", " not in content  # no whitespace after array commas
        assert ": " not in content  # no whitespace after key colons
        # And the file ends in a single LF, not CRLF.
        assert content.endswith("\n")
        assert "\r" not in content

    def test_finalize_atomic_rename(self, tmp_path):
        path = str(tmp_path / "x.findings.jsonl")
        with report.JsonlFindingsWriter(path, src_name="x.txt") as writer:
            writer.write_entry(self._entry())
            writer.finalize()
        assert os.path.exists(path)
        assert not os.path.exists(path + ".partial")

    def test_interrupted_run_leaves_no_partial(self, tmp_path):
        # Context-manager exit without finalize unlinks the .partial.
        path = str(tmp_path / "x.findings.jsonl")
        with report.JsonlFindingsWriter(path, src_name="x.txt") as writer:
            writer.write_entry(self._entry())
            # exit without finalize
        assert not os.path.exists(path)
        assert not os.path.exists(path + ".partial")

    def test_empty_finalize_creates_empty_file(self, tmp_path):
        path = str(tmp_path / "x.findings.jsonl")
        with report.JsonlFindingsWriter(path, src_name="x.txt") as writer:
            writer.finalize()
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as handle:
            assert handle.read() == ""

    def test_sort_keys_byte_determinism(self, tmp_path):
        # Two writers running on the same logical entry produce
        # byte-identical files — key order is sorted, not dict-insertion.
        path_a = str(tmp_path / "a.findings.jsonl")
        path_b = str(tmp_path / "b.findings.jsonl")
        for path in (path_a, path_b):
            with report.JsonlFindingsWriter(path, src_name="x.txt") as writer:
                writer.write_entry(self._entry())
                writer.finalize()
        with open(path_a, "rb") as a, open(path_b, "rb") as b:
            assert a.read() == b.read()

    def test_finalize_failure_leaves_partial(self, tmp_path, monkeypatch):
        # If os.replace raises during finalize, the partial is left behind
        # so the next run's pre-run scrub can purge it. Mirrors the
        # BrokenFileWriter contract; spec §8.7.
        path = str(tmp_path / "x.findings.jsonl")

        def boom(*args, **kwargs):
            raise OSError("simulated rename failure")

        with report.JsonlFindingsWriter(path, src_name="x.txt") as writer:
            writer.write_entry(self._entry())
            monkeypatch.setattr("os.replace", boom)
            with pytest.raises(OSError, match="simulated rename failure"):
                writer.finalize()
        assert not os.path.exists(path)
        # The .partial does NOT survive the context-manager exit because
        # _completed was never set to True (finalize raised before the
        # line that sets it) so __exit__ unlinks it.
        assert not os.path.exists(path + ".partial")


class TestRejectSink:
    """The single-mutation entry point that enforces the per-rule cap
    by construction (issue #19). Owns ``BrokenFileWriter`` in clean mode;
    skips it in validate mode; on ``finalize`` hands out an immutable
    :class:`FileSample`.
    """

    def _stub(self, src, rule=RuleID.CHECKSUM_MISMATCH):
        """One minimal RejectEntry for cap-bound tests."""
        return report.RejectEntry(
            raw_lines=[f"1 stub-{src}".encode("ascii")],
            source_lines=[src],
            primary=_diag(rule, src=src),
        )

    def test_add_under_cap_accepts(self):
        # Three entries, one rule — all three survive to the sample.
        sink = report.RejectSink(cap=5)
        for i in range(3):
            sink.add(self._stub(i))
        sample = sink.finalize(entries=3)
        assert len(sample.buckets[RuleID.CHECKSUM_MISMATCH]) == 3

    def test_add_over_cap_silently_drops(self):
        # Six entries, cap of 5 — the 6th drops silently. Matches today's
        # pipeline._record_reject behaviour; reject_counts retains the truth
        # so no information is lost at the operator level.
        sink = report.RejectSink(cap=5)
        for i in range(6):
            sink.add(self._stub(i))  # must not raise
        sample = sink.finalize(entries=6)
        assert len(sample.buckets[RuleID.CHECKSUM_MISMATCH]) == 5

    def test_cap_holds_under_skew(self):
        # 1000 of one rule, then 1 of another. With per-rule buckets, the
        # noisy rule cannot crowd the rare rule out of the sample.
        sink = report.RejectSink(cap=5)
        for i in range(1000):
            sink.add(self._stub(i, RuleID.CHECKSUM_MISMATCH))
        sink.add(self._stub(9999, RuleID.BAD_PREFIX))
        sample = sink.finalize(entries=1001)
        assert len(sample.buckets[RuleID.CHECKSUM_MISMATCH]) == 5
        assert len(sample.buckets[RuleID.BAD_PREFIX]) == 1

    def test_cap_holds_under_random_input(self):
        # Deterministic seed: a 1000-element stream of random (rule, entry)
        # pairs across every RuleID member must produce a sample whose every
        # bucket honours the cap. Catches off-by-ones the targeted tests miss.
        import random

        rng = random.Random(42)
        rules = list(RuleID)
        sink = report.RejectSink(cap=5)
        for i in range(1000):
            sink.add(self._stub(i, rng.choice(rules)))
        sample = sink.finalize(entries=1000)
        for bucket in sample.buckets.values():
            assert len(bucket) <= 5

    def test_finalize_returns_filesample_with_matching_cap(self):
        # The cap travels with the sample so renderers can show truncation.
        sink = report.RejectSink(cap=5)
        sample = sink.finalize(entries=0)
        assert sample.cap == 5

    def test_validate_mode_skips_writer(self, tmp_path):
        # No broken_path -> sink is purely in-memory; no temp file leakage.
        sink = report.RejectSink(cap=5)  # no broken_path
        with sink:
            sink.add(self._stub(1))
            sink.finalize(entries=1)
        # The parent dir should have no partials touched by the sink.
        assert list(tmp_path.iterdir()) == []

    def test_clean_mode_writes_byte_faithful_sidecar(self, tmp_path):
        # Each added entry's _render_entry bytes appear verbatim in the
        # finalized file; the header preamble names the source and the
        # quarantine count. Matches the existing TestStreamingRejects
        # assertion pattern (substring checks; the header timestamp is
        # volatile so we don't compare full bytes).
        path = tmp_path / "x.broken.txt"
        entries = [self._stub(i) for i in range(3)]
        sink = report.RejectSink(broken_path=str(path), src_name="x.txt", cap=5)
        with sink:
            for entry in entries:
                sink.add(entry)
            sink.finalize(entries=3)
        body = path.read_bytes()
        assert b"# source: x.txt" in body
        assert b"# 3 quarantined of 3 entries" in body
        for idx, entry in enumerate(entries, start=1):
            assert report._render_entry(idx, entry) in body

    def test_exit_without_finalize_cleans_partials(self, tmp_path):
        # An exception inside the `with` block leaves no debris. The
        # writer's __exit__ discards body + final partials when finalize
        # was not reached.
        path = tmp_path / "x.broken.txt"
        with (
            pytest.raises(RuntimeError, match="simulated"),
            report.RejectSink(broken_path=str(path), src_name="x.txt", cap=5) as sink,
        ):
            sink.add(self._stub(1))
            raise RuntimeError("simulated mid-file failure")
        assert list(tmp_path.glob("*.partial")) == []
        assert not path.exists()  # final file never published

    def test_add_after_finalize_raises(self):
        # Sink is single-use — post-finalize mutation has no defined
        # semantics. RuntimeError surfaces the misuse loudly. Locks the
        # spec §4.5 contract so future contributors don't accidentally
        # turn the sink into a reusable container.
        sink = report.RejectSink(cap=5)
        sink.finalize(entries=0)
        with pytest.raises(RuntimeError, match="already finalized"):
            sink.add(self._stub(1))

    def test_dropped_count_zero_when_under_cap(self):
        # Three entries under a cap of 5 — no drops, dropped_count stays
        # empty so renderers (issue #46) can omit the "K dropped" hint.
        sink = report.RejectSink(cap=5)
        for i in range(3):
            sink.add(self._stub(i))
        sample = sink.finalize(entries=3)
        assert sample.dropped_count == {}

    def test_dropped_count_increments_per_drop(self):
        # Seven entries one rule, cap of 5 — two drops accrue to that
        # rule's slot in the finalized sample.
        sink = report.RejectSink(cap=5)
        for i in range(7):
            sink.add(self._stub(i))
        sample = sink.finalize(entries=7)
        assert sample.dropped_count[RuleID.CHECKSUM_MISMATCH] == 2

    def test_dropped_count_per_rule_independent(self):
        # Mixed traffic: one rule overflows, another stays under cap.
        # Drops are accounted per-rule so the operator can tell which
        # rule lost evidence and which did not.
        sink = report.RejectSink(cap=5)
        for i in range(8):  # 3 drops
            sink.add(self._stub(i, RuleID.CHECKSUM_MISMATCH))
        for i in range(2):  # no drops
            sink.add(self._stub(i + 100, RuleID.BAD_PREFIX))
        sample = sink.finalize(entries=10)
        assert sample.dropped_count[RuleID.CHECKSUM_MISMATCH] == 3
        assert RuleID.BAD_PREFIX not in sample.dropped_count

    def test_dropped_count_under_random_input(self):
        # Deterministic seed: across a 1000-entry mixed-rule stream, the
        # invariant ``buckets[rule] + dropped_count[rule] == total
        # seen`` must hold for every rule the sink saw.
        import collections
        import random

        rng = random.Random(42)
        rules = list(RuleID)
        seen = collections.Counter()
        sink = report.RejectSink(cap=5)
        for i in range(1000):
            rule = rng.choice(rules)
            seen[rule] += 1
            sink.add(self._stub(i, rule))
        sample = sink.finalize(entries=1000)
        for rule, total in seen.items():
            bucket_size = len(sample.buckets.get(rule, ()))
            dropped = sample.dropped_count.get(rule, 0)
            assert bucket_size + dropped == total, rule

    # --- JSONL streaming integration (issue #9, spec §4.3) ---

    def test_sink_streams_to_jsonl_when_path_given(self, tmp_path):
        # When jsonl_path is set, every add() call appends a well-formed
        # JSON line to the shard.
        jsonl_path = str(tmp_path / "tle.findings.jsonl")
        with report.RejectSink(
            cap=5, jsonl_path=jsonl_path, src_name="tle.txt"
        ) as sink:
            sink.add(self._stub(10))
            sink.add(self._stub(20))
            sink.add(self._stub(30))
            sink.finalize(entries=3)
        with open(jsonl_path, encoding="utf-8") as handle:
            lines = handle.readlines()
        assert len(lines) == 3
        for line in lines:
            parsed = json.loads(line)
            assert parsed["rule_id"] == "TLE-CHK-001"
            assert parsed["file"] == "tle.txt"

    def test_sink_skips_jsonl_when_no_path(self, tmp_path):
        # No jsonl_path -> no shard artifact. Validate-mode contract.
        with report.RejectSink(cap=5) as sink:
            sink.add(self._stub(1))
            sink.finalize(entries=1)
        assert os.listdir(tmp_path) == []

    def test_jsonl_path_requires_src_name(self, tmp_path):
        # Mirrors broken_path's contract: src_name is required because
        # the JSONL writer needs the per-file ``file`` field value.
        jsonl_path = str(tmp_path / "x.findings.jsonl")
        with pytest.raises(ValueError, match="src_name"):
            report.RejectSink(cap=5, jsonl_path=jsonl_path)

    def test_dropped_from_sample_still_in_jsonl(self, tmp_path):
        # Cap governs the in-memory sample, NOT the on-disk JSONL.
        # 10 entries with cap 3 -> sample has 3, JSONL has all 10.
        jsonl_path = str(tmp_path / "tle.findings.jsonl")
        with report.RejectSink(
            cap=3, jsonl_path=jsonl_path, src_name="tle.txt"
        ) as sink:
            for i in range(10):
                sink.add(self._stub(i))
            sample = sink.finalize(entries=10)
        assert len(sample.buckets[RuleID.CHECKSUM_MISMATCH]) == 3
        with open(jsonl_path, encoding="utf-8") as handle:
            lines = handle.readlines()
        assert len(lines) == 10


class TestNoradTracker:
    """Per-NORAD per-rule quarantine tracker (issue #47 refactor)."""

    def test_record_creates_new_satellite_bucket(self):
        # The single mutation entry point — first record() for a NORAD
        # must initialise the bucket and tally the rule at 1.
        tracker = report.NoradTracker()
        tracker.record(25544, RuleID.CHECKSUM_MISMATCH)
        assert tracker.counts == {25544: {RuleID.CHECKSUM_MISMATCH: 1}}

    def test_record_increments_existing_pair(self):
        # Repeated calls for the same (norad, rule) accrue — the
        # encapsulation contract is that record() owns the +1.
        tracker = report.NoradTracker()
        for _ in range(3):
            tracker.record(25544, RuleID.CHECKSUM_MISMATCH)
        assert tracker.counts[25544][RuleID.CHECKSUM_MISMATCH] == 3

    def test_record_distinguishes_rules_for_same_satellite(self):
        # Two rule violations against the same NORAD live in the same
        # inner dict under their own RuleID keys, each tallied separately.
        tracker = report.NoradTracker()
        tracker.record(25544, RuleID.CHECKSUM_MISMATCH)
        tracker.record(25544, RuleID.NON_ASCII_BYTE)
        assert tracker.counts[25544] == {
            RuleID.CHECKSUM_MISMATCH: 1,
            RuleID.NON_ASCII_BYTE: 1,
        }

    def test_record_distinguishes_satellites_for_same_rule(self):
        # Two NORADs hitting the same rule each get their own outer
        # entry with the rule tallied at 1 — no cross-contamination.
        tracker = report.NoradTracker()
        tracker.record(25544, RuleID.CHECKSUM_MISMATCH)
        tracker.record(42, RuleID.CHECKSUM_MISMATCH)
        assert tracker.counts == {
            25544: {RuleID.CHECKSUM_MISMATCH: 1},
            42: {RuleID.CHECKSUM_MISMATCH: 1},
        }
