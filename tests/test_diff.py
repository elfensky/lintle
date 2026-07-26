"""Tests for lintle.diff — the per-rule delta between two run outputs (issue #10).

Fixtures are built through the real producer serializer
``report_writers.entry_to_jsonl_dict`` so the test corpus tracks the actual
``report.jsonl`` wire schema rather than a hand-rolled copy that rots.
"""

import collections
import io
import json

import pytest
from rich.console import Console

from lintle import REPORT_DIRNAME, cli, diff, report, report_writers
from lintle.diagnostics import RuleID, diagnostic


def _diag(rule_id, src=1, **kwargs):
    """Build a Diagnostic with sane defaults for tests."""
    return diagnostic(rule_id, source_line_nos=(src,), **kwargs)


def _entry(primary_rule, related_rules=(), *, norad_id=25544):
    """Build a QuarantineEntry with a primary rule and optional related rules."""
    return report.QuarantineEntry(
        raw_lines=[b"1 x", b"2 x"],
        source_lines=[1, 2],
        primary=_diag(primary_rule),
        related=tuple(_diag(r) for r in related_rules),
        norad_id=norad_id,
    )


def _report_path(run_dir):
    """Return the report.00001.jsonl chunk path under ``run_dir``, creating
    the ``03-report/`` directory that holds it (the flat numbered output
    layout — ``<run>/03-report/report.NNNNN.jsonl``)."""
    path = run_dir / REPORT_DIRNAME / "report.00001.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_run(run_dir, entries, *, file="tle.txt"):
    """Write a report.jsonl into ``run_dir`` from a list of QuarantineEntry,
    serialized through the real producer renderer.
    """
    path = _report_path(run_dir)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            payload = report_writers.entry_to_jsonl_dict(
                entry, file=file, norad_id=entry.norad_id
            )
            fh.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    return run_dir


def _write_run_files(run_dir, file_rules):
    """Write a report.jsonl with one finding per ``(filename, RuleID)`` pair,
    so a single run can span multiple files."""
    path = _report_path(run_dir)
    with path.open("w", encoding="utf-8") as fh:
        for filename, rule in file_rules:
            entry = _entry(rule)
            payload = report_writers.entry_to_jsonl_dict(
                entry, file=filename, norad_id=entry.norad_id
            )
            fh.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    return run_dir


def _rule_ids(run_dir):
    """Extract just rule_ids from iter_findings — mirrors the deleted
    iter_primary_rule_ids helper; used by TestDiffReader to keep the
    streaming-reader contract tests on the production code path."""
    return [rule_id for _file, rule_id in diff.iter_findings(run_dir)]


