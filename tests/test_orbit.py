"""Tests for ``lintle verify --orbit`` (Increment 2: the sampled sgp4 pass).

The golden fixture is a real Vanguard (catalog 5) track pulled from the corpus;
its adjacent-pair residuals are asserted to the 0.1 km quantum to lock the
cross-platform determinism of the residual pipeline."""

import json

from sgp4.api import Satrec

from lintle import cli, tle
from lintle.verify import epoch, orbit, run_verify
from lintle.verify.records import CleanedRecord, catalog_of
from lintle.verify.report import VrfyRule

# Six real, valid catalog-5 (Vanguard 1) TLEs in epoch order.
TRACK = [
    (
        "1 00005U 58002B   05365.88048800 +.00000180 +00000-0 +24599-3 0 00313",
        "2 00005 034.2551 283.3714 1848015 007.2019 355.1657 10.83831997632014",
    ),
    (
        "1 00005U 58002B   06001.89460089 +.00000195 +00000-0 +26713-3 0 00427",
        "2 00005 034.2542 280.2534 1848039 011.7582 352.0724 10.83832515632121",
    ),
    (
        "1 00005U 58002B   06002.90872048 +.00000098 +00000-0 +14485-3 0 00540",
        "2 00005 034.2532 277.1377 1847701 016.3191 348.9513 10.83831996632236",
    ),
    (
        "1 00005U 58002B   06003.83064404  .00000165  00000-0  23248-3 0   218",
        "2 00005 034.2524 274.3002 1847924 020.4681 346.1085 10.83832913632333",
    ),
    (
        "1 00005U 58002B   06003.92283591 +.00000191 +00000-0 +26515-3 0 00239",
        "2 00005 034.2519 274.0172 1847947 020.8808 345.8250 10.83833149632345",
    ),
    (
        "1 00005U 58002B   06004.93694145 +.00000229 +00000-0 +31653-3 0 00356",
        "2 00005 034.2503 270.8969 1848011 025.4390 342.6637 10.83833886632450",
    ),
]

# Golden residuals for the clean track (km, 0.1 km quantum) — locks determinism.
GOLDEN_RESIDUALS = [8.4, 0.9, 0.6, 0.3, 0.4]


def fix(line: str) -> str:
    return line[:68] + str(tle.compute_checksum(line))


def rec(line1: str, line2: str, src: str = "tle01", idx: int = 0) -> CleanedRecord:
    cat = catalog_of(line1)
    return CleanedRecord(
        cat if cat is not None else -1, epoch.epoch_key(line1), line1, line2, src, idx
    )


def track_records(pairs=TRACK) -> list[CleanedRecord]:
    return [rec(a, b, idx=i) for i, (a, b) in enumerate(pairs)]


def diverged_last() -> tuple[str, str]:
    """The last track record with its mean anomaly (line-2 cols 44-51) bumped +10°
    — a valid TLE whose orbit no longer follows its neighbour (a big residual)."""
    l1, l2 = TRACK[-1]
    return l1, fix(l2[:43] + "352.6637" + l2[51:])


def build_tree(tmp_path, pairs, stem="tle01"):
    out = tmp_path / "output"
    (out / "cleaned").mkdir(parents=True, exist_ok=True)
    (out / "cleaned" / f"{stem}.cleaned.txt").write_text(
        "".join(f"{a}\n{b}\n" for a, b in pairs), encoding="ascii"
    )
    return str(out)


class TestResidual:
    def test_golden_clean_track_residuals_are_deterministic(self):
        sats = [Satrec.twoline2rv(a, b) for a, b in TRACK]
        got = [orbit._pair_residual(sats[i], sats[i + 1]) for i in range(len(sats) - 1)]
        assert got == GOLDEN_RESIDUALS

    def test_clean_track_has_no_suspects(self):
        suspects, pairs = orbit._track_suspects(track_records())
        assert suspects == [] and pairs == 5


class TestThreshold:
    def test_flat_floor_below_ten_epochs(self):
        assert orbit._threshold([1.0, 2.0, 3.0]) == 100.0

    def test_low_spread_stays_at_floor(self):
        assert orbit._threshold([1.0] * 12) == 100.0

    def test_high_spread_uses_median_plus_10_mad(self):
        # 11 values 10..110: median 60, MAD 30 -> 60 + 10*30 = 360
        vals = [float(x) for x in range(10, 111, 10)]
        assert orbit._threshold(vals) == 360.0


