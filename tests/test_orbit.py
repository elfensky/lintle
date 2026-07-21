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

# A GEO-regime satellite (~1.0027 rev/day) at two epochs 5 days apart: under the
# flat 3-day gate this pair was skipped; #4's regime-aware gate gives GEO 7 days.
GEO_5DAY = [
    (
        "1 26900U 01037A   06001.50000000  .00000000  00000+0  00000+0 0  9998",
        "2 26900 000.0500 095.0000 0001000 000.0000 000.0000 01.00270000 00009",
    ),
    (
        "1 26900U 01037A   06006.50000000  .00000000  00000+0  00000+0 0  9993",
        "2 26900 000.0500 095.0000 0001000 000.0000 000.0000 01.00270000 00009",
    ),
]
# The same LEO satellite (~10.84 rev/day) at two epochs 5 days apart: still skipped
# — LEO keeps the 3-day gate.
LEO_5DAY = [
    (
        "1 00005U 58002B   06001.00000000 +.00000180 +00000-0 +24599-3 0 00315",
        "2 00005 034.2551 283.3714 1848015 007.2019 355.1657 10.83831997632014",
    ),
    (
        "1 00005U 58002B   06006.00000000 +.00000180 +00000-0 +24599-3 0 00310",
        "2 00005 034.2551 283.3714 1848015 007.2019 355.1657 10.83831997632014",
    ),
]


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


def bump_ma(l2: str, deg: float) -> str:
    """Line 2 with its mean anomaly (cols 44-51) bumped by ``deg`` degrees and
    re-checksummed — a valid record whose orbit no longer follows its neighbours."""
    ma = (float(l2[43:51]) + deg) % 360
    return fix(l2[:43] + f"{ma:08.4f}" + l2[51:])


def at_day(l1: str, day: float) -> str:
    """Line 1 with its epoch rewritten to 2006 day-of-year ``day`` (cols 19-32) and
    re-checksummed — lets a test set the cadence around a synthetic culprit."""
    return fix(l1[:20] + f"{day:012.8f}" + l1[32:])


# A GEO-regime record (~1.0027 rev/day) @ 2006 day 3, catalog 00005 — used as an
# interior culprit whose 7-day gate makes the leave-one-out probe gap over-wide.
GEO_CULPRIT = (
    fix("1 00005U 58002B   06003.00000000  .00000000  00000+0  00000+0 0  999"),
    fix("2 00005 000.0500 095.0000 0001000 000.0000 000.0000 01.00270000 0000"),
)


