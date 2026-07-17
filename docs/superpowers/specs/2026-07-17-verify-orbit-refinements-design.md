# verify --orbit refinements (#163) — design

**Date:** 2026-07-17 · **Issue:** [#163](https://github.com/elfensky/lintle/issues/163)
(epic [#148](https://github.com/elfensky/lintle/issues/148)) · **Status:** approved

The core `lintle verify --orbit` pass shipped in #144/#162: for each sampled
satellite's epoch-sorted track, propagate every TLE forward to its neighbour's
epoch with `sgp4`, measure the position residual (km), and flag residuals over a
robust per-satellite threshold as **soft** `VRFY-ORBIT-OUTLIER`. This spec covers
the five refinements deferred from that core-first slice. All code is in
[`src/lintle/verify/orbit.py`](../../../src/lintle/verify/orbit.py) plus the CLI
wiring in `cli.py` and the dup-epoch collection point in `verify/__init__.py`.

## Invariants preserved (why this design exists)

Every refinement is bound by the same four hard constraints as the core pass:

1. **Soft stays soft.** Orbit findings are always `VRFY-ORBIT-OUTLIER` (soft,
   exit 0). Only `sgp4` error codes 1–5 hard-convict (`VRFY-ORBIT-ERROR`). A real
   manoeuvre is indistinguishable from corruption over a single pair — no
   refinement may turn an orbit finding hard.
2. **Byte-determinism.** Residuals round to a 0.1 km quantum and an outlier must
   clear its threshold by a full quantum. The suspect set and exit code are locked
   by the golden fixture in `tests/test_orbit.py`. Any change to that fixture is
   deliberate and cross-platform reproducible.
3. **Constant memory (Rule #3).** The pass streams one satellite's track at a time
   through the external merge sort. New per-track state (the track's `Satrec`
   list, a local-median window) is bounded by a single satellite's epoch count,
   never the corpus.
4. **The `sgp4` wall.** Only `verify/orbit.py` imports `sgp4`, lazily, only under
   `--orbit`. Validity always routes through `tle.validate_record`; `sgp4`
   measures position, never validity.

The golden fixture is a 6-epoch Vanguard-1 track. Its cases are all single-hot-pair
or wide-gap-LEO, so none of the five refinements alters its suspects — the fixture
stays byte-identical (verified deliberately, not assumed).

## Landing order

Five independent refinements, five small PRs to `develop`, localized-first:

1. **#5 windowed local-median term** — smallest, self-contained threshold tweak.
2. **#4 regime-aware gap gates** — one helper, one call-site change.
3. **#1 leave-one-out culprit isolation** — the meaty one; restructures
   `_track_suspects`.
4. **#3 `--sensitivity` dial** — CLI surface + threshold parameterization.
5. **#2 stratified oversampling** — dup-epoch priority sample.

Order rationale: #5 and #4 are localized and independent; landing them first keeps
#1's larger refactor working against a settled threshold/gate. #3 parameterizes the
threshold #5 just extended, so it follows. #2 is orthogonal (sampling, not
detection) and lands last.

---

## #5 — windowed local-median term

**Today:** `_threshold(residuals)` returns one scalar for the whole track:
`max(RESIDUAL_FLOOR_KM, median + MAD_K·MAD)` (floor until ≥ `MIN_EPOCHS_FOR_MAD`
epochs). #144's original spec also wanted a `20·local_median` term — a *per-position
local* baseline, not the whole-track median — so a genuine spike must exceed 20× the
*locally* typical residual, adapting to a track segment that is uniformly elevated
(e.g. a high-drag phase) instead of drowning it in the global median.

**Change:** the threshold becomes per-pair.

- Keep `_threshold(residuals)` computing the global scalar `max(floor, median +
  MAD_K·MAD)` unchanged (the "global" term).
- Add `_local_threshold(residuals, i)` → `LOCAL_K · median(window)` where `window`
  is residuals in `[i-LOCAL_HALF, i+LOCAL_HALF]`. Inactive (returns `0.0`, so the
  outer `max` ignores it) when the window has fewer than `MIN_EPOCHS_FOR_MAD`
  points — same "not enough data to trust a spread" rule the global term uses.
- Per pair, the effective bar is `max(global_threshold, local_threshold(i))`.

**Constants:** `LOCAL_K = 20.0`, `LOCAL_HALF = 5` (a symmetric ±5 → up-to-11-point
window). Both `ponytail:`-commented as tunable.

**Direction of effect:** the term enters via `max()`, so it can only *raise* a bar
and thus only *remove* false positives — never add a suspect. Any golden-fixture
change from #5 could only be a *reduction*; the Vanguard track (5 residuals < 10)
never activates the local term, so it is unchanged.

**Memory:** the window is a slice of the per-track `measured` list already held;
bounded by one satellite. No new corpus-scale state.

**Tests:** a synthetic track with a locally-elevated segment where the global
median+MAD would flag a point but `20·local_median` correctly does not; the
window-too-small inactivity case.

---

## #4 — regime-aware gap gates

**Today:** a flat `GAP_LIMIT_DAYS = 3.0` skips any adjacent pair wider than 3 days
(the `sgp4` residual grows with the propagation gap). GEO objects are re-issued less
often, so a flat 3-day gate discards most GEO pairs.

**Change:** replace the flat gate with `_gap_limit(mean_motion_rev_per_day)`:

```
GEO_MEAN_MOTION_MAX = 1.5   # rev/day; below this = geosync/GEO regime
GAP_LIMIT_LEO_MEO_DAYS = 3.0
GAP_LIMIT_GEO_DAYS = 7.0

_gap_limit(n) -> GAP_LIMIT_GEO_DAYS if n < GEO_MEAN_MOTION_MAX else GAP_LIMIT_LEO_MEO_DAYS
```

Two buckets, matching the issue: GEO/geosync (< 1.5 rev/day) → 7 days; everything
else (LEO ~11–16, MEO ~2) → 3 days. The 1.5 rev/day boundary sits cleanly between
MEO (~2) and geosync (~1.0027), far from any real object's mean motion, so
float-parse jitter cannot flip a classification.

**Mean-motion source:** `sat.no_kozai` (radians/min, `sgp4`'s own parse), converted
to rev/day (`no_kozai · 1440 / 2π`) — reuses the `Satrec` already built in the loop,
consistent with the "epoch comes from `sgp4`'s parse" precedent. Classify on the
propagation source record (`prev_sat`); mean motion is near-constant across a pair,
so either endpoint is equivalent.

**Constant `GAP_LIMIT_DAYS`** is removed; call site `0 < dt <= GAP_LIMIT_DAYS`
becomes `0 < dt <= _gap_limit(...)`. Vanguard (~10.8 rev/day) → LEO → 3-day gate;
the existing wide-gap test (~4-day LEO pair) still skips. Golden unchanged.

**Tests:** a GEO-regime pair (~1 rev/day) at a 5-day gap is now *measured* (was
skipped); a LEO pair at 5 days still skipped; the 1.5 boundary.

---

## #1 — leave-one-out culprit isolation

**Today:** a corrupt record R between neighbours A and B makes both pairs (A→R) and
(R→B) hot. Each hot pair is attributed to its *second* record, so R is flagged (from
A→R) *and* B is flagged (from R→B) — two suspects, one of them the innocent
successor.

**Change:** restructure `_track_suspects` to hold the track's `Satrec`s and classify
hot pairs before emitting.

1. Build `sats: list[tuple[Satrec | None, CleanedRecord]]` for the track (None =
   `sgp4` init error; still breaks the propagation chain and emits `ORBIT_ERROR` for
   codes 1–5 exactly as today).
2. Compute in-gate adjacent residuals as `pairs[i] = residual(sats[i-1], sats[i])`
   for consecutive non-None pairs within `_gap_limit`. Threshold from all residuals.
3. A pair is **hot** iff `resid > threshold_for_that_pair + RESIDUAL_QUANTUM_KM`
   (unchanged guardband; per-pair threshold from #5).
4. **Isolation:** for record at position `i`, if the *incoming* pair `pairs[i]` and
   the *outgoing* pair `pairs[i+1]` are both hot, both neighbours (`i-1`, `i+1`) are
   non-None, and the **leave-`i`-out** residual `residual(sats[i-1], sats[i+1])` is
   within `_gap_limit(i-1)` and **not hot** → record `i` is an isolated culprit:
   emit **one** `ORBIT_OUTLIER` for record `i` with an `(isolated by leave-one-out:
   neighbours agree without it)` detail, and mark pairs `i` and `i+1` consumed so
   neither emits again.
5. **Remaining hot pairs** (not consumed): emit as today — one `ORBIT_OUTLIER`
   attributed to the pair's second record. This covers the manoeuvre *step* (single
   hot pair: A→M big, M→M+1 small — everything after follows the new orbit) and the
   ambiguous case (both hot but leave-one-out still hot / un-measurable).

**Severity:** everything stays `VRFY-ORBIT-OUTLIER` **soft**. Isolation improves
*attribution* (culprit alone, no double-flag), never certainty — a spike and a
manoeuvre are still both soft. The `(isolated)` detail is telemetry, not a verdict.

**Determinism & memory:** all ops are on the 0.1-km-rounded residuals; the extra
`Satrec` list and the one leave-one-out `sgp4` call per candidate are bounded by one
satellite's epoch count. The golden Vanguard track has only single-hot-pair cases →
no isolation → suspect set unchanged.

**Tests:** a synthetic 3+-record spike (interior record corrupted, both neighbours
clean) → exactly one isolated suspect on the culprit, none on the successor; a
manoeuvre step (one hot transition pair, tail follows) → one suspect at the
transition, no isolation; both-hot-but-leave-one-out-still-hot → falls back to
per-pair (two suspects); endpoint corruption (only one neighbour) → per-pair.

---

## #3 — `--sensitivity` dial

**New CLI:** `--sensitivity {sensitive,strict}` on the `verify` subparser, default
`sensitive` (today's behaviour). Threaded `run_verify(..., sensitivity="sensitive")`
→ `run_orbit_pass(..., sensitivity=...)` → `_track_suspects` → `_threshold`.

**Model:** a small frozen dataclass maps a tier to the threshold knobs:

```
@dataclass(slots=True, frozen=True)
class Sensitivity:
    floor_km: float
    mad_k: float

SENSITIVE = Sensitivity(floor_km=100.0, mad_k=10.0)   # today
STRICT    = Sensitivity(floor_km=200.0, mad_k=20.0)   # fewer, higher-confidence

_TIERS = {"sensitive": SENSITIVE, "strict": STRICT}
```

`RESIDUAL_FLOOR_KM` / `MAD_K` module constants become `SENSITIVE`'s fields (the
default), so nothing changes unless `--strict` is chosen. `LOCAL_K` (#5) stays a
fixed constant — the dial scales the global floor+MAD terms only, keeping the surface
minimal. Two tiers, per the approved decision.

**Determinism:** the tier is a fixed table, no RNG; default keeps the golden fixture
byte-identical. `strict` gets its own small test (a residual flagged under
`sensitive` that clears the higher `strict` bar).

---

## #2 — stratified oversampling (dup-epoch)

**Today:** `sample_catalogs(population, sample, all_sats)` returns all sats (when
`all_sats`/small) else an evenly-spaced deterministic slice of sorted catalog ids.
Nothing biases the sample toward the interesting satellites.

**Change:** oversample **dup-epoch** satellites — those with same-`(catalog, epoch)`
re-issues, the cases most likely to carry an orbit inconsistency. This set is
**free**: `checks.find_conflicts` already streams the fully-sorted corpus before the
orbit pass; it will additionally collect the set of catalogs that had any re-issue or
clash into a `dup_epoch_catalogs: set[int]` and return it. `run_verify` passes that
set into the orbit pass.

`sample_catalogs` gains a priority set:

```
sample_catalogs(population, sample, all_sats, oversample) ->
    all when all_sats / population <= sample;
    else: take (oversample ∩ population), sorted, up to `sample`;
          fill any remaining budget with the evenly-spaced slice of the rest.
```

All deterministic (sorted sets, integer arithmetic, no RNG). When `oversample` is
empty the result is exactly today's evenly-spaced sample — a pure superset behaviour,
so existing `TestSample` cases pass unchanged.

**"Repaired" satellites — deferred (per approved decision).** Repair provenance is
not visible to `verify`: it lives in the clean run's `report.jsonl`, not in
`cleaned/`. Oversampling it would require `verify` to read clean-run findings
artifacts — a new coupling for a pure consumer. Dropped from scope; noted here and in
the issue as a possible follow-up if dup-epoch stratification proves insufficient.

**Tests:** dup-epoch catalogs are all included even when they'd fall outside the
evenly-spaced slice; empty oversample reproduces today's sample byte-for-byte; the
budget is respected (oversample larger than `sample` is truncated deterministically
by sorted id).

---

## Out of scope

- "Repaired" oversampling (needs `report.jsonl` coupling) — deferred, see #2.
- Any change to the hard/soft verdict model, the quantum, or the external-sort
  streaming shape.
- Three-tier sensitivity — two tiers approved.

## Docs to update on landing

- `CHANGELOG.md` `[Unreleased]` — one note per refinement as it lands.
- `ARCHITECTURE.md` — the `orbit` line (§ module table) to mention regime-aware
  gates, local-median term, LOO isolation, `--sensitivity`, and stratified sampling.
- Close #163, tick #148 (and close #148 if this completes verify).
