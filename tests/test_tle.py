"""Tests for lintle.tle — the TLE validator."""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lintle import tle


class TestComputeChecksum:
    def test_checksum_of_canonical_line1(self, line1):
        # NORAD 00005 line 1 checksum digit (column 69) is 3.
        assert tle.compute_checksum(line1) == 3
        assert tle.compute_checksum(line1) == int(line1[68])

    def test_checksum_of_canonical_line2(self, line2):
        assert tle.compute_checksum(line2) == int(line2[68])

    def test_minus_sign_counts_as_one(self):
        # compute_checksum sums only the first 68 characters.
        assert tle.compute_checksum("-" * 10 + " " * 58) == 0  # 10 % 10
        assert tle.compute_checksum("-" * 7 + " " * 61) == 7

    def test_non_digit_non_minus_counts_as_zero(self):
        assert tle.compute_checksum("ABCDE.+ " * 8 + "    ") == 0

    def test_unicode_digit_counts_as_zero_not_value(self):
        # '²' (SUPERSCRIPT TWO) is str.isdigit()-True but int('²')
        # raises ValueError; only ASCII 0-9 may contribute to the checksum,
        # so it must be treated as a zero-contributing character, not crash.
        assert tle.compute_checksum("²" + " " * 67) == 0


class TestCheckColumns:
    def test_valid_line1_passes_column_checks(self, line1):
        assert tle._check_columns(line1[:68], 1) == []

    def test_valid_line2_passes_column_checks(self, line2):
        assert tle._check_columns(line2[:68], 2) == []

    def test_wrong_body_length_reported(self):
        errs = tle._check_columns("1 00005U", 1)
        assert errs and "length" in errs[0]

    def test_bad_line_number_prefix(self, line1):
        body = "9" + line1[1:68]
        assert any("line number" in e for e in tle._check_columns(body, 1))

    def test_missing_separator_space(self, line2):
        # Index 8 (column 9) starts the inclination field; 'X' is not in DIGIT_SPACE.
        body = line2[:8] + "X" + line2[9:68]
        assert tle._check_columns(body, 2)

    def test_letter_in_digit_only_field_rejected(self, line1):
        # Epoch year (columns 19-20) must be digits.
        body = line1[:18] + "X" + line1[19:68]
        assert tle._check_columns(body, 1)


class TestValidateBody:
    def test_validate_body_accepts_canonical(self, line1, line2):
        assert tle.validate_body(line1[:68], 1) == []
        assert tle.validate_body(line2[:68], 2) == []

    def test_inclination_out_of_range_rejected(self, line2):
        # Replace columns 9-16 with an inclination of 999.2682 degrees.
        body = line2[:8] + "999.2682" + line2[16:68]
        assert any("inclination" in e for e in tle.validate_body(body, 2))

    def test_mean_motion_must_be_positive(self, line2):
        body = line2[:52] + "00.00000000" + line2[63:68]
        assert any("mean motion" in e for e in tle.validate_body(body, 2))

    def test_column_failure_short_circuits_semantics(self, line1):
        # A bad prefix is a column error; semantics are not even attempted.
        errs = tle.validate_body("9" + line1[1:68], 1)
        assert errs and all("column" in e or "length" in e for e in errs)


class TestChecksumError:
    def test_checksum_error_returns_none_when_valid(self, line1):
        assert tle.checksum_error(line1) is None

    def test_checksum_error_non_digit(self, line1):
        # The non-digit checksum branch is distinct from a numeric mismatch.
        bad = line1[:68] + "X"
        err = tle.checksum_error(bad)
        assert err is not None and "not a digit" in err

    def test_checksum_error_rejects_unicode_digit(self, line1):
        # '٣' (ARABIC-INDIC DIGIT THREE) is str.isdigit()-True and int()==3,
        # which equals the canonical checksum — so it would spuriously
        # validate the line as perfect. Only ASCII 0-9 are valid in column 69.
        assert tle.compute_checksum(line1) == 3  # canonical checksum is 3
        bad = line1[:68] + "٣"
        err = tle.checksum_error(bad)
        assert err is not None and "not a digit" in err


