from tlekit import tle


def test_checksum_of_canonical_line1(line1):
    # NORAD 00005 line 1 checksum digit (column 69) is 3.
    assert tle.compute_checksum(line1) == 3
    assert tle.compute_checksum(line1) == int(line1[68])


def test_checksum_of_canonical_line2(line2):
    assert tle.compute_checksum(line2) == int(line2[68])


def test_minus_sign_counts_as_one():
    # compute_checksum sums only the first 68 characters.
    assert tle.compute_checksum("-" * 10 + " " * 58) == 0  # 10 % 10
    assert tle.compute_checksum("-" * 7 + " " * 61) == 7


def test_non_digit_non_minus_counts_as_zero():
    assert tle.compute_checksum("ABCDE.+ " * 8 + "    ") == 0


def test_valid_line1_passes_column_checks(line1):
    assert tle._check_columns(line1[:68], 1) == []


def test_valid_line2_passes_column_checks(line2):
    assert tle._check_columns(line2[:68], 2) == []


def test_wrong_body_length_reported():
    errs = tle._check_columns("1 00005U", 1)
    assert errs and "length" in errs[0]


def test_bad_line_number_prefix(line1):
    body = "9" + line1[1:68]
    assert any("line number" in e for e in tle._check_columns(body, 1))


def test_missing_separator_space(line2):
    # Index 8 (column 9) starts the inclination field; 'X' is not in DIGIT_SPACE.
    body = line2[:8] + "X" + line2[9:68]
    assert tle._check_columns(body, 2)


def test_letter_in_digit_only_field_rejected(line1):
    # Epoch year (columns 19-20) must be digits.
    body = line1[:18] + "X" + line1[19:68]
    assert tle._check_columns(body, 1)


def test_validate_body_accepts_canonical(line1, line2):
    assert tle.validate_body(line1[:68], 1) == []
    assert tle.validate_body(line2[:68], 2) == []


def test_inclination_out_of_range_rejected(line2):
    # Replace columns 9-16 with an inclination of 999.2682 degrees.
    body = line2[:8] + "999.2682" + line2[16:68]
    assert any("inclination" in e for e in tle.validate_body(body, 2))


def test_mean_motion_must_be_positive(line2):
    body = line2[:52] + "00.00000000" + line2[63:68]
    assert any("mean motion" in e for e in tle.validate_body(body, 2))


def test_column_failure_short_circuits_semantics(line1):
    # A bad prefix is a column error; semantics are not even attempted.
    errs = tle.validate_body("9" + line1[1:68], 1)
    assert errs and all("column" in e or "length" in e for e in errs)


def test_validate_line_accepts_canonical(line1, line2):
    assert tle.validate_line(line1, 1) == []
    assert tle.validate_line(line2, 2) == []


def test_validate_line_rejects_wrong_length(line1):
    assert tle.validate_line(line1[:68], 1)  # 68 chars -> error


def test_checksum_mismatch_detected(line1):
    bad = line1[:68] + "9"  # canonical checksum is 3
    assert any("checksum" in e for e in tle.validate_line(bad, 1))


def test_checksum_error_returns_none_when_valid(line1):
    assert tle.checksum_error(line1) is None


def test_validate_record_accepts_canonical(line1, line2):
    assert tle.validate_record(line1, line2) == []


def test_validate_record_detects_catalog_mismatch(line1, line2):
    other_body = "2 09999" + line2[7:68]
    other = other_body + str(tle.compute_checksum(other_body))
    assert any("catalog" in e for e in tle.validate_record(line1, other))


def test_checksum_error_non_digit(line1):
    # The non-digit checksum branch is distinct from a numeric mismatch.
    bad = line1[:68] + "X"
    err = tle.checksum_error(bad)
    assert err is not None and "not a digit" in err
