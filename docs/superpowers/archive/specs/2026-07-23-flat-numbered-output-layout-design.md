# Flat numbered output layout + per-dir READMEs + extract default dest

**Date:** 2026-07-23 · **Status:** approved design, pre-implementation

## Purpose

Make the out-dir self-explanatory at a glance: one flat level of directories
numbered in pipeline order, each carrying its own README. Retire the `data/`
grouping (0.10.1) — less clicking, easier to eyeball across steps. **Breaking
output-layout change** (like 0.10.0/0.10.1 before it); ships with `extract`
in 0.11.0. Outputs from ≤ 0.10.3 must be regenerated (everything under an
out-dir is reproducible; no migration tool — the established policy).

## The layout

```
<out-dir>/
├── README.md          # overview: the six dirs, order of operations, regen note
├── 01-cleaned/        # <stem>.NNNNN.cleaned.txt chunk sets      + README.md
├── 02-broken/         # <stem>.NNNNN.broken.txt sidecars          + README.md
├── 03-report/         # report.md · report.json · report.NNNNN.jsonl ·
│                      # broken-noradids.ndjson                    + README.md
├── 04-verify/         # suspects.NNNNN.jsonl · summary.{json,md}  + README.md
├── 05-dedup/          # import.NNNNN.txt · notes.NNNNN.jsonl ·
│                      # summary.json                              + README.md
└── 06-extract/        # <id>.txt · <id>.json                      + README.md
```

Transient run state stays hidden at the root, unchanged: `.shards/`,
`.clean-state.json`, `.clean.lock`, `.lintle-output` marker.

## Decisions (user-pinned)

- **Numbered plain names** (`01-cleaned`…), not `data-*` prefixes and not the
  old bare names — sorted display order == order of operations.
- **All machine artifacts stay**: `report.jsonl` (consumed by `lintle diff`),
  `report.json` (consumed by `lintle report`; byte-twin of `--report json`),
  `broken-noradids.ndjson` (the only complete quarantined-ID list; report.md
  caps its table and points here).
- **`extract` default dest** becomes `<out-dir>/06-extract/`; an explicit
  `--dest` still wins (and is never config-defaulted). The extract-vs-clean
  collision concern that motivated cwd is gone — extract owns a numbered dir
  like every other step.

## Naming constants — one authority, as today

All in `lintle/__init__.py` (the established single source):

```python
CLEANED_DIRNAME = "01-cleaned"
BROKEN_DIRNAME = "02-broken"
REPORT_DIRNAME = "03-report"
VERIFY_DIRNAME = "04-verify"   # moves here from verify/report.py
DEDUP_DIRNAME = "05-dedup"     # moves here from dedup.py
EXTRACT_DIRNAME = "06-extract" # new
DATA_DIRNAME — deleted (grep confirms consumers: cli, dedup, diff,
output_artifacts, pipeline, report_writers, resume, run_planning, summary,
verify/{__init__,records}; every path expression drops the `data/` segment).
```

`verify/report.py` and `dedup.py` re-export or import from `lintle` so
existing importers (`dedup` imports `VERIFY_DIRNAME` from `verify.report`)
keep one definition. Suffixes/stems are unchanged; chunk-set naming is
untouched — this is a directory-level move only, so every chunk reader/writer
works as-is once the dir constants change.

## Per-dir READMEs

- Each step writes the README(s) for the dir(s) it owns, at the same point it
  writes that dir's artifacts: `clean` → root README + 01/02/03; `verify` →
  04; `dedup` → 05; `extract` → 06.
- Static deterministic ASCII/UTF-8 text (no timestamps, no counts — counts
  live in the reports), committed via `fsutil.durable_write_text`. Content:
  what the dir contains, the file formats (one line each), what to do with it
  (e.g. 01: "concatenate a stem's chunks in index order for the original
  single-file form"; 03: "report.md is the human summary; report.jsonl feeds
  `lintle diff`"), and which command regenerates it.
- The existing `output_artifacts.write_layout_readme` (root README) is
  rewritten for the new layout; per-dir README writers live beside it and in
  the respective step modules (verify/dedup/extract write their own at
  finalize time). A README is data *about* the dir, not a chunk — readers'
  anchored chunk parses already ignore it; the checkpoint's `output_sizes`
  must skip `README.md` basenames (they are not per-file outputs).

## Scrub & resume

- `run_planning.scrub_outputs` scrubs the new six dirs **plus both legacy
  layouts**: the 0.10.1–0.10.3 `data/` tree and the ≤ 0.10.0 root
  `cleaned//broken/` + root report artifacts (existing logic, kept).
- `resume`: `_OUTPUT_DIRS`/`output_sizes` move to the new constants;
  checkpoint `schema_version` bumps 3 → 4 (recorded chunk basenames resolve
  against new dirs; an old checkpoint must classify STALE, which the
  version bump guarantees).
- The out-dir ownership marker and lock are unchanged.

## Consumers to update (complete list)

`pipeline._clean_output_paths` · `report_writers` (shard concat dest) ·
`output_artifacts` (report paths + READMEs) · `summary.run` (report.json
path) · `diff` (report.jsonl chunks path) · `resume` (`_OUTPUT_DIRS`,
`output_sizes`, schema bump) · `run_planning` (scrub) · `verify`
(`records.cleaned_stems`/`iter_file` read 01-cleaned; `report.SuspectSink`
writes 04-verify; README) · `dedup` (reads 01-cleaned + 04-verify suspects;
writes 05-dedup; README) · `extract` (reads 05-dedup; default dest
06-extract; README) · `cli` (extract `--dest` default None → resolved to
`<out-dir>/06-extract` when absent) · docs (ARCHITECTURE §output-tree,
README, CLAUDE.md corpus section) · CHANGELOG (breaking entry under
[Unreleased]).

## Testing

- Golden end-to-end: `cli.main(["clean", …])` then assert the exact root
  listing (six dirs appear as steps run; 01/02/03 + READMEs after clean).
- README presence + byte-determinism (two runs → identical README bytes).
- Resume: checkpoint from the new layout resumes; a `data/`-era checkpoint
  (schema 3) classifies STALE → fresh run; fresh run scrubs a planted legacy
  `data/` tree and root-era files.
- verify/dedup/extract read the new dirs end-to-end (existing e2e tests
  updated); extract's default-dest test replaces the cwd-default test.
- `output_sizes` skips README.md; `lintle report` and `lintle diff` read the
  new paths.

## Non-goals

- No migration of existing out-dirs (regenerate — established 0.10.0 policy).
- No renaming of stems/suffixes/chunk format; no README for hidden dirs.
- No config knob for the layout. One layout.
