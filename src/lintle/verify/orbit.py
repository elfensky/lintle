"""``verify`` Increment 2 (goal 2): the sampled ``sgp4`` orbit-consistency pass.

For each sampled satellite's epoch-sorted track, propagate every TLE forward to
its neighbour's epoch with ``sgp4`` and measure the position residual (km). A
residual over a robust per-satellite threshold is a **soft** ``VRFY-ORBIT-OUTLIER``
— *inconclusive*, never a conviction, because a real manoeuvre looks the same as
a corruption from a single pair (leave-one-out culprit isolation is a follow-up).
The only **hard** verdict is ``VRFY-ORBIT-ERROR``: ``sgp4`` rejecting an element
set as physically unphysical (error codes 1-5). Decayed orbits (error 6) are real,
not corruption, so they merely break the propagation chain.

Determinism: residuals are rounded to a 0.1 km quantum *before* thresholding, and
an outlier must clear the threshold by a full quantum — so the suspect set and
exit code are byte-reproducible across platforms even though raw ``sgp4`` floats
are not (a golden cross-platform fixture locks this). Epochs come from
``Satrec.jdsatepoch + jdsatepochF`` — ``sgp4``'s own parse, never re-derived — so
the sort order and the propagation target share one source of truth.

This module is the sole ``sgp4`` importer in the package; the clean/validate/repair
path stays walled off from it (import-graph test). Sampling unit is the satellite
(continuity needs a contiguous track); the sample is deterministic."""

import math
import statistics

from sgp4.api import Satrec

from lintle.verify import grouping, records
from lintle.verify.records import CleanedRecord
from lintle.verify.report import Suspect, SuspectSink, VrfyRule

GEO_MEAN_MOTION_MAX = 1.5  # ponytail: rev/day below this = geosync/GEO regime
GAP_LIMIT_LEO_MEO_DAYS = 3.0  # LEO/MEO/Molniya: sgp4 residual grows fast over a gap
GAP_LIMIT_GEO_DAYS = 7.0  # GEO/geosync: re-issued less often, so tolerate a wider gap
RESIDUAL_FLOOR_KM = 100.0  # a lone residual under this is never an outlier
RESIDUAL_QUANTUM_KM = 0.1  # rounding quantum = the cross-platform determinism guardband
MIN_EPOCHS_FOR_MAD = 10  # below this, trust only the flat floor, not a per-sat spread
MAD_K = 10.0  # robust bound: median + 10·MAD
LOCAL_K = 20.0  # ponytail: local-median multiplier — a spike must exceed 20× local
LOCAL_HALF = 5  # ponytail: half-width of the time-local window (±5 → up to 11 pairs)
DEFAULT_SAMPLE = 3000
# sgp4 init errors that mean "these mean elements are not a physical orbit" -> hard.
# Error 6 (decayed) is a real end-of-life state, not corruption, so it is excluded.
_HARD_SGP4_ERRORS = frozenset({1, 2, 3, 4, 5})


def _gap_limit(mean_motion_rev_per_day: float) -> float:
    """The #4 regime-aware propagation-gap gate (days): GEO/geosync (< 1.5 rev/day)
    is re-issued less often, so it tolerates a 7-day gap; everything else (LEO ~11-16,
    MEO ~2, Molniya ~2) keeps the tighter 3-day gate where the ``sgp4`` residual is
    still measurable. A boundary object that flips class merely swaps one soft gap
    gate for the other — never a verdict — so the split is safe."""
    if mean_motion_rev_per_day < GEO_MEAN_MOTION_MAX:
        return GAP_LIMIT_GEO_DAYS
    return GAP_LIMIT_LEO_MEO_DAYS


def _pair_residual(sat_a: Satrec, sat_b: Satrec) -> float | None:
    """Position residual (km, rounded to the 0.1 km quantum) between ``sat_a``
    propagated to ``sat_b``'s epoch and ``sat_b`` at its own epoch; ``None`` if
    either propagation errors (the pair can't be measured)."""
    jd, fr = sat_b.jdsatepoch, sat_b.jdsatepochF
    err_a, r_a, _ = sat_a.sgp4(jd, fr)
    err_b, r_b, _ = sat_b.sgp4(jd, fr)
    if err_a or err_b:
        return None
    return round(math.dist(r_a, r_b), 1)


def _threshold(residuals: list[float]) -> float:
    """Robust per-satellite outlier threshold: a flat 100 km floor until there are
    enough epochs (< 10) to trust a per-sat spread, then ``max(floor, median +
    10·MAD)``. Rounded to the 0.1 km quantum so the verdict is deterministic."""
    if len(residuals) < MIN_EPOCHS_FOR_MAD:
        return RESIDUAL_FLOOR_KM
    med = statistics.median(residuals)
    mad = statistics.median([abs(r - med) for r in residuals])
    return round(max(RESIDUAL_FLOOR_KM, med + MAD_K * mad), 1)


def _local_threshold(pairs: list[float | None], i: int) -> float:
    """The #5 per-pair local-median bar: ``round(LOCAL_K · median(window), 0.1)``,
    where ``window`` is the residuals at positions ``[i-LOCAL_HALF, i+LOCAL_HALF]``
    restricted to the *contiguous run of non-``None`` pairs* around ``i`` — a hole
    (a skipped/ungated/error pair) bounds the window, so a spike is measured against
    what is *locally* typical in time, never residuals pulled across a gap. Inactive
    (``0.0``, so the outer ``max`` ignores it) below ``MIN_EPOCHS_FOR_MAD`` window
    points. Rounded to the quantum for the same "clear the bar by a full quantum"
    determinism contract the global term keeps. ``pairs[i]`` is assumed non-``None``."""
    window = [pairs[i]]
    j = i - 1
    while j >= i - LOCAL_HALF and j >= 0 and pairs[j] is not None:
        window.append(pairs[j])
        j -= 1
    j = i + 1
    while j <= i + LOCAL_HALF and j < len(pairs) and pairs[j] is not None:
        window.append(pairs[j])
        j += 1
    if len(window) < MIN_EPOCHS_FOR_MAD:
        return 0.0
    return round(LOCAL_K * statistics.median(window), 1)