class TestLocalThreshold:
    """The #5 windowed local-median term: per-pair bar = round(20 * median of a
    time-local window, 0.1 km), inactive below MIN_EPOCHS_FOR_MAD window points."""

    def test_inactive_below_min_epochs(self):
        # fewer than MIN_EPOCHS_FOR_MAD window points -> no local term (0.0)
        assert orbit._local_threshold([50.0] * 9, 4) == 0.0

    def test_active_window_is_twenty_times_local_median(self):
        # a uniformly-elevated segment (>= 10 points): 20 * local median = 1000
        assert orbit._local_threshold([50.0] * 11, 5) == 1000.0

    def test_window_rounds_to_quantum(self):
        # even-count window -> median averages two 0.1-quantised values to a 0.05
        # multiple; 20 * 5.05 = 101.00000000000001 must round to the 0.1 km quantum
        assert orbit._local_threshold([5.0] * 5 + [5.1] * 5, 5) == 101.0

    def test_window_is_time_local_not_index_local(self):
        # a None hole (a skipped/ungated pair) bounds the window: the elevated
        # post-hole segment must not pull the small pre-hole residuals into its
        # "local" median. Around index 8 only the 6 post-hole points are in the
        # run (< 10) -> inactive, not a full 11-point index window.
        pairs = [5.0] * 5 + [None] + [500.0] * 6
        assert orbit._local_threshold(pairs, 8) == 0.0

    def test_local_term_suppresses_globally_flagged_pair(self):
        # an elevated plateau: a residual that clears the GLOBAL bar is held below
        # the LOCAL bar, so max(global, local) removes it (monotone: local only
        # raises, never adds a suspect).
        seg = [200.0] * 11
        glob = orbit._threshold(seg)  # median 200, MAD 0 -> 200
        loc = orbit._local_threshold(seg, 5)  # 20 * 200 = 4000
        spike = 260.0
        assert spike > glob + orbit.RESIDUAL_QUANTUM_KM  # global alone flags it
        assert spike <= max(glob, loc) + orbit.RESIDUAL_QUANTUM_KM  # local suppresses


class TestSample:
    def test_all_sats_returns_population(self):
        assert orbit.sample_catalogs({1, 2, 3}, 2, True) == {1, 2, 3}

    def test_population_within_sample_returns_all(self):
        assert orbit.sample_catalogs({1, 2, 3}, 5, False) == {1, 2, 3}

    def test_evenly_spaced_deterministic_sample(self):
        assert orbit.sample_catalogs(set(range(1, 11)), 3, False) == {1, 4, 7}


class TestTrackVerdict:
    def test_mean_anomaly_outlier_flagged_soft(self):
        recs = track_records()
        recs[-1] = rec(*diverged_last(), idx=5)
        suspects, pairs = orbit._track_suspects(recs)
        assert pairs == 5
        assert len(suspects) == 1
        s = suspects[0]
        assert s.rule is VrfyRule.ORBIT_OUTLIER and s.severity == "soft"
        assert s.index == 5

    def test_decayed_record_is_not_convicted(self):
        # a huge mean motion -> sgp4 error 6 (decayed); a real end-of-life state,
        # never a hard suspect, and it breaks the propagation chain.
        l1, l2 = TRACK[2]
        decayed = fix(l2[:52] + "20.00000000" + l2[63:])
        recs = track_records()
        recs[2] = rec(l1, decayed, idx=2)
        suspects, _ = orbit._track_suspects(recs)
        assert all(s.rule is not VrfyRule.ORBIT_ERROR for s in suspects)

    def test_wide_gap_pair_is_skipped(self):
        # first and last epoch are ~4 days apart -> beyond the 3-day gate
        recs = [rec(*TRACK[0], idx=0), rec(*TRACK[-1], idx=1)]
        suspects, pairs = orbit._track_suspects(recs)
        assert pairs == 0 and suspects == []


class TestEndToEnd:
    def test_clean_track_passes_with_census(self, tmp_path):
        out = build_tree(tmp_path, TRACK)
        assert run_verify(out, None, orbit=True) == 0
        summary = json.loads(
            (tmp_path / "output" / "verify" / "summary.json").read_text()
        )
        checked = summary["checked"]
        assert checked["orbit_satellites_checked"] == 1
        assert checked["orbit_pairs_measured"] == 5
        assert checked["orbit_population"] == 1

    def test_outlier_is_soft_exit_zero(self, tmp_path):
        pairs = TRACK[:-1] + [diverged_last()]
        out = build_tree(tmp_path, pairs)
        assert run_verify(out, None, orbit=True) == 0  # inconclusive never blocks
        rows = (tmp_path / "output" / "verify" / "suspects.jsonl").read_text()
        assert "VRFY-ORBIT-OUTLIER" in rows

    def test_orbit_off_by_default(self, tmp_path):
        out = build_tree(tmp_path, TRACK)
        assert run_verify(out, None) == 0
        summary = json.loads(
            (tmp_path / "output" / "verify" / "summary.json").read_text()
        )
        assert "orbit_population" not in summary["checked"]


class TestCLI:
    def test_orbit_flag_dispatches(self, tmp_path):
        out = build_tree(tmp_path, TRACK)
        assert cli.main(["verify", out, "--orbit", "--no-source-diff"]) == 0