class TestDiffReader:
    """``iter_findings`` streams ``(file, rule_id)`` for each finding,
    validates the schema version, and fails loudly on anything it cannot
    interpret. Tests use ``_rule_ids`` to extract only the rule_id column
    where the file field is irrelevant to the contract under test.
    """

    def test_yields_primary_rule_ids_in_file_order(self, tmp_path):
        run = _write_run(
            tmp_path / "run",
            [
                _entry(RuleID.CHECKSUM_MISMATCH),
                _entry(RuleID.LINE_LENGTH),
                _entry(RuleID.CHECKSUM_MISMATCH),
            ],
        )
        assert _rule_ids(str(run)) == [
            "TLE-CHK-001",
            "TLE-COL-001",
            "TLE-CHK-001",
        ]

    def test_empty_report_jsonl_yields_nothing(self, tmp_path):
        run = _write_run(tmp_path / "run", [])
        assert _rule_ids(str(run)) == []

    def test_blank_lines_are_skipped(self, tmp_path):
        run = _write_run(tmp_path / "run", [_entry(RuleID.BAD_PREFIX)])
        # Append a blank line — a stray trailing newline must not break parsing.
        _report_path(run).open("a", encoding="utf-8").write("\n")
        assert _rule_ids(str(run)) == ["TLE-PAIR-002"]

    def test_missing_report_jsonl_raises_difference_error(self, tmp_path):
        empty = tmp_path / "no-report"
        empty.mkdir()
        with pytest.raises(diff.DiffError, match="report.jsonl"):
            list(diff.iter_findings(str(empty)))

    def test_schema_version_mismatch_raises(self, tmp_path):
        run = tmp_path / "run"
        payload = report_writers.entry_to_jsonl_dict(
            _entry(RuleID.CHECKSUM_MISMATCH), file="tle.txt", norad_id=25544
        )
        payload["schema_version"] = "2"  # forge a future envelope
        _report_path(run).write_text(json.dumps(payload) + "\n")
        with pytest.raises(diff.DiffError, match="schema_version"):
            list(diff.iter_findings(str(run)))

    def test_missing_schema_version_raises(self, tmp_path):
        # A line with no schema_version at all is as untrustworthy as a wrong
        # one — it must not be silently treated as v1.
        run = tmp_path / "run"
        payload = report_writers.entry_to_jsonl_dict(
            _entry(RuleID.CHECKSUM_MISMATCH), file="tle.txt", norad_id=25544
        )
        del payload["schema_version"]
        _report_path(run).write_text(json.dumps(payload) + "\n")
        with pytest.raises(diff.DiffError, match="schema_version"):
            list(diff.iter_findings(str(run)))

    def test_finding_without_rule_id_raises(self, tmp_path):
        # A schema-valid envelope that somehow lacks a primary rule_id cannot
        # be aggregated; fail loudly rather than count a phantom None.
        run = tmp_path / "run"
        payload = report_writers.entry_to_jsonl_dict(
            _entry(RuleID.CHECKSUM_MISMATCH), file="tle.txt", norad_id=25544
        )
        del payload["rule_id"]
        _report_path(run).write_text(json.dumps(payload) + "\n")
        with pytest.raises(diff.DiffError, match="rule_id"):
            list(diff.iter_findings(str(run)))

    def test_malformed_json_line_raises(self, tmp_path):
        run = tmp_path / "run"
        _report_path(run).write_text("{not valid json\n")
        with pytest.raises(diff.DiffError):
            list(diff.iter_findings(str(run)))

    def test_schema_version_as_integer_raises(self, tmp_path):
        # The producer always writes the string "1"; an integer 1 is a foreign
        # envelope and must not be accepted by loose equality.
        run = tmp_path / "run"
        payload = report_writers.entry_to_jsonl_dict(
            _entry(RuleID.CHECKSUM_MISMATCH), file="tle.txt", norad_id=25544
        )
        payload["schema_version"] = 1  # int, not "1"
        _report_path(run).write_text(json.dumps(payload) + "\n")
        with pytest.raises(diff.DiffError, match="schema_version"):
            list(diff.iter_findings(str(run)))

    def test_report_jsonl_that_is_a_directory_raises(self, tmp_path):
        # A directory where report.jsonl should be is an operational error, not
        # a crash — open() raises IsADirectoryError, which must surface as DiffError.
        run = tmp_path / "run"
        _report_path(run).mkdir()
        with pytest.raises(diff.DiffError):
            list(diff.iter_findings(str(run)))

    def test_non_utf8_bytes_raise_diff_error(self, tmp_path):
        # report.jsonl is always UTF-8; foreign bytes must produce a clean
        # DiffError, not an unhandled UnicodeDecodeError traceback.
        run = tmp_path / "run"
        _report_path(run).write_bytes(b"\xff\xfe not utf-8\n")
        with pytest.raises(diff.DiffError):
            list(diff.iter_findings(str(run)))

    def test_reader_streams_yielding_valid_lines_before_a_later_bad_line(
        self, tmp_path
    ):
        # Constant-memory proof (CLAUDE.md rule #3): a streaming reader yields
        # the first valid finding, then raises only when it reaches the bad
        # second line. A buffering implementation that read()s and parses the
        # whole file up front would raise on the FIRST next() — failing the
        # first assertion below.
        run = _write_run(tmp_path / "run", [_entry(RuleID.CHECKSUM_MISMATCH)])
        with _report_path(run).open("a", encoding="utf-8") as fh:
            fh.write("{not valid json\n")
        gen = diff.iter_findings(str(run))
        first_file, first_rule = next(gen)
        assert first_rule == "TLE-CHK-001"
        with pytest.raises(diff.DiffError):
            next(gen)


