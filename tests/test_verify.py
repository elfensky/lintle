"""Tests for ``lintle verify`` (Increment 1: the exhaustive, sgp4-free core)."""

import ast
import json
from pathlib import Path

import lintle
from lintle import CLEANED_DIRNAME, VERIFY_DIRNAME, cli, tle
from lintle.verify import checks, epoch, grouping, records, report, run
from lintle.verify.records import CleanedRecord
from lintle.verify.report import Suspect, VerifyRule

# A canonical known-good record (Vanguard 1, NORAD 00005).
L1 = "1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753"
L2 = "2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667"


def fix(line: str) -> str:
    """Return ``line`` with a correct column-69 checksum — lets a test mutate an
    interior column and keep the record valid."""
    return line[:68] + str(tle.compute_checksum(line))


def mutated_l2() -> str:
    """L2 with one inclination digit changed (34.2682 -> 34.3682): still a valid
    record, same catalog and epoch, different orbital elements."""
    return fix(L2[:13] + "3" + L2[14:])


def reissued_l1() -> str:
    """L1 with a bumped element-set number (cols 65-68) — a normal space-track
    re-issue: same catalog, epoch, and orbital state, only the admin field and
    checksum change. Must NOT be treated as a contradiction."""
    return fix(L1[:64] + "9999")


def reissued_l2() -> str:
    """L2 with a bumped revolution number (cols 64-68) — same orbital state; the
    other admin-only re-issue shape seen in the real corpus."""
    return fix(L2[:63] + "99999")


def reissued_refined_l1() -> str:
    """L1 as a real re-issue: a bumped element-set (cols 65-68 -> 9999) AND a
    refined B* — space-track's successive orbit solution at the same epoch. A
    different element-set AND orbit: a benign re-issue, not a contradiction (#158)."""
    return fix(L1[:53] + " 20000-3" + L1[61:64] + "9999")


def nddot_signed(sign: str) -> str:
    """L1 with its 2nd-derivative field (cols 45-52) written as ``sign`` + a zero
    mantissa: ``' 00000-0'`` and ``'+00000-0'`` encode the SAME value (0).
    Space-track emits both; the differing byte sits inside cols 1-64, so a
    raw-byte mask reads them as a contradiction — the real-corpus FP in #154."""
    return fix(L1[:44] + sign + "00000-0" + L1[52:])


def rec(line1=L1, line2=L2, src="tle01", idx=0) -> CleanedRecord:
    return CleanedRecord(
        tle.extract_norad_id(line1), epoch.epoch_key(line1), line1, line2, src, idx
    )


def build_tree_with_source(tmp_path, cleaned_pairs, source_lines=None, stem="tle01"):
    """Write a minimal clean-run output tree (and optional source file); return
    ``(out_dir, source_dir)`` as strings."""
    out = tmp_path / "output"
    (out / CLEANED_DIRNAME).mkdir(parents=True, exist_ok=True)
    (out / CLEANED_DIRNAME / f"{stem}.00001.cleaned.txt").write_text(
        "".join(f"{a}\n{b}\n" for a, b in cleaned_pairs), encoding="ascii"
    )
    src = tmp_path / "source"
    src.mkdir(parents=True, exist_ok=True)
    if source_lines is not None:
        (src / f"{stem}.txt").write_text(
            "".join(line + "\n" for line in source_lines), encoding="ascii"
        )
    return str(out), str(src)


def _epoch_record(catalog: int, year: int, day: int) -> tuple[str, str]:
    """A valid ``(line1, line2)`` pair for ``catalog`` at day-of-year ``day``
    in ``year`` — the L1/L2 template with the catalog and epoch-year/day
    columns overwritten and the checksum recomputed."""
    yy = f"{year % 100:02d}"
    l1 = fix(L1[:2] + f"{catalog:05d}" + L1[7:18] + yy + f"{day:03d}" + L1[23:])
    l2 = fix(L2[:2] + f"{catalog:05d}" + L2[7:])
    return l1, l2


