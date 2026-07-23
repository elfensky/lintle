# `lintle extract` — per-satellite TLE history extraction

**Date:** 2026-07-23 · **Status:** approved design, pre-implementation

## Purpose

Answer "give me this satellite's complete history" from an existing pipeline
output tree: `lintle extract 25544` writes `25544.txt` (the satellite's full
deduped TLE history, epoch-ascending) plus a `25544.json` stats sidecar. A
read-only consumer of a prior `dedup` run, exactly as `dedup` is a read-only
consumer of `clean`.

## CLI

```
lintle extract <noradID> [<noradID>…] [--out-dir DIR] [--dest DIR]
```

- `<noradID>` — one or more catalog numbers (integers 1–99999; the corpus's
  TLE inputs are 5-char catalog fields). No subject word (`extract sat …`) —
  a second extraction axis doesn't exist yet, and a future one (epoch ranges,
  international designators) is shape-distinguishable from a bare integer.
- `--out-dir DIR` — the pipeline output tree to read (default: stored
  `.lintle.json` config, else `data/output`, matching `verify`/`dedup`).
- `--dest DIR` — where per-satellite files are written (default: cwd).
- Name chosen by adversarial multi-model debate (sonnet/opencode/fable,
  2026-07-23): `extract` was the pairwise winner; `fetch`/`get` were
  unanimously vetoed for falsely implying a network download; `sat` vetoed as
  an opaque noun; `history` runner-up (noun-in-verb-table tax).

## Mechanics — the sorted fixed-width stream *is* the index

`dedup/import.NNNNN.txt` chunks hold only validated-perfect records — two
69-char lines + `\n` each, so **every record is exactly 140 bytes** — globally
sorted by `(catalog, epoch_key)`, so each satellite's records are one
contiguous run. Extraction is therefore pure binary search, no index artifact:

1. Enumerate the chunk set via `ChunkedReader.chunk_paths()` (the one naming
   authority). Error if empty.
2. **Guard the invariant:** for each chunk consulted, assert
   `size % 140 == 0`; on violation exit 2 with a "corrupted or foreign import
   chunk" operational error (never emit possibly-torn records — correctness
   over recovery).
3. Locate the catalog: read each chunk's first/last record (two 140-byte reads
   per chunk) to pick the chunk(s) whose catalog range covers the ID, then
   bisect within a chunk on record boundaries, widening to the full contiguous
   run. A run may straddle consecutive chunks.
4. Stream the byte range to `<dest>/<id>.txt` verbatim — the file is a slice
   of the import stream, so it is byte-deterministic by construction and every
   record re-validates against `tle.py`.
5. Compute sidecar stats in the same pass (the records are already in hand).

Catalog parsing: line-1 cols 3–7, space-padded ints handled the way
`verify/records.catalog_of` already does — reuse it, never a second parse.

Constant memory: at most one 140-byte record buffered during search; the
output copy streams in fixed-size blocks.

## The `<id>.json` sidecar (always written)

Deterministic JSON (sorted keys not required — fixed construction order, LF,
ASCII), `schema_version: "1"`:

| field | meaning |
|---|---|
| `schema_version` | `"1"` |
| `norad_id` | the catalog number |
| `records` | record count in `<id>.txt` |
| `first_epoch` / `last_epoch` | ISO-8601 UTC of first/last record epoch |
| `span_days` | `last - first`, fractional days |
| `mean_records_per_day` | `records / span_days` (`null` if span is 0) |
| `largest_gap_days` | biggest inter-record epoch gap |
| `largest_gap_at` | ISO epoch of the record *after* that gap |
| `element_set_first` / `element_set_last` | elset numbers at history ends |
| `source` | `{out_dir, dedup_records_written, dedup_schema_version}` — provenance from `dedup/summary.json` (which is deliberately timestamp-free, so provenance is the run's deterministic identity, not a date) |

Epoch parsing reuses `verify/epoch.parse_epoch` (year pivot, one definition);
it returns `(year, day_of_year)`, and the ISO-8601 UTC strings are derived as
`datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=day_of_year - 1)` — pure
arithmetic, no wall clock, so sidecar bytes stay deterministic.
`<id>.txt` stays pure 2-line TLE — parseable by sgp4/skyfield/anything; all
metadata lives in the sidecar. Both files commit via `fsutil` durable writes
(`<id>.txt` streamed through a `.partial` + `durable_replace`; sidecar via
`durable_write_text`).

## Errors & exit codes

- Missing dedup tree → exit 2, "run `lintle dedup` first" hint.
- Unknown ID → per-ID error line, no files for it, other IDs still extracted;
  exit 2 if any ID was missed, else 0. (Exit 1 stays reserved for findings-
  style verdicts, per the existing exit-code discipline.)
- `% 140` guard trip → exit 2 operational error.
- Runs under the out-dir advisory lock (`fsutil.out_dir_lock`) like
  `verify`/`dedup`, so a concurrent `clean` scrub can't tear the read.

## Placement & dependency wall

New leaf `src/lintle/extract.py`: `cli → extract → {chunking, fsutil, term,
verify.records (catalog_of), verify.epoch (parse_epoch)}`. Imported lazily by
`cli` in its dispatch arm (same pattern as `verify`/`dedup`) so the clean
path's import closure is untouched; never imports `sgp4` or touches the clean
path — the existing import-graph test gains `extract` in its read-only-
consumer set. Reusing `verify`'s parsers keeps one definition of catalog and
epoch (Critical Rule #4 discipline applied to parsing).

## Testing

- Golden extraction from a synthetic multi-chunk dedup tree (small
  `--chunk-records` so runs straddle chunk boundaries).
- Boundary cases: first catalog in the set, last catalog, run exactly at a
  chunk seam, single-record satellite.
- Missing ID among present IDs → partial success + exit 2.
- `% 140` guard: corrupt a chunk by one byte → exit 2, no output files.
- Sidecar: golden JSON bytes for a fixture history (epoch math pinned).
- CLI: `cli.main(["extract", …])` end-to-end through a real tmp tree.

## Non-goals (deliberate)

- **No index artifact** — the sorted fixed-width stream makes one redundant;
  revisit only if the 140-byte invariant ever breaks (it is guarded, not
  assumed).
- **No full-corpus explode** into ~50k per-satellite files.
- **No chunking of `<id>.txt`** — it is a query result (≤ ~130k records), not
  a pipeline record stream; a deliberate, documented exception to the
  chunk-set rule.
- **No satellite-name resolution** — the corpus is 2-line; no names exist.
- **No network anything.**
