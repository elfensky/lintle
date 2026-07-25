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

import dataclasses
import math
import statistics

from sgp4.api import Satrec

from lintle import cli_progress
from lintle.verify import grouping, records
from lintle.verify.records import CleanedRecord
from lintle.verify.report import Suspect, SuspectSink, VerifyRule

GEO_MEAN_MOTION_MAX = 1.5  # ponytail: rev/day below this = geosync/GEO regime
GAP_LIMIT_LEO_MEO_DAYS = 3.0  # LEO/MEO/Molniya: sgp4 residual grows fast over a gap
GAP_LIMIT_GEO_DAYS = 7.0  # GEO/geosync: re-issued less often, so tolerate a wider gap
RESIDUAL_QUANTUM_KM = 0.1  # rounding quantum = the cross-platform determinism guardband
MIN_EPOCHS_FOR_MAD = 10  # below this, trust only the flat floor, not a per-sat spread
LOCAL_K = 20.0  # ponytail: local-median multiplier — a spike must exceed 20× local
LOCAL_HALF = 5  # ponytail: half-width of the time-local window (±5 → up to 11 pairs)
DEFAULT_SAMPLE = 3000


@dataclasses.dataclass(slots=True, frozen=True)
class Sensitivity:
    """The #3 --sensitivity tier: the two knobs on the *global* outlier threshold —
    the flat floor and the MAD multiplier. ``LOCAL_K`` (#5) and ``MIN_EPOCHS_FOR_MAD``
    stay fixed module constants; the dial scales only the floor + MAD terms."""

    floor_km: float
    mad_k: float


SENSITIVE = Sensitivity(floor_km=100.0, mad_k=10.0)  # today's default
STRICT = Sensitivity(floor_km=200.0, mad_k=20.0)  # fewer, higher-confidence outliers
# Public name → tier mapping; the CLI's --sensitivity choices resolve through this.
TIERS = {"sensitive": SENSITIVE, "strict": STRICT}
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


def _threshold(residuals: list[float], sensitivity: Sensitivity = SENSITIVE) -> float:
    """Robust per-satellite outlier threshold: a flat floor until there are enough
    epochs (< 10) to trust a per-sat spread, then ``max(floor, median + k·MAD)``.
    The floor and ``k`` come from the #3 sensitivity tier (default ``SENSITIVE`` =
    100 km, 10·MAD). Rounded to the 0.1 km quantum so the verdict is deterministic."""
    if len(residuals) < MIN_EPOCHS_FOR_MAD:
        return sensitivity.floor_km
    med = statistics.median(residuals)
    mad = statistics.median([abs(r - med) for r in residuals])
    return round(max(sensitivity.floor_km, med + sensitivity.mad_k * mad), 1)


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


def _rev_per_day(sat: Satrec) -> float:
    """A ``Satrec``'s mean motion in rev/day from ``no_kozai`` (radians/min). The
    parens are load-bearing: ``no_kozai * 1440 / (2 * math.pi)``, never
    ``no_kozai * 1440 / 2 * math.pi`` (left-to-right, off by π²)."""
    return sat.no_kozai * 1440 / (2 * math.pi)


