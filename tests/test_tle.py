from tlekit import tle


def test_checksum_of_canonical_line1(line1):
    # NORAD 00005 line 1 checksum digit (column 69) is 3.
    assert tle.compute_checksum(line1) == 3
    assert tle.compute_checksum(line1) == int(line1[68])


def test_checksum_of_canonical_line2(line2):
    assert tle.compute_checksum(line2) == int(line2[68])


def test_minus_sign_counts_as_one():
    # compute_checksum sums only the first 68 characters.
    assert tle.compute_checksum("-" * 10 + " " * 58) == 0   # 10 % 10
    assert tle.compute_checksum("-" * 7 + " " * 61) == 7


def test_non_digit_non_minus_counts_as_zero():
    assert tle.compute_checksum("ABCDE.+ " * 8 + "    ") == 0