def _track_suspects(track: list[CleanedRecord]) -> tuple[list[Suspect], int]:
    """Suspects for one satellite's epoch-sorted track: per-record hard ``sgp4``
    element errors, plus adjacent-pair residual outliers over the robust threshold
    (soft/inconclusive). The threshold is the per-pair ``max(global, local)`` (#5):
    the global ``median + MAD`` bar OR the windowed ``20·local median``. Returns
    ``(suspects, pairs_measured)``. Holds the track's position-aligned residual
    array — bounded by a single satellite's epoch count, never the corpus."""
    suspects: list[Suspect] = []
    # Position-aligned residuals: pairs[i] is the residual of track[i-1] -> track[i]
    # (index = the second record's position), or None when that pair is unmeasurable
    # (endpoint / out of gate / sgp4 chain break). Holes make #5's window time-local.
    pairs: list[float | None] = []
    prev: tuple[Satrec, CleanedRecord] | None = None
    for rec in track:
        sat = Satrec.twoline2rv(rec.line1, rec.line2)
        if sat.error:
            if sat.error in _HARD_SGP4_ERRORS:
                suspects.append(
                    Suspect(
                        VrfyRule.ORBIT_ERROR,
                        rec.catalog,
                        rec.epoch_key,
                        rec.src_file,
                        rec.index,
                        f"sgp4 rejects these elements (error {sat.error})",
                    )
                )
            prev = None  # unphysical or decayed: breaks the propagation chain
            pairs.append(None)
            continue
        resid: float | None = None
        if prev is not None:
            prev_sat, _ = prev
            dt = (sat.jdsatepoch + sat.jdsatepochF) - (
                prev_sat.jdsatepoch + prev_sat.jdsatepochF
            )
            # Classify the regime on the propagation source (prev_sat, always
            # error==0 here). no_kozai is radians/min; rev/day = no_kozai*1440/(2π)
            # — the parens matter: no_kozai*1440/2*math.pi would be off by π².
            rev_per_day = prev_sat.no_kozai * 1440 / (2 * math.pi)
            if 0 < dt <= _gap_limit(rev_per_day):
                resid = _pair_residual(prev_sat, sat)
        pairs.append(resid)
        prev = (sat, rec)
    measured = [r for r in pairs if r is not None]
    threshold = _threshold(measured)
    for i, resid in enumerate(pairs):
        if resid is None:
            continue
        bar = max(threshold, _local_threshold(pairs, i))
        if resid > bar + RESIDUAL_QUANTUM_KM:  # one-quantum guardband
            rec = track[i]
            suspects.append(
                Suspect(
                    VrfyRule.ORBIT_OUTLIER,
                    rec.catalog,
                    rec.epoch_key,
                    rec.src_file,
                    rec.index,
                    f"orbit residual {resid} km vs its neighbour exceeds the "
                    f"{bar} km threshold (inconclusive)",
                )
            )
    return suspects, len(measured)


def sample_catalogs(
    population: set[int], sample: int | None, all_sats: bool
) -> set[int]:
    """Deterministic satellite sample: all of them when ``all_sats`` or the
    population already fits, else an evenly-spaced slice of the sorted catalog ids
    (spread across the id range, byte-reproducible — no RNG)."""
    if all_sats or sample is None or len(population) <= sample:
        return set(population)
    cats = sorted(population)
    n = len(cats)
    return {cats[(i * n) // sample] for i in range(sample)}


def run_orbit_pass(
    out_dir: str,
    stems: list[str],
    population: set[int],
    sink: SuspectSink,
    *,
    sample: int | None,
    all_sats: bool,
) -> dict:
    """The sampled orbit-consistency pass. Streams the sampled satellites' cleaned
    records through the external sort, then per epoch-sorted track flags hard
    ``sgp4`` element errors and soft residual outliers into ``sink`` (which spills
    to disk, so a corpus's worth of outliers never accumulates in RAM — #156).
    Returns the census. Constant memory w.r.t. the corpus (one satellite's track
    at a time). ponytail: re-reads ``cleaned/`` to gather the sample — a
    single-pass sampling optimisation is a follow-up (issue #144)."""
    sampled = sample_catalogs(population, sample, all_sats)
    sorter = grouping.ExternalSorter()
    for stem in stems:
        for rec in records.iter_file(out_dir, stem):
            if rec.catalog in sampled:
                sorter.add(rec)

    n_pairs = n_tracks = 0
    track: list[CleanedRecord] = []
    current: int | None = None
    for rec in sorter.sorted_records():
        if rec.catalog != current:
            if track:
                found, pairs = _track_suspects(track)
                sink.add_all(found)
                n_pairs += pairs
                n_tracks += 1
            current = rec.catalog
            track = []
        track.append(rec)
    if track:
        found, pairs = _track_suspects(track)
        sink.add_all(found)
        n_pairs += pairs
        n_tracks += 1

    return {
        "orbit_population": len(population),
        "orbit_sampled": len(sampled),
        "orbit_satellites_checked": n_tracks,
        "orbit_pairs_measured": n_pairs,
    }