def _track_suspects(
    track: list[CleanedRecord], sensitivity: Sensitivity = SENSITIVE
) -> tuple[list[Suspect], int]:
    """Suspects for one satellite's epoch-sorted track: per-record hard ``sgp4``
    element errors, plus adjacent-pair residual outliers over the per-pair
    ``max(global, local)`` threshold (#5), soft/inconclusive. ``sensitivity`` (#3)
    scales the global floor + MAD terms (default ``SENSITIVE``). A lone interior
    spike is isolated to its culprit by leave-one-out (#1) instead of double-flagging
    the innocent successor. Returns ``(suspects, pairs_measured)``. Holds the track's
    ``Satrec`` list and position-aligned residual array — bounded by a single
    satellite's epoch count, never the corpus."""
    suspects: list[Suspect] = []
    # Retain each record's Satrec (None on sgp4 init error, which still breaks the
    # chain and hard-convicts codes 1-5) so the #1 leave-one-out probe can
    # re-propagate a record's neighbours skipping it.
    sats: list[tuple[Satrec | None, CleanedRecord]] = []
    for rec in track:
        sat = Satrec.twoline2rv(rec.line1, rec.line2)
        if sat.error:
            if sat.error in _HARD_SGP4_ERRORS:
                suspects.append(
                    Suspect(
                        VerifyRule.ORBIT_ERROR,
                        rec.catalog,
                        rec.epoch_key,
                        rec.src_file,
                        rec.index,
                        f"sgp4 rejects these elements (error {sat.error})",
                    )
                )
            sats.append((None, rec))  # unphysical or decayed: breaks the chain
        else:
            sats.append((sat, rec))

    # Position-aligned residuals: pairs[i] = residual of sats[i-1] -> sats[i] (index
    # = the second record's position), or None when unmeasurable (either endpoint
    # None, out of gate, or the propagation errored). Holes keep #5's window
    # time-local AND stop #1's neighbour math conflating non-adjacent pairs.
    pairs: list[float | None] = [None] * len(sats)
    for i in range(1, len(sats)):
        prev_sat, _ = sats[i - 1]
        sat, _ = sats[i]
        if prev_sat is None or sat is None:
            continue
        dt = (sat.jdsatepoch + sat.jdsatepochF) - (
            prev_sat.jdsatepoch + prev_sat.jdsatepochF
        )
        # Full gate — the 0 < lower bound is load-bearing: a dt == 0 same-epoch
        # re-issue would inject a ~0 residual and shift every pair's threshold.
        if 0 < dt <= _gap_limit(_rev_per_day(prev_sat)):
            pairs[i] = _pair_residual(prev_sat, sat)

    measured = [r for r in pairs if r is not None]
    threshold = _threshold(measured, sensitivity)

    def hot(i: int) -> bool:
        r = pairs[i]
        return (
            r is not None
            and r > max(threshold, _local_threshold(pairs, i)) + RESIDUAL_QUANTUM_KM
        )

    def outlier(i: int, detail: str) -> Suspect:
        rec = sats[i][1]
        return Suspect(
            VerifyRule.ORBIT_OUTLIER,
            rec.catalog,
            rec.epoch_key,
            rec.src_file,
            rec.index,
            detail,
        )

    # #1 leave-one-out isolation: a lone interior spike makes its incoming pair
    # (pairs[i]) AND its outgoing pair (pairs[i+1]) hot. If propagating the culprit's
    # neighbours *around* it reconciles them, flag the culprit alone. Iterate
    # left-to-right; a consumed pair is re-emitted nowhere, so overlapping candidates
    # resolve deterministically.
    consumed = [False] * len(pairs)
    for i in range(len(sats)):
        inc, out = i, i + 1  # incoming pair (i-1->i) and outgoing pair (i->i+1)
        if out >= len(pairs) or consumed[inc]:
            continue
        if pairs[inc] is None or pairs[out] is None or not (hot(inc) and hot(out)):
            continue
        prev_sat, _ = sats[i - 1]
        next_sat, _ = sats[i + 1]
        if prev_sat is None or next_sat is None:
            continue
        # The probe skips record i, so its gap is up to twice a cadence step -> gate
        # on 2*_gap_limit (classify off sats[i-1]); judge "not hot" against the GLOBAL
        # threshold only (the probe has no pair position, so no #5 window applies).
        dt = (next_sat.jdsatepoch + next_sat.jdsatepochF) - (
            prev_sat.jdsatepoch + prev_sat.jdsatepochF
        )
        if not 0 < dt <= 2 * _gap_limit(_rev_per_day(prev_sat)):
            continue  # probe over the doubled gate: isolation genuinely un-measurable
        probe = _pair_residual(prev_sat, next_sat)
        if probe is None or probe > threshold + RESIDUAL_QUANTUM_KM:
            continue  # un-measurable or leave-one-out still hot: fall back to per-pair
        bar = max(threshold, _local_threshold(pairs, inc))
        suspects.append(
            outlier(
                inc,
                f"orbit residual {pairs[inc]} km vs its neighbour exceeds the "
                f"{bar} km threshold (isolated by leave-one-out: neighbours agree "
                "without it)",
            )
        )
        consumed[inc] = consumed[out] = True

    # Remaining (unconsumed) hot pairs: one soft outlier attributed to the pair's
    # second record, exactly as before isolation — the manoeuvre step, the ambiguous
    # both-hot cases, and endpoint corruption all land here.
    for i in range(len(pairs)):
        if consumed[i] or not hot(i):
            continue
        bar = max(threshold, _local_threshold(pairs, i))
        suspects.append(
            outlier(
                i,
                f"orbit residual {pairs[i]} km vs its neighbour exceeds the "
                f"{bar} km threshold (inconclusive)",
            )
        )

    return suspects, len(measured)


