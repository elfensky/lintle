"""Tests for lintle.repair — speculative, validated line and record repair."""

from lintle import repair, tle
from lintle.categories import FixClass, RejectCategory


class TestRepairLine:
    def test_strip_trailing_backslash(self, line1):
        raw = (line1 + "\\").encode("ascii")  # 70 bytes: 69 columns + '\'
        clean, fixes, err, cat = repair.repair_line(raw, 1)
        assert err is None and clean == line1
        assert FixClass.TRAILING_BACKSLASH in fixes

    def test_reconstruct_missing_checksum(self, line1):
        raw = line1[:68].encode("ascii")  # 68 columns, checksum absent
        clean, fixes, err, cat = repair.repair_line(raw, 1)
        assert err is None and clean == line1
        assert FixClass.RECONSTRUCTED_CHECKSUM in fixes

    def test_reconstruct_with_backslash_artifact(self, line1):
        raw = (line1[:68] + "\\").encode("ascii")  # 69 bytes: 68 columns + '\'
        clean, fixes, err, cat = repair.repair_line(raw, 1)
        assert err is None and clean == line1
        assert FixClass.TRAILING_BACKSLASH in fixes
        assert FixClass.RECONSTRUCTED_CHECKSUM in fixes

    def test_crlf_normalised(self, line1):
        clean, fixes, err, cat = repair.repair_line((line1 + "\r").encode("ascii"), 1)
        assert err is None and clean == line1 and FixClass.CRLF in fixes

    def test_checksum_mismatch_rejected(self, line1):
        raw = (line1[:68] + "9").encode("ascii")  # 69 chars, wrong checksum
        clean, fixes, err, cat = repair.repair_line(raw, 1)
        assert clean is None and cat == RejectCategory.CHECKSUM_MISMATCH

    def test_non_ascii_byte_rejected(self, line1):
        clean, fixes, err, cat = repair.repair_line(line1.encode("ascii") + b"\xff", 1)
        assert clean is None and cat == RejectCategory.NON_ASCII

    def test_interior_character_missing_rejected(self, line1):
        # Delete an interior digit: 68 chars whose columns 1-68 fail layout.
        raw = (line1[:30] + line1[31:]).encode("ascii")
        clean, fixes, err, cat = repair.repair_line(raw, 1)
        assert clean is None and cat == RejectCategory.INTERIOR_CHAR_MISSING

    def test_wrong_length_rejected(self, line1):
        raw = (line1 + "XX").encode("ascii")  # 71 chars, not a known shape
        clean, fixes, err, cat = repair.repair_line(raw, 1)
        assert clean is None and cat == RejectCategory.WRONG_LENGTH

    def test_leading_whitespace_trimmed(self, line1):
        raw = ("  " + line1).encode("ascii")
        clean, fixes, err, cat = repair.repair_line(raw, 1)
        assert err is None and clean == line1
        assert FixClass.LEADING_TRIM in fixes

    def test_trailing_whitespace_trimmed(self, line1):
        raw = (line1 + "  ").encode("ascii")
        clean, fixes, err, cat = repair.repair_line(raw, 1)
        assert err is None and clean == line1
        assert FixClass.TRAILING_WS in fixes

    def test_invalid_columns_rejected(self, line1):
        # Replace line-number '1' with '3': valid length, bad column layout.
        bad = "3" + line1[1:]
        clean, fixes, err, cat = repair.repair_line(bad.encode("ascii"), 1)
        assert clean is None and cat == RejectCategory.INVALID_COLUMNS


class TestProcessRecord:
    def test_process_accepts_clean_record(self, line1, line2):
        result = repair.process_record(
            line1.encode("ascii"), 10, line2.encode("ascii"), 11
        )
        assert isinstance(result, repair.Accepted)
        assert result.line1 == line1 and result.line2 == line2
        assert result.fixes == []

    def test_process_repairs_backslash_and_checksum(self, line1, line2):
        raw1 = (line1[:68] + "\\").encode("ascii")  # checksumless + backslash
        raw2 = line2[:68].encode("ascii")  # checksumless
        result = repair.process_record(raw1, 4, raw2, 5)
        assert isinstance(result, repair.Accepted)
        assert result.line1 == line1 and result.line2 == line2
        assert FixClass.TRAILING_BACKSLASH in result.fixes
        assert result.fixes.count(FixClass.RECONSTRUCTED_CHECKSUM) == 2  # one per line

    def test_process_rejects_bad_line(self, line1, line2):
        raw1 = (line1[:68] + "9").encode("ascii")  # bad checksum
        result = repair.process_record(raw1, 4, line2.encode("ascii"), 5)
        assert isinstance(result, repair.Rejected)
        assert result.category == RejectCategory.CHECKSUM_MISMATCH
        assert result.source_lines == [4, 5]
        assert result.raw_lines == [raw1, line2.encode("ascii")]

    def test_process_rejects_catalog_mismatch(self, line1, line2):
        other_body = "2 09999" + line2[7:68]
        other = other_body + str(tle.compute_checksum(other_body))
        result = repair.process_record(
            line1.encode("ascii"), 1, other.encode("ascii"), 2
        )
        assert isinstance(result, repair.Rejected)
        assert result.category == RejectCategory.CATALOG_MISMATCH

    def test_process_rejects_both_bad_lines(self, line1, line2):
        raw1 = (line1[:68] + "9").encode("ascii")  # line 1: bad checksum
        raw2 = line2.encode("ascii") + b"\xff"  # line 2: non-ASCII byte
        result = repair.process_record(raw1, 1, raw2, 2)
        assert isinstance(result, repair.Rejected)
        # Both failures are preserved in the human-readable reason...
        assert "line 1:" in result.reason and "line 2:" in result.reason
        # ...and the record's category is line 1's (deterministic precedence).
        assert result.category == RejectCategory.CHECKSUM_MISMATCH