class TestValidateLine:
    def test_validate_line_accepts_canonical(self, line1, line2):
        assert tle.validate_line(line1, 1) == []
        assert tle.validate_line(line2, 2) == []

    def test_validate_line_rejects_wrong_length(self, line1):
        assert tle.validate_line(line1[:68], 1)  # 68 chars -> error

    def test_checksum_mismatch_detected(self, line1):
        bad = line1[:68] + "9"  # canonical checksum is 3
        assert any("checksum" in e for e in tle.validate_line(bad, 1))


class TestValidateRecord:
    def test_validate_record_accepts_canonical(self, line1, line2):
        assert tle.validate_record(line1, line2) == []

    def test_validate_record_detects_catalog_mismatch(self, line1, line2):
        other_body = "2 09999" + line2[7:68]
        other = other_body + str(tle.compute_checksum(other_body))
        assert any("catalog" in e for e in tle.validate_record(line1, other))


class TestChecksumRoutingWordIsPinned:
    """repair.py routes a failed repair to the public RuleID CHECKSUM_MISMATCH
    vs INVALID_COLUMN_LAYOUT by substring-matching ``"checksum"`` in the
    validator's prose (``any("checksum" in e for e in errors)``). That contract
    is unpinned, so a future reword could silently misroute a never-recycled
    RuleID. Pin it from both sides (issue #106): checksum errors MUST carry the
    word; column/semantic errors and field descriptions MUST NOT.
    """

    def test_checksum_errors_contain_the_word(self, line1):
        assert "checksum" in tle.checksum_error(line1[:68] + "X")  # non-digit
        assert "checksum" in tle.checksum_error(line1[:68] + "9")  # wrong digit

    def test_no_column_or_semantic_error_contains_checksum(self, line1, line2):
        cases = [
            ("9" + line1[1:68], 1),  # bad line-number prefix (column)
            (line1[:18] + "X" + line1[19:68], 1),  # letter in digit field (column)
            (line2[:8] + "999.2682" + line2[16:68], 2),  # inclination range (semantic)
            (line2[:52] + "00.00000000" + line2[63:68], 2),  # mean motion (semantic)
        ]
        for body, lineno in cases:
            errs = tle.validate_body(body, lineno)
            assert errs, (body, lineno)  # genuinely invalid
            assert not any("checksum" in e for e in errs), (lineno, errs)

    def test_no_field_description_mentions_checksum(self):
        for chars, fields in tle._LINE_SPEC.values():
            for *_, desc in chars:
                assert "checksum" not in desc.lower()
            for *_, desc in fields:
                assert "checksum" not in desc.lower()


class TestValidateRecordCatalog:
    """Tests for the fast catalog-only cross-check (issue #109)."""

    def test_matching_catalog_returns_empty(self, line1, line2):
        assert tle.validate_record_catalog(line1, line2) == []

    def test_mismatch_returns_same_error_as_validate_record(self, line1, line2):
        other_body = "2 09999" + line2[7:68]
        other = other_body + str(tle.compute_checksum(other_body))
        cat_errors = tle.validate_record_catalog(line1, other)
        full_errors = tle.validate_record(line1, other)
        # validate_record on two valid lines returns only the catalog error.
        assert cat_errors == full_errors

    @given(
        satnum=st.integers(min_value=0, max_value=99999),
    )
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_equivalence_for_any_valid_pair(self, line1, line2, satnum):
        """validate_record_catalog == validate_record for two valid lines."""
        sat_field = f"{satnum:05d}"
        body1 = line1[:2] + sat_field + line1[7:68]
        body2 = line2[:2] + sat_field + line2[7:68]
        l1 = body1 + str(tle.compute_checksum(body1))
        l2 = body2 + str(tle.compute_checksum(body2))
        # Both are valid; catalog cross-check fast path must agree with full check.
        assert tle.validate_record_catalog(l1, l2) == tle.validate_record(l1, l2)

    @given(
        satnum1=st.integers(min_value=0, max_value=99999),
        satnum2=st.integers(min_value=0, max_value=99999),
    )
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_equivalence_for_mismatched_valid_pair(
        self, line1, line2, satnum1, satnum2
    ):
        """Mismatch case: both functions return the same single catalog error."""
        sat1 = f"{satnum1:05d}"
        sat2 = f"{satnum2:05d}"
        body1 = line1[:2] + sat1 + line1[7:68]
        body2 = line2[:2] + sat2 + line2[7:68]
        l1 = body1 + str(tle.compute_checksum(body1))
        l2 = body2 + str(tle.compute_checksum(body2))
        assert tle.validate_record_catalog(l1, l2) == tle.validate_record(l1, l2)