def _by_catalog(rec: CleanedRecord) -> int:
    """Group key for :func:`grouping.grouped`: one track per satellite."""
    return rec.catalog


def sample_catalogs(
    population: set[int],
    sample: int | None,
    all_sats: bool,
    oversample: frozenset[int] | set[int] = frozenset(),
) -> set[int]:
    """Deterministic satellite sample: all of them when ``all_sats`` or the
    population already fits, else an evenly-spaced slice of the sorted catalog ids
    (spread across the id range, byte-reproducible — no RNG). ``oversample`` (#2) is
    a priority stratum (dup-epoch catalogs); the ``oversample ∩ population``
    intersection is load-bearing — it keeps the ``-1`` unparseable sentinel (which
    ``find_conflicts`` does not filter) out of the ``-1``-free ``population``. When
    the priority stratum overflows the budget it is evenly spaced *within* itself
    (not truncated to the lowest ids); otherwise all of it is kept and the remaining
    budget is filled with the evenly-spaced slice of the rest. Empty ``oversample``
    reproduces the legacy slice byte-for-byte."""
    if all_sats or sample is None or len(population) <= sample:
        return set(population)
    prio = sorted(oversample & population)
    if len(prio) > sample:
        p = len(prio)
        return {prio[(i * p) // sample] for i in range(sample)}
    rest = sorted(population - set(prio))
    fill = sample - len(prio)
    m = len(rest)
    return set(prio) | {rest[(i * m) // fill] for i in range(fill)}


def run_orbit_pass(
    out_dir: str,
    stems: list[str],
    population: set[int],
    sink: SuspectSink,
    *,
    sample: int | None,
    all_sats: bool,
    sensitivity: Sensitivity = SENSITIVE,
    oversample: frozenset[int] | set[int] = frozenset(),
) -> dict:
    """The sampled orbit-consistency pass. Streams the sampled satellites' cleaned
    records through the external sort, then per epoch-sorted track flags hard
    ``sgp4`` element errors and soft residual outliers into ``sink`` (which spills
    to disk, so a corpus's worth of outliers never accumulates in RAM — #156).
    ``sensitivity`` (#3) scales the global threshold; ``oversample`` (#2) is the
    dup-epoch priority stratum for the sample. Returns the census. Constant memory
    w.r.t. the corpus (one satellite's track at a time). ponytail: re-reads
    ``cleaned/`` to gather the sample — a single-pass sampling optimisation is a
    follow-up (issue #144)."""
    sampled = sample_catalogs(population, sample, all_sats, oversample)
    sorter = grouping.ExternalSorter()
    with cli_progress.phase_bar("orbit: sampling", len(stems)) as progress:
        for stem in stems:
            progress(description=f"orbit: sampling {stem}")
            for rec in records.iter_file(out_dir, stem):
                if rec.catalog in sampled:
                    sorter.add(rec)
            progress(advance=1)

    n_pairs = n_tracks = 0
    with cli_progress.phase_bar("orbit: propagating", len(sampled)) as progress:
        for _, track in grouping.grouped(sorter.sorted_records(), key=_by_catalog):
            found, pairs = _track_suspects(track, sensitivity)
            sink.add_all(found)
            n_pairs += pairs
            n_tracks += 1
            progress(advance=1)

    return {
        "orbit_population": len(population),
        "orbit_sampled": len(sampled),
        "orbit_satellites_checked": n_tracks,
        "orbit_pairs_measured": n_pairs,
    }
