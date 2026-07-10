"""Tests for ``lintle verify`` (Increment 1: the exhaustive, sgp4-free core)."""

import inspect
import json

from lintle import cli, pipeline, repair, tle
from lintle.verify import checks, epoch, grouping, records, report, run_verify
from lintle.verify.records import CleanedRecord
from lintle.verify.report import Suspect, VrfyRule

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


def build_tree(tmp_path, cleaned_pairs, source_lines=None, stem="tle01"):
    """Write a minimal clean-run output tree (and optional source file); return
    ``(out_dir, source_dir)`` as strings."""
    out = tmp_path / "output"
    (out / "cleaned").mkdir(parents=True, exist_ok=True)
    (out / "cleaned" / f"{stem}.cleaned.txt").write_text(
        "".join(f"{a}\n{b}\n" for a, b in cleaned_pairs), encoding="ascii"
    )
    src = tmp_path / "source"
    src.mkdir(parents=True, exist_ok=True)
    if source_lines is not None:
        (src / f"{stem}.txt").write_text(
            "".join(line + "\n" for line in source_lines), encoding="ascii"
        )
    return str(out), str(src)


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
        assert s is not None and s.rule is VrfyRule.REVALIDATE_FAIL
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
        assert list(checks.find_conflicts(iter(stream))) == []

    def test_different_orbital_elements_conflict(self):
        stream = [rec(idx=0), rec(line2=mutated_l2(), idx=1)]  # inclination differs
        out = list(checks.find_conflicts(iter(stream)))
        assert len(out) == 1
        assert out[0].rule is VrfyRule.EPOCH_CONFLICT

    def test_element_set_reissue_is_not_a_conflict(self):
        # same orbital state, only the element-set number (cols 65-68) differs
        r0, r1 = rec(idx=0), rec(line1=reissued_l1(), idx=1)
        assert r0.line1[:64] == r1.line1[:64] and r0.line1 != r1.line1
        assert list(checks.find_conflicts(iter([r0, r1]))) == []

    def test_revolution_number_reissue_is_not_a_conflict(self):
        # same orbital state, only the revolution number (cols 64-68) differs
        r0, r1 = rec(idx=0), rec(line2=reissued_l2(), idx=1)
        assert r0.line2[:63] == r1.line2[:63] and r0.line2 != r1.line2
        assert list(checks.find_conflicts(iter([r0, r1]))) == []

    def test_sign_encoding_reissue_is_not_a_conflict(self):
        # identical orbit; 2nd-deriv field ' 00000-0' vs '+00000-0' (same value).
        # The differing byte is inside cols 1-64, so a byte mask false-positives
        # (real-corpus #154: ~3.2M such re-issues flagged as contradictions).
        r0 = rec(line1=nddot_signed(" "), idx=0)
        r1 = rec(line1=nddot_signed("+"), idx=1)
        assert r0.line1[:64] != r1.line1[:64]  # would false-positive under a byte mask
        assert list(checks.find_conflicts(iter([r0, r1]))) == []

    def test_different_drag_value_still_conflicts(self):
        # a genuine B* difference IS a different orbital state -> still a conflict
        r0 = rec(line1=fix(L1[:53] + " 10000-3" + L1[61:]), idx=0)
        r1 = rec(line1=fix(L1[:53] + " 20000-3" + L1[61:]), idx=1)
        out = list(checks.find_conflicts(iter([r0, r1])))
        assert len(out) == 1 and out[0].rule is VrfyRule.EPOCH_CONFLICT

    def test_different_satellites_no_conflict(self):
        other1 = fix(L1[:2] + "00006" + L1[7:])  # different catalog
        stream = sorted(
            [rec(idx=0), rec(line1=other1, idx=1)],
            key=lambda r: (r.catalog, r.epoch_key),
        )
        assert list(checks.find_conflicts(iter(stream))) == []