class TestAggregate:
    """``_totals(aggregate_by_file(...))`` collapses a run into a Counter of
    primary rule_id → count, mirroring pipeline._record_quarantine (primary
    only; related[] ignored). This is the production path used by ``diff.run``.
    """

    def test_counts_primary_rule_ids(self, tmp_path):
        run = _write_run(
            tmp_path / "run",
            [
                _entry(RuleID.CHECKSUM_MISMATCH),
                _entry(RuleID.CHECKSUM_MISMATCH),
                _entry(RuleID.LINE_LENGTH),
            ],
        )
        assert diff._totals(diff.aggregate_by_file(str(run))) == collections.Counter(
            {"TLE-CHK-001": 2, "TLE-COL-001": 1}
        )

    def test_related_diagnostics_are_not_counted(self, tmp_path):
        # One finding: primary CHK, with two related diagnostics. The producer
        # tallies only the primary, so the diff must too — related must not
        # inflate any rule's count.
        run = _write_run(
            tmp_path / "run",
            [
                _entry(
                    RuleID.CHECKSUM_MISMATCH,
                    related_rules=(RuleID.LINE_LENGTH, RuleID.NON_ASCII_BYTE),
                )
            ],
        )
        counts = diff._totals(diff.aggregate_by_file(str(run)))
        assert counts == collections.Counter({"TLE-CHK-001": 1})
        assert "TLE-COL-001" not in counts
        assert "TLE-COL-003" not in counts

    def test_empty_run_aggregates_to_empty_counter(self, tmp_path):
        run = _write_run(tmp_path / "run", [])
        assert diff._totals(diff.aggregate_by_file(str(run))) == collections.Counter()


class TestDiffCompare:
    """``compute_delta`` categorizes every rule into new / fixed / changed /
    unchanged, deterministically and order-independently.
    """

    def test_new_rule_appears_only_in_b(self):
        delta = diff.compute_delta(
            collections.Counter({"TLE-CHK-001": 5}),
            collections.Counter({"TLE-CHK-001": 5, "TLE-PAIR-001": 3}),
        )
        assert [rd.rule_id for rd in delta.new] == ["TLE-PAIR-001"]
        assert delta.new[0].count_a == 0
        assert delta.new[0].count_b == 3
        assert delta.new[0].delta == 3

    def test_fixed_rule_present_in_a_absent_in_b(self):
        delta = diff.compute_delta(
            collections.Counter({"TLE-CHK-001": 5, "TLE-COL-001": 2}),
            collections.Counter({"TLE-CHK-001": 5}),
        )
        assert [rd.rule_id for rd in delta.fixed] == ["TLE-COL-001"]
        assert delta.fixed[0].count_a == 2
        assert delta.fixed[0].count_b == 0
        assert delta.fixed[0].delta == -2

    def test_changed_rule_carries_signed_delta(self):
        delta = diff.compute_delta(
            collections.Counter({"TLE-CHK-001": 10}),
            collections.Counter({"TLE-CHK-001": 4}),
        )
        assert [rd.rule_id for rd in delta.changed] == ["TLE-CHK-001"]
        assert delta.changed[0].delta == -6

    def test_changed_rule_can_grow(self):
        delta = diff.compute_delta(
            collections.Counter({"TLE-CHK-001": 4}),
            collections.Counter({"TLE-CHK-001": 30}),
        )
        assert delta.changed[0].delta == 26

    def test_unchanged_rule_is_segregated(self):
        delta = diff.compute_delta(
            collections.Counter({"TLE-CHK-001": 7}),
            collections.Counter({"TLE-CHK-001": 7}),
        )
        assert delta.new == ()
        assert delta.fixed == ()
        assert delta.changed == ()
        assert [rd.rule_id for rd in delta.unchanged] == ["TLE-CHK-001"]

    def test_self_diff_produces_zero_changes(self):
        counts = collections.Counter({"TLE-CHK-001": 5, "TLE-COL-001": 2})
        delta = diff.compute_delta(counts, counts)
        assert delta.new == ()
        assert delta.fixed == ()
        assert delta.changed == ()

    def test_categories_are_sorted_by_rule_id(self):
        delta = diff.compute_delta(
            collections.Counter(),
            collections.Counter(
                {"TLE-PAIR-002": 1, "TLE-CHK-001": 1, "TLE-COL-001": 1}
            ),
        )
        assert [rd.rule_id for rd in delta.new] == [
            "TLE-CHK-001",
            "TLE-COL-001",
            "TLE-PAIR-002",
        ]


