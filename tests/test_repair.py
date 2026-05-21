from tlekit import repair, tle


def test_strip_trailing_backslash(line1):
    raw = (line1 + "\\").encode("ascii")  # 70 bytes: 69 columns + '\'
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert err is None and clean == line1
    assert "trailing-backslash" in fixes


def test_reconstruct_missing_checksum(line1):
    raw = line1[:68].encode("ascii")  # 68 columns, checksum absent
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert err is None and clean == line1
    assert "reconstructed-checksum" in fixes


def test_reconstruct_with_backslash_artifact(line1):
    raw = (line1[:68] + "\\").encode("ascii")  # 69 bytes: 68 columns + '\'
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert err is None and clean == line1
    assert "trailing-backslash" in fixes and "reconstructed-checksum" in fixes


def test_crlf_normalised(line1):
    clean, fixes, err, cat = repair.repair_line((line1 + "\r").encode("ascii"), 1)
    assert err is None and clean == line1 and "crlf" in fixes


def test_checksum_mismatch_rejected(line1):
    raw = (line1[:68] + "9").encode("ascii")  # 69 chars, wrong checksum
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert clean is None and cat == "checksum-mismatch"


def test_non_ascii_byte_rejected(line1):
    clean, fixes, err, cat = repair.repair_line(line1.encode("ascii") + b"\xff", 1)
    assert clean is None and cat == "non-ascii"


def test_interior_character_missing_rejected(line1):
    # Delete an interior digit: 68 chars whose columns 1-68 fail layout.
    raw = (line1[:30] + line1[31:]).encode("ascii")
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert clean is None and cat == "interior-char-missing"


def test_wrong_length_rejected(line1):
    raw = (line1 + "XX").encode("ascii")  # 71 chars, not a known shape
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert clean is None and cat == "wrong-length"


def test_leading_whitespace_trimmed(line1):
    raw = ("  " + line1).encode("ascii")
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert err is None and clean == line1
    assert "leading-trim" in fixes


def test_trailing_whitespace_trimmed(line1):
    raw = (line1 + "  ").encode("ascii")
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert err is None and clean == line1
    assert "trailing-ws" in fixes


def test_invalid_columns_rejected(line1):
    # Replace line-number '1' with '3': valid length, bad column layout.
    bad = "3" + line1[1:]
    clean, fixes, err, cat = repair.repair_line(bad.encode("ascii"), 1)
    assert clean is None and cat == "invalid-columns"