class TestFieldError:
    """The validator's structured error type (#120): each error is its own
    prose string (so all human output and substring checks are byte-identical)
    AND carries typed routing/column fields for repair + report.jsonl."""

    def test_column_error_is_str_with_structured_fields(self, line1):
        body = "9" + line1[1:68]  # bad line-number column (column 1)
        (err,) = [e for e in tle._check_columns(body, 1) if "line number" in e]
        assert isinstance(err, str)  # byte-compatible prose
        assert err == "column 1 (line number): got '9', expected one of '1'"
        assert err.kind == "column"
        assert err.column_range == (1, 1)
        assert err.observed == "9"
        assert err.expected == "1"

    def test_field_error_in_a_multi_char_field(self, line1):
        body = line1[:18] + "X" + line1[19:68]  # letter in the epoch-year field
        (err,) = [e for e in tle._check_columns(body, 1) if "epoch year" in e]
        assert err.kind == "column"
        assert err.column_range == (19, 20)  # epoch year is columns 19-20
        assert err.observed == body[18:20]  # the offending field substring
        # A multi-char field constraint is a charset, not a single value:
        # `expected` stays None (the full set is in the prose), avoiding a
        # misleading 16-char-truncated charset in report.jsonl.
        assert err.expected is None

    def test_checksum_error_kind_and_fields(self, line1):
        err = tle.checksum_error(line1[:68] + "9")  # wrong checksum digit
        assert err.kind == "checksum"
        assert err.column_range == (69, 69)
        assert err.observed == "9"
        assert err.expected == str(tle.compute_checksum(line1[:68] + "9"))

    def test_semantic_error_carries_field_span(self, line2):
        body = line2[:8] + "999.2682" + line2[16:68]  # inclination out of range
        (err,) = [e for e in tle.validate_body(body, 2) if "inclination" in e]
        assert err.kind == "semantic"
        assert err.column_range == (9, 16)

    def test_catalog_error_kind_and_fields(self, line1, line2):
        other_body = "2 09999" + line2[7:68]
        other = other_body + str(tle.compute_checksum(other_body))
        (err,) = tle.validate_record_catalog(line1, other)
        assert err.kind == "catalog"
        assert err.column_range == (3, 7)
        assert err.observed == line1[2:7] and err.expected == other[2:7]