def _build_cleaned(out: str, catalogs: dict[int, list[tuple[int, int]]]) -> None:
    """Write a minimal ``01-cleaned`` tree (no source dir): one valid record per
    ``(year, day_of_year)`` epoch, for each catalog in ``catalogs``."""
    pairs = [
        _epoch_record(catalog, year, day)
        for catalog, epochs in catalogs.items()
        for year, day in epochs
    ]
    cleaned_dir = Path(out) / CLEANED_DIRNAME
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    (cleaned_dir / "tle01.00001.cleaned.txt").write_text(
        "".join(f"{a}\n{b}\n" for a, b in pairs), encoding="ascii"
    )


class TestEpoch:
    def with_year(self, yy):
        return fix(L1[:18] + yy + L1[20:])

    def test_year_pivot(self):
        assert epoch.parse_epoch(self.with_year("57"))[0] == 1957
        assert epoch.parse_epoch(self.with_year("56"))[0] == 2056
        assert epoch.parse_epoch(self.with_year("00"))[0] == 2000
        assert epoch.parse_epoch(self.with_year("99"))[0] == 1999

    def test_day_of_year(self):
        year, day = epoch.parse_epoch(L1)
        assert year == 2000
        assert abs(day - 179.78495062) < 1e-9

    def test_key_orders_across_years(self):
        assert epoch.epoch_key(self.with_year("99")) < epoch.epoch_key(
            self.with_year("00")
        )


class TestSanctioned:
    def test_reduce_undoes_each_edge_repair(self):
        assert checks.sanctioned_reduce(L1 + "\r") == L1
        assert checks.sanctioned_reduce("   " + L1) == L1
        assert checks.sanctioned_reduce(L1 + "   ") == L1
        assert checks.sanctioned_reduce(L1 + "\\") == L1
        # a valid multi-repair combo: CRLF + leading + trailing whitespace
        assert checks.sanctioned_reduce("  " + L1 + "  \r") == L1

    def test_match_exact_and_padded(self):
        assert checks.sanctioned_match(L1, L1)
        assert checks.sanctioned_match("  " + L1 + "  \r", L1)

    def test_match_reconstructed_checksum(self):
        # a 68-char body source line -> cleaned appended the recomputed digit
        assert checks.sanctioned_match(L1[:68], L1)

    def test_interior_change_does_not_match(self):
        assert not checks.sanctioned_match(L2, mutated_l2())


class TestRevalidate:
    def test_good_record_passes(self):
        assert checks.revalidate(rec()) is None

    def test_broken_record_flagged(self):
        bad = CleanedRecord(
            -1, -1.0, "garbage line one", "garbage line two", "tle01", 3
        )
        s = checks.revalidate(bad)
        assert s is not None and s.rule is VerifyRule.REVALIDATE_FAIL
        assert s.severity == "hard"


class TestCatalogExtraction:
    def test_space_padded_catalog_recovered(self):
        # space-track writes low catalog numbers space-padded, not zero-padded:
        # cols 3-7 '  836' is catalog 836 and validates (the charset allows
        # spaces), but tle.extract_norad_id's strict 5-digit contract returns
        # None -> the -1 sentinel manufactures epoch conflicts (#157).
        l1 = fix(L1[:2] + "  836" + L1[7:])
        cat, key = records._catalog_and_key(l1)
        assert cat == 836 and key != -1.0

    def test_unparseable_line1_stays_sentinel(self):
        assert records._catalog_and_key("garbage not a tle") == (-1, -1.0)


