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
3. **Constant memory (Rule #3).** The *detection* pass streams one satellite's
   track at a time through the external merge sort. New per-track state (the track's
   `Satrec` list, the position-aligned `pairs` array, a local-median window) is
   bounded by a single satellite's epoch count, never the corpus. #2's sampling-side
   `dup_epoch_catalogs` set is *not* per-track state — it is O(distinct catalogs),
   the same budget as the existing `population`/sample sets; Rule #3 governs the
   streaming passes, not the sample selection (see #2's memory note).
4. **The `sgp4` wall.** Only `verify/orbit.py` imports `sgp4`, lazily, only under
   `--orbit`. Validity always routes through `tle.validate_record`; `sgp4`
   measures position, never validity.

The golden fixture is a 6-epoch Vanguard-1 track. Its cases are all single-hot-pair
or wide-gap-LEO, so none of the five refinements alters its suspects — the fixture
stays byte-identical (verified deliberately, not assumed).

## Landing order

Five refinements, five small PRs to `develop`, **ordered so each one's dependencies
have already landed** (localized-first). They are *not* mutually independent: #1
consumes #5's `_local_threshold` and #4's `_gap_limit`, so #5 and #4 must precede it.

1. **#5 windowed local-median term** — smallest, self-contained threshold tweak.
2. **#4 regime-aware gap gates** — one helper, one call-site change.
3. **#1 leave-one-out culprit isolation** — the meaty one; restructures
   `_track_suspects` (introduces the position-aligned `pairs`/`sats` arrays #5's
   window is then re-expressed over — see #5 and #1 below).
4. **#3 `--sensitivity` dial** — CLI surface + threshold parameterization.
5. **#2 stratified oversampling** — dup-epoch priority sample.

Order rationale: #5 and #4 are localized; landing them first keeps #1's larger
refactor working against a settled threshold/gate. #3 parameterizes the threshold #5
just extended, so it follows. #2 is orthogonal (sampling, not detection) and lands
last.

**Shared structural note (affects #5 and #1).** Today's `_track_suspects` holds a
*compacted* `measured` list — a pair is appended only when it is in-gate and
measurable, so **list index ≠ record position**. Both #5's window and #1's
neighbour math need *position* alignment, not list adjacency. #5 lands first over
the compacted list but with its window defined on chain-contiguous, time-adjacent
residuals (below); #1 then restructures to an explicit position-aligned `pairs`
array with `None` holes, and #5's window is read over that. Neither may treat two
list-adjacent residuals separated by a skipped gap as "local" or "adjacent".

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
- Add `_local_threshold(residuals, i)` → `round(LOCAL_K · median(window), 1)` where
  `window` is residuals in `[i-LOCAL_HALF, i+LOCAL_HALF]`. **Round to the 0.1 km
  quantum**, exactly as `_threshold` does — otherwise `LOCAL_K · median` of an
  even-length window (an average of two 0.1-quantised residuals → a 0.05 multiple,
  e.g. `20·5.05 ≈ 101.00000000000001`) feeds sub-quantum float dust through the
  outer `max()` into the guardband comparison against a rounded residual, deciding
  the boundary case on the last ULP. The "round the threshold, then require a full
  quantum over it" contract must hold for *both* terms.
- Inactive (returns `0.0`, so the outer `max` ignores it) when the window has fewer
  than `MIN_EPOCHS_FOR_MAD` points — same "not enough data to trust a spread" rule
  the global term uses.
- Per pair, the effective bar is `max(global_threshold, local_threshold(i))`.

**The window must be *local in time*, not local in list index.** The residual list
is compacted (out-of-gate and unmeasurable pairs leave no slot), so a naive
±LOCAL_HALF *index* window can straddle an arbitrarily long skipped gap or a broken
propagation chain — pulling stale residuals from a different track segment into the
"local" median. That defeats the purpose (a post-gap spike would be masked by
pre-gap residuals) and, though it still only *raises* the bar, the raise removes a
*true* positive. The window is therefore the residuals in `[i-LOCAL_HALF,
i+LOCAL_HALF]` **restricted to a contiguous run of chain-adjacent, in-gate pairs
around `i`** — any skipped/ungated/error pair breaks the run and bounds the window.
When #1's position-aligned `pairs` array lands, this is exactly "walk out from `i`
while pairs are non-`None`, stop at the first hole".

**Constants:** `LOCAL_K = 20.0`, `LOCAL_HALF = 5` (a symmetric ±5 → up-to-11-point
window). Both `ponytail:`-commented as tunable.

**Direction of effect:** the term enters via `max()`, so it can only *raise* a bar
and thus only *remove* a suspect — never add one. This is monotone, not "only false
positives": a bar raised by a *correctly* local window removes false positives, but
a bar raised by a *polluted* (non-time-local) window can mask a real spike — which
is exactly why the window is time-restricted above. Any golden-fixture change from
#5 could only be a *reduction*; the Vanguard track (5 residuals < 10) never
activates the local term, so it is unchanged.

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
else (LEO ~11–16, MEO ~2, Molniya ~2) → 3 days. The 1.5 rev/day boundary sits
between the named MEO/geosync classes (MEO ~2, Tundra ~1.0, geosync ~1.0027). It is
*not* true that no real object sits near 1.5 — the HEO/GTO/disposal-orbit population
is roughly continuous across 1.0–2.0 rev/day, so objects with ~16–20 h periods do
land near the boundary. The design is safe anyway because **the misclassification is
harmless**: it only swaps a 3-day vs 7-day *soft* gap gate, never a verdict, and the
gates differ by a couple of days where the residual is still measurable. (Float-parse
jitter is likewise a non-issue for the same reason — a boundary object that flips
class merely swaps one soft gate for the other.)

**Mean-motion source:** `sat.no_kozai` (radians/min, `sgp4`'s own parse), converted
to rev/day (`no_kozai · 1440 / 2π`) — reuses the `Satrec` already built in the loop,
consistent with the "epoch comes from `sgp4`'s parse" precedent. **Implementation
trap:** code this as `no_kozai * 1440 / (2 * math.pi)` — `no_kozai * 1440 / 2 *
math.pi` evaluates left-to-right (`/` and `*` share precedence) to `(…/2)·π`, off by
π². Classify on the propagation source record (`prev_sat`, always `error == 0` at the
call site — `prev` is assigned only past the `if sat.error: continue` guard); mean
motion is near-constant across a *clean* pair, so either endpoint is equivalent.
(Caveat: if `prev`'s mean motion is itself corrupted to a different regime — but
still `sgp4`-valid — the bucket is chosen from the corrupt value. Impact is bounded
to the same soft gap-gate swap, and such a pair's residual is large regardless, so no
special handling; noted for the reviewer.)

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
2. Build a **position-aligned** `pairs` array, *not* today's compacted `measured`
   list: `pairs[i]` is the residual of `sats[i-1] → sats[i]` (indexed by the second
   record's position `i`), or **`None`** when that pair is unmeasurable — either
   endpoint `None`, out of gate, or `_pair_residual` returned `None`. The gate is the
   **full** `0 < dt <= _gap_limit(...)` — the `0 <` lower bound must be restated
   here; dropping it would admit a `dt == 0` same-epoch re-issue whose residual is
   ≈0.0, and injecting that into the threshold set shifts every pair's bar (and #2
   deliberately oversamples exactly these dup-epoch satellites, maximising exposure).
   `pairs[i]` and `pairs[i+1]` are a record's *incoming* and *outgoing* pair only
   because the array is position-indexed with holes — never conflate list-adjacent
   entries separated by a `None`. Threshold from the non-`None` residuals.
3. A pair is **hot** iff `resid > threshold_for_that_pair + RESIDUAL_QUANTUM_KM`
   (unchanged guardband; per-pair threshold from #5).
4. **Isolation:** for record at position `i`, if the *incoming* pair `pairs[i]` and
   the *outgoing* pair `pairs[i+1]` are both present and both hot, both neighbours
   (`i-1`, `i+1`) are non-`None`, and the **leave-`i`-out** residual
   `residual(sats[i-1], sats[i+1])` is measurable and **not hot** → record `i` is an
   isolated culprit: emit **one** `ORBIT_OUTLIER` for record `i` with an `(isolated
   by leave-one-out: neighbours agree without it)` detail, and mark pairs `i` and
   `i+1` consumed so neither emits again. Iterate `i` left-to-right and skip a
   candidate whose incoming pair a prior isolation already consumed, so overlapping
   candidates resolve deterministically.

   Two properties of the leave-one-out probe the naive version gets wrong:
   - **Its gap is doubled.** The probe skips record `i`, so `dt(i-1, i+1) ≈ dt(i-1,i)
     + dt(i,i+1)` — up to twice a single cadence step. Gating it on the ordinary
     `_gap_limit` would reject the probe for the *typical* case (two in-gate ~2-day
     LEO pairs → a ~4-day probe > the 3-day LEO gate), aborting isolation and
     re-emitting the double-flag this refinement exists to remove. Gate the probe on
     `2 · _gap_limit(prev_sat)` (classify off `sats[i-1]`), i.e. `0 < dt(i-1,i+1) <=
     2·_gap_limit`; if it exceeds even that, isolation is genuinely un-measurable →
     fall back to step 5.
   - **"Not hot" is judged against the global threshold only.** The probe residual
     has no pair position and therefore no #5 local window, so there is no
     `threshold_for_that_pair` to reuse. Compare it to the **global** `_threshold`
     (plus the one-quantum guardband) — a single, unambiguous rule, so two
     implementers cannot produce two deterministic-but-different suspect sets.
5. **Remaining hot pairs** (not consumed): emit as today — one `ORBIT_OUTLIER`
   attributed to the pair's second record. This covers the manoeuvre *step* (single
   hot pair: A→M big, M→M+1 small — everything after follows the new orbit) and the
   ambiguous cases: both hot but leave-one-out still hot / un-measurable / over the
   doubled gate, and two *adjacent* corrupt records (the probe lands on the second
   corrupt record → hot → no isolation, so the pair-level flags — including the clean
   successor of the run — still emit; isolation only cleans up a lone interior
   spike).

**Severity:** everything stays `VRFY-ORBIT-OUTLIER` **soft**. Isolation improves
*attribution* (culprit alone, no double-flag), never certainty — a spike and a
manoeuvre are still both soft. The `(isolated)` detail is telemetry, not a verdict.

**Determinism & memory:** all ops are on the 0.1-km-rounded residuals; the extra
`Satrec` list and the one leave-one-out `sgp4` call per candidate are bounded by one
satellite's epoch count. The golden Vanguard track has only single-hot-pair cases →
no isolation → suspect set unchanged.

**Tests:** a synthetic 3+-record spike (interior record corrupted, both neighbours
clean, adjacent gaps small enough that the doubled probe is in `2·_gap_limit`) →
exactly one isolated suspect on the culprit, none on the successor; the **same spike
with wider cadence** so the doubled probe exceeds `2·_gap_limit` → falls back to
per-pair (isolation un-measurable, not silently dropped); a manoeuvre step (one hot
transition pair, tail follows) → one suspect at the transition, no isolation;
both-hot-but-leave-one-out-still-hot → falls back to per-pair (two suspects); a
skipped interior pair (a `None` hole) between two hot pairs → the two are *not*
treated as one record's incoming/outgoing; endpoint corruption (only one neighbour)
→ per-pair.

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
byte-identical (`SENSITIVE(100.0, 10.0)` substitutes the identical float literals
into the identical arithmetic; `MIN_EPOCHS_FOR_MAD` stays a module constant, not a
tier field). `strict` gets its own small test: a residual sitting **between** the two
floors — flagged under `sensitive` (over 100), **not** flagged under `strict` (under
200), e.g. `150.2 km` with fewer than 10 residuals. (A residual over the *strict* bar
would clear the *sensitive* bar too and test nothing — the suppression is only
visible in the gap between the tiers.)

---

## #2 — stratified oversampling (dup-epoch)

**Today:** `sample_catalogs(population, sample, all_sats)` returns all sats (when
`all_sats`/small) else an evenly-spaced deterministic slice of sorted catalog ids.
Nothing biases the sample toward the interesting satellites.

**Change:** oversample **dup-epoch** satellites — those whose track carries more than
one record at the same `(catalog, epoch)`, the cases most likely to carry an orbit
inconsistency. `checks.find_conflicts` already streams the fully-sorted corpus before
the orbit pass, so it is the natural collection point — but this is *not* "free", and
three things the earlier draft glossed must be stated:

- **New collection logic, not the existing counter.** `find_conflicts` today only
  increments `reissues` when a same-epoch record carries a *different* orbital state;
  exact-duplicate and admin-only re-issues (same orbit, new element-set) are not
  counted, yet they are still dup-epoch. The set must be populated by its own test —
  "this `(catalog, epoch)` group has ≥ 2 records" — keyed off the group boundary the
  function already tracks, independent of the state-difference branch.
- **Gate the collection behind `orbit`.** `find_conflicts` runs on *every* verify,
  but `population` (and thus any consumer of `dup_epoch_catalogs`) is built only under
  `--orbit` (`run_verify` at `__init__.py`). Collecting the set unconditionally makes
  the default `sgp4`-free path pay for a structure it never reads. Pass an
  `orbit: bool` (or a nullable out-set) into `find_conflicts` so the set is gathered
  only when the orbit pass will consume it.
- **Return-shape and call-site churn is real.** `find_conflicts` returns a 2-tuple
  `(conflicts, reissues)` today; adding the set makes it a 3-tuple. That changes the
  unpack in `run_verify` (`conflicts, epoch_reissues = …`) **and every assertion in
  `tests/test_verify.py`** that compares against `([], 0)` / `([], 1)` (eight of
  them). These land in this PR, not "for free".

`run_verify` passes the collected set into the orbit pass. `sample_catalogs` gains a
priority set (**with a default so existing 3-arg callers keep working** — the current
`TestSample` cases call `sample_catalogs(pop, sample, all_sats)` positionally and
would otherwise raise `TypeError`):

```
sample_catalogs(population, sample, all_sats, oversample=frozenset()) ->
    all when all_sats / population <= sample;
    else:
      prio = sorted(oversample ∩ population)
      if len(prio) > sample:            # priority stratum overflows the budget:
          evenly-space *within* prio    #   spread across the id range, don't take
          (prio[(i*len(prio))//sample]) #   the lowest `sample` ids
      else:
          take all of prio, then fill the remaining budget with the evenly-spaced
          slice of (population − prio).
```

The `oversample ∩ population` intersection is **load-bearing, not cosmetic**:
`find_conflicts` does not filter the `-1` unparseable-catalog sentinel (unlike the
`population` build, which does), so intersecting against the `-1`-free `population` is
what keeps `-1` out of the sample. Implement the intersection, not a raw
`oversample`.

All deterministic (sorted sets, integer arithmetic, no RNG). **Superset only in the
empty case:** when `oversample` is empty the result must be *byte-identical* to
today's `{cats[(i*n)//sample] for i in range(sample)}` — an implementation obligation
(reuse that exact formula over the full sorted population and full budget), which is
why the existing `TestSample` cases pass unchanged. When `oversample` is non-empty the
sample is a *different* same-size set (priority ids replace evenly-spaced ones), not a
superset — that is intended, and no existing test locks it.

**Memory:** `dup_epoch_catalogs` is a `set[int]` bounded by the number of *distinct
dup-epoch catalogs* — the same O(distinct-catalogs) class as the existing
`population`/sample sets, **not** corpus-record-scale (so no OOM) and **not** the
per-track "one satellite at a time" bound of Rule #3. Rule #3 governs the streaming
*detection* passes; this is sampling-side state, in the same budget as `population`.
The design note is: it is catalog-scale and orbit-gated, not "free" and not per-track.

**"Repaired" satellites — deferred (per approved decision).** Repair provenance is
not visible to `verify`: it lives in the clean run's `report.jsonl`, not in
`cleaned/`. Oversampling it would require `verify` to read clean-run findings
artifacts — a new coupling for a pure consumer. Dropped from scope; noted here and in
the issue as a possible follow-up if dup-epoch stratification proves insufficient.

**Tests:** dup-epoch catalogs are all included even when they'd fall outside the
evenly-spaced slice; empty oversample reproduces today's sample byte-for-byte (the
existing `TestSample` cases, run through the new 4-arg default); the budget is
respected and, when the priority stratum overflows `sample`, the kept ids are
**evenly spaced across the stratum's id range**, not the lowest `sample` ids
(assert a high-id dup-epoch catalog survives); the `-1` sentinel never reaches the
sample; `find_conflicts` returns the dup-epoch set only under `orbit` and its
3-tuple shape is asserted (with `test_verify.py` updated to the new arity).

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
