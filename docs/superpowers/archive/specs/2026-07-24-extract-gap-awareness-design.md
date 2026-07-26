# `lintle extract` gap awareness — design

**Date:** 2026-07-24
**Status:** approved
**Extends:** `2026-07-23-extract-satellite-history-design.md`

## Problem

`extract` reads only the dedup import set, which holds validated-perfect records —
broken records were quarantined at `clean` time and never reach it. That means
extract can never emit a wrong record, but it can silently emit an *incomplete*
history: a satellite whose records were partially quarantined exports with holes,
and the user gets no signal beyond the sidecar's single `largest_gap_days` number.

Quarantined records carry no reliable epoch (`broken-noradids.ndjson` is a bare
`{"noradId": N}` presence list; `report.jsonl` findings have `norad_id` but no
epoch — often the epoch field itself is what broke). So "where are the gaps"
can only be answered from the temporal structure of the deduped history itself,
with the quarantine flag as corroborating context.

## Behaviour

### Gap detection (pure arithmetic)

Walking a satellite's span read-only (pass 1), collect every inter-epoch delta.
Median spacing via `statistics.median` (stdlib). A gap is **reportable** when its
delta exceeds **10× the median spacing**. Fewer than 3 records → no analysis
(zero or one delta; one delta cannot exceed 10× itself).

Memory: the delta list for one satellite is bounded (~tens of thousands of
floats, hundreds of KB worst case). Critical Rule #3 targets whole multi-GB
files streaming through the pipeline; a single satellite's delta list is fine
and this design says so explicitly.

### Quarantine flag

Read `<out-dir>/data/report/broken-noradids.ndjson` **once per run** into a set
of NORAD IDs. Per catalog, `had_quarantined_records` is `true`/`false`, or
`null` when the file is absent (extract may run against a tree whose clean
report was pruned — unknown is not false).

### Warn + confirm flow (per catalog)

Trigger: ≥1 reportable gap **or** `had_quarantined_records is true`. Then, to
stderr via `term`:

```
warning: history for 25544 has 3 gaps (median spacing 0.25 d):
  2019-03-02 → 2019-04-11  (40.1 d)
  2021-07-19 → 2021-08-02  (14.3 d)
  ...and 1 more
warning: records for 25544 were quarantined during clean — gaps may stem
  from that; see report/report.jsonl
continue export of 25544? [Y/n]
```

- Terminal list capped at the **10 largest** gaps, shown chronologically, with
  an "…and N more" line when capped.
- Interactive (`term.is_interactive()`): `prompt_yes_no(..., default=True)`;
  `None` (EOF) → default → proceed.
- **Non-TTY: warn + proceed** — scripts and CI keep working; the warning is in
  the log.
- Decline ("n") → skip this catalog, `term.note(...)`, **not an error** — it
  does not force exit 2. Per-catalog prompting: each gappy satellite gets its
  own report and y/n; the run continues either way.
- No new CLI flags. `--yes` / `--strict` are deferred until someone needs them
  (YAGNI); the non-TTY default already covers unattended runs.

### Sidecar additions

`schema_version` bumps `"1"` → `"2"`. New fields (still sorted-keys, 2-space
indent, trailing LF, pure arithmetic — byte-deterministic):

```json
"median_spacing_days": 0.25,
"gap_count": 3,
"gaps": [{"start": "...Z", "end": "...Z", "days": 40.1}],
"had_quarantined_records": true
```

- `gaps` holds the 10 largest reportable gaps, listed chronologically;
  `gap_count` is the total number of reportable gaps (may exceed `len(gaps)`).
- `median_spacing_days` is `null` when analysis was skipped (<3 records);
  `gap_count` is then `0` and `gaps` is `[]`.
- `had_quarantined_records` is `true`/`false`/`null` as above.

## Structure

`_extract_one`'s interleaved copy-and-compute loop untangles into:

- `_analyze(spans) -> HistoryStats` — frozen slotted dataclass: record count,
  first/last epoch, first/last element set, median spacing, reportable gaps,
  largest gap. Pure, no writing, independently testable.
- `_copy_spans(spans, tmp)` — dumb verbatim byte copy, no stats.
- `_extract_one`: `find_spans` → `_analyze` → warn/confirm (may skip) →
  `_copy_spans` → commit txt → sidecar built from the `HistoryStats`. The
  existing atomic all-or-nothing cleanup (txt + sidecar as one unit, failed
  runs leave pre-existing outputs untouched) is unchanged.

Two passes over the span instead of one: pass 1 analyzes (seek+read, no
writes), pass 2 copies. A satellite's whole history is a few MB at most
(~40k records × 140 B ≈ 5.6 MB), so the second read is irrelevant, and the
prompt genuinely happens before any export work.

The import-graph wall is untouched: no new imports beyond stdlib
(`statistics`) and existing in-package modules; `sgp4` stays out.

## Errors and exit codes

Unchanged: absent catalog or raised extraction → exit 2; torn/missing dedup
set → upfront `ExtractError` (exit 2, nothing written). New third outcome:
user-declined skip — reported, nothing written for that catalog, exit stays 0
(unless other catalogs independently fail).

## Testing

- **Gap detection** on synthetic epoch sequences: uniform cadence → no gaps;
  one 40-day hole in a daily cadence → exactly one gap with correct
  start/end/days; <3 records → analysis skipped; cap: 11+ gaps → 10 in
  `gaps`, all in `gap_count`.
- **Prompt flow** via monkeypatched `term.is_interactive` / `prompt_yes_no`:
  decline → catalog skipped, exit 0; accept → export proceeds; non-TTY →
  warn + proceed, no prompt.
- **Quarantine flag**: ndjson present with/without the catalog → `true`/`false`;
  file absent → `null`.
- **Sidecar bytes**: golden test extended for the v2 fields.
- **Atomicity**: existing tests keep passing unmodified in behaviour.