class TestConflicts:
    def test_exact_duplicate_is_not_a_conflict(self):
        stream = [rec(idx=0), rec(idx=1)]  # identical bytes, same (catalog, epoch)
        assert checks.find_conflicts(iter(stream)) == ([], 0, set())

    def test_different_orbital_elements_conflict(self):
        # same element-set (default L1), different orbit -> a hard clash
        stream = [rec(idx=0), rec(line2=mutated_l2(), idx=1)]  # inclination differs
        out, n, _ = checks.find_conflicts(iter(stream))
        assert len(out) == 1 and out[0].rule is VerifyRule.EPOCH_CONFLICT and n == 0

    def test_element_set_reissue_is_not_a_conflict(self):
        # same orbital state, only the element-set number (cols 65-68) differs
        r0, r1 = rec(idx=0), rec(line1=reissued_l1(), idx=1)
        assert r0.line1[:64] == r1.line1[:64] and r0.line1 != r1.line1
        assert checks.find_conflicts(iter([r0, r1])) == ([], 0, set())

    def test_revolution_number_reissue_is_not_a_conflict(self):
        # same orbital state, only the revolution number (cols 64-68) differs
        r0, r1 = rec(idx=0), rec(line2=reissued_l2(), idx=1)
        assert r0.line2[:63] == r1.line2[:63] and r0.line2 != r1.line2
        assert checks.find_conflicts(iter([r0, r1])) == ([], 0, set())

    def test_sign_encoding_reissue_is_not_a_conflict(self):
        # identical orbit; 2nd-deriv field ' 00000-0' vs '+00000-0' (same value).
        # The differing byte is inside cols 1-64, so a byte mask false-positives
        # (real-corpus #154: ~3.2M such re-issues flagged as contradictions).
        r0 = rec(line1=nddot_signed(" "), idx=0)
        r1 = rec(line1=nddot_signed("+"), idx=1)
        assert r0.line1[:64] != r1.line1[:64]  # would false-positive under a byte mask
        assert checks.find_conflicts(iter([r0, r1])) == ([], 0, set())

    def test_different_drag_value_same_elset_is_hard(self):
        # same element-set, a genuine B* difference -> a hard clash (not re-issue)
        r0 = rec(line1=fix(L1[:53] + " 10000-3" + L1[61:]), idx=0)
        r1 = rec(line1=fix(L1[:53] + " 20000-3" + L1[61:]), idx=1)
        out, n, _ = checks.find_conflicts(iter([r0, r1]))
        assert len(out) == 1 and out[0].rule is VerifyRule.EPOCH_CONFLICT and n == 0

    def test_refined_reissue_is_census_not_conflict(self):
        # same (catalog, epoch), a NEW element-set AND a refined orbit -> a benign
        # re-issue: counted, never a hard conflict (#158)
        r0, r1 = rec(idx=0), rec(line1=reissued_refined_l1(), idx=1)
        assert checks.find_conflicts(iter([r0, r1])) == ([], 1, set())

    def test_different_satellites_no_conflict(self):
        other1 = fix(L1[:2] + "00006" + L1[7:])  # different catalog
        stream = sorted(
            [rec(idx=0), rec(line1=other1, idx=1)],
            key=lambda r: (r.catalog, r.epoch_key),
        )
        assert checks.find_conflicts(iter(stream)) == ([], 0, set())

    def test_returns_three_tuple_with_empty_set_by_default(self):
        # the #2 dup-epoch set is the third element; empty and off unless orbit=True
        result = checks.find_conflicts(iter([rec(idx=0), rec(idx=1)]))
        assert len(result) == 3 and result[2] == set()

    def test_dup_epoch_catalogs_collected_only_under_orbit(self):
        # two records share (catalog 5, epoch) -> a dup-epoch group; the catalog is
        # collected only when the orbit pass will consume it.
        stream = [rec(idx=0), rec(idx=1)]
        assert checks.find_conflicts(iter(stream))[2] == set()
        assert checks.find_conflicts(iter(stream), orbit=True)[2] == {5}

    def test_singleton_epoch_group_is_not_dup(self):
        # a lone record per (catalog, epoch) is never a dup-epoch group
        stream = [rec(idx=0)]
        assert checks.find_conflicts(iter(stream), orbit=True)[2] == set()

    def test_admin_only_reissue_is_still_dup_epoch(self):
        # an exact-orbit re-issue (only the element-set differs) is not a conflict
        # and not counted as a re-issue, but it IS a dup-epoch group (#2 keys on the
        # group boundary, independent of the state-difference branch).
        r0, r1 = rec(idx=0), rec(line1=reissued_l1(), idx=1)
        conflicts, reissues, dup = checks.find_conflicts(iter([r0, r1]), orbit=True)
        assert conflicts == [] and reissues == 0 and dup == {5}