class TestExtractNoradId:
    def test_extracts_from_canonical_line1(self, line1):
        assert tle.extract_norad_id(line1) == 5

    def test_extracts_from_canonical_line1_bytes(self, line1):
        assert tle.extract_norad_id(line1.encode("ascii")) == 5

    def test_extracts_from_line1_orphan_without_checksum(self, line1):
        # A truncated line 1 (68 chars, no checksum) is still readable for
        # the catalog-number field — extraction is independent of the rest
        # of the line, including the checksum at column 69.
        assert tle.extract_norad_id(line1[:68]) == 5

    def test_returns_none_for_line2_prefix(self, line2):
        # A line 2 starts "2 " — the issue is explicit that only line 1's
        # catalog field counts as a decodable NORAD ID. Line 2 may carry a
        # matching number but is rejected by prefix to keep the rule simple.
        assert tle.extract_norad_id(line2) is None

    def test_returns_none_for_bad_prefix(self):
        assert tle.extract_norad_id("garbage line not a tle") is None

    def test_returns_none_for_short_line(self):
        # A truncated "1 " line with fewer than 7 characters has no catalog
        # field to read at all — must not raise.
        assert tle.extract_norad_id("1 12") is None

    def test_returns_none_for_non_digit_field(self, line1):
        # Modern Alpha-5 NORAD encoding allows letters (e.g. "B1234"),
        # but the issue's contract is "5-digit integer" — letters are
        # treated as undecodable so downstream sees only pure-int IDs.
        body = "1 B0005" + line1[7:]
        assert tle.extract_norad_id(body) is None

    def test_returns_none_for_non_ascii_bytes(self):
        # A line with a non-ASCII byte in the prefix region is unreadable
        # via the canonical extractor — must not raise on decode failure.
        assert tle.extract_norad_id(b"1 \xff0005U more") is None

    def test_leading_zeros_normalize_to_int(self):
        # NORAD 5 is the canonical satellite (Vanguard 1) and renders as
        # "00005" on the wire — the extractor returns the integer, not the
        # zero-padded string, so dedup across "  005" and "00005" collapses.
        body = "1 00005U junk"
        assert tle.extract_norad_id(body) == 5


class TestSemanticBoundaries:
    """Explicit boundary-value tests for every _check_semantics range.

    Inclusive edges are accepted; exclusive edges are rejected. These
    document the intended bounds and anchor the hypothesis property tests.
    """

    # epoch day-of-year (line 1): 0.0 < day < 367.0 (both exclusive)
    def test_epoch_day_lower_exclusive_rejected(self, line1):
        body = line1[:20] + "000.00000000" + line1[32:68]  # day = 0.0
        assert any("epoch day-of-year" in e for e in tle.validate_body(body, 1))

    def test_epoch_day_just_above_zero_accepted(self, line1):
        body = line1[:20] + "000.00100000" + line1[32:68]  # day = 0.001
        assert not any("epoch day-of-year" in e for e in tle.validate_body(body, 1))

    def test_epoch_day_upper_exclusive_rejected(self, line1):
        body = line1[:20] + "367.00000000" + line1[32:68]  # day = 367.0
        assert any("epoch day-of-year" in e for e in tle.validate_body(body, 1))

    def test_epoch_day_just_below_upper_accepted(self, line1):
        body = line1[:20] + "366.99900000" + line1[32:68]  # day = 366.999
        assert not any("epoch day-of-year" in e for e in tle.validate_body(body, 1))

    # inclination (line 2): 0.0 <= inc <= 180.0 (both inclusive)
    def test_inclination_lower_inclusive_accepted(self, line2):
        body = line2[:8] + "000.0000" + line2[16:68]  # inc = 0.0
        assert not any("inclination" in e for e in tle.validate_body(body, 2))

    def test_inclination_upper_inclusive_accepted(self, line2):
        body = line2[:8] + "180.0000" + line2[16:68]  # inc = 180.0
        assert not any("inclination" in e for e in tle.validate_body(body, 2))

    def test_inclination_just_above_upper_rejected(self, line2):
        body = line2[:8] + "180.0001" + line2[16:68]  # inc = 180.0001
        assert any("inclination" in e for e in tle.validate_body(body, 2))

    # RAAN (line 2): 0.0 <= raan < 360.0 (inclusive lower, exclusive upper)
    def test_raan_upper_exclusive_rejected(self, line2):
        body = line2[:17] + "360.0000" + line2[25:68]  # raan = 360.0
        assert any("RAAN" in e for e in tle.validate_body(body, 2))

    def test_raan_just_below_upper_accepted(self, line2):
        body = line2[:17] + "359.9999" + line2[25:68]  # raan = 359.9999
        assert not any("RAAN" in e for e in tle.validate_body(body, 2))

    # eccentricity (line 2): 0.0 <= ecc < 1.0; field = int(body[26:33]) / 1e7
    def test_eccentricity_zero_accepted(self, line2):
        body = line2[:26] + "0000000" + line2[33:68]  # ecc = 0.0
        assert not any("eccentricity" in e for e in tle.validate_body(body, 2))

    def test_eccentricity_max_field_accepted(self, line2):
        # 9999999 -> 0.9999999, the largest value a 7-digit field can encode;
        # the < 1.0 upper bound is therefore structurally unreachable via
        # column data (the rejection branch is defensive only).
        body = line2[:26] + "9999999" + line2[33:68]
        assert not any("eccentricity" in e for e in tle.validate_body(body, 2))

    # argument of perigee (line 2): 0.0 <= argp < 360.0
    def test_argp_upper_exclusive_rejected(self, line2):
        body = line2[:34] + "360.0000" + line2[42:68]  # argp = 360.0
        assert any("argument of perigee" in e for e in tle.validate_body(body, 2))

    def test_argp_just_below_upper_accepted(self, line2):
        body = line2[:34] + "359.9999" + line2[42:68]  # argp = 359.9999
        assert not any("argument of perigee" in e for e in tle.validate_body(body, 2))

    # mean anomaly (line 2): 0.0 <= mean_anom < 360.0
    def test_mean_anomaly_upper_exclusive_rejected(self, line2):
        body = line2[:43] + "360.0000" + line2[51:68]  # mean_anom = 360.0
        assert any("mean anomaly" in e for e in tle.validate_body(body, 2))

    def test_mean_anomaly_just_below_upper_accepted(self, line2):
        body = line2[:43] + "359.9999" + line2[51:68]  # mean_anom = 359.9999
        assert not any("mean anomaly" in e for e in tle.validate_body(body, 2))

    # mean motion (line 2): mean_motion > 0.0 (strictly positive)
    def test_mean_motion_small_positive_accepted(self, line2):
        body = line2[:52] + "00.00010000" + line2[63:68]  # 0.0001 rev/day
        assert not any("mean motion" in e for e in tle.validate_body(body, 2))

    # numeric-parse-failure path (call _check_semantics directly: it assumes
    # columns already passed, so a parse-breaking field reaches the except branch)
    def test_unparseable_numeric_field_reports_parse_failure(self, line2):
        body = line2[:26] + "       " + line2[33:68]  # eccentricity = 7 spaces
        errs = tle._check_semantics(body, 2)
        assert any("could not be parsed" in e for e in errs)


