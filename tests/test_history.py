"""Tests for lintle.history — the pure epoch/gap reducer shared by extract
and dedup."""

import datetime as dt

from lintle.history import analyze_epochs


def _days(n):  # helper: build epochs n days apart from a fixed origin
    base = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    return [base + dt.timedelta(days=d) for d in n]


class TestAnalyzeEpochs:
    def test_uniform_cadence_no_gaps(self):
        hs = analyze_epochs(_days([0, 1, 2, 3, 4]), [1, 2, 3, 4, 5])
        assert hs.count == 5
        assert hs.gap_count == 0
        assert hs.median_spacing_days == 1.0
        assert hs.elset_first == 1 and hs.elset_last == 5

    def test_one_hole_is_one_gap(self):
        hs = analyze_epochs(_days([0, 1, 2, 42, 43]), [1, 2, 3, 4, 5])
        assert hs.gap_count == 1
        assert hs.gaps[0].days == 40.0

    def test_fewer_than_three_records_skips_analysis(self):
        hs = analyze_epochs(_days([0, 1]), [1, 2])
        assert hs.median_spacing_days is None
        assert hs.gap_count == 0  # the trivial-gapless footgun, asserted

    def test_three_record_gap_is_reachable(self):
        # The n=3 dead zone (#199): thresholding on the interpolated median
        # (500.5 here) can never flag anything with 2 deltas — d > 10*(a+d)/2
        # is algebraically impossible; median_low thresholds on 1.0 instead.
        hs = analyze_epochs(_days([0, 1, 1001]), [1, 2, 3])
        assert hs.gap_count == 1
        assert hs.largest_gap_days == 1000.0
        assert hs.median_spacing_days == 500.5  # the REPORTED field unchanged

    def test_three_record_uniform_cadence_still_gapless(self):
        hs = analyze_epochs(_days([0, 1, 2]), [1, 2, 3])
        assert hs.gap_count == 0

    def test_min_gap_records_names_the_policy(self):
        from lintle.history import MIN_GAP_RECORDS

        assert MIN_GAP_RECORDS == 3

    def test_empty(self):
        hs = analyze_epochs([], [])
        assert hs.count == 0 and hs.first is None and hs.gap_count == 0