class TestHasEpochClash:
    """The group-level #158 predicate shared with ``dedup`` — it must agree with
    ``find_conflicts`` on what a same-epoch clash is (one definition, #164)."""

    def test_same_elset_different_orbit_clashes(self):
        # same element-set (default L1), different orbit -> a genuine clash
        assert checks.has_epoch_clash([rec(idx=0), rec(line2=mutated_l2(), idx=1)])

    def test_refined_reissue_is_not_a_clash(self):
        # a NEW element-set AND a refined orbit -> benign re-issue, not a clash
        assert not checks.has_epoch_clash(
            [rec(idx=0), rec(line1=reissued_refined_l1(), idx=1)]
        )

    def test_identical_records_no_clash(self):
        assert not checks.has_epoch_clash([rec(idx=0), rec(idx=1)])

    def test_agrees_with_find_conflicts_on_every_group_shape(self):
        # The property the old hand-synced twin implementations protected:
        # for any same-(catalog, epoch) group, the group-level boolean and
        # find_conflicts' per-record findings name the same clashes.
        groups = [
            [rec(idx=0), rec(line2=mutated_l2(), idx=1)],  # clash
            [rec(idx=0), rec(line1=reissued_refined_l1(), idx=1)],  # re-issue
            [rec(idx=0), rec(idx=1)],  # exact duplicate
            [rec(idx=0)],  # singleton
        ]
        for group in groups:
            conflicts, _, _ = checks.find_conflicts(iter(group))
            assert bool(conflicts) == checks.has_epoch_clash(group)


class TestSourceAlignerNullObject:
    """The debate-consensus (C) seam: always construct, inert without a source,
    skip policy behind feed() — no caller-side guards or skip conventions."""

    def test_open_without_source_dir_is_inert(self):
        aligner = checks.SourceAligner.open(None, "tle_x")
        assert not aligner.active
        assert aligner.feed(rec()) is None  # unconditionally callable
        aligner.close()  # and unconditionally closable

    def test_open_with_missing_file_is_inert(self, tmp_path):
        aligner = checks.SourceAligner.open(str(tmp_path), "tle_missing")
        assert not aligner.active
        assert aligner.feed(rec()) is None
        aligner.close()

    def test_open_with_real_file_is_active(self, tmp_path):
        (tmp_path / "tle_x.txt").write_text(f"{L1}\n{L2}\n", encoding="ascii")
        aligner = checks.SourceAligner.open(str(tmp_path), "tle_x")
        assert aligner.active
        assert aligner.feed(rec()) is None  # clean match
        aligner.close()

    def test_revalidate_failed_record_does_not_consume_source(self, tmp_path):
        # The old caller-side skip contract, now inside the interface: feeding a
        # revalidate-failed record must leave the buffer untouched, so the SAME
        # origin still matches the next (revalidated) record.
        (tmp_path / "tle_x.txt").write_text(f"{L1}\n{L2}\n", encoding="ascii")
        aligner = checks.SourceAligner.open(str(tmp_path), "tle_x")
        assert aligner.feed(rec(), revalidated=False) is None
        assert aligner.feed(rec()) is None  # origin still there — not consumed
        aligner.close()