class TestDiffFormat:
    """``format_text`` renders a deterministic plain-text report."""

    def test_reports_new_fixed_and_changed(self):
        delta = diff.compute_delta(
            collections.Counter({"TLE-CHK-001": 10, "TLE-COL-001": 3}),
            collections.Counter({"TLE-CHK-001": 4, "TLE-PAIR-001": 2}),
        )
        text = diff.format_text(delta, run_a="a", run_b="b")
        assert "TLE-PAIR-001" in text  # new
        assert "TLE-COL-001" in text  # fixed
        assert "TLE-CHK-001" in text  # changed
        assert "10 -> 4" in text or "10 → 4" in text
        assert "-6" in text

    def test_empty_sections_say_none(self):
        delta = diff.compute_delta(collections.Counter(), collections.Counter())
        text = diff.format_text(delta, run_a="a", run_b="b")
        assert "(none)" in text

    def test_growth_renders_a_plus_sign(self):
        delta = diff.compute_delta(
            collections.Counter({"TLE-CHK-001": 4}),
            collections.Counter({"TLE-CHK-001": 30}),
        )
        text = diff.format_text(delta, run_a="a", run_b="b")
        assert "+26" in text

    def test_output_is_byte_for_byte_deterministic(self):
        counts_a = collections.Counter({"TLE-CHK-001": 10, "TLE-COL-001": 3})
        counts_b = collections.Counter({"TLE-CHK-001": 4, "TLE-PAIR-001": 2})
        delta = diff.compute_delta(counts_a, counts_b)
        first = diff.format_text(delta, run_a="run-a", run_b="run-b")
        second = diff.format_text(delta, run_a="run-a", run_b="run-b")
        assert first == second

    def test_includes_run_labels(self):
        delta = diff.compute_delta(collections.Counter(), collections.Counter())
        text = diff.format_text(delta, run_a="path/to/may", run_b="path/to/june")
        assert "path/to/may" in text
        assert "path/to/june" in text

    def test_unknown_rule_id_renders_without_title(self):
        # An ID not in the current registry (e.g. retired in a newer build,
        # still present in an old run) must render its count without crashing
        # on the missing title lookup.
        delta = diff.compute_delta(
            collections.Counter(), collections.Counter({"TLE-XXX-999": 2})
        )
        text = diff.format_text(delta, run_a="a", run_b="b")
        assert "TLE-XXX-999" in text


class TestDiffCli:
    """End-to-end: ``lintle diff RUN-A RUN-B`` through the argparse entry point."""

    def test_diff_two_runs_exits_zero_and_prints_delta(self, tmp_path, capsys):
        run_a = _write_run(
            tmp_path / "a",
            [_entry(RuleID.CHECKSUM_MISMATCH), _entry(RuleID.LINE_LENGTH)],
        )
        run_b = _write_run(
            tmp_path / "b",
            [_entry(RuleID.CHECKSUM_MISMATCH), _entry(RuleID.BAD_PREFIX)],
        )
        code = cli.main(["diff", str(run_a), str(run_b)])
        out = capsys.readouterr().out
        assert code == 0
        assert "TLE-PAIR-002" in out  # new in B
        assert "TLE-COL-001" in out  # fixed (gone in B)

    def test_missing_run_dir_exits_two_with_error(self, tmp_path, capsys):
        run_a = _write_run(tmp_path / "a", [_entry(RuleID.CHECKSUM_MISMATCH)])
        code = cli.main(["diff", str(run_a), str(tmp_path / "does-not-exist")])
        err = capsys.readouterr().err
        assert code == 2
        assert "error:" in err

    def test_schema_mismatch_exits_two(self, tmp_path, capsys):
        run_a = _write_run(tmp_path / "a", [_entry(RuleID.CHECKSUM_MISMATCH)])
        run_b = tmp_path / "b"
        payload = report_writers.entry_to_jsonl_dict(
            _entry(RuleID.CHECKSUM_MISMATCH), file="tle.txt", norad_id=25544
        )
        payload["schema_version"] = "99"
        _report_path(run_b).write_text(json.dumps(payload) + "\n")
        code = cli.main(["diff", str(run_a), str(run_b)])
        err = capsys.readouterr().err
        assert code == 2
        assert "schema_version" in err


