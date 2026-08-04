# Epoch single definition: one truth for a record's moment in time — design

**Date:** 2026-07-30
**Status:** proposed
**Issue:** [#199](https://github.com/elfensky/lintle/issues/199) (follow-up: [#200](https://github.com/elfensky/lintle/issues/200))

## Problem

"A record's moment in time" is defined three times, and the definitions disagree
at year boundaries:

1. `verify/epoch.py` — `epoch_key(line1) = yy*1000 + day` (the sort/group key)
2. `history.py:17-21` — `epoch_dt(line1)` → aware UTC datetime (the instant)
3. `verify/__init__.py:103-107` — a third inline copy computing the histogram bucket

`tle.py:231-240` accepts `0.0 < day < 367.0` with no leap-year logic, so these
are all clean, non-quarantined records:

- `yy=19 day=366.5` → key `2019366.5`, but true instant `2020-01-01T12:00Z`
- `yy=20 day=000.5` → key `2020000.5`, but true instant `2019-12-31T12:00Z`

Every consequence below was repro'd end-to-end against the real functions on
2026-07-30 (post-`4b0d925` — the July fix series is in and none of it touches this):

- **Key order ≠ instant order at year boundaries** → the dedup import stream
  (sorted by `(catalog, epoch_key)`) feeds `history.analyze_epochs` non-monotone
  epochs → negative deltas → `manifest.jsonl` rows with `span_days: -0.5`,
  `first_epoch > last_epoch`; extract sidecars with negative `span_days` /
  `mean_records_per_day`. Negative deltas also drag the median down and
  suppress real gaps.
- **Same instant, two keys** (`20/000.5` vs `19/365.5`) → `dedup._group_key`
  puts them in different groups → the re-issue the dedup exists to collapse
  survives; verify's conflict rules (`checks.py`) are equally blind.
- **Histogram year-bucket bug**: the inline copy takes `month` from the
  rolled-over datetime but reuses `year` from the raw field → `19/366.5` lands
  in bucket `2019-01` (true: `2020-01`). Naive-vs-aware tzinfo is *not* the bug
  (verified harmless).
- **Orbit pass drops year-boundary pairs**: `orbit.py:176`'s `0 < dt` gate
  discards negative-dt pairs → never residual-checked, `pairs_measured`
  under-reports.
- **n=3 gap dead zone** (related, fixed together): `history.py:66-73`,
  `GAP_FACTOR = 10`. With exactly 2 deltas, `d > 10*median` where
  `median = (a+d)/2` is algebraically impossible → a 3-record history with a
  274-year hole reports `gap_count: 0` while shipping `largest_gap_days: 99999`
  in the same row.

## Design

### One module: `src/lintle/epoch.py` (top-level, stdlib-only)

Not under `verify/` — two structural reasons: `tle.py:231` parses the same
columns "in lockstep" (manual lockstep is what broke), and the one definition
must be importable from anywhere without crossing the sgp4/verify wall; a
`lintle.verify` home forbids that. Top-level also dissolves the would-be cycle
`verify/__init__ → history → verify.epoch`. `verify/epoch.py` is **deleted**;
importers repointed: `verify/records.py:13`, `verify/__init__.py:22`,
`history.py:11`, `tests/test_dedup.py:10`, `tests/test_orbit.py:12`.
`history.py` keeps re-exporting `epoch_dt`/`iso` so `extract.py` is untouched.

### Interface: four wrappers over one normalizer — no dataclass

```python
parse_epoch(line1) -> (year, day)   # NORMALIZED four-digit year + day-of-year
epoch_key(line1)   -> float         # year*1000 + day, sorts ≡ chronology
epoch_dt(line1)    -> datetime      # aware UTC instant (moves from history.py)
iso(dt)            -> str           # moves from history.py:24
```

All four route through one private `_normalize(line1) -> (year, day)`. An
`Epoch(year, day, key, instant)` dataclass was considered and dropped: every
consumer wants exactly one of these values (`epoch_key` in the corpus-scale
sort path must not eagerly build a `datetime`), so the box had zero direct
consumers. The guarantee that matters is one *definition*, not one *parse* —
add the dataclass later if a caller ever wants key + instant together.

**`parse_epoch` returns the normalized pair** — every public function in the
module speaks the same truth; a raw-columns accessor would re-arm the footgun.
Anyone who genuinely wants raw columns can slice the line.

### Normalization: on the decimal string, never float subtraction

Parse raw `yy` → four-digit year (pivot 57: `57`–`99` → 19xx, `00`–`56` →
20xx), raw `day` float. Then roll whole days across year lengths
(`calendar.isleap`):

- `day < 1.0` (only `0.x` is possible — `tle.py` excludes 0.0): roll **back**
  using the *prior* year's length — `2021/000.5` → `2020/366.5` (2020 is leap).
- `day ≥ year_length + 1`: roll **forward** — `2019/366.5` (non-leap) →
  `2020/001.5`. Leap-year `366.x` stays put. `day < 367` caps the roll at one
  year in either direction.

The normalized day is re-formed **as a string** — `f"{ndd:03d}.{line1[24:32]}"`
with the fractional digits verbatim (rolling shifts whole days only) — then
`float()`ed once. This is load-bearing: the normalized key of
`19/366.99999999` must be bit-identical (`==` *and* `repr()`-equal) to a
literal `20/001.99999999`, because `epoch_key` is serialized via `repr()` in
the sorter spill (`grouping.py:37`) and suspect frame (`verify/report.py:223`).
In-range records take the no-roll path and their keys stay byte-identical to
today's formula (minimal artifact diff).

### Invariants (each carries a test)

1. **Key order ≡ instant order**: `epoch_key(a) < epoch_key(b) ⇔
   epoch_dt(a) < epoch_dt(b)` (hypothesis property).
2. **Equal instants ⇒ bit-equal keys** with identical `repr()`.
3. **In-range back-compat**: keys for records needing no roll are bit-identical
   to the v1 formula.
4. **`epoch_dt` output is unchanged for *all* inputs** — `datetime` +
   `timedelta` arithmetic already rolled correctly; normalization must not
   move any instant.
5. **Raise-on-garbage preserved**: non-numeric epoch columns raise
   `ValueError`, never a silent zero. Dedup's `DEDUP-UNUSABLE-RECORD` write
   seam (`4ce7513`) and verify's revalidate both depend on this contract.
6. **Stdlib-only closure**: `lintle.epoch` imports nothing outside the
   standard library (import-graph test leg).

Pivot edge: normalization applies to the four-digit year, so a roll can leave
the two-digit-expressible range (back-roll from `57/000.x` lands in 1956) —
keys use the four-digit year, so `epoch_key("57/000.5") = 1956366.5` cannot
collide with a literal `56/366.5 = 2056366.5`. (The mirror case — forward roll
out of 2056 — is unreachable: 2056 is a leap year, so its `366.x` is a valid
Dec 31.)

### What is deliberately untouched

- **`tle.py`'s `(0, 367)` bound stays.** Tightening it changes what "perfect"
  means (Critical Rule #4) and would newly quarantine real space-track
  rollover records. `tle.py` gains only a comment pointing at `lintle.epoch`
  as the owner of normalization.
- **`01-cleaned/*`** — no corpus re-clean; the clean path never sees epochs.
- **The resume checkpoint** — carries no epoch data (verified).
- **Extract's binary search** — needs only catalog monotonicity; the
  `958d40b` preflight probes catalog order only (verified), so a new
  `extract` reads an old `05-dedup/` tree fine.

### Consumer fixes riding the single definition

- **Histogram** (`verify/__init__.py:103-107`): the inline third copy is
  deleted at the call site — `dt = epoch_dt(rec.line1)`, bucket on
  `f"{dt.year}-{dt.month:02d}"`. Year and month now both come from the
  normalized instant.
- **n=3 gap dead zone** (`history.py`): keep *reporting*
  `statistics.median(deltas)` (byte-stable field), but *threshold* on
  `statistics.median_low(deltas)` — one-token change that makes the n=3 case
  reachable (`d > 10·min(a,d)` holds for a genuine outlier).
- **Low-record satellites**: gap analysis stays definitionally silent below 3
  records — with one delta there is no "typical spacing" to be 10× of, and
  the row already tells the truth (`largest_gap_days` visible,
  `median_spacing_days: null`). The threshold gets a name —
  `MIN_GAP_RECORDS = 3` in `history.py` — and dedup's `summary.json` gains a
  `gap_silent_satellites` tally (manifest rows with
  `count < MIN_GAP_RECORDS`). No per-row flag (derivable from `count`), no
  separate file: the "low numbers bucket" is a query
  (`jq 'select(.count < 3)'`), not an artifact.

### Schema versions

`dedup.SCHEMA_VERSION` and `verify/report.SCHEMA_VERSION`: `"1"` → `"2"`.
The row *shape* barely changes (one added summary tally), but epoch keys are
load-bearing values — sort keys and group identities — and their meaning
changes at year boundaries. `schema_version` is the only in-band
comparability signal between two runs' artifacts; a version that doesn't
change when values change meaning guards nothing. Nothing downstream
hard-codes `"1"` (extract reads dedup's summary tolerantly since `958d40b`).

## Ripple

A re-run of `verify`/`dedup`/`extract` on the same `01-cleaned/` produces
different bytes than a v1 run — acceptable across versions, noted in
CHANGELOG: `import.*` order at year boundaries, `notes.*` epoch_key values,
manifest spans go non-negative, `suspects.*` keys/order/prose, `summary.json`
histogram buckets, orbit `pairs_measured` rises (previously-dropped
year-boundary pairs return).

## Out of scope

`verify/orbit.py:171-176` derives dt from sgp4's own epoch fields — a
*fourth* parse, tracked as [#200](https://github.com/elfensky/lintle/issues/200)
(census re-baseline + divergence audit after this lands). Hard constraint
either way: `sgp4` never comes near `lintle/epoch.py`; the stdlib-only test
leg enforces it.

## Docs

`ARCHITECTURE.md`: module map gains `epoch.py`, verify submodule list drops
`epoch`, `history.py` entry updated, the `extract → history → verify.epoch`
prose repointed, the day-of-year note repointed. `CLAUDE.md`: project-layout
tree updated. CHANGELOG note rides the branch (collected at next release).