class TestSourceAligner:
    def test_clean_padded_match(self, tmp_path):
        src = tmp_path / "s.txt"
        src.write_text(f"  {L1}  \n{L2}\r\n", encoding="ascii")
        aligner = checks.SourceAligner(str(src))
        assert aligner.feed(rec()) is None
        aligner.close()

    def test_interior_mutation_flagged(self, tmp_path):
        src = tmp_path / "s.txt"
        src.write_text(f"{L1}\n{L2}\n", encoding="ascii")
        aligner = checks.SourceAligner(str(src))
        s = aligner.feed(rec(line2=mutated_l2()))
        aligner.close()
        assert s is not None and s.rule is VerifyRule.INTERIOR_MUT

    def test_origin_missing(self, tmp_path):
        src = tmp_path / "s.txt"
        src.write_text("unrelated one\nunrelated two\n", encoding="ascii")
        aligner = checks.SourceAligner(str(src))
        s = aligner.feed(rec())
        aligner.close()
        assert s is not None and s.rule is VerifyRule.ORIGIN_MISSING
        assert s.severity == "soft"

    def test_resyncs_across_quarantine_gap(self, tmp_path):
        # two dropped (garbage) source lines precede the real pair
        src = tmp_path / "s.txt"
        src.write_text(f"junk a\njunk b\n{L1}\n{L2}\n", encoding="ascii")
        aligner = checks.SourceAligner(str(src))
        assert aligner.feed(rec()) is None
        aligner.close()

    def test_resyncs_across_long_quarantine_gap(self, tmp_path):
        # A run of quarantined (dropped) source records LONGER than the resync
        # window sits between two cleaned records. The real corpus (tle2020,
        # cleaned without --reconstruct-checksum) has runs of 20k+ consecutive
        # 68-char missing-checksum records — far past _RESYNC_WINDOW — between
        # two accepted records. Skipping such a run is normal alignment, not an
        # ORIGIN_MISSING: the second record's origin genuinely exists just past
        # the gap. Regression for the verify desync cascade (44M false suspects,
        # 31h runtime on the full corpus).
        gap = "".join(
            f"1 {10000 + i:05d}U 20001A   00179.00000000  .00000000  00000-0  "
            f"00000-0 0  000\n2 {10000 + i:05d}  00.0000 000.0000 0000000 "
            "000.0000 000.0000 15.00000000000000\n"
            for i in range(checks._RESYNC_WINDOW)  # 2*window lines > window
        )
        src = tmp_path / "s.txt"
        src.write_text(f"{L1}\n{L2}\n{gap}{L1}\n{L2}\n", encoding="ascii")
        aligner = checks.SourceAligner(str(src))
        assert aligner.feed(rec()) is None  # first record matches at the top
        assert aligner.feed(rec()) is None  # second record's origin is past the gap
        aligner.close()

    def test_quarantined_duplicate_is_not_interior_mutation(self, tmp_path):
        # tle2020 carries each satellite twice at one epoch: a +signed 68-char
        # missing-checksum copy clean QUARANTINES, then the real space-signed
        # 69-char copy it keeps. Both share the aligner anchor (catalog + epoch
        # cols). The dropped copy must not be reported as an interior mutation of
        # the cleaned record whose true origin (a byte-match) lies further ahead.
        # A same-anchor 68-char (invalid) shadow: keep cols [0:32] (the anchor)
        # from L1, differ in the body, drop the checksum -> not a sanctioned match
        # and not clean-able.
        shadow1 = L1[:32] + " +.00000023 +00000-0 +28098-4 0 0001"  # 68 chars
        shadow2 = L2[:68]
        assert len(shadow1) == 68 and checks._anchor(shadow1) == checks._anchor(L1)
        src = tmp_path / "s.txt"
        src.write_text(f"{shadow1}\n{shadow2}\n{L1}\n{L2}\n", encoding="ascii")
        aligner = checks.SourceAligner(str(src))
        assert aligner.feed(rec()) is None  # real origin found past the shadow
        aligner.close()

    def test_blank_line_between_pair_is_clean_match(self, tmp_path):
        # tle2019 source has stray blank lines between line 1 and line 2, both
        # missing their checksum (#155). clean skips the blank, pairs them, and
        # reconstructs the checksums; the aligner must skip the blank too, else
        # the interposed line breaks the adjacent-pair match -> false INTERIOR_MUT.
        src = tmp_path / "s.txt"
        src.write_text(f"{L1[:68]}\n\n{L2[:68]}\n", encoding="ascii")
        aligner = checks.SourceAligner(str(src))
        assert aligner.feed(rec()) is None
        aligner.close()


class TestReport:
    def test_suspects_jsonl_is_deterministic(self):
        a = Suspect(VerifyRule.EPOCH_CONFLICT, 5, 2000179.0, "tle01", 2, "x")
        b = Suspect(VerifyRule.INTERIOR_MUT, 5, 2000179.0, "tle01", 1, "y")
        assert report.render_suspects_jsonl([a, b]) == report.render_suspects_jsonl(
            [b, a]
        )

    def test_exit_code(self):
        soft = Suspect(VerifyRule.ORIGIN_MISSING, 5, 1.0, "t", 0, "x")
        hard = Suspect(VerifyRule.INTERIOR_MUT, 5, 1.0, "t", 0, "y")

        def code(suspects):
            sink = report.SuspectSink()
            sink.add_all(suspects)
            return sink.exit_code

        assert code([]) == 0
        assert code([soft]) == 0
        assert code([soft, hard]) == 1


