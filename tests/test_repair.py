"""Tests for lintle.repair — speculative, validated line and record repair."""

from lintle import repair, tle
from lintle.categories import FixClass
from lintle.diagnostics import RepairTier, RuleID


class TestRepairLine:
    def test_strip_trailing_backslash(self, line1):
        raw = (line1 + "\\").encode("ascii")  # 70 bytes: 69 columns + '\'
        clean, fixes, diag = repair.repair_line(raw, 1, source_line_no=1)
        assert diag is None and clean == line1
        assert FixClass.TRAILING_BACKSLASH in fixes

    def test_reconstruct_missing_checksum(self, line1):
        raw = line1[:68].encode("ascii")  # 68 columns, checksum absent
        clean, fixes, diag = repair.repair_line(raw, 1, source_line_no=1)
        assert diag is None and clean == line1
        assert FixClass.RECONSTRUCTED_CHECKSUM in fixes

    def test_reconstruct_with_backslash_artifact(self, line1):
        raw = (line1[:68] + "\\").encode("ascii")  # 69 bytes: 68 columns + '\'
        clean, fixes, diag = repair.repair_line(raw, 1, source_line_no=1)
        assert diag is None and clean == line1
        assert FixClass.TRAILING_BACKSLASH in fixes
        assert FixClass.RECONSTRUCTED_CHECKSUM in fixes

    def test_crlf_normalised(self, line1):
        clean, fixes, diag = repair.repair_line(
            (line1 + "\r").encode("ascii"), 1, source_line_no=1
        )
        assert diag is None and clean == line1 and FixClass.CRLF in fixes

    def test_checksum_mismatch_rejected(self, line1):
        raw = (line1[:68] + "9").encode("ascii")  # 69 chars, wrong checksum
        clean, fixes, diag = repair.repair_line(raw, 1, source_line_no=42)
        assert clean is None and diag.rule_id == RuleID.CHECKSUM_MISMATCH
        assert diag.column_range == (69, 69)
        assert diag.observed == "9"
        assert diag.expected == str(tle.compute_checksum(line1[:68] + "9"))
        assert diag.source_line_nos == (42,)

    def test_non_ascii_byte_rejected(self, line1):
        clean, fixes, diag = repair.repair_line(
            line1.encode("ascii") + b"\xff", 1, source_line_no=1
        )
        assert clean is None and diag.rule_id == RuleID.NON_ASCII_BYTE

    def test_interior_character_missing_rejected(self, line1):
        # Delete an interior digit: 68 chars whose columns 1-68 fail layout.
        raw = (line1[:30] + line1[31:]).encode("ascii")
        clean, fixes, diag = repair.repair_line(raw, 1, source_line_no=1)
        assert clean is None and diag.rule_id == RuleID.INTERIOR_CHAR_MISSING
        assert diag.tier_attempted == RepairTier.NORMALIZATION

    def test_wrong_length_rejected(self, line1):
        raw = (line1 + "XX").encode("ascii")  # 71 chars, not a known shape
        clean, fixes, diag = repair.repair_line(raw, 1, source_line_no=1)
        assert clean is None and diag.rule_id == RuleID.LINE_LENGTH
        assert diag.observed == "71"
        assert diag.expected == "68 or 69"

    def test_leading_whitespace_trimmed(self, line1):
        raw = ("  " + line1).encode("ascii")
        clean, fixes, diag = repair.repair_line(raw, 1, source_line_no=1)
        assert diag is None and clean == line1
        assert FixClass.LEADING_TRIM in fixes

    def test_trailing_whitespace_trimmed(self, line1):
        raw = (line1 + "  ").encode("ascii")
        clean, fixes, diag = repair.repair_line(raw, 1, source_line_no=1)
        assert diag is None and clean == line1
        assert FixClass.TRAILING_WS in fixes

    def test_invalid_columns_rejected(self, line1):
        # Replace line-number '1' with '3': valid length, bad column layout.
        bad = "3" + line1[1:]
        clean, fixes, diag = repair.repair_line(
            bad.encode("ascii"), 1, source_line_no=1
        )
        assert clean is None and diag.rule_id == RuleID.INVALID_COLUMN_LAYOUT


class TestProcessRecord:
    def test_process_accepts_clean_record(self, line1, line2):
        result = repair.repair_record(
            line1.encode("ascii"), 10, line2.encode("ascii"), 11
        )
        assert isinstance(result, repair.Accepted)
        assert result.line1 == line1 and result.line2 == line2
        assert result.fixes == []

    def test_process_repairs_backslash_and_checksum(self, line1, line2):
        raw1 = (line1[:68] + "\\").encode("ascii")  # checksumless + backslash
        raw2 = line2[:68].encode("ascii")  # checksumless
        result = repair.repair_record(raw1, 4, raw2, 5)
        assert isinstance(result, repair.Accepted)
        assert result.line1 == line1 and result.line2 == line2
        assert FixClass.TRAILING_BACKSLASH in result.fixes
        assert result.fixes.count(FixClass.RECONSTRUCTED_CHECKSUM) == 2  # one per line

    def test_process_quarantines_bad_line(self, line1, line2):
        raw1 = (line1[:68] + "9").encode("ascii")  # bad checksum
        result = repair.repair_record(raw1, 4, line2.encode("ascii"), 5)
        assert isinstance(result, repair.Quarantined)
        assert result.primary.rule_id == RuleID.CHECKSUM_MISMATCH
        assert result.related == ()
        assert result.source_lines == [4, 5]
        assert result.raw_lines == [raw1, line2.encode("ascii")]

    def test_process_quarantines_catalog_mismatch(self, line1, line2):
        other_body = "2 09999" + line2[7:68]
        other = other_body + str(tle.compute_checksum(other_body))
        result = repair.repair_record(
            line1.encode("ascii"), 1, other.encode("ascii"), 2
        )
        assert isinstance(result, repair.Quarantined)
        assert result.primary.rule_id == RuleID.CATALOG_MISMATCH
        assert result.primary.source_line_nos == (1, 2)
        # No reconstruction occurred; tier-1 (normalization) is correct.
        assert result.primary.tier_attempted == RepairTier.NORMALIZATION

    def test_catalog_mismatch_after_reconstruction_reports_tier_2(self, line1, line2):
        # Both line 1 and line 2 are checksumless (forcing tier-2
        # reconstruction), but they reference different NORAD IDs so the
        # record-level catalog check fails. The diagnostic must surface
        # tier-2 — a CATALOG_MISMATCH after reconstruction is a stronger
        # corruption signal than one caught at first read.
        other_body = "2 09999" + line2[7:68]
        # 68-char checksumless versions of each line:
        raw1 = line1[:68].encode("ascii")
        raw2 = other_body.encode("ascii")
        result = repair.repair_record(raw1, 1, raw2, 2)
        assert isinstance(result, repair.Quarantined)
        assert result.primary.rule_id == RuleID.CATALOG_MISMATCH
        assert result.primary.tier_attempted == RepairTier.CHECKSUM_RECONSTRUCT

    def test_process_quarantines_both_bad_lines(self, line1, line2):
        raw1 = (line1[:68] + "9").encode("ascii")  # line 1: bad checksum
        raw2 = line2.encode("ascii") + b"\xff"  # line 2: non-ASCII byte
        result = repair.repair_record(raw1, 1, raw2, 2)
        assert isinstance(result, repair.Quarantined)
        # Line 1's diagnostic is primary; line 2's is in related.
        assert result.primary.rule_id == RuleID.CHECKSUM_MISMATCH
        assert len(result.related) == 1
        assert result.related[0].rule_id == RuleID.NON_ASCII_BYTE
