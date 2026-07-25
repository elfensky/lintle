# Corpus coverage: epoch histogram + per-satellite manifest — design

**Date:** 2026-07-25
**Status:** proposed
**Extends:** `2026-07-24-extract-gap-awareness-design.md`, `2026-07-23-extract-satellite-history-design.md`

## Problem

`extract` surfaced a corpus-wide temporal hole (2017-03 → 2017-06 — no TLEs for
many satellites) but only **one satellite at a time**: the hole is visible only by
extracting individual histories and eyeballing their gaps. Two distinct questions
hide behind that experience, and conflating them is the central design risk:

1. **Source-level:** *is a time window globally missing from the corpus?* (the 2017
   hole — a missing space-track export window, a property of the source).
2. **Per-satellite:** *which objects have holes / which are gap-free?* (a property of
   each satellite's own track).

A naive per-satellite gap census answers neither well: it would report the single
2017 source hole **N times as N satellite gaps**, drowning the real signal. The two
questions need two different, cheap artifacts — and both fall out of streams the code
already produces.

## Behaviour

### 1. Epoch record-density in `verify`'s summary (source-level)

`verify.run` already streams every cleaned record (`records.iter_file`, verify/`__init__.py`).
Add **one `collections.Counter`** in that existing first pass, keyed by `(year, month)`
derived from `parse_epoch(rec.line1)` — no new pass, no new sort, no `sgp4`.

- Bin key: `f"{year}-{month:02d}"`. Month granularity (not year) so a ~3-month hole
  reads as consecutive collapsing bins; a `year` bin would hide it.
- Emitted into the existing `checked` dict → `summary.{json,md}` as
  `epoch_distribution` (a `dict()` of the Counter at the output boundary, sorted by
  key — byte-deterministic).
- **Informational only.** No suspects, no exit-code effect — a corpus hole is source
  telemetry, not a validation failure.

**Honest naming (load-bearing).** This is **record density over time**, *not*
"coverage." Record density ≠ coverage: a window can be dense from heavy re-issue while
some objects vanish; early corpus years are legitimately sparse. So it answers *"is
there a sharp global discontinuity?"* (the 2017 case — a corpus-wide cliff) and does
**not** claim per-satellite coverage. Naming it "coverage gaps" would smuggle in an
inference it can't support. The field name and summary prose say "epoch distribution /
record density."

### 2. `manifest.jsonl` — per-satellite corpus manifest (per-satellite level)

One JSON row per catalog, emitted as a **byproduct of `dedup`**, where the globally
sorted, re-issue-collapsed `(catalog, epoch)` stream already flows (`dedup._groups`).
Because all of a catalog's groups are contiguous in that stream, accumulate one
catalog's epochs, finalize its row on the **catalog boundary**, flush the last catalog
after the loop. Memory: one catalog's epoch list — the same bound `extract._analyze`
already deems safe (~tens of thousands of floats, hundreds of KB worst case). Written
in dedup's existing main loop (dedup/`run`).

**Deduped, not raw — deliberately.** Gaps are computed on the *collapsed* epochs (the
`g.kept` records = the import stream). Raw cleaned output has re-issues at identical
epochs (spacing 0), which are not gaps. So `dedup` is the correct home; `verify`'s
stream (which keeps re-issues) is not.

**One file, not a chunk set.** Bounded by catalog count (~50k in a 5-char corpus), not
record count → a few MB. A single durable `manifest.jsonl` via `fsutil.durable_write_text`,
alongside `import.*` / `notes.*` / `summary.json` under `05-dedup/`. No `ChunkedWriter`.

**Row schema** (compact ASCII JSON, one per line, catalog-ascending — deterministic):

```json
{"norad_id":25544,"records":8123,"first_epoch":"1998-10-24T...Z","last_epoch":"2024-...Z",
 "span_days":9500.0,"median_spacing_days":1.02,"largest_gap_days":41.3,"gap_count":0}
```

The per-gap array is **dropped** (extract's sidecar detail; ×50k rows it is pure
bloat and available on-demand via `extract`). Every useful predicate survives as a
`jq` filter over the fields above.

**The trivial-gapless footgun (must be documented).** `_analyze` sets `median = None`
for `< 3` records, so `gap_count == 0` for any 1- or 2-record satellite — they are
*trivially* gap-free. A naive `select(.gap_count==0)` therefore returns near-empty
tracks. The manifest exposes `records` and `span_days` **precisely so** the honest
predicate can exclude them:

```bash
# "10 random satellites with no gaps" — no lintle RNG, no new verb:
jq -r 'select(.gap_count==0 and .records>=50 and .span_days>=365) | .norad_id' \
  05-dedup/manifest.jsonl | shuf -n 10 | xargs lintle extract
```

`extract.run` already accepts `catalogs: list[int]`; the CLI already globs ids. So the
**only** missing piece for "search + random sample + extract" is `manifest.jsonl`.
Search is `jq`, random is `shuf`, fan-out is `xargs`. **lintle never owns the
randomness** — a `--random`/`--seed` verb would fight byte-determinism (Critical Rules
#1/#2) and is explicitly rejected. If the raw pipe ever proves annoying, a thin
`extract --from-manifest --sample N` wrapper is a *future* option — YAGNI now.

### 3. Staleness fingerprint (dedup → extract trust)

The `clean → dedup → verify → extract` chain reads `01-cleaned/` with nothing binding a
downstream run to a specific clean run's bytes. Stale `cleaned/` + fresh `dedup`, or a
subset re-clean, drifts silently — and re-clean is a ~30 GB operation.

Fingerprint = **`(sorted stem set, per-chunk sizes, records_read)`** — all already
computed by `dedup` (stems, `st_size`, the `n_read` tally). Written into dedup's
existing `summary.json`. `extract` (which already reads that summary) compares it
against the live `cleaned/` stems + sizes at run start.

- **NOT SHA-256 per chunk.** Hashing 28.7 GB is a full extra read pass every run
  (minutes of I/O) and targets *bit-rot*, not the stated threat (*staleness*). Rejected
  until bit-rot is an actual requirement.
- **Mismatch → `term.warning` + proceed, exit stays 0** — identical to extract's
  existing quarantine warning (`_warn_and_confirm`). Exit 2 stays reserved for
  absent/torn. Hard-failing would contradict extract's established warn-and-proceed
  contract.

### 4. Shared `analyze_epochs()` reducer (prerequisite for #2)

`extract._analyze` interleaves **I/O** (byte-copy span read) with a **pure reduction**
(deltas → median → reportable gaps → `HistoryStats`). Only the reduction is shared.
Lift it:

```python
def analyze_epochs(epochs: list[datetime], elsets: list[int | None]) -> HistoryStats  # pure, no I/O
```

- `extract` builds the two lists in its existing decode loop, then calls it.
- `dedup`-manifest builds them from each catalog's `g.kept.line1` (`parse_epoch` +
  `checks.element_set`, both already imported in dedup), then calls it.

Both reduce **identical inputs by construction** (the manifest reduces `g.kept`; the
import stream *is* `g.kept`; extract reads that stream), so byte-agreement is free
rather than a testing burden. `HistoryStats`, `Gap`, and `_epoch_dt`/`_iso` move to a
shared pure module (they carry no I/O); `extract` and `dedup` both import them. This
keeps the one-definition discipline the code already keeps with `checks.element_set`.

## Structure

- New shared pure module (proposed `history.py` or fold into an existing pure leaf):
  `HistoryStats`, `Gap`, `analyze_epochs`, `_epoch_dt`, `_iso`. No I/O, no `sgp4`.
- `extract.py`: `_analyze` becomes a thin I/O wrapper that builds the lists and calls
  `analyze_epochs`; sidecar unchanged.
- `dedup.py`: accumulate per-catalog epochs in `run`'s main loop; flush `manifest.jsonl`
  rows on catalog boundary + after the loop; add the fingerprint to `summary.json`. The
  manifest is written regardless of the conflict exit code (it is telemetry).
- `verify/__init__.py`: one `Counter` in the first pass; `epoch_distribution` into
  `checked`.

Import-graph wall untouched: no new `sgp4`/`lintle.verify` imports into the clean path;
`dedup`/`extract`/`verify` already sit on the auditor side.

## Errors and exit codes

- `verify`: unchanged (histogram is informational).
- `dedup`: unchanged (0 clean / 1 contradiction / 2 no cleaned output); manifest +
  fingerprint write on every non-error run.
- `extract`: unchanged; fingerprint mismatch is a warning, exit stays 0.

## Testing

- **Histogram**: synthetic records across months → correct per-bin counts; a 3-month
  hole → three empty/absent consecutive bins; deterministic bytes (golden on
  `epoch_distribution`).
- **`analyze_epochs`** (pure): uniform cadence → no gaps; one 40-day hole in daily
  cadence → one gap, correct span; `<3` records → `median None`, `gap_count 0`; extract
  and a manifest-style caller over the same epochs → identical `HistoryStats`.
- **Manifest**: multi-catalog synthetic dedup stream → one row per catalog, catalog
  order, deterministic bytes; the trivial-gapless case (1–2 record sat → `gap_count 0`,
  small `records`) is asserted so the footgun stays visible; last-catalog flush covered.
- **Fingerprint**: matching stems/sizes → no warning; altered/missing/extra stem →
  warning, exit 0.
- **`jq`/`shuf` workflow**: doc-level example only (no lintle code owns it); assert
  `extract` still accepts an id list unchanged.
```