class TestReadme:
    """``SuspectSink.write`` drops a static ``README.md`` in ``04-verify/`` —
    the dir's own self-description, deterministic across runs."""

    def test_writes_readme(self, tmp_path):
        sink = report.SuspectSink()
        sink.write(str(tmp_path), checked={})
        readme = tmp_path / VERIFY_DIRNAME / "README.md"
        assert readme.is_file()
        text = readme.read_text(encoding="utf-8")
        assert "04-verify" in text
        assert "lintle verify" in text
        assert "suspects.NNNNN.jsonl" in text

    def test_readme_is_deterministic(self, tmp_path):
        report.SuspectSink().write(str(tmp_path), checked={})
        first = (tmp_path / VERIFY_DIRNAME / "README.md").read_bytes()
        report.SuspectSink().write(str(tmp_path), checked={})
        assert (tmp_path / VERIFY_DIRNAME / "README.md").read_bytes() == first


class TestGrouping:
    def test_external_sort_orders_by_catalog_then_epoch(self):
        sorter = grouping.ExternalSorter(chunk_size=2)  # force a spill
        other = fix(L1[:2] + "00006" + L1[7:])
        later = fix(L1[:18] + "00200.50000000" + L1[32:])
        given = [
            rec(line1=other, idx=0),
            rec(line1=later, idx=1),
            rec(idx=2),
        ]
        for r in given:
            sorter.add(r)
        keys = [(r.catalog, r.epoch_key) for r in sorter.sorted_records()]
        assert keys == sorted(keys)


class TestEndToEnd:
    def test_clean_tree_passes(self, tmp_path):
        out, src = build_tree_with_source(tmp_path, [(L1, L2)], source_lines=[L1, L2])
        assert run(out, src) == 0
        suspects = (
            tmp_path / "output" / VERIFY_DIRNAME / "suspects.00001.jsonl"
        ).read_text()
        assert suspects == ""

    def test_interior_mutation_fails(self, tmp_path):
        out, src = build_tree_with_source(
            tmp_path, [(L1, mutated_l2())], source_lines=[L1, L2]
        )
        assert run(out, src) == 1
        rows = [
            json.loads(line)
            for line in (tmp_path / "output" / VERIFY_DIRNAME / "suspects.00001.jsonl")
            .read_text()
            .splitlines()
        ]
        assert any(r["rule"] == "VRFY-INTERIOR-MUT" for r in rows)

    def test_contradiction_fails(self, tmp_path):
        # same satellite+epoch, SAME element-set, different orbit -> a hard clash
        out, _ = build_tree_with_source(tmp_path, [(L1, L2), (L1, mutated_l2())])
        assert run(out, None) == 1
        rows = (
            tmp_path / "output" / VERIFY_DIRNAME / "suspects.00001.jsonl"
        ).read_text()
        assert "VRFY-EPOCH-CONFLICT" in rows

    def test_refined_reissue_is_census_pass(self, tmp_path):
        # same satellite+epoch, a NEW element-set AND refined orbit -> a benign
        # re-issue: verify PASSES and the record is counted in the census (#158)
        out, _ = build_tree_with_source(
            tmp_path, [(L1, L2), (reissued_refined_l1(), L2)]
        )
        assert run(out, None) == 0
        suspects = (
            tmp_path / "output" / VERIFY_DIRNAME / "suspects.00001.jsonl"
        ).read_text()
        assert suspects == ""
        summary = json.loads(
            (tmp_path / "output" / VERIFY_DIRNAME / "summary.json").read_text()
        )
        assert summary["checked"]["epoch_reissues"] == 1

    def test_missing_cleaned_dir_is_operational_error(self, tmp_path):
        assert run(str(tmp_path / "nope"), None) == 2

    def test_source_diff_skipped_when_no_source(self, tmp_path):
        out, _ = build_tree_with_source(tmp_path, [(L1, L2)])
        assert run(out, None) == 0
        summary = json.loads(
            (tmp_path / "output" / VERIFY_DIRNAME / "summary.json").read_text()
        )
        assert summary["checked"]["source_diff"] == "skipped"