def build_tree(tmp_path, pairs, stem="tle01"):
    out = tmp_path / "output"
    (out / "cleaned").mkdir(parents=True, exist_ok=True)
    (out / "cleaned" / f"{stem}.00001.cleaned.txt").write_text(
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


class TestGapLimit:
    """The #4 regime-aware gap gate: GEO/geosync (< 1.5 rev/day) tolerates a 7-day
    propagation gap; everything else (LEO/MEO/Molniya) only 3 days."""

    def test_geo_regime_gets_seven_days(self):
        assert orbit._gap_limit(1.0) == 7.0

    def test_leo_regime_gets_three_days(self):
        assert orbit._gap_limit(11.0) == 3.0

    def test_boundary_is_leo(self):
        # exactly GEO_MEAN_MOTION_MAX (1.5) -> `n < 1.5` is False -> 3-day gate
        assert orbit._gap_limit(1.5) == 3.0
        assert orbit._gap_limit(1.4999) == 7.0


class TestRegimeGapGate:
    def test_geo_pair_at_five_day_gap_is_measured(self):
        recs = [rec(a, b, idx=i) for i, (a, b) in enumerate(GEO_5DAY)]
        _, pairs = orbit._track_suspects(recs)
        assert pairs == 1  # GEO 7-day gate admits the 5-day gap (was skipped)

    def test_leo_pair_at_five_day_gap_is_skipped(self):
        recs = [rec(a, b, idx=i) for i, (a, b) in enumerate(LEO_5DAY)]
        _, pairs = orbit._track_suspects(recs)
        assert pairs == 0  # LEO keeps the 3-day gate


class TestSample:
    def test_all_sats_returns_population(self):
        assert orbit.sample_catalogs({1, 2, 3}, 2, True) == {1, 2, 3}

    def test_population_within_sample_returns_all(self):
        assert orbit.sample_catalogs({1, 2, 3}, 5, False) == {1, 2, 3}

    def test_evenly_spaced_deterministic_sample(self):
        assert orbit.sample_catalogs(set(range(1, 11)), 3, False) == {1, 4, 7}


class TestOversample:
    """#2 stratified oversampling: dup-epoch catalogs get sampling priority. The
    empty-oversample path must reproduce the legacy slice byte-for-byte."""

    def test_empty_oversample_matches_legacy(self):
        # the 4-arg default reproduces the plain evenly-spaced slice exactly
        pop = set(range(1, 11))
        assert orbit.sample_catalogs(pop, 3, False) == {1, 4, 7}
        assert orbit.sample_catalogs(pop, 3, False, oversample=frozenset()) == {1, 4, 7}

    def test_priority_catalog_included_outside_plain_slice(self):
        # id 999 is a dup-epoch catalog the plain slice of 1..10,999 would miss
        pop = set(range(1, 11)) | {999}
        assert 999 not in orbit.sample_catalogs(pop, 3, False)
        got = orbit.sample_catalogs(pop, 3, False, oversample={999})
        assert 999 in got and len(got) == 3

    def test_overflow_stratum_is_evenly_spaced_not_truncated(self):
        # more priority ids than the budget -> spread across the id range, not the
        # lowest `sample` ids, so a high id survives.
        prio = set(range(1, 21))
        got = orbit.sample_catalogs(prio, 3, False, oversample=prio)
        assert len(got) == 3 and got <= prio
        assert got != {1, 2, 3}  # not lowest-`sample` truncation
        assert max(got) > 3  # a high id the truncated block would drop survives

    def test_minus_one_sentinel_never_sampled(self):
        # population excludes -1; the oversample ∩ population intersection keeps the
        # unparseable-catalog sentinel out even though find_conflicts doesn't filter it.
        pop = set(range(1, 11))
        got = orbit.sample_catalogs(pop, 3, False, oversample={-1, 5})
        assert -1 not in got and 5 in got and len(got) == 3


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


class TestLeaveOneOut:
    """#1 leave-one-out culprit isolation: a lone interior spike is attributed to
    the culprit alone (no double-flag onto the innocent successor); every ambiguous
    case falls back to per-pair attribution. All findings stay soft."""

    def test_interior_spike_isolates_the_culprit(self):
        # record 2 corrupted, both neighbours clean: incoming and outgoing pairs
        # both hot, but leave-2-out (record 1 -> record 3) reconciles -> one
        # isolated suspect on the culprit, none on its successor.
        recs = track_records()
        recs[2] = rec(TRACK[2][0], bump_ma(TRACK[2][1], 10), idx=2)
        suspects, _ = orbit._track_suspects(recs)
        assert len(suspects) == 1
        s = suspects[0]
        assert s.rule is VrfyRule.ORBIT_OUTLIER and s.severity == "soft"
        assert s.index == 2  # the culprit, not the successor (3)
        assert "isolated" in s.detail

    def test_wide_probe_falls_back_to_per_pair(self):
        # an uneven cadence around a regime-shifted culprit makes the doubled probe
        # gap (7 d) exceed 2*_gap_limit(LEO)=6 d: isolation is un-measurable, so
        # both hot pairs still emit per-pair rather than being silently dropped.
        l1, l2 = TRACK[1]
        recs = [
            rec(at_day(l1, 1.0), l2, idx=0),  # LEO
            rec(*GEO_CULPRIT, idx=1),  # GEO-regime culprit @ day 3
            rec(at_day(l1, 8.0), l2, idx=2),  # LEO, 5 days after the culprit
        ]
        suspects, _ = orbit._track_suspects(recs)
        assert len(suspects) == 2
        assert all(s.rule is VrfyRule.ORBIT_OUTLIER for s in suspects)
        assert all("isolated" not in s.detail for s in suspects)

    def test_manoeuvre_step_is_a_single_suspect(self):
        # a real manoeuvre: the orbit shifts at record 3 and the tail follows it,
        # so only the transition pair is hot -> one per-pair suspect, no isolation.
        recs = track_records()
        for i in (3, 4, 5):
            recs[i] = rec(TRACK[i][0], bump_ma(TRACK[i][1], 10), idx=i)
        suspects, _ = orbit._track_suspects(recs)
        assert len(suspects) == 1
        assert suspects[0].index == 3
        assert "isolated" not in suspects[0].detail

    def test_both_hot_but_loo_still_hot_is_two_suspects(self):
        # record 1 spiked and record 2 shifted to a different orbit: removing record
        # 1 does NOT reconcile records 0 and 2 (leave-one-out still hot) -> ambiguous
        # -> both pairs emit per-pair, no isolation.
        l1, l2 = TRACK[1]
        recs = [
            rec(at_day(l1, 1.0), l2, idx=0),
            rec(at_day(l1, 2.0), bump_ma(l2, 10), idx=1),
            rec(at_day(l1, 3.0), bump_ma(l2, 30), idx=2),
        ]
        suspects, _ = orbit._track_suspects(recs)
        assert len(suspects) == 2
        assert all("isolated" not in s.detail for s in suspects)

    def test_hole_between_hot_pairs_is_not_conflated(self):
        # two hot pairs separated by a None hole (a 5-day out-of-gate gap): the hole
        # is record 2's incoming pair, so records 1 and 3 are never treated as one
        # record's incoming/outgoing -> no isolation, two per-pair suspects.
        l1, l2 = TRACK[1]
        recs = [
            rec(at_day(l1, 1.0), l2, idx=0),
            rec(at_day(l1, 2.0), bump_ma(l2, 15), idx=1),  # pairs[1] hot
            rec(at_day(l1, 7.0), l2, idx=2),  # gap 5 d -> pairs[2] is None
            rec(at_day(l1, 8.0), bump_ma(l2, 15), idx=3),  # pairs[3] hot
        ]
        suspects, _ = orbit._track_suspects(recs)
        assert len(suspects) == 2
        assert {s.index for s in suspects} == {1, 3}
        assert all("isolated" not in s.detail for s in suspects)

    def test_endpoint_corruption_is_per_pair(self):
        # the first record is corrupt: it has only one neighbour, so isolation
        # (which needs an incoming AND an outgoing hot pair) cannot apply.
        recs = track_records()
        recs[0] = rec(TRACK[0][0], bump_ma(TRACK[0][1], 10), idx=0)
        suspects, _ = orbit._track_suspects(recs)
        assert len(suspects) == 1
        assert "isolated" not in suspects[0].detail


class TestSensitivity:
    """#3 --sensitivity dial: two tiers scale the global floor + MAD-k. Default
    `sensitive` (100 km, 10·MAD) is today's behaviour; `strict` (200 km, 20·MAD)
    surfaces fewer, higher-confidence outliers."""

    def test_strict_raises_the_flat_floor(self):
        # < 10 residuals -> flat floor; strict lifts it from 100 to 200
        assert orbit._threshold([1.0, 2.0, 3.0], orbit.SENSITIVE) == 100.0
        assert orbit._threshold([1.0, 2.0, 3.0], orbit.STRICT) == 200.0

    def test_strict_doubles_mad_k(self):
        # 11 values 10..110: median 60, MAD 30 -> sensitive 60+10*30, strict 60+20*30
        vals = [float(x) for x in range(10, 111, 10)]
        assert orbit._threshold(vals, orbit.SENSITIVE) == 360.0
        assert orbit._threshold(vals, orbit.STRICT) == 660.0

    def test_residual_between_floors_flagged_only_under_sensitive(self):
        # a ~153 km residual sits over the sensitive floor (100) but under strict (200)
        recs = [
            rec(*TRACK[4], idx=0),
            rec(TRACK[5][0], bump_ma(TRACK[5][1], 0.85), idx=1),
        ]
        sens, _ = orbit._track_suspects(recs, orbit.SENSITIVE)
        strict, _ = orbit._track_suspects(recs, orbit.STRICT)
        assert len(sens) == 1 and sens[0].rule is VrfyRule.ORBIT_OUTLIER
        assert strict == []

    def test_default_tier_is_sensitive(self):
        # the no-arg default must reproduce sensitive exactly (golden byte-identical)
        recs = [
            rec(*TRACK[4], idx=0),
            rec(TRACK[5][0], bump_ma(TRACK[5][1], 0.85), idx=1),
        ]
        default = orbit._track_suspects(recs)
        assert default == orbit._track_suspects(recs, orbit.SENSITIVE)


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
        rows = (tmp_path / "output" / "verify" / "suspects.00001.jsonl").read_text()
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

    def test_sensitivity_flag_dispatches(self, tmp_path):
        out = build_tree(tmp_path, TRACK)
        code = cli.main(
            ["verify", out, "--orbit", "--no-source-diff", "--sensitivity", "strict"]
        )
        assert code == 0
