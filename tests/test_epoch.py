"""Tests for ``lintle.epoch`` — the single normalized definition of a record's
moment in time (#199). Grouped per behaviour: normalization rolls, bit-equal
keys for equal instants, the hypothesis monotonicity/instant invariants, the
year pivot, v1 key back-compat, and the raise-on-garbage contract."""

import calendar
import datetime as dt

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lintle.epoch import epoch_dt, epoch_key, iso, parse_epoch


def _line1(yy: int, day: str) -> str:
    """A 69-char line 1 whose epoch columns are ``yy`` + ``day`` (a 12-char
    ``DDD.FFFFFFFF`` string); the other columns are plausible filler — the
    epoch functions slice only ``[18:32]``."""
    assert len(day) == 12
    return f"1 00005U 58002B   {yy:02d}{day}" + " " * 37


# Line-1 epochs as digit strings: any 2-digit year, whole day 000-366, 8
# fractional digits — the full (0, 367) range tle.py accepts, zero excluded.
_epochs = (
    st.tuples(
        st.integers(0, 99),
        st.integers(0, 366),
        st.text("0123456789", min_size=8, max_size=8),
    )
    .map(lambda t: _line1(t[0], f"{t[1]:03d}.{t[2]}"))
    .filter(lambda line: float(line[20:23] + "." + line[24:32]) > 0.0)
)


def _v1_key(line1: str) -> float:
    """The pre-#199 formula, verbatim: raw pivot year * 1000 + raw day."""
    yy = int(line1[18:20])
    year = 2000 + yy if yy < 57 else 1900 + yy
    return year * 1000.0 + float(line1[20:23] + "." + line1[24:32])


class TestNormalization:
    def test_nonleap_366_rolls_forward(self):
        assert parse_epoch(_line1(19, "366.50000000")) == (2020, 1.5)

    def test_leap_366_stays(self):
        # Year 2000 pins the divisible-by-400 leap rule.
        assert calendar.isleap(2000)
        assert parse_epoch(_line1(0, "366.50000000")) == (2000, 366.5)

    def test_day_zero_rolls_back_using_prior_year_length(self):
        # 2020 is leap: day 0.5 of 2021 is 2020's day 366.5, not 365.5.
        assert parse_epoch(_line1(21, "000.50000000")) == (2020, 366.5)

    def test_in_range_unchanged(self):
        assert parse_epoch(_line1(19, "365.50000000")) == (2019, 365.5)

    def test_rolled_instant_is_the_true_instant(self):
        assert iso(epoch_dt(_line1(19, "366.50000000"))) == "2020-01-01T12:00:00Z"
        assert iso(epoch_dt(_line1(20, "000.50000000"))) == "2019-12-31T12:00:00Z"


class TestSameInstantSameKey:
    def test_boundary_reissue_pair_bit_equal(self):
        k_late = epoch_key(_line1(19, "365.50000000"))
        k_early = epoch_key(_line1(20, "000.50000000"))
        assert k_late == k_early
        assert repr(k_late) == repr(k_early)

    def test_full_fraction_bit_equal(self):
        # The string re-forming guarantee: a rolled 8-digit fraction is
        # bit-identical to the literal in-range spelling of the same instant.
        k_rolled = epoch_key(_line1(19, "366.99999999"))
        k_literal = epoch_key(_line1(20, "001.99999999"))
        assert k_rolled == k_literal
        assert repr(k_rolled) == repr(k_literal)


class TestKeyIsMonotoneInInstant:
    @given(_epochs, _epochs)
    def test_key_order_is_instant_order(self, a, b):
        assert (epoch_key(a) < epoch_key(b)) == (epoch_dt(a) < epoch_dt(b))
        assert (epoch_key(a) == epoch_key(b)) == (epoch_dt(a) == epoch_dt(b))


class TestEpochDtUnchanged:
    @given(_epochs)
    def test_matches_raw_arithmetic(self, line1):
        # Normalization must not move any instant: the datetime equals the
        # pre-#199 raw computation (timedelta rolled year boundaries anyway).
        yy = int(line1[18:20])
        year = 2000 + yy if yy < 57 else 1900 + yy
        day = float(line1[20:23] + "." + line1[24:32])
        raw = dt.datetime(year, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=day - 1)
        assert epoch_dt(line1) == raw


class TestYearPivot:
    def test_57_is_1957(self):
        assert parse_epoch(_line1(57, "100.00000000"))[0] == 1957

    def test_56_is_2056(self):
        assert parse_epoch(_line1(56, "100.00000000"))[0] == 2056

    def test_pivot_edge_roll_cannot_collide(self):
        # A back-roll out of 1957 lands in 1956 — a year no 2-digit ``yy``
        # spells (56 → 2056). Four-digit keys keep the two apart.
        rolled = epoch_key(_line1(57, "000.50000000"))
        literal = epoch_key(_line1(56, "366.50000000"))
        assert rolled == 1956366.5
        assert literal == 2056366.5
        assert rolled != literal


class TestKeyBackCompat:
    @pytest.mark.parametrize(
        "yy,day",
        [
            (19, "365.50000000"),  # non-leap Dec 31
            (0, "366.99999999"),  # leap Dec 31, full fraction
            (99, "001.00000000"),  # Jan 1 midnight, 1999
            (57, "182.62539682"),  # mid-year, pivot low edge
            (56, "100.00000001"),  # smallest fraction, pivot high edge
        ],
    )
    def test_in_range_keys_bit_identical_to_v1(self, yy, day):
        new = epoch_key(_line1(yy, day))
        old = _v1_key(_line1(yy, day))
        assert new == old
        assert repr(new) == repr(old)


class TestExceptionContract:
    # Dedup's unusable-record seam and verify's revalidate both rely on a
    # ValueError from garbage columns — never a silent zero.
    @pytest.mark.parametrize(
        "line",
        [
            _line1(20, "36a.50000000"),  # alpha in the whole day
            _line1(20, "365.5000000a"),  # alpha in the fraction
            "1 00005U 58002B   XX365.50000000" + " " * 37,  # alpha year
            "1 00005",  # truncated line
        ],
    )
    def test_garbage_raises_value_error(self, line):
        for fn in (parse_epoch, epoch_key, epoch_dt):
            with pytest.raises(ValueError):
                fn(line)