class TestSourceAligner:
    def test_clean_padded_match(self, tmp_path):
        src = tmp_path / "s.txt"
        src.write_text(f"  {L1}  \n{L2}\r\n", encoding="ascii")
        aligner = checks.SourceAligner(str(src))
        assert aligner.check(rec()) is None
        aligner.close()

    def test_interior_mutation_flagged(self, tmp_path):
        src = tmp_path / "s.txt"
        src.write_text(f"{L1}\n{L2}\n", encoding="ascii")
        aligner = checks.SourceAligner(str(src))
        s = aligner.check(rec(line2=mutated_l2()))
        aligner.close()
        assert s is not None and s.rule is VrfyRule.INTERIOR_MUT

    def test_origin_missing(self, tmp_path):
        src = tmp_path / "s.txt"
        src.write_text("unrelated one\nunrelated two\n", encoding="ascii")
        aligner = checks.SourceAligner(str(src))
        s = aligner.check(rec())
        aligner.close()
        assert s is not None and s.rule is VrfyRule.ORIGIN_MISSING
        assert s.severity == "soft"

    def test_resyncs_across_quarantine_gap(self, tmp_path):
        # two dropped (garbage) source lines precede the real pair
        src = tmp_path / "s.txt"
        src.write_text(f"junk a\njunk b\n{L1}\n{L2}\n", encoding="ascii")
        aligner = checks.SourceAligner(str(src))
        assert aligner.check(rec()) is None
        aligner.close()

    def test_blank_line_between_pair_is_clean_match(self, tmp_path):
        # tle2019 source has stray blank lines between line 1 and line 2, both
        # missing their checksum (#155). clean skips the blank, pairs them, and
        # reconstructs the checksums; the aligner must skip the blank too, else
        # the interposed line breaks the adjacent-pair match -> false INTERIOR_MUT.
        src = tmp_path / "s.txt"
        src.write_text(f"{L1[:68]}\n\n{L2[:68]}\n", encoding="ascii")
        aligner = checks.SourceAligner(str(src))
        assert aligner.check(rec()) is None
        aligner.close()


class TestReport:
    def test_suspects_jsonl_is_deterministic(self):
        a = Suspect(VrfyRule.EPOCH_CONFLICT, 5, 2000179.0, "tle01", 2, "x")
        b = Suspect(VrfyRule.INTERIOR_MUT, 5, 2000179.0, "tle01", 1, "y")
        assert report.render_suspects_jsonl([a, b]) == report.render_suspects_jsonl(
            [b, a]
        )

    def test_exit_code(self):
        soft = Suspect(VrfyRule.ORIGIN_MISSING, 5, 1.0, "t", 0, "x")
        hard = Suspect(VrfyRule.INTERIOR_MUT, 5, 1.0, "t", 0, "y")
        assert report.exit_code([]) == 0
        assert report.exit_code([soft]) == 0
        assert report.exit_code([soft, hard]) == 1


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
        out, src = build_tree(tmp_path, [(L1, L2)], source_lines=[L1, L2])
        assert run_verify(out, src) == 0
        suspects = (tmp_path / "output" / "verify" / "suspects.jsonl").read_text()
        assert suspects == ""

    def test_interior_mutation_fails(self, tmp_path):
        out, src = build_tree(tmp_path, [(L1, mutated_l2())], source_lines=[L1, L2])
        assert run_verify(out, src) == 1
        rows = [
            json.loads(line)
            for line in (tmp_path / "output" / "verify" / "suspects.jsonl")
            .read_text()
            .splitlines()
        ]
        assert any(r["rule"] == "VRFY-INTERIOR-MUT" for r in rows)

    def test_contradiction_fails(self, tmp_path):
        # same satellite+epoch twice with different element bytes
        out, _ = build_tree(tmp_path, [(L1, L2), (L1, mutated_l2())])
        assert run_verify(out, None) == 1
        rows = (tmp_path / "output" / "verify" / "suspects.jsonl").read_text()
        assert "VRFY-EPOCH-CONFLICT" in rows

    def test_missing_cleaned_dir_is_operational_error(self, tmp_path):
        assert run_verify(str(tmp_path / "nope"), None) == 2

    def test_source_diff_skipped_when_no_source(self, tmp_path):
        out, _ = build_tree(tmp_path, [(L1, L2)])
        assert run_verify(out, None) == 0
        summary = json.loads(
            (tmp_path / "output" / "verify" / "summary.json").read_text()
        )
        assert summary["checked"]["source_diff"] == "skipped"


class TestCLI:
    def test_verify_subcommand_dispatches(self, tmp_path):
        out, src = build_tree(tmp_path, [(L1, L2)], source_lines=[L1, L2])
        assert cli.main(["verify", out, "--source", src]) == 0

    def test_verify_flags_via_cli(self, tmp_path):
        out, src = build_tree(tmp_path, [(L1, mutated_l2())], source_lines=[L1, L2])
        assert cli.main(["verify", out, "--source", src]) == 1


class TestImportGuard:
    def test_clean_core_never_imports_sgp4_or_verify(self):
        for module in (tle, repair, pipeline):
            source = inspect.getsource(module)
            assert "import sgp4" not in source
            assert "from sgp4" not in source
            assert "lintle.verify" not in source
            assert "import verify" not in source