class TestDiffSemanticAlignment:
    """The contract test: the diff's per-rule counts must equal what the
    producer's own ``stats.quarantine_counts`` records on the same findings —
    primary rule_id only, related[] never counted (pipeline.py:334-336).
    Uses ``_totals(aggregate_by_file(...))`` — the same path ``diff.run`` uses.
    """

    def test_aggregate_matches_producer_quarantine_counts(self, tmp_path):
        entries = [
            _entry(RuleID.CHECKSUM_MISMATCH, related_rules=(RuleID.LINE_LENGTH,)),
            _entry(RuleID.CHECKSUM_MISMATCH),
            _entry(RuleID.BAD_PREFIX, related_rules=(RuleID.NON_ASCII_BYTE,)),
        ]
        # Mirror pipeline._record_quarantine's tally: primary rule_id only.
        producer_counts = collections.Counter(e.primary.rule_id.value for e in entries)
        run = _write_run(tmp_path / "run", entries)
        assert diff._totals(diff.aggregate_by_file(str(run))) == producer_counts


class TestFindingFileValidation:
    """Issue #96: a finding with a missing or non-string ``file`` key must raise
    DiffError (not let None through to sorting, which triggers TypeError)."""

    def _write_jsonl(self, run_dir, payload):
        _report_path(run_dir).write_text(
            __import__("json").dumps(payload) + "\n", encoding="utf-8"
        )

    def test_missing_file_raises_diff_error(self, tmp_path):
        from lintle import report_writers

        payload = report_writers.entry_to_jsonl_dict(
            _entry(RuleID.CHECKSUM_MISMATCH), file="tle.txt", norad_id=25544
        )
        del payload["file"]
        run = tmp_path / "run"
        self._write_jsonl(run, payload)
        with pytest.raises(diff.DiffError, match="file"):
            list(diff.iter_findings(str(run)))

    def test_null_file_raises_diff_error(self, tmp_path):
        from lintle import report_writers

        payload = report_writers.entry_to_jsonl_dict(
            _entry(RuleID.CHECKSUM_MISMATCH), file="tle.txt", norad_id=25544
        )
        payload["file"] = None
        run = tmp_path / "run"
        self._write_jsonl(run, payload)
        with pytest.raises(diff.DiffError, match="file"):
            list(diff.iter_findings(str(run)))

    def test_non_string_file_raises_diff_error(self, tmp_path):
        from lintle import report_writers

        payload = report_writers.entry_to_jsonl_dict(
            _entry(RuleID.CHECKSUM_MISMATCH), file="tle.txt", norad_id=25544
        )
        payload["file"] = 42
        run = tmp_path / "run"
        self._write_jsonl(run, payload)
        with pytest.raises(diff.DiffError, match="file"):
            list(diff.iter_findings(str(run)))

    def test_lintle_diff_with_missing_file_exits_2(self, tmp_path, capsys):
        # End-to-end: lintle diff returns 2 with a clear message, not TypeError.
        from lintle import report_writers

        run_a = _write_run(tmp_path / "a", [_entry(RuleID.CHECKSUM_MISMATCH)])
        run_b = tmp_path / "b"
        payload = report_writers.entry_to_jsonl_dict(
            _entry(RuleID.CHECKSUM_MISMATCH), file="tle.txt", norad_id=25544
        )
        del payload["file"]
        _report_path(run_b).write_text(
            __import__("json").dumps(payload) + "\n", encoding="utf-8"
        )
        code = cli.main(["diff", str(run_a), str(run_b)])
        err = capsys.readouterr().err
        assert code == 2
        assert "error:" in err