class TestChecksumProperties:
    """Property-based invariants for the mod-10 checksum."""

    @given(
        st.text(
            alphabet=st.characters(min_codepoint=32, max_codepoint=126),
            min_size=68,
            max_size=68,
        )
    )
    def test_checksum_is_a_single_digit(self, body):
        assert tle.compute_checksum(body) in range(10)

    @given(st.text(alphabet="0123456789 .-+", min_size=68, max_size=68))
    def test_appended_checksum_satisfies_checksum_error(self, body):
        line = body + str(tle.compute_checksum(body))
        assert tle.checksum_error(line) is None

    @given(
        st.text(alphabet="0123456789 .-+", min_size=68, max_size=68),
        st.integers(min_value=1, max_value=9),
    )
    def test_wrong_checksum_digit_is_rejected(self, body, offset):
        correct = tle.compute_checksum(body)
        wrong = (correct + offset) % 10
        assert tle.checksum_error(body + str(wrong)) is not None


class TestSemanticRangeProperties:
    """Fuzz inclination around its [0, 180] bound on a valid line-2 body."""

    @given(
        st.floats(min_value=0.0, max_value=270.0, allow_nan=False, allow_infinity=False)
    )
    @settings(
        max_examples=300,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_inclination_accepted_iff_in_range(self, line2, inc):
        # Non-negative only: a leading '-' would fail column layout (a column
        # error, not an inclination error), desyncing the oracle. 0..270 still
        # spans in-range and above-range. Width is always 8 (e.g. "270.0000").
        field = f"{inc:08.4f}"
        body = line2[:8] + field + line2[16:68]
        in_range = 0.0 <= float(field) <= 180.0  # value as the column encodes it
        has_error = any("inclination" in e for e in tle.validate_body(body, 2))
        assert has_error != in_range
