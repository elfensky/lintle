# Output chunking — split every record/line stream into fixed-count chunks — design

**Date:** 2026-07-21 · **Status:** approved (revised 2026-07-21 after adversarial review —
added the `dedup` suspects reader, the resume checkpoint-set schema, the concat writer
lifecycle, the chunk-index overflow guard, the regex stem parse, and the explicit
sequential-execution and intermediate-shard notes)

`lintle`'s pipeline emits several multi-gigabyte single files — the worst is
`dedup/import.txt` at **28.7 GB** (214.7 M records in one file), with
`dedup/notes.jsonl` (~3.6 GB) and the largest `cleaned/<stem>.cleaned.txt` (~3 GB)
close behind. These are unwieldy to move, import, retry, and inspect. This spec
splits every record/line *stream* the pipeline writes into fixed-count chunks so no
single output file is ever huge, without breaking determinism, constant memory, or
the existing per-input-file parallelism.

## Decision summary

- **Chunk by record count, not byte size.** TLE records are fixed-width (2 lines ×
  ~70 chars ≈ 140 B), so a record count ≈ a predictable file size (1 M records ≈
  140 MB) *and* counting records respects the 2-line record boundary for free — a
  record is never split across a chunk. Byte-size chunking would have to buffer to a
  record boundary and roll over near the threshold anyway, i.e. count records under
  the hood.
- **Default 1,000,000 records per chunk** (~140 MB). Tunable via `--chunk-records`.
- **Applies to every record/line *stream*** the pipeline writes; aggregate summary
  documents are not streams and stay single files (see scope table).
- **Approach A — a shared `ChunkedWriter`/`ChunkedReader` primitive threaded through
  every writer/reader.** Rejected: (B) chunk only `import.txt` — not "all steps",
  and leaves the ~3 GB cleaned files whole; (C) a post-hoc `lintle split` command —
  first materialises the 28.7 GB file, defeating the purpose.

## Invariants preserved (why this design is safe)

`lintle`'s only parallelism is `clean`'s process pool: **one worker per input file**,
each worker writing only files namespaced by its own stem
(`cleaned/<stem>.cleaned.txt`, its `.shards/<stem>.findings.jsonl`, etc.). Two workers
never touch the same file. Chunking rides on that partition and **adds no shared
state**, which is the whole safety argument. Six invariants make it concrete:

1. **Per-stream counting, never global.** Each chunk set is counted by its single
   owning writer — per-worker for `clean`, single-process for `dedup`/`verify`. There
   is no global "first 1 M records across all workers" counter (that would make
   boundaries depend on worker finish order — nondeterministic). Boundaries are
   deterministic because each input file is processed by exactly one worker and each
   stream is counted locally.
2. **Always-index, no renames.** A writer emits `<stem>.00001<suffix>` from the first
   byte and rolls forward to `.00002`, …; it never writes a plain name and renames it
   after the fact. This removes the one crash/race window a "rename-on-roll" scheme
   would introduce, and gives readers a single uniform contract (always glob a set).