class TestIterFindings:
    """``iter_findings`` is the core reader — it yields ``(file, rule_id)`` for
    every finding, the unit per-file aggregation needs."""

    def test_yields_file_and_primary_rule_id(self, tmp_path):
        run = _write_run_files(
            tmp_path / "run",
            [
                ("tle2024.txt", RuleID.CHECKSUM_MISMATCH),
                ("tle2025.txt", RuleID.LINE_LENGTH),
            ],
        )
        assert list(diff.iter_findings(str(run))) == [
            ("tle2024.txt", "TLE-CHK-001"),
            ("tle2025.txt", "TLE-COL-001"),
        ]


class TestAggregateByFile:
    """``aggregate_by_file`` returns ``{basename: Counter(rule_id)}``. Because
    clean accepts only a single positional input, each basename is a unique file
    within a run, so the grouping is unambiguous."""

    def test_groups_counts_by_file(self, tmp_path):
        run = _write_run_files(
            tmp_path / "run",
            [
                ("a.txt", RuleID.CHECKSUM_MISMATCH),
                ("a.txt", RuleID.CHECKSUM_MISMATCH),
                ("b.txt", RuleID.LINE_LENGTH),
            ],
        )
        by_file = diff.aggregate_by_file(str(run))
        assert by_file["a.txt"] == collections.Counter({"TLE-CHK-001": 2})
        assert by_file["b.txt"] == collections.Counter({"TLE-COL-001": 1})

    def test_related_not_counted_per_file(self, tmp_path):
        entry = _entry(RuleID.CHECKSUM_MISMATCH, related_rules=(RuleID.LINE_LENGTH,))
        run = tmp_path / "run"
        payload = report_writers.entry_to_jsonl_dict(
            entry, file="a.txt", norad_id=entry.norad_id
        )
        _report_path(run).write_text(json.dumps(payload) + "\n")
        by_file = diff.aggregate_by_file(str(run))
        assert by_file["a.txt"] == collections.Counter({"TLE-CHK-001": 1})

    def test_empty_run_is_empty_dict(self, tmp_path):
        run = _write_run(tmp_path / "run", [])
        assert diff.aggregate_by_file(str(run)) == {}


class TestComputeFileDelta:
    """``compute_file_delta`` reports, per basename, the rules whose counts
    changed; identical files are omitted, one-sided files are flagged."""

    def test_file_in_both_with_changed_counts(self):
        fds = diff.compute_file_delta(
            {"x.txt": collections.Counter({"TLE-CHK-001": 10})},
            {"x.txt": collections.Counter({"TLE-CHK-001": 4})},
        )
        assert len(fds) == 1
        assert fds[0].file == "x.txt"
        assert fds[0].presence == "both"
        assert [rd.rule_id for rd in fds[0].rules] == ["TLE-CHK-001"]
        assert fds[0].rules[0].delta == -6

    def test_file_only_in_a(self):
        fds = diff.compute_file_delta(
            {"gone.txt": collections.Counter({"TLE-CHK-001": 3})}, {}
        )
        assert fds[0].presence == "a_only"
        assert fds[0].rules[0].count_a == 3

    def test_file_only_in_b(self):
        fds = diff.compute_file_delta(
            {}, {"new.txt": collections.Counter({"TLE-PAIR-002": 2})}
        )
        assert fds[0].presence == "b_only"
        assert fds[0].rules[0].count_b == 2

    def test_unchanged_file_is_omitted(self):
        a = {"same.txt": collections.Counter({"TLE-CHK-001": 5})}
        b = {"same.txt": collections.Counter({"TLE-CHK-001": 5})}
        assert diff.compute_file_delta(a, b) == ()

    def test_files_sorted_by_name(self):
        fds = diff.compute_file_delta(
            {},
            {
                "zeta.txt": collections.Counter({"TLE-CHK-001": 1}),
                "alpha.txt": collections.Counter({"TLE-CHK-001": 1}),
            },
        )
        assert [fd.file for fd in fds] == ["alpha.txt", "zeta.txt"]

    def test_new_rule_within_a_both_file_shows_only_the_change(self):
        # File present in both runs; one rule unchanged, one rule new in B.
        # The unchanged rule is omitted; only the new rule surfaces.
        fds = diff.compute_file_delta(
            {"x.txt": collections.Counter({"TLE-CHK-001": 5})},
            {"x.txt": collections.Counter({"TLE-CHK-001": 5, "TLE-COL-001": 2})},
        )
        assert fds[0].presence == "both"
        rules = {rd.rule_id: rd for rd in fds[0].rules}
        assert "TLE-CHK-001" not in rules  # unchanged within the file
        assert rules["TLE-COL-001"].count_a == 0
        assert rules["TLE-COL-001"].count_b == 2


