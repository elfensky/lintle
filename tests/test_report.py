"""Tests for lintle.report — statistics, the quarantine sidecar, and summaries."""

import dataclasses
import json
import os
import re

import pytest

from lintle import report, report_aggregation
from lintle.categories import FixClass
from lintle.diagnostics import RuleID, diagnostic


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
    stats.quarantine_counts = {RuleID.CHECKSUM_MISMATCH: 2}
    return stats


def _two_file_stats():
    a = report.FileStats(src_name="tle2004.txt")
    a.paired_records = 1000
    a.orphan_entries = 0
    a.input_lines_seen = 2000
    a.clean_count = 990
    a.quarantined_count = 10
    a.fix_counts = {FixClass.TRAILING_BACKSLASH: 990}
    a.quarantine_counts = {RuleID.CHECKSUM_MISMATCH: 10}
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


class TestQuarantineEntryConstructorContract:
    """Lock the QuarantineEntry field order so pipeline._record_quarantine's positional
    construction (pipeline.py:299) stays correct. norad_id MUST be the trailing
    field — see issue #9 spec §4.5.
    """

    def test_existing_keyword_construction_unchanged(self):
        # Locks the default-value contract for the 18 existing test-fixture
        # call sites that omit norad_id.
        entry = report.QuarantineEntry(
            raw_lines=[b"1 garbage"],
            source_lines=[42],
            primary=_diag(RuleID.BAD_PREFIX, src=42),
        )
        assert entry.norad_id is None
        assert entry.related == ()

    def test_positional_construction_pins_field_order(self):
        # Locks the (raw_lines, source_lines, primary, related) positional
        # contract used by pipeline._record_quarantine.
        primary = _diag(RuleID.CHECKSUM_MISMATCH, src=10)
        related = (_diag(RuleID.LINE_LENGTH, src=10),)
        entry = report.QuarantineEntry([b"1 x"], [10], primary, related)
        assert entry.raw_lines == [b"1 x"]
        assert entry.source_lines == [10]
        assert entry.primary is primary
        assert entry.related is related
        assert entry.norad_id is None  # appended trailing default

    def test_norad_id_must_be_keyword_to_avoid_corruption(self):
        # Documents the construction pattern that pipeline._record_quarantine
        # MUST use after adding norad_id to QuarantineEntry.
        primary = _diag(RuleID.CHECKSUM_MISMATCH, src=10)
        entry = report.QuarantineEntry([b"1 x"], [10], primary, (), norad_id=12345)
        assert entry.norad_id == 12345


class TestSummaries:
    def test_summary_dict_is_json_friendly(self):
        data = report.summary_dict(_stats_with_counts())
        assert data["src_name"] == "tle2022.txt"
        assert data["paired_records"] == 100
        assert data["orphan_entries"] == 0
        assert data["input_lines_seen"] == 200
        assert data["fix_counts"]["trailing-backslash"] == 50
        # quarantine_counts is keyed by stable rule IDs — TLE-CHK-001, not the
        # old free-form "checksum-mismatch" string.
        assert data["quarantine_counts"]["TLE-CHK-001"] == 2
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
        # the live FileStats (mirrors fix_counts / quarantine_counts).
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
        stats.quarantine_sample = report.FileSample.from_bounded(
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
        # key — same contract as fix_counts / quarantine_counts. JSON
        # consumers can rely on the field always being present.
        data = report.summary_dict(_stats_with_counts())
        assert data["dropped_counts"] == {}

    def test_summary_dict_dropped_counts_is_shallow_copy(self):
        # Mutating the returned dict must not leak back to the live
        # FileSample (mirrors fix_counts / quarantine_counts contract).
        stats = report.FileStats(src_name="x.txt")
        stats.quarantine_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={},
            dropped_count={RuleID.CHECKSUM_MISMATCH: 42},
        )
        data = report.summary_dict(stats)
        data["dropped_counts"].pop(RuleID.CHECKSUM_MISMATCH)
        assert RuleID.CHECKSUM_MISMATCH in stats.quarantine_sample.dropped_count

    def test_summary_dict_key_set_pins_envelope_per_file_contract(self):
        # Issue #20 spec: the exact set of keys returned by summary_dict
        # is the per-file portion of the v1 envelope contract — both
        # `lintle ... --report json`'s `files[i]` payload and any
        # programmatic consumer pin against this key set. Issue #9's
        # defensive bound (no per-finding fields leak in) still holds:
        # nothing from the per-record .jsonl stream appears here.
        data = report.summary_dict(_stats_with_counts())
        expected = {
            "src_name",
            "elapsed_seconds",
            "bytes",
            "records_per_sec",
            "paired_records",
            "orphan_entries",
            "input_lines_seen",
            "clean_count",
            "quarantined_count",
            "fix_counts",
            "quarantine_counts",
            "dropped_counts",
            "quarantined_norad_ids",
        }
        assert set(data.keys()) == expected