class TestEpochHistogram:
    """The epoch record-density histogram: a sibling top-level key in
    ``summary.json``, binned only from records that survive ``revalidate``."""

    def test_summary_bins_records_by_month(self, tmp_path):
        # 3 records in 2017-01, 0 in Feb/Mar, 2 in 2017-04 (doy 91/92 ~ Apr 1/2)
        out = str(tmp_path)
        _build_cleaned(
            out, {100: [(2017, 15), (2017, 16), (2017, 17), (2017, 91), (2017, 92)]}
        )
        run(out, source_dir=None)
        summary = json.loads((tmp_path / VERIFY_DIRNAME / "summary.json").read_text())
        hist = summary["epoch_distribution"]
        assert hist["2017-01"] == 3
        assert hist["2017-04"] == 2
        assert "2017-02" not in hist  # the hole reads as an absent bin

    def test_broken_records_are_not_binned(self, tmp_path):
        # a revalidate-failing record (bad checksum) must not contribute a bin
        out = str(tmp_path)
        _build_cleaned(out, {100: [(2017, 15)]})
        good_l1, good_l2 = _epoch_record(100, 2017, 15)
        broken_l2 = good_l2[:-1] + str((int(good_l2[-1]) + 1) % 10)
        cleaned = Path(out) / CLEANED_DIRNAME / "tle01.00001.cleaned.txt"
        cleaned.write_text(f"{good_l1}\n{broken_l2}\n", encoding="ascii")
        run(out, source_dir=None)
        summary = json.loads((tmp_path / VERIFY_DIRNAME / "summary.json").read_text())
        assert summary["epoch_distribution"] == {}

    def test_epoch_distribution_is_sibling_of_checked(self, tmp_path):
        out = str(tmp_path)
        _build_cleaned(out, {100: [(2017, 15)]})
        run(out, source_dir=None)
        summary = json.loads((tmp_path / VERIFY_DIRNAME / "summary.json").read_text())
        assert "epoch_distribution" not in summary["checked"]
        assert "2017-01" in summary["epoch_distribution"]


class TestCLI:
    def test_verify_subcommand_dispatches(self, tmp_path):
        out, src = build_tree_with_source(tmp_path, [(L1, L2)], source_lines=[L1, L2])
        assert cli.main(["verify", out, "--source", src]) == 0

    def test_verify_flags_via_cli(self, tmp_path):
        out, src = build_tree_with_source(
            tmp_path, [(L1, mutated_l2())], source_lines=[L1, L2]
        )
        assert cli.main(["verify", out, "--source", src]) == 1