class TestFormatFileDeltas:
    """``format_file_deltas`` renders the per-file section deterministically,
    labels one-sided files, and never asserts a false drop-to-zero."""

    def test_renders_both_file_as_delta(self):
        fds = diff.compute_file_delta(
            {"x.txt": collections.Counter({"TLE-CHK-001": 10})},
            {"x.txt": collections.Counter({"TLE-CHK-001": 4})},
        )
        text = diff.format_file_deltas(fds)
        assert "x.txt" in text
        assert "10 -> 4" in text
        assert "-6" in text

    def test_a_only_file_labeled_without_false_zero(self):
        fds = diff.compute_file_delta(
            {"gone.txt": collections.Counter({"TLE-CHK-001": 3})}, {}
        )
        text = diff.format_file_deltas(fds)
        assert "gone.txt" in text
        assert "only in run A" in text
        # A one-sided file must NOT claim it went to zero — it may have been
        # removed or renamed, not fixed.
        assert "-> 0" not in text
        assert "3" in text

    def test_b_only_file_labeled(self):
        fds = diff.compute_file_delta(
            {}, {"new.txt": collections.Counter({"TLE-PAIR-002": 2})}
        )
        text = diff.format_file_deltas(fds)
        assert "new.txt" in text
        assert "only in run B" in text

    def test_empty_says_none(self):
        assert "(none)" in diff.format_file_deltas(())

    def test_deterministic(self):
        fds = diff.compute_file_delta(
            {"x.txt": collections.Counter({"TLE-CHK-001": 10})},
            {"x.txt": collections.Counter({"TLE-CHK-001": 4})},
        )
        assert diff.format_file_deltas(fds) == diff.format_file_deltas(fds)


class TestDiffCliPerFile:
    """End-to-end: the per-file section appears in ``lintle diff`` output."""

    def test_per_file_section_shows_changed_files(self, tmp_path, capsys):
        run_a = _write_run_files(
            tmp_path / "a",
            [
                ("tle2024.txt", RuleID.CHECKSUM_MISMATCH),
                ("tle2024.txt", RuleID.CHECKSUM_MISMATCH),
            ],
        )
        run_b = _write_run_files(
            tmp_path / "b",
            [
                ("tle2024.txt", RuleID.CHECKSUM_MISMATCH),
                ("tle2025.txt", RuleID.BAD_PREFIX),
            ],
        )
        code = cli.main(["diff", str(run_a), str(run_b)])
        out = capsys.readouterr().out
        assert code == 0
        assert "Per-file changes" in out
        assert "tle2024.txt" in out  # CHK 2 -> 1
        assert "tle2025.txt" in out  # only in run B


class TestTableRendering:
    """diff renders tables on a TTY and the byte-exact plain text off one — the
    piped contract is the plain path, which the rest of this module locks."""

    def _delta(self):
        return diff.compute_delta(
            collections.Counter({"TLE-CHK-001": 2}),
            collections.Counter({"TLE-CHK-001": 5, "TLE-COL-001": 1}),
        )

    def test_tty_render_uses_the_shared_table_chrome(self):
        console = Console(file=io.StringIO(), force_terminal=True, width=120)
        diff.render_tables(self._delta(), (), run_a="a", run_b="b", console=console)
        out = console.file.getvalue()
        assert "rule" in out and "change" in out
        assert "TLE-CHK-001" in out and "+3" in out  # 2 -> 5
        assert "TLE-COL-001" in out and "+1" in out  # new in B

    def test_one_sided_file_shows_a_bare_count_never_a_false_zero(self):
        rd = diff.RuleDelta("TLE-CHK-001", 4, 0)
        assert diff._file_rule_change(diff._A_ONLY, rd) == "4"
        assert diff._file_rule_change(diff._B_ONLY, diff.RuleDelta("X", 0, 7)) == "7"