class TestStatsFromSummary:
    """`stats_from_summary` is the inverse of `summary_dict` — it rebuilds a
    `FileStats` from a JSON-deserialised summary so a resumed run (#56) can
    include files completed in an earlier session in the final report without
    re-reading them. The `quarantine_sample` exemplars (raw bytes) are not stored,
    so they reconstruct empty; every counter and tally must round-trip exactly.
    """

    def test_round_trips_through_summary_dict(self):
        original = _stats_with_counts()
        original.elapsed_seconds = 12.5
        original.bytes = 4096
        original.quarantined_norad_ids = report.NoradTracker(
            counts={25544: {RuleID.CHECKSUM_MISMATCH: 2, RuleID.NON_ASCII_BYTE: 1}}
        )
        original.quarantine_sample = report.FileSample.from_bounded(
            cap=5, entries_by_rule={}, dropped_count={RuleID.CHECKSUM_MISMATCH: 3}
        )
        # Through a real JSON round-trip, exactly as the checkpoint persists it.
        serialised = json.loads(json.dumps(report.summary_dict(original)))
        restored = report.stats_from_summary(serialised)
        assert report.summary_dict(restored) == report.summary_dict(original)

    def test_coerces_keys_back_to_their_live_types(self):
        # JSON stringifies all keys; reconstruction must restore int NORAD ids,
        # RuleID rule keys, and FixClass fix keys so the rebuilt FileStats is
        # indistinguishable from a freshly-produced one.
        serialised = json.loads(json.dumps(report.summary_dict(_stats_with_counts())))
        restored = report.stats_from_summary(serialised)
        assert isinstance(restored, report.FileStats)
        assert FixClass.TRAILING_BACKSLASH in restored.fix_counts
        assert restored.fix_counts[FixClass.TRAILING_BACKSLASH] == 50
        assert restored.quarantine_counts[RuleID.CHECKSUM_MISMATCH] == 2

    def test_quarantine_sample_reconstructs_without_exemplars(self):
        restored = report.stats_from_summary(
            json.loads(json.dumps(report.summary_dict(_stats_with_counts())))
        )
        # No raw-byte exemplars are stored; the sample comes back empty of
        # buckets but is a valid FileSample renderers can consume.
        assert restored.quarantine_sample.buckets == {}


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
        # _two_file_stats does not set quarantine_sample so every rule's
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
        a.quarantine_counts = {RuleID.CHECKSUM_MISMATCH: 1000}
        a.quarantine_sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={},
            dropped_count={RuleID.CHECKSUM_MISMATCH: 995},
        )
        b = report.FileStats(src_name="tle-b.txt")
        b.paired_records = 500
        b.clean_count = 0
        b.quarantined_count = 500
        b.quarantine_counts = {RuleID.CHECKSUM_MISMATCH: 500}
        b.quarantine_sample = report.FileSample.from_bounded(
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
        a.quarantine_counts = {
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


class TestFileSample:
    """The immutable per-file bounded sample (issue #19 refactor)."""

    def _stub_entries(self, count, rule=RuleID.CHECKSUM_MISMATCH):
        """Build N minimal QuarantineEntry stubs for cap-bound tests."""
        return [
            report.QuarantineEntry(
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
        # The sentinel for files with no quarantines — no None-checks needed
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
        # No quarantines → no drops. The sentinel must initialise the per-rule
        # drop counter cleanly so renderers and aggregators do not need to
        # special-case the empty case.
        sample = report.FileSample.empty(cap=5)
        assert sample.dropped_count == {}

    def test_from_bounded_default_dropped_count_is_empty(self):
        # When the caller does not pass dropped_count, the field defaults
        # to an empty dict — matches how existing TestWriteBrokenFile and
        # TestFormatQuarantineLines fixtures invoke from_bounded (issue #46
        # backwards compat).
        sample = report.FileSample.from_bounded(
            cap=5,
            entries_by_rule={RuleID.CHECKSUM_MISMATCH: self._stub_entries(2)},
        )
        assert sample.dropped_count == {}

    def test_from_bounded_round_trips_dropped_count(self):
        # The drop counter passes through and is keyed by RuleID so
        # programmatic consumers can join it against quarantine_counts /
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


class TestFileStatsTimingFields:
    """Issue #20: FileStats carries per-file timing for the JSON envelope."""

    def test_timing_fields_default_to_zero(self):
        # Defaults keep validate-mode runs (no timing capture before the
        # pipeline change lands) from raising — they emit 0.0 / 0 and
        # consumers see the rigid type contract, not a missing key.
        stats = report.FileStats(src_name="x.txt")
        assert stats.elapsed_seconds == 0.0
        assert isinstance(stats.elapsed_seconds, float)
        assert stats.bytes == 0
        assert isinstance(stats.bytes, int)

    def test_timing_fields_accept_assignment(self):
        # The pipeline writes these after process_file completes; this
        # locks the assignment shape so a future field rename forces a
        # pipeline.py update too.
        stats = report.FileStats(src_name="tle2022.txt")
        stats.elapsed_seconds = 1.5
        stats.bytes = 12_345
        assert stats.elapsed_seconds == 1.5
        assert stats.bytes == 12_345


class TestSummaryDictTimingFields:
    """Issue #20: summary_dict surfaces timing + throughput per file."""

    def test_summary_dict_carries_timing(self):
        stats = _stats_with_counts()
        stats.elapsed_seconds = 2.0
        stats.bytes = 1_000_000
        data = report.summary_dict(stats)
        assert data["elapsed_seconds"] == 2.0
        assert data["bytes"] == 1_000_000

    def test_records_per_sec_basic(self):
        # 100 paired records over 2s = 50 r/s.
        stats = _stats_with_counts()
        stats.elapsed_seconds = 2.0
        data = report.summary_dict(stats)
        assert data["records_per_sec"] == 50.0

    def test_records_per_sec_clamp_floors_denominator(self):
        # Gate R2 (blocking): records_per_sec MUST be float, never null.
        # Sub-millisecond elapsed time clamps to 0.001 so the rate stays
        # a stable upper-bound float — typed consumers never see None.
        stats = _stats_with_counts()
        stats.elapsed_seconds = 0.0
        data = report.summary_dict(stats)
        assert isinstance(data["records_per_sec"], float)
        assert data["records_per_sec"] == 100 / 0.001  # 100_000.0

    def test_records_per_sec_zero_records_returns_zero(self):
        # Zero paired_records over any duration is 0.0 r/s — a real
        # number, not a null. Empty corpora are valid.
        stats = report.FileStats(src_name="x.txt")
        stats.elapsed_seconds = 1.0
        data = report.summary_dict(stats)
        assert data["records_per_sec"] == 0.0
        assert isinstance(data["records_per_sec"], float)

    def test_records_per_sec_never_null(self):
        # Scan every plausible degenerate combo; none should produce
        # JSON null. The contract is a single, stable float.
        for elapsed in (0.0, 1e-9, 0.0005, 0.001, 1.0, 60.0):
            for paired in (0, 1, 1_000_000):
                stats = report.FileStats(src_name="x.txt")
                stats.paired_records = paired
                stats.elapsed_seconds = elapsed
                data = report.summary_dict(stats)
                assert data["records_per_sec"] is not None
                assert isinstance(data["records_per_sec"], float)


class TestBuildRunEnvelope:
    """Issue #20: the top-level versioned envelope wraps run + summary + files.

    These tests lock the schema contract a consumer pins against — adding,
    removing, or renaming any envelope key without updating the spec doc
    must trip one of these tests.
    """

    def _envelope(self, **overrides):
        defaults = {
            "all_stats": _two_file_stats(),
            "command": "validate",
            "started_at": "2026-05-25T13:00:00Z",
            "elapsed_seconds": 1.25,
        }
        defaults.update(overrides)
        return report.build_run_envelope(**defaults)

    def test_top_level_keys_pinned(self):
        env = self._envelope()
        assert set(env.keys()) == {
            "schema_version",
            "run",
            "environment",
            "summary",
            "files",
        }

    def test_schema_version_is_string_three(self):
        # String, not int — leaves room for "3.1" tags in additive
        # minor revisions without changing the field's JSON type.
        # Bumped "2" -> "3" when run.failed_files + summary.failed_count
        # were added (issue #83).
        env = self._envelope()
        assert env["schema_version"] == "3"
        assert isinstance(env["schema_version"], str)

    def test_run_block_shape(self):
        env = self._envelope()
        run = env["run"]
        assert set(run.keys()) == {
            "command",
            "timestamp",
            "elapsed_seconds",
            "failed_files",
        }
        assert run["command"] == "validate"
        assert run["timestamp"] == "2026-05-25T13:00:00Z"
        assert run["elapsed_seconds"] == 1.25
        assert isinstance(run["elapsed_seconds"], float)

    def test_environment_block_strict_allowlist(self):
        # Privacy: the environment block carries ONLY tool + Python
        # version. No env vars, no paths, no hostnames. Locking the
        # key set is how we keep a future contributor from sneaking
        # in a `cwd` or `user` field. ``tool_version`` is also format-
        # checked here (rather than only type-checked) so a regression
        # where ``__version__`` resolves to an empty/garbled string
        # surfaces independently of the golden-fixture test (which
        # patches ``tool_version`` from the live value and so cannot
        # catch a self-consistent regression).
        env = self._envelope()
        envblock = env["environment"]
        assert set(envblock.keys()) == {"tool_version", "python_version"}
        assert isinstance(envblock["tool_version"], str)
        # Match the dotted-numeric prefix lintle's __version__ always
        # carries — guards against ``""`` / ``"unknown"`` / garbled
        # outputs from importlib.metadata. PEP 440 admits suffixes
        # like ``.post1`` / ``-rc1`` so the prefix-anchored shape is
        # the right level of strictness.
        assert re.match(r"^\d+\.\d+\.\d+", envblock["tool_version"]), envblock[
            "tool_version"
        ]
        # python_version is the running interpreter, dotted major.minor.micro.
        import sys

        expected = (
            f"{sys.version_info.major}.{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        )
        assert envblock["python_version"] == expected

    def test_summary_block_shape(self):
        env = self._envelope()
        summary = env["summary"]
        assert set(summary.keys()) == {
            "files_processed",
            "paired_records",
            "orphan_entries",
            "input_lines_seen",
            "clean_count",
            "quarantined_count",
            "fix_counts",
            "quarantine_counts",
            "failed_count",
        }
        # _two_file_stats: 1000 + 3000 paired = 4000 records, 10 quarantined.
        assert summary["files_processed"] == 2
        assert summary["paired_records"] == 4000
        assert summary["clean_count"] == 3990
        assert summary["quarantined_count"] == 10

    def test_summary_aggregates_match_aggregate_helper(self):
        # The summary block IS the corpus-wide aggregate; if these
        # diverge from report_aggregation.aggregate() then two surfaces of the
        # same data have drifted, which the issue explicitly warns against.
        stats_list = _two_file_stats()
        env = self._envelope(all_stats=stats_list)
        totals = report_aggregation.aggregate(stats_list)
        assert env["summary"]["paired_records"] == totals.paired
        assert env["summary"]["orphan_entries"] == totals.orphans
        assert env["summary"]["input_lines_seen"] == totals.lines_seen
        assert env["summary"]["clean_count"] == totals.clean
        assert env["summary"]["quarantined_count"] == totals.quarantined
        # fix_counts / quarantine_counts use StrEnum keys that serialize to
        # their stable wire tokens once JSON-encoded.
        assert (
            env["summary"]["fix_counts"]["trailing-backslash"]
            == totals.fixes[FixClass.TRAILING_BACKSLASH]
        )

    def test_files_array_preserves_summary_dict_shape(self):
        # The per-file entries in the envelope are exactly summary_dict
        # output, in the order of `all_stats`. Reusing summary_dict
        # keeps one canonical per-file shape — drift between the
        # envelope's `files[i]` and stand-alone `summary_dict()` would
        # break consumers that already pin against either.
        stats_list = _two_file_stats()
        env = self._envelope(all_stats=stats_list)
        assert env["files"] == [report.summary_dict(s) for s in stats_list]

    def test_empty_corpus_renders_zero_summary(self):
        env = self._envelope(all_stats=[])
        assert env["files"] == []
        assert env["summary"]["files_processed"] == 0
        assert env["summary"]["paired_records"] == 0
        assert env["summary"]["fix_counts"] == {}
        assert env["summary"]["quarantine_counts"] == {}

    def test_full_envelope_is_json_serialisable(self):
        # The whole envelope must round-trip through json.dumps without
        # raising and without losing structure — this is what cli.py
        # actually prints. Locks the StrEnum-key serialisation contract
        # at the envelope boundary, not just the inner per-file shape.
        env = self._envelope()
        encoded = json.dumps(env)
        decoded = json.loads(encoded)
        assert decoded["schema_version"] == "3"
        assert decoded["run"]["command"] == "validate"
        assert decoded["summary"]["paired_records"] == 4000
        # StrEnum keys serialise as their stable wire tokens.
        assert decoded["summary"]["fix_counts"]["trailing-backslash"] == 1990

    def test_envelope_uses_basename_only_for_src_name(self):
        # Privacy: no absolute paths leak through. FileStats.src_name
        # is already a basename — this test pins that the envelope
        # builder does not regress by accidentally substituting a path.
        stats = report.FileStats(src_name="tle2022.txt")
        env = self._envelope(all_stats=[stats])
        assert env["files"][0]["src_name"] == "tle2022.txt"
        assert "/" not in env["files"][0]["src_name"]


class TestEnvelopeGoldenFixture:
    """Gate R7: a checked-in JSON fixture locks the wire format.

    The fixture (``tests/fixtures/report-envelope-v3.golden.json``) is
    the contract a downstream consumer can copy verbatim and parse —
    any accidental drift in field names, types, or ordering shows up
    as a diff in the test output.
    """

    def test_envelope_matches_golden_fixture(self):
        fixture_path = os.path.join(
            os.path.dirname(__file__), "fixtures", "report-envelope-v3.golden.json"
        )
        with open(fixture_path, encoding="utf-8") as handle:
            golden = json.load(handle)

        # Build an envelope whose inputs are entirely deterministic.
        stats = report.FileStats(src_name="tle2099.txt")
        stats.paired_records = 10
        stats.orphan_entries = 0
        stats.input_lines_seen = 20
        stats.clean_count = 9
        stats.quarantined_count = 1
        stats.fix_counts = {FixClass.TRAILING_BACKSLASH: 9}
        stats.quarantine_counts = {RuleID.CHECKSUM_MISMATCH: 1}
        stats.elapsed_seconds = 0.5
        stats.bytes = 2_048
        env = report.build_run_envelope(
            [stats],
            command="validate",
            started_at="2026-05-25T13:00:00Z",
            elapsed_seconds=0.75,
        )

        # The Python version field comes from the running interpreter, so
        # we patch the golden's expectation to the current value before
        # comparing — keeps the fixture stable across local Python patches
        # without weakening the schema lock.
        import sys

        golden["environment"]["python_version"] = (
            f"{sys.version_info.major}.{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        )
        golden["environment"]["tool_version"] = env["environment"]["tool_version"]

        # Round-trip through JSON so dict order, StrEnum coercion, and
        # int-key stringification match what cli.py actually prints.
        encoded = json.loads(json.dumps(env))
        assert encoded == golden


class TestEnvelopeBreakingChange:
    """Gate R3: the v1 envelope is a clean break from the legacy flat array.

    This test documents the breaking change in executable form — anyone
    grepping for the old shape in tests sees the assertion that calls
    out the migration.
    """

    def test_envelope_is_object_not_array(self):
        # Legacy --report json emitted a flat array of summary_dict
        # entries. v1 emits a top-level object. Consumers that did
        # `payload[0]` will now fail-fast at the type level, which is
        # the desired migration signal.
        env = report.build_run_envelope(
            _two_file_stats(),
            command="validate",
            started_at="2026-05-25T13:00:00Z",
            elapsed_seconds=0.1,
        )
        assert isinstance(env, dict)
        assert not isinstance(env, list)
        # And the per-file array lives under the well-known key.
        assert isinstance(env["files"], list)


class TestWriteRunJson:
    """``write_run_json`` is the byte-identical persisted twin of the
    ``--report json`` stdout output (Critical Rules #1/#2)."""

    def _envelope(self):
        stats = [_stats_with_counts()]
        return report.build_run_envelope(
            stats,
            command="clean",
            started_at="2026-05-31T00:00:00Z",
            elapsed_seconds=1.5,
        )

    def test_bytes_match_report_json_serialization(self, tmp_path):
        env = self._envelope()
        path = tmp_path / "report.json"
        report.write_run_json(str(path), env)
        # The exact bytes the `--report json` stdout path emits: cli.py prints
        # `json.dumps(envelope, indent=2)` followed by print's trailing newline.
        expected = json.dumps(env, indent=2) + "\n"
        assert path.read_text(encoding="utf-8") == expected

    def test_deterministic_for_same_logical_run(self, tmp_path):
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        report.write_run_json(str(a), self._envelope())
        report.write_run_json(str(b), self._envelope())
        assert a.read_bytes() == b.read_bytes()


class TestEnvelopeRawNumbers:
    """The machine envelope carries raw numbers, never humanized strings —
    guards against a humanize leak into byte-deterministic output."""

    def test_elapsed_and_counts_are_numbers_not_strings(self):
        env = report.build_run_envelope(
            [report.FileStats(src_name="tle.txt", elapsed_seconds=12.0)],
            command="clean",
            started_at="2026-06-07T00:00:00Z",
            elapsed_seconds=124.0,
        )
        assert isinstance(env["run"]["elapsed_seconds"], float)
        assert isinstance(env["summary"]["files_processed"], int)
        assert isinstance(env["files"][0]["elapsed_seconds"], float)


class TestFailedFilesEnvelope:
    """Issue #83: failed input files are recorded in the run envelope (schema v3)."""

    def _envelope(self, failed_files=None, **overrides):
        defaults = {
            "all_stats": _two_file_stats(),
            "command": "clean",
            "started_at": "2026-05-25T13:00:00Z",
            "elapsed_seconds": 1.25,
        }
        defaults.update(overrides)
        if failed_files is not None:
            defaults["failed_files"] = failed_files
        return report.build_run_envelope(**defaults)

    def test_schema_version_bumped_to_three(self):
        env = self._envelope()
        assert env["schema_version"] == "3"
        assert isinstance(env["schema_version"], str)

    def test_run_failed_files_present_when_empty(self):
        # Always present, even with no failures — stable shape for consumers.
        env = self._envelope()
        assert "failed_files" in env["run"]
        assert env["run"]["failed_files"] == []

    def test_summary_failed_count_present_when_zero(self):
        env = self._envelope()
        assert "failed_count" in env["summary"]
        assert env["summary"]["failed_count"] == 0

    def test_failed_files_recorded_in_run_block(self):
        failed = [("/data/source/tle2099.txt", "OSError: disk full")]
        env = self._envelope(failed_files=failed)
        assert env["run"]["failed_files"] == [
            {"file": "tle2099.txt", "error": "OSError: disk full"}
        ]

    def test_failed_count_reflects_failures(self):
        failed = [
            ("/data/source/tle2099.txt", "OSError: disk full"),
            ("/data/source/tle2100.txt", "RuntimeError: boom"),
        ]
        env = self._envelope(failed_files=failed)
        assert env["summary"]["failed_count"] == 2

    def test_failed_files_sorted_by_basename(self):
        # Byte-determinism: list is always sorted by the file key.
        failed = [
            ("/data/source/tle_z.txt", "err z"),
            ("/data/source/tle_a.txt", "err a"),
        ]
        env = self._envelope(failed_files=failed)
        files = env["run"]["failed_files"]
        assert [f["file"] for f in files] == ["tle_a.txt", "tle_z.txt"]

    def test_failed_files_default_is_empty(self):
        # Calling without failed_files= still gives empty list + 0 count.
        env = report.build_run_envelope(
            _two_file_stats(),
            command="clean",
            started_at="2026-05-25T13:00:00Z",
            elapsed_seconds=1.25,
        )
        assert env["run"]["failed_files"] == []
        assert env["summary"]["failed_count"] == 0

    def test_summary_block_shape_includes_failed_count(self):
        env = self._envelope()
        assert set(env["summary"].keys()) == {
            "files_processed",
            "paired_records",
            "orphan_entries",
            "input_lines_seen",
            "clean_count",
            "quarantined_count",
            "fix_counts",
            "quarantine_counts",
            "failed_count",
        }

    def test_run_block_shape_includes_failed_files(self):
        env = self._envelope()
        assert set(env["run"].keys()) == {
            "command",
            "timestamp",
            "elapsed_seconds",
            "failed_files",
        }

    def test_failed_file_basename_used_not_full_path(self):
        # Privacy: only basenames leak through.
        failed = [("/absolute/path/to/tle2099.txt", "err")]
        env = self._envelope(failed_files=failed)
        entry = env["run"]["failed_files"][0]
        assert entry["file"] == "tle2099.txt"
        assert "/" not in entry["file"]

    def test_report_md_includes_failures_section_when_failures(self):
        # format_run_report must produce a Failures section when there are failures.
        failed = [("/data/source/tle2099.txt", "OSError: disk full")]
        out = report.format_run_report(_two_file_stats(), failed_files=failed)
        assert "Failures" in out
        assert "tle2099.txt" in out
        assert "OSError: disk full" in out

    def test_report_md_no_failures_section_when_clean(self):
        # format_run_report must NOT produce a Failures section on a clean run.
        out = report.format_run_report(_two_file_stats())
        assert "## Failures" not in out

    def test_envelope_json_serialisable_with_failures(self):
        failed = [("/data/source/tle2099.txt", "err")]
        env = self._envelope(failed_files=failed)
        encoded = json.loads(json.dumps(env))
        assert encoded["schema_version"] == "3"
        assert encoded["run"]["failed_files"][0]["file"] == "tle2099.txt"