class TestImportGuard:
    """The sgp4/verify import wall (Critical Rule #4 and the dependency
    policy): the clean path must never import ``sgp4`` or ``lintle.verify``.
    Enforced over the *transitive* module-level import closure of the clean
    path, not just three files' direct source text, so a collaborator quietly
    importing sgp4 two hops away still trips the wall. Function-level lazy
    imports are deliberately outside the walk — those are the sanctioned
    dispatch points (``cli.main``'s verify/dedup/extract branches), which
    only run when the operator asks for a verify/dedup/extract command.
    ``lintle.extract`` is a read-only consumer of a prior ``dedup`` run
    (like ``dedup`` is of ``verify``), so it too must stay out of the clean
    path's closure and its own closure must stay ``sgp4``-free even though
    it legitimately reaches into ``verify`` for the shared epoch/catalog
    parsers."""

    @staticmethod
    def _module_level_imports(path):
        """Names imported at module level: lintle submodule names, plus the
        sentinel ``"sgp4"`` for any sgp4 import."""
        names = set()
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            match node:
                case ast.Import(names=aliases):
                    for a in aliases:
                        root = a.name.split(".")[0]
                        if root == "sgp4":
                            names.add("sgp4")
                        elif root == "lintle":
                            names.add(
                                a.name.split(".")[1] if "." in a.name else "__init__"
                            )
                case ast.ImportFrom(module=mod, names=aliases) if mod:
                    root = mod.split(".")[0]
                    if root == "sgp4":
                        names.add("sgp4")
                    elif mod == "lintle":
                        # ``from lintle import x`` — x may be a submodule or a
                        # package attribute; submodules resolve below.
                        names.update(a.name for a in aliases)
                        names.add("__init__")
                    elif root == "lintle":
                        names.add(mod.split(".")[1])
        return names

    def test_clean_path_closure_never_imports_sgp4_or_verify(self):
        src = Path(lintle.__file__).parent
        seeds = {"cli", "pipeline", "repair", "tle"}
        seen: set[str] = set()
        frontier = set(seeds)
        while frontier:
            name = frontier.pop()
            seen.add(name)
            assert name != "sgp4", f"sgp4 reached from the clean path via {seen}"
            assert name != "verify", (
                f"lintle.verify reached from the clean path via {seen}"
            )
            assert name != "extract", (
                f"lintle.extract reached from the clean path via {seen}"
            )
            mod_path = src / "__init__.py" if name == "__init__" else src / f"{name}.py"
            if not mod_path.is_file():
                continue  # a package attribute (constant/function), not a module
            frontier |= self._module_level_imports(mod_path) - seen

    def test_extract_closure_never_imports_sgp4(self):
        """``lintle.extract`` reaches into ``verify.{checks,records}``
        directly for the shared catalog/element-set parsers (and reaches
        ``verify.epoch`` transitively via ``lintle.history``, which owns the
        epoch-datetime reduction shared with ``dedup``) — those edges are
        expected and fine — but it must never drag in ``sgp4`` itself, which
        stays the sole province of ``verify/orbit.py`` under the lazy
        ``--orbit`` gate. Same walk as the clean-path test, seeded at
        ``extract``. NOTE: ``_module_level_imports`` collapses any
        ``lintle.verify.X`` import to the single name ``"verify"`` (there is
        no ``verify.py`` file to descend into, only the ``verify/`` package),
        so this walk alone cannot see past that collapse into the ``verify``
        submodules — it would not notice ``extract`` reaching ``verify.orbit``.
        See ``test_verify_submodules_are_sgp4_free_except_orbit`` for the leg
        that actually enforces the submodule boundary."""
        src = Path(lintle.__file__).parent
        seen: set[str] = set()
        frontier = {"extract"}
        while frontier:
            name = frontier.pop()
            seen.add(name)
            assert name != "sgp4", f"sgp4 reached from lintle.extract via {seen}"
            mod_path = src / "__init__.py" if name == "__init__" else src / f"{name}.py"
            if not mod_path.is_file():
                continue  # a package attribute (constant/function), not a module
            frontier |= self._module_level_imports(mod_path) - seen

    def test_verify_submodules_are_sgp4_free_except_orbit(self):
        """Sweeps every ``lintle.verify.*`` module except ``orbit.py`` (the
        sanctioned sole ``sgp4`` importer) and asserts none of them import
        ``sgp4`` at module level, using the same ``_module_level_imports``
        detector the coarse closure walks above use. This is deliberately
        independent of which submodules any particular caller (``extract``,
        the shared ``history`` reducer, the future ``dedup`` manifest) happens
        to import directly: pinning the checked set to one caller's import
        list is fragile — it silently drops a submodule's own sgp4-freedom
        check the moment that caller stops importing it, which is exactly
        what happened when ``extract`` stopped importing ``verify.epoch``
        directly after the history reduction moved behind ``lintle.history``;
        nothing else was then checking ``epoch.py`` itself. Sweeping the whole
        ``verify/`` package unconditionally means new callers (or new
        indirection) can never quietly remove a module from coverage."""
        verify_dir = Path(lintle.__file__).parent / "verify"
        modules = sorted(p for p in verify_dir.glob("*.py") if p.stem != "orbit")
        assert modules, "expected to find verify submodules to sweep"
        for mod_path in modules:
            imports = self._module_level_imports(mod_path)
            assert "sgp4" not in imports, (
                f"verify.{mod_path.stem} imports sgp4 directly"
            )


class TestProgressLabels:
    """The phase-2 bar label must describe the stem it names: the record count
    beside a stem is that stem's own, not a running corpus total."""

    def test_record_count_in_the_label_resets_per_stem(self, tmp_path, monkeypatch):
        import contextlib

        from lintle import cli_progress, verify

        seen = []

        @contextlib.contextmanager
        def _capture(description, total):
            def update(**fields):
                if "description" in fields:
                    seen.append(fields["description"])

            yield update

        monkeypatch.setattr(cli_progress, "phase_bar", _capture)
        # Two stems of 100k records each: with a corpus-cumulative counter the
        # second stem's label would read 200,000.
        monkeypatch.setattr(verify.records, "cleaned_stems", lambda _d: ["a", "b"])
        sample = rec()
        monkeypatch.setattr(
            verify.records, "iter_file", lambda _d, _s: (sample for _ in range(100_000))
        )
        verify.run(str(tmp_path), None)

        counted = [d for d in seen if "records" in d]
        assert counted == [
            "verifying a — 100,000 records",
            "verifying b — 100,000 records",
        ]