3. **Concat-identity.** `b"".join(chunk bytes in index order)` is byte-identical to
   the pre-chunking single file. Split points are deterministic (every N units) and
   each chunk is independently byte-deterministic — so this preserves the
   byte-deterministic-output invariant (Critical Rules #1/#2) for `cleaned/*`,
   `report.jsonl`, `import.txt`, `suspects.jsonl`, and the `.broken.txt` sidecar.
4. **Atomic commit per chunk.** Each chunk is written to a temp and
   `fsutil.durable_replace`d the instant it fills — the same atomic-durable path the
   pipeline already uses (`pipeline.py`, `report_writers.py`, `report.py`). A crash
   leaves a set of complete, committed chunks plus at most one discarded temp — never
   a torn file.
5. **Stale-chunk scrub on (re)run/resume.** Before (re)writing a stem's stream, delete
   its existing chunk set, so a shorter re-run (fewer records than a prior attempt)
   never orphans high-index chunks into the set. This is the one genuinely new
   correctness obligation chunking adds. Deterministic input → deterministic chunk
   set, so the rewrite is byte-identical anyway; the scrub only removes a longer prior
   run's tail.
6. **Constant memory (Rule #3).** The writer holds one open chunk; the reader streams
   one chunk at a time. Per-stream state is O(1) (a running count + one file handle).

The advisory-flock out-dir lock and the atomic-durable-commit invariant are unchanged
— chunking reuses `durable_replace` per chunk rather than per whole-file.

**Steps run sequentially, never concurrently.** `clean`, `verify`, `dedup`, and `diff` are
separate CLI invocations: `verify`/`dedup` run *after* `clean` has finished, and the
advisory-flock out-dir lock forbids two `clean` runs sharing an out-dir. There is therefore
**no reader-globbing-while-a-writer-rolls window** and no scrub-outside-lock race — a reader
never observes a half-written chunk set, because writing and reading of a given stream never
overlap in time. (This is stated explicitly because it is load-bearing: it is the reason the
per-chunk atomic commit is sufficient and no cross-process snapshot guard on the reader glob
is needed.)

## The primitive

### `ChunkedWriter`

A streaming writer over *logical units*, in a new stdlib-only leaf
`src/lintle/chunking.py` (no `sgp4`, no I/O beyond `fsutil`):

- Constructed with `(directory, stem, suffix, units_per_chunk=CHUNK_RECORDS_DEFAULT)`.
  A "unit" is one `write()` call: for TLE output one unit = one 2-line record
  (`write_record(line1, line2)` emits two `\n`-terminated lines and counts 1); for
  JSONL one unit = one line (`write_line(s)` counts 1). Counting *calls*, not bytes,
  is what makes the record boundary structural.
- Rolls when the unit count reaches `units_per_chunk`: closes and durably commits the
  current chunk, opens the next. `units_per_chunk == 0` (or `None`) means never roll
  (a single `.00001` chunk — the "I want one file" escape hatch, still uniformly
  named).
- **Naming: `{stem}.{index:05d}{suffix}`**, 1-based, zero-padded 5 digits — the index
  is inserted right after the stem:
  - `tle2004.00001.cleaned.txt`, `tle2004.00002.cleaned.txt`, …
  - `import.00001.txt`, `notes.00001.jsonl`, `suspects.00001.jsonl`,
    `tle2004.00001.broken.txt`, `report.00001.jsonl`
  - Lexical order == numeric order. 5 digits → up to 99 999 chunks/stream (~100 B
    records at the default) — the 232 M-record corpus needs ~232 chunks for `import`.
  - **Overflow is a hard error, not a silent wrap.** Rolling past index 99 999 raises
    (a `ValueError`) rather than writing `.100000` (which would break lexical==numeric
    order and the reader's 5-digit parse). The current corpus cannot reach it, but a
    correctness tool must never silently corrupt its own naming; the fix is a one-line
    guard, and the ceiling lifts by widening the pad if a future corpus needs it.
- **Always writes at least chunk `00001`**, even for an empty stream (an empty
  `.00001` file), so a stream is always a non-empty set on disk. An exact multiple of
  N produces no trailing empty chunk (the roll happens *before* writing unit N+1).
- Each chunk goes to a temp path and is `durable_replace`d on close (invariant 4).
  `close()` commits the final in-progress chunk.

### `ChunkedReader`

Given `(directory, stem, suffix)`, globs `{stem}.*{suffix}`, parses and sorts by the
5-digit index, and yields records/lines across the whole set as **one logical
stream**. Stems are dot-free (the `stem()` helper strips the input `.txt`), so the
name parse `^(?P<stem>.+)\.(?P<idx>\d{5})(?P<suffix>…)$` is unambiguous. Every reader
that currently opens a single `<stem><suffix>` file swaps to this.

## Per-step application

Every record/line stream swaps its single-file writer for `ChunkedWriter`; every
reader that opened those files swaps to `ChunkedReader`. **Aggregate summary
documents are not streams and stay single files:** `report.md`, `report.json`,
`verify/summary.{json,md}`, `dedup/summary.json`, `broken-noradids.ndjson`.

| Step | Chunked writers (site) | Reader changes |
|---|---|---|
| `clean` | `cleaned/<stem>.NNNNN.cleaned.txt` (the `cleaned_handle` write in `pipeline.py:359`); `broken/<stem>.NNNNN.broken.txt` (`report_writers.write_broken_file`); the `report.jsonl` concat → `report.NNNNN.jsonl` (`report_writers.concat_findings_shards`, single-process) | — (producer) |
| `dedup` | `dedup/import.NNNNN.txt`, `dedup/notes.NNNNN.jsonl` (single writer each) | reads `cleaned/` as chunk sets; **also reads `verify/suspects.NNNNN.jsonl` as a chunk set** — `dedup._load_hard_positions` currently opens the single `verify/suspects.jsonl` by name with `is_file()`, so once `verify` chunks it that read must swap to `ChunkedReader`. If left unswapped it silently returns an empty exclusion set (missing file → no error), importing duplicates that should be excluded — a Rule #1 regression. |
| `verify` | `verify/suspects.NNNNN.jsonl` (the `SuspectSink.write` k-way merge in `report.py`) | reads `cleaned/` as chunk sets; **source-alignment** drives the cleaned chunk set (one logical stream) against the single, un-chunked `original/<stem>.txt` |
| `diff` | — | reads each run's `report.NNNNN.jsonl` set |

The reader work concentrates in `verify/records.py`: `cleaned_stems()` derives distinct
stems from `{stem}.NNNNN.cleaned.txt` names (glob `*.cleaned.txt`, **parse each name with
the `ChunkedReader` regex `^(?P<stem>.+)\.(?P<idx>\d{5})\.cleaned\.txt$` and take the
`stem` group** — not a fixed-length tail slice, which would corrupt the stem on any stray
legacy or partial file that doesn't match; non-matching names are skipped), dedupe;
`iter_file()` streams a stem's ordered chunk set. `dedup` and `verify` both reuse it;
`diff` gets the same treatment for `report.jsonl`.

The per-worker findings **shards** in `.shards/<stem>.findings.jsonl` stay whole
intermediate files (see *intermediate shards* below) — only the final concatenated
`report.jsonl` is chunked, at concat time in the single main process (so no cross-worker
coordination). **This concat is a rewrite, not a drop-in writer swap.**
`report_writers.concat_findings_shards` currently does a byte-block
`shutil.copyfileobj(..., 65536)` copy that is oblivious to line boundaries; record-count
chunking forces line iteration, and the 1 M-record chunk boundary does **not** align with
the per-stem shard boundaries — so the single `ChunkedWriter` must persist **across the
whole `for stats in all_stats` shard loop**, not reset per shard (one chunk may span the
tail of one stem's shard and the head of the next). The writer is opened once before the
loop and `close()`d once after it.

**Intermediate shards stay whole — deliberately.** The chunking goal ("no single output
file is ever huge") applies to the *streams the pipeline commits as outputs*, not to the
`.shards/<stem>.findings.jsonl` scratch files, which are consumed and discarded at concat
time. A multi-GB input can therefore still produce a multi-GB shard mid-run; that is
accepted because a shard is a short-lived per-worker intermediate, never a delivered
artifact. (If a huge shard ever becomes a problem in its own right, chunk it separately —
out of scope here.)

**Clean cutover.** Readers understand only the new always-indexed layout, so outputs
from ≤ 0.9.0 must be regenerated. This is a non-issue: everything under `output/` is
derived from `source/` and is reproducible by re-running the step. No dual-format
reader, no migration tool (YAGNI).

## Config

- `--chunk-records N` on `clean`, `dedup`, and `verify`; **default 1,000,000**.
  `N == 0` → never roll (single `.00001` chunk). Threaded to the `ChunkedWriter`
  constructions in each step.
- `CHUNK_RECORDS_DEFAULT = 1_000_000` module constant in `chunking.py`,
  `ponytail:`-commented as tunable.
- Not stored in `.lintle.json` for now (YAGNI — a flag with a sensible default is
  enough; add a config key only if repeated use bites).

## Resume interaction (invariant 5 in detail)

`clean --resume` reclassifies each input file as done / redo via the checkpoint
(`resume.py`, `run_planning.py`). With chunking, a *done* stem is recorded once its
whole chunk set is committed and the checkpoint entry is written; a *redo* stem
**scrubs its existing chunk set first** (delete `cleaned/<stem>.*.cleaned.txt`,
`broken/<stem>.*.broken.txt`, `.shards/<stem>.findings.jsonl`), then re-writes from
scratch. Because the input → chunk-set mapping is deterministic, the only thing the
scrub removes is a longer prior attempt's trailing chunks; the rewritten prefix is
byte-identical. The output-scrub that `run_planning` already performs for a fresh
(non-resume) run extends to globbing the chunk sets.

**Checkpoint schema change (a chunk set, not one name+size).** `resume.py` today records one
`basename → st_size` per artifact kind (`cleaned`, `broken`, `shard`) in `output_sizes`, and
`verify_completed_outputs`/`_locate_output` re-validate that single name+size on resume. A
chunk *set* is N files whose count is not known until the input is fully read, so the
single-name schema cannot describe it. The `CompletedEntry` therefore records the set as a
**count + total size** (sum of chunk sizes) per artifact kind — enough to detect a truncated
or tampered prior run without a full per-chunk manifest — and validation re-globs the set,
counts it, and sums the sizes. The unchunked `.shards/<stem>.findings.jsonl` and the
single-file summaries keep the existing single-name check.

**Resume must pin the chunk size.** Chunk boundaries depend on `--chunk-records`; a resume that
changed it would mix chunk sizes within one logical run (completed stems at the old size,
redone/remaining stems at the new size), breaking the concat-identity a fresh run would produce.
So the effective `--chunk-records` is stored in the run stamp and a resume with a different value
is **refused** (the user re-runs fresh), not silently honoured.

## Edge cases

- **Empty stream** → one empty `.00001` chunk (a stream is always a set on disk).
- **Exact multiple of N** → no trailing empty chunk.
- **`N == 0`** → a single `.00001` chunk regardless of size (uniform naming, old
  single-file behaviour otherwise).
- **A stem with 0 cleaned but some broken records** → `cleaned/<stem>.00001.cleaned.txt`
  is an empty chunk; the broken set carries the content.
- **Non-matching files in the directory** (e.g. `summary.json`) are ignored by the
  reader's `{stem}.*{suffix}` glob + index parse.

## Testing

- **Unit (`chunking.py`):** roll boundary (N=3, feed 7 records → chunk sizes 3/3/1);
  exact multiple (feed 6 → 3/3, no empty third); empty (0 records → one empty
  `.00001`); `N=0` → single chunk; naming/zero-pad/lexical-ordering; atomic commit
  (a temp exists mid-write, the committed name appears only on roll/close); reader
  reassembles chunks in index order, handles single- and multi-chunk sets, and
  ignores non-matching files.
- **Concat-identity golden:** for each stream type, `b"".join(chunks) ==
  old_single_file_bytes` on an input crossing a chunk boundary — the property that
  locks Critical Rules #1/#2.
- **Integration:** `clean` over multiple input files → each stem's set is independent
  and byte-identical across two runs (deterministic under the process pool);
  `verify`/`dedup` read the chunk sets and produce unchanged findings vs a single-file
  baseline; **resume scrub** — re-run a stem that now yields fewer records and assert
  no orphaned high-index chunk survives.
- The existing `clean`/`verify`/`dedup` end-to-end tests migrate to the new
  always-indexed layout (updated expected filenames), which also guards the cutover.
- **`dedup` reads chunked suspects:** run `verify` so it emits `suspects.NNNNN.jsonl`, then
  `dedup`, and assert the hard-suspect exclusions are actually applied — i.e. `dedup`'s
  exclusion set is non-empty (the regression guard for finding A; a single-name reader would
  silently return empty).
- **Resume rejects a changed `--chunk-records`:** a `clean --resume` whose flag differs from the
  stamped value errors rather than mixing chunk sizes; and a completed chunk-set entry validates
  by count + total size (not a single name).
- **Concat-identity for mixed empty/non-empty streams:** a stem with 0 cleaned but some broken
  records → `cleaned/<stem>.00001.cleaned.txt` is an empty chunk and `b"".join(cleaned chunks)`
  still equals the old single-file bytes; likewise an exact-multiple-of-N boundary produces no
  trailing empty chunk.
- **Chunk-index overflow raises:** with a tiny `--chunk-records` that would need > 99 999 chunks,
  the writer raises rather than emitting `.100000`.

## Out of scope

- Byte-size chunking (record count ≈ size for fixed-width TLEs — the size knob buys
  nothing here).
- Chunking aggregate summary documents (`report.md`/`report.json`/`summary.*`/
  `broken-noradids.ndjson`) — they are not record streams.
- A `.lintle.json` chunk-size key, a dual-format reader, and an output migration tool
  — all YAGNI.

## Docs to update on landing

- `CHANGELOG.md` `[Unreleased]` — the new chunked output layout + `--chunk-records`
  (a **breaking** output-layout change; call it out).
- `ARCHITECTURE.md` — a `chunking.py` leaf in the module table + a note on the chunked
  output layout and the six invariants.
- `README.md` — the output-tree layout and the `--chunk-records` flag.
- `CLAUDE.md` — the byte-deterministic-output invariant list, to note outputs are now
  chunk sets whose concatenation is the byte-deterministic artifact.
