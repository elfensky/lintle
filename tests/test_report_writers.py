"""Tests for report_writers.py — the .broken.txt sidecar, report.jsonl shards,
the QuarantineSink, broken-noradids.ndjson, and shard concat."""

import collections
import json
import os
import random

import pytest

from lintle import report, report_writers
from lintle.diagnostics import RepairTier, RuleID, diagnostic


def _diag(rule_id, src=1, **kwargs):
    """Build a Diagnostic with sane defaults for tests."""
    return diagnostic(rule_id, source_line_nos=(src,), **kwargs)


class TestEntryToJsonlDict:
    """The pure-function envelope+nested renderer that produces the wire
    shape of one ``report.jsonl`` line (issue #9, spec §4.2). Tests cover
    field presence, StrEnum coercion, tuple-to-list flattening, null
    handling, and the related-array nesting.
    """

    def test_envelope_carries_required_fields(self):
        entry = report.QuarantineEntry(
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
        out = report_writers.entry_to_jsonl_dict(
            entry, file="tle2022.txt", norad_id=25544
        )
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
        entry = report.QuarantineEntry([b"1", b"2"], [10, 11], primary, related)
        out = report_writers.entry_to_jsonl_dict(entry, file="x.txt", norad_id=None)
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
        entry = report.QuarantineEntry(
            [b"1"],
            [1],
            diagnostic(
                RuleID.NON_ASCII_BYTE,
                source_line_nos=(1,),
                tier_attempted=RepairTier.NORMALIZATION,
            ),
        )
        out = report_writers.entry_to_jsonl_dict(entry, file="x.txt", norad_id=None)
        assert out["rule_id"] == "TLE-COL-003"
        assert out["tier_attempted"] == "tier-1"
        # And these must be plain JSON-serializable strings — round-tripping
        # through json must not raise.
        assert json.dumps(out)

    def test_tuples_become_lists(self):
        # source_line_nos is a tuple internally; JSON has no tuple type, so
        # the renderer MUST coerce to list. Same for column_range.
        entry = report.QuarantineEntry(
            [b"1"],
            [10],
            diagnostic(
                RuleID.CHECKSUM_MISMATCH,
                source_line_nos=(10, 11),
                column_range=(69, 69),
            ),
        )
        out = report_writers.entry_to_jsonl_dict(entry, file="x.txt", norad_id=None)
        assert isinstance(out["source_lines"], list)
        assert isinstance(out["column_range"], list)

    def test_none_fields_stay_none(self):
        # Diagnostic fields that are absent (column_range, observed, expected)
        # render as JSON null. note coerces "" -> null.
        entry = report.QuarantineEntry(
            [b"x"],
            [1],
            diagnostic(RuleID.BAD_PREFIX, source_line_nos=(1,)),
        )
        out = report_writers.entry_to_jsonl_dict(entry, file="x.txt", norad_id=None)
        assert out["column_range"] is None
        assert out["observed"] is None
        assert out["expected"] is None
        assert out["note"] is None

    def test_norad_id_null_when_unreadable(self):
        entry = report.QuarantineEntry(
            [b"2 something"],
            [42],
            diagnostic(RuleID.ORPHAN_LINE, source_line_nos=(42,)),
            norad_id=None,
        )
        out = report_writers.entry_to_jsonl_dict(entry, file="x.txt", norad_id=None)
        assert out["norad_id"] is None


class TestReportJsonlSchemaLock:
    """The schema contract for ``report.jsonl`` (issue #9 spec §8.6 / §5).
    Failures here force a spec revision: removing or renaming pinned
    fields requires bumping ``schema_version``. Adding new optional
    fields is non-breaking and should NOT fail these tests.
    """

    def _entry(self, src=10, norad_id=None):
        return report.QuarantineEntry(
            raw_lines=[b"1 x"],
            source_lines=[src],
            primary=diagnostic(RuleID.CHECKSUM_MISMATCH, source_line_nos=(src,)),
            norad_id=norad_id,
        )

    def test_schema_version_is_pinned(self, tmp_path):
        # Every line of a synthesized report.jsonl carries schema_version="1".
        path = str(tmp_path / "x.findings.jsonl")
        with report_writers.JsonlFindingsWriter(path, src_name="x.txt") as writer:
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
        with report_writers.JsonlFindingsWriter(path, src_name="x.txt") as writer:
            writer.write_entry(self._entry())
            writer.finalize()
        with open(path, encoding="utf-8") as handle:
            parsed = json.loads(handle.readline())
            assert parsed["outcome"] == "quarantined"

    def test_envelope_field_set_is_locked(self):
        # The exact set of top-level keys is the spec contract; both
        # accidental additions and accidental removals fail here.
        entry = self._entry()
        out = report_writers.entry_to_jsonl_dict(
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
        stats.quarantine_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.BAD_PREFIX: [
                    report.QuarantineEntry(
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

        report_writers.write_broken_file(str(out), "tle2099.txt", stats)

        text = out.read_bytes()
        assert b"# source: tle2099.txt" in text
        # Denominator is paired_records + orphan_entries — what the file's
        # quarantine count is measured against. With 0 orphans here, that
        # equals paired_records (5).
        assert b"1 quarantined of 5 entries" in text
        assert b"source line 42" in text
        assert b"rule: TLE-PAIR-002" in text  # BAD_PREFIX
        assert b"1 garbage" in text

    def test_non_ascii_source_name_does_not_crash_header(self, tmp_path):
        # A non-ASCII source filename must not raise UnicodeEncodeError at
        # finalize (which would fail the whole file after all its work). The
        # header encodes with errors="replace", matching the body renderer.
        stats = report.FileStats(src_name="tlé.txt")
        stats.paired_records = 3
        stats.quarantined_count = 1
        stats.quarantine_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.BAD_PREFIX: [
                    report.QuarantineEntry(
                        raw_lines=[b"1 garbage"],
                        source_lines=[1],
                        primary=_diag(RuleID.BAD_PREFIX, src=1),
                    )
                ]
            },
        )
        out = tmp_path / "out.broken.txt"

        report_writers.write_broken_file(str(out), "tlé.txt", stats)

        assert b"# source: tl?.txt" in out.read_bytes()

    def test_broken_file_is_byte_faithful(self, tmp_path):
        # A line quarantined for a non-ASCII byte must appear verbatim.
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.quarantine_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.NON_ASCII_BYTE: [
                    report.QuarantineEntry(
                        raw_lines=[b"1 \xff\xfe non-ascii"],
                        source_lines=[7],
                        primary=_diag(RuleID.NON_ASCII_BYTE, src=7),
                    )
                ]
            },
        )
        out = tmp_path / "x.broken.txt"

        report_writers.write_broken_file(str(out), "x.txt", stats)

        assert b"\xff\xfe" in out.read_bytes()

    def test_two_line_record_location(self, tmp_path):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.quarantine_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.CHECKSUM_MISMATCH: [
                    report.QuarantineEntry(
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

        report_writers.write_broken_file(str(out), "x.txt", stats)

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
        stats.quarantine_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={
                RuleID.CHECKSUM_MISMATCH: [
                    report.QuarantineEntry(
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
        report_writers.write_broken_file(str(out), "x.txt", stats)
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
                report.QuarantineEntry(
                    raw_lines=[f"row-{s}".encode("ascii")],
                    source_lines=[s],
                    primary=_diag(rule, src=s),
                )
                for s in srcs
            ]
        stats.quarantine_sample = report.FileSample.from_bounded(
            cap=5, entries_by_rule=buckets
        )

        out = tmp_path / "x.broken.txt"
        report_writers.write_broken_file(str(out), "x.txt", stats)

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
                report.QuarantineEntry(
                    raw_lines=[f"row-{s}".encode("ascii")],
                    source_lines=[s],
                    primary=_diag(rule, src=s),
                )
                for s in srcs
            ]
        stats.quarantine_sample = report.FileSample.from_bounded(
            cap=5, entries_by_rule=buckets
        )

        out = tmp_path / "x.broken.txt"
        report_writers.write_broken_file(str(out), "x.txt", stats)
        text = out.read_text("ascii")

        # Order of appearance must follow source_lines, not dict insertion
        # order or rule grouping.
        positions = [text.index(f"row-{s}") for s in (10, 20, 30, 40, 50, 60)]
        assert positions == sorted(positions)


class TestBrokenNoradIdsNdjson:
    def test_format_emits_empty_when_nothing_quarantined(self):
        # NDJSON has no header; an empty corpus produces an empty file,
        # not a blank line — consumers reading line-by-line just see zero
        # records, which is the same shape as a non-quarantine run.
        stats = report.FileStats(
            src_name="tle2099.txt", quarantined_norad_ids=report.NoradTracker(counts={})
        )
        out = report_writers.format_broken_noradids_ndjson([stats])
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
        out = report_writers.format_broken_noradids_ndjson([a, b])
        assert out == '{"noradId":5}\n{"noradId":42}\n{"noradId":26125}\n'

    def test_format_uses_compact_json(self):
        # Compact separators (no space after the colon) keep the wire
        # format tight — kilobytes matter less than predictability for
        # downstream diffs and byte-exact CI assertions.
        stats = report.FileStats(
            src_name="x.txt", quarantined_norad_ids=report.NoradTracker(counts={5: {}})
        )
        assert (
            report_writers.format_broken_noradids_ndjson([stats]) == '{"noradId":5}\n'
        )

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
        out = report_writers.format_broken_noradids_ndjson([a, b])
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
        out = report_writers.format_broken_noradids_ndjson([stats])
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
        report_writers.write_broken_noradids_ndjson(str(out), [stats])
        assert out.read_bytes() == b'{"noradId":1}\n{"noradId":2}\n'

    def test_write_emits_empty_file_when_nothing_quarantined(self, tmp_path):
        stats = report.FileStats(
            src_name="x.txt", quarantined_norad_ids=report.NoradTracker(counts={})
        )
        out = tmp_path / "broken-noradids.ndjson"
        report_writers.write_broken_noradids_ndjson(str(out), [stats])
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
        assert report_writers.aggregate_broken_norad_ids([a, b]) == [1, 2, 3]


class TestConcatFindingsShards:
    """End-of-run concatenation of per-worker findings shards into the
    corpus-wide ``report.jsonl`` (issue #9, spec §4.6).
    """

    def _make_shard(self, shard_dir, stem_name, payload_lines):
        path = shard_dir / f"{stem_name}.findings.jsonl"
        body = "".join(line + "\n" for line in payload_lines)
        path.write_text(body, encoding="utf-8")
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
        report_writers.concat_findings_shards(str(tmp_path), str(dest), all_stats)
        lines = dest.read_text(encoding="utf-8").splitlines()
        files = [json.loads(line)["file"] for line in lines]
        assert files == ["tle2004.txt", "tle2013.txt", "tle2022.txt"]

    def test_concat_creates_empty_file_when_no_shards(self, tmp_path):
        # Empty .shards/ and empty all_stats -> empty report.jsonl
        # (matches broken-noradids.ndjson's zero-quarantine contract).
        (tmp_path / ".shards").mkdir()
        dest = tmp_path / "report.jsonl"
        report_writers.concat_findings_shards(str(tmp_path), str(dest), [])
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
        report_writers.concat_findings_shards(str(tmp_path), str(dest), all_stats)
        lines = dest.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["file"] == "tle2022.txt"

    def test_concat_preserves_shard_directory(self, tmp_path):
        # concat only READS shards — it must NOT remove .shards (issue #56).
        # Shard cleanup is tied to the resume-checkpoint lifecycle in cli.py so
        # an interrupted/failed run keeps its shards for a later --resume to
        # rebuild a complete report.jsonl. The shard survives the concat.
        shard_dir = tmp_path / ".shards"
        shard_dir.mkdir()
        self._make_shard(shard_dir, "tle2022", ['{"file":"tle2022.txt"}'])
        report_writers.concat_findings_shards(
            str(tmp_path),
            str(tmp_path / "report.jsonl"),
            [report.FileStats(src_name="tle2022.txt")],
        )
        assert shard_dir.exists()
        assert (shard_dir / "tle2022.findings.jsonl").exists()

    def test_concat_atomic_rename(self, tmp_path):
        # The destination is written via .partial + os.replace, so no
        # .partial is left after success.
        shard_dir = tmp_path / ".shards"
        shard_dir.mkdir()
        self._make_shard(shard_dir, "tle2022", ['{"file":"tle2022.txt"}'])
        dest = tmp_path / "report.jsonl"
        report_writers.concat_findings_shards(
            str(tmp_path),
            str(dest),
            [report.FileStats(src_name="tle2022.txt")],
        )
        assert dest.exists()
        assert not (tmp_path / "report.jsonl.partial").exists()

    def test_concat_failure_preserves_prior_report_jsonl(self, tmp_path, monkeypatch):
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
            report_writers.concat_findings_shards(
                str(tmp_path),
                str(dest),
                [report.FileStats(src_name="tle2022.txt")],
            )
        # Prior content is untouched.
        assert dest.read_text(encoding="utf-8") == "from-prior-run\n"


class TestJsonlFindingsWriter:
    """The streaming writer for one file's findings shard (issue #9,
    spec §4.3). Mirrors ``BrokenFileWriter``'s lifecycle pattern but
    emits one JSON object per line with explicit UTF-8 / LF / sort_keys
    discipline.
    """

    def _entry(self, src=10, rule=RuleID.CHECKSUM_MISMATCH, norad_id=None):
        return report.QuarantineEntry(
            raw_lines=[b"1 x"],
            source_lines=[src],
            primary=diagnostic(rule, source_line_nos=(src,)),
            norad_id=norad_id,
        )

    def test_writes_one_line_per_entry(self, tmp_path):
        path = str(tmp_path / "tle2022.findings.jsonl")
        with report_writers.JsonlFindingsWriter(path, src_name="tle2022.txt") as writer:
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
        with report_writers.JsonlFindingsWriter(path, src_name="x.txt") as writer:
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
        with report_writers.JsonlFindingsWriter(path, src_name="x.txt") as writer:
            writer.write_entry(self._entry())
            writer.finalize()
        assert os.path.exists(path)
        assert not os.path.exists(path + ".partial")

    def test_interrupted_run_leaves_no_partial(self, tmp_path):
        # Context-manager exit without finalize unlinks the .partial.
        path = str(tmp_path / "x.findings.jsonl")
        with report_writers.JsonlFindingsWriter(path, src_name="x.txt") as writer:
            writer.write_entry(self._entry())
            # exit without finalize
        assert not os.path.exists(path)
        assert not os.path.exists(path + ".partial")

    def test_empty_finalize_creates_empty_file(self, tmp_path):
        path = str(tmp_path / "x.findings.jsonl")
        with report_writers.JsonlFindingsWriter(path, src_name="x.txt") as writer:
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
            with report_writers.JsonlFindingsWriter(path, src_name="x.txt") as writer:
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

        with report_writers.JsonlFindingsWriter(path, src_name="x.txt") as writer:
            writer.write_entry(self._entry())
            monkeypatch.setattr("os.replace", boom)
            with pytest.raises(OSError, match="simulated rename failure"):
                writer.finalize()
        assert not os.path.exists(path)
        # The .partial does NOT survive the context-manager exit because
        # _completed was never set to True (finalize raised before the
        # line that sets it) so __exit__ unlinks it.
        assert not os.path.exists(path + ".partial")


class TestQuarantineSink:
    """The single-mutation entry point that enforces the per-rule cap
    by construction (issue #19). Owns ``BrokenFileWriter`` in clean mode;
    skips it in validate mode; on ``finalize`` hands out an immutable
    :class:`FileSample`.
    """

    def _stub(self, src, rule=RuleID.CHECKSUM_MISMATCH):
        """One minimal QuarantineEntry for cap-bound tests."""
        return report.QuarantineEntry(
            raw_lines=[f"1 stub-{src}".encode("ascii")],
            source_lines=[src],
            primary=_diag(rule, src=src),
        )

    def test_add_under_cap_accepts(self):
        # Three entries, one rule — all three survive to the sample.
        sink = report_writers.QuarantineSink(cap=5)
        for i in range(3):
            sink.add(self._stub(i))
        sample = sink.finalize(entries=3)
        assert len(sample.buckets[RuleID.CHECKSUM_MISMATCH]) == 3

    def test_add_over_cap_silently_drops(self):
        # Six entries, cap of 5 — the 6th drops silently. Matches today's
        # pipeline._record_quarantine behaviour; quarantine_counts retains the truth
        # so no information is lost at the operator level.
        sink = report_writers.QuarantineSink(cap=5)
        for i in range(6):
            sink.add(self._stub(i))  # must not raise
        sample = sink.finalize(entries=6)
        assert len(sample.buckets[RuleID.CHECKSUM_MISMATCH]) == 5

    def test_cap_holds_under_skew(self):
        # 1000 of one rule, then 1 of another. With per-rule buckets, the
        # noisy rule cannot crowd the rare rule out of the sample.
        sink = report_writers.QuarantineSink(cap=5)
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

        rng = random.Random(42)
        rules = list(RuleID)
        sink = report_writers.QuarantineSink(cap=5)
        for i in range(1000):
            sink.add(self._stub(i, rng.choice(rules)))
        sample = sink.finalize(entries=1000)
        for bucket in sample.buckets.values():
            assert len(bucket) <= 5

    def test_finalize_returns_filesample_with_matching_cap(self):
        # The cap travels with the sample so renderers can show truncation.
        sink = report_writers.QuarantineSink(cap=5)
        sample = sink.finalize(entries=0)
        assert sample.cap == 5

    def test_validate_mode_skips_writer(self, tmp_path):
        # No broken_path -> sink is purely in-memory; no temp file leakage.
        sink = report_writers.QuarantineSink(cap=5)  # no broken_path
        with sink:
            sink.add(self._stub(1))
            sink.finalize(entries=1)
        # The parent dir should have no partials touched by the sink.
        assert list(tmp_path.iterdir()) == []

    def test_clean_mode_writes_byte_faithful_sidecar(self, tmp_path):
        # Each added entry's _render_entry bytes appear verbatim in the
        # finalized file; the header preamble names the source and the
        # quarantine count. Matches the existing TestStreamingQuarantines
        # assertion pattern (substring checks; the header timestamp is
        # volatile so we don't compare full bytes).
        path = tmp_path / "x.broken.txt"
        entries = [self._stub(i) for i in range(3)]
        sink = report_writers.QuarantineSink(
            broken_path=str(path), src_name="x.txt", cap=5
        )
        with sink:
            for entry in entries:
                sink.add(entry)
            sink.finalize(entries=3)
        body = path.read_bytes()
        assert b"# source: x.txt" in body
        assert b"# 3 quarantined of 3 entries" in body
        for idx, entry in enumerate(entries, start=1):
            assert report_writers._render_entry(idx, entry) in body

    def test_exit_without_finalize_cleans_partials(self, tmp_path):
        # An exception inside the `with` block leaves no debris. The
        # writer's __exit__ discards body + final partials when finalize
        # was not reached.
        path = tmp_path / "x.broken.txt"
        with (
            pytest.raises(RuntimeError, match="simulated"),
            report_writers.QuarantineSink(
                broken_path=str(path), src_name="x.txt", cap=5
            ) as sink,
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
        sink = report_writers.QuarantineSink(cap=5)
        sink.finalize(entries=0)
        with pytest.raises(RuntimeError, match="already finalized"):
            sink.add(self._stub(1))

    def test_dropped_count_zero_when_under_cap(self):
        # Three entries under a cap of 5 — no drops, dropped_count stays
        # empty so renderers (issue #46) can omit the "K dropped" hint.
        sink = report_writers.QuarantineSink(cap=5)
        for i in range(3):
            sink.add(self._stub(i))
        sample = sink.finalize(entries=3)
        assert sample.dropped_count == {}

    def test_dropped_count_increments_per_drop(self):
        # Seven entries one rule, cap of 5 — two drops accrue to that
        # rule's slot in the finalized sample.
        sink = report_writers.QuarantineSink(cap=5)
        for i in range(7):
            sink.add(self._stub(i))
        sample = sink.finalize(entries=7)
        assert sample.dropped_count[RuleID.CHECKSUM_MISMATCH] == 2

    def test_dropped_count_per_rule_independent(self):
        # Mixed traffic: one rule overflows, another stays under cap.
        # Drops are accounted per-rule so the operator can tell which
        # rule lost evidence and which did not.
        sink = report_writers.QuarantineSink(cap=5)
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
        import random

        rng = random.Random(42)
        rules = list(RuleID)
        seen = collections.Counter()
        sink = report_writers.QuarantineSink(cap=5)
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
        with report_writers.QuarantineSink(
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
        with report_writers.QuarantineSink(cap=5) as sink:
            sink.add(self._stub(1))
            sink.finalize(entries=1)
        assert os.listdir(tmp_path) == []

    def test_jsonl_path_requires_src_name(self, tmp_path):
        # Mirrors broken_path's contract: src_name is required because
        # the JSONL writer needs the per-file ``file`` field value.
        jsonl_path = str(tmp_path / "x.findings.jsonl")
        with pytest.raises(ValueError, match="src_name"):
            report_writers.QuarantineSink(cap=5, jsonl_path=jsonl_path)

    def test_dropped_from_sample_still_in_jsonl(self, tmp_path):
        # Cap governs the in-memory sample, NOT the on-disk JSONL.
        # 10 entries with cap 3 -> sample has 3, JSONL has all 10.
        jsonl_path = str(tmp_path / "tle.findings.jsonl")
        with report_writers.QuarantineSink(
            cap=3, jsonl_path=jsonl_path, src_name="tle.txt"
        ) as sink:
            for i in range(10):
                sink.add(self._stub(i))
            sample = sink.finalize(entries=10)
        assert len(sample.buckets[RuleID.CHECKSUM_MISMATCH]) == 3
        with open(jsonl_path, encoding="utf-8") as handle:
            lines = handle.readlines()
        assert len(lines) == 10
