# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **`verify --orbit` survives records sgp4 cannot parse.** sgp4's parser is stricter than
  `tle.py` in two digit-or-space fields (line-1 element-set number, line-2 revolution number
  — both bare `int()` for sgp4), so a lintle-perfect record with a blank field crashed the
  whole pass with an anonymous `invalid literal for int()` and exit 2. The `ValueError` is now
  caught per record and emitted as a hard `VRFY-ORBIT-ERROR` suspect naming the file, index,
  and catalog. Init-error details now cite sgp4's own `SGP4_ERRORS` message alongside the
  code, and a test pins the hard/soft code tables to the installed library's registry.
- **A gapped chunk set is refused, not silently read around.** `ChunkedReader` never checked
  that chunk indices run `1..N`, so a deleted interior chunk read as a shorter-but-whole
  stream: `extract` exported confidently truncated histories claiming the full span with
  `gap_count: 0`, `iter_file` renumbered record indices (desyncing the `(src_file, index)`
  addresses `suspects.jsonl` uses, so `dedup` excluded the wrong records), and `diff`
  undercounted findings. Read consumers now assert contiguity (`complete_chunk_paths`,
  `ChunkSetError` naming the first missing index); scrub paths stay gap-tolerant.
- **`dedup` skips unusable records as findings instead of crashing or poisoning.** A
  legitimate checksum-valid Alpha-5 id (accepted by the validator) was written as the
  `catalog=-1` first record of the import set — failing every later `lintle extract` with a
  bogus "corrupted set" error — and an unparseable epoch or a non-ASCII byte crashed the run
  outright. Such groups now produce one `DEDUP-UNUSABLE-RECORD` note in `notes.*.jsonl`, an
  `unusable_records` tally in `summary.json`, and a verdict mention — telemetry, never an
  exit-code change.
- **`extract` preflights its preconditions and bounds its reads.** `05-dedup/summary.json`
  was re-read unguarded once per written catalog *after* the txt commit — a pruned or corrupt
  tree crashed each extraction post-commit, and the pair rollback then deleted a prior run's
  still-good outputs. It is now read once per run, tolerantly (absent/corrupt → null `source`
  fields). A preflight probe of each chunk's boundary catalogs turns one unusable record —
  which sorts first and used to fail *every* per-catalog lookup — into a single up-front
  error naming the chunk and record. Both span-copy loops now raise if a chunk shrinks
  mid-read instead of spinning forever on a zero-length read.
- **Short terminals no longer lose the results table.** At a height at or below the table
  chrome (an 8-line tmux split), the windowing guard inverted and handed rich an uncroppable
  full-length live region — cropped from the top, active row never visible — while leaving
  `windowed` false, which also suppressed the complete-table reprint on exit. The window now
  clamps to one row and the full table always prints.
- **Ctrl-C teardown uses the public pool API.** The interrupt branch reached into the private
  `ProcessPoolExecutor._processes` (which becomes `None` after shutdown — the fallback
  guarded a rename, not the documented value) and then called `shutdown` separately. Python
  3.14's public `terminate_workers()` replaces both calls with the closed/exited races
  guarded; the hand-rolled helper and its fallback tests are deleted.
- **Resume archives never clobber; planning never aborts on a stat race.** Two checkpoint
  archives within the same second silently overwrote each other via `os.replace` — destroying
  exactly the "recoverable interrupted run" the archive exists to preserve; archiving now
  uses `os.link` with a `-N` suffix on collision. And an output file vanishing between
  `exists()` and `stat()` aborted resume planning where the contract says "reprocess that
  input" — the race now takes the reprocess branch.
- **The live table's zero-denominator dash is ASCII-safe.** `cli_progress._percent`
  hard-coded an em dash; ASCII-only consoles now get the `-` fallback through the same
  `summary.can_encode` decision every results table makes (the #97 rule).

- **One epoch definition** (#199): a record's moment in time was defined three times
  (`verify/epoch.py`'s key, `history.py`'s datetime, an inline histogram copy) and the
  definitions disagreed at year boundaries — `tle.py` accepts day-of-year in `(0, 367)` with
  no leap-year logic, so a clean `19/366.5` keyed as 2019 while its true instant is
  2020-01-01. All epoch parsing now routes through the new stdlib-only `lintle/epoch.py`,
  which normalizes rollover epochs on the decimal string (equal instants ⇒ bit-equal keys;
  in-range keys unchanged). Consequences fixed: dedup now collapses same-instant re-issues
  spelled across a year boundary; `manifest.jsonl` / extract-sidecar spans are never negative
  (`first_epoch <= last_epoch`); verify's `epoch_distribution` bins rollovers into the January
  they belong to; negative deltas no longer suppress real gaps. `verify/epoch.py` is deleted.
- **n=3 gap dead zone**: with exactly 2 deltas, the gap threshold (10× the interpolated
  median) was algebraically unreachable — a 3-record history with a 274-year hole reported
  `gap_count: 0`. The threshold now uses `statistics.median_low`; the *reported*
  `median_spacing_days` is unchanged.

### Added

- **Alpha-5 catalog ids are decoded, not skipped** (#203). The SATCAT passed 99,999 in 2026
  and new ids use the Alpha-5 wire form — a letter in column 3 encoding the leading 10–33,
  with `I` and `O` omitted so it can never be read as `1` or `0` (`E8493` = 148493, up to
  `Z9999` = 339999). `tle.decode_catalog` now reads both spellings and is the one reader of
  the cols 3–7 field; `verify.records.catalog_of` routes through it, so such records get a
  real catalog instead of the `-1` sentinel and `dedup` imports them like any other satellite
  rather than skipping them as `DEDUP-UNUSABLE-RECORD` (that arm remains, for genuinely
  corrupt lines). `lintle extract` accepts either spelling — `extract E8493` and
  `extract 148493` name the same satellite — and every artifact keeps speaking the decoded
  integer (`manifest.jsonl`'s `norad_id`, the `<id>.json` sidecar, the `<id>.txt` filename);
  exported TLE bytes stay verbatim. The decode table is pinned to space-track's own
  documented vectors, and `sgp4.io`'s `from_alpha5` stays out of the clean path.
  `tle.extract_norad_id` is deliberately unchanged — it feeds `broken-noradids.ndjson`'s
  5-digit contract. The 2004–2025 corpus contains zero Alpha-5 records, so this is
  future-proofing with no effect on current output.

### Changed

- `dedup` and `verify` artifact `schema_version` bumped `"1"` → `"2"`: row shapes are
  near-identical, but epoch keys are sort keys and group identities, and their meaning
  changes at year boundaries — v1 and v2 artifacts from the same `01-cleaned/` are not
  byte-comparable (`import.*` order, `notes.*` keys, `suspects.*` order, histogram buckets,
  orbit `pairs_measured`). `01-cleaned/*` and the resume checkpoint are unaffected.
- `dedup`'s `summary.json` gains `gap_silent_satellites` — satellites with fewer than
  `history.MIN_GAP_RECORDS` (3) records, for which gap analysis is definitionally silent
  (one delta has no typical spacing); the per-row signal remains `records` +
  `median_spacing_days: null`.

## [0.13.6] - 2026-07-29

### Changed

- **The summary panel glosses every code it prints.** `Fixes applied` and
  `Quarantined by rule` gained a *what it means* column — `TLE-COL-001` now
  reads alongside "line length after normalization is not 69 columns" —
  sourced from the same registries `lintle explain` and `lintle diff` already
  use, so there is still one definition of what a rule means. Unknown or
  retired IDs render unglossed rather than failing. `report.md` already
  carried this legend; the terminal panel did not.

## [0.13.5] - 2026-07-29

### Fixed

- **`clean`'s live table has a heartbeat now, like every other command.**
  `verify` and `dedup` carried a turning glyph in the bottom-left of their
  summary row; `clean` — the command that runs longest — had none, so beyond
  the bars there was no sign it was alive. Over the same run the summary row
  goes from 2 spinner frames in total (both from the `status()` spinners at the
  run's edges, not from the table) to a steady frame every 0.106 s.

  The heartbeat is now one shared function both tables call, so they cannot
  drift apart again: same glyph, same rate, and the same rule about being
  absent from the finished frame and from piped output.

## [0.13.4] - 2026-07-28

### Fixed

- **`verify` and `dedup`'s heartbeat turns on the clock, not on the work.** The
  spinner took its frame from the redraw count, and redraws only happened when
  a unit reported — so it moved in bursts: a flurry while records streamed,
  then nothing at all through the stages that report rarely. On a corpus run
  that meant minutes of a frozen glyph through `verify`'s external sort, the
  contradiction pass, and the orbit sweep, which reads as a hang — the one
  thing a heartbeat exists to rule out.

  Measured over an 8-second `verify`: the worst gap between redraws drops from
  1.105 s to 0.155 s, and the rate goes from a bursty 3.6/s to a steady 11.8/s.
  On the corpus the old gap is minutes, not a second.

- `clean`'s display has no spinner — its motion is the filling bars — but it
  had the same shape of bug: the queue-drain loop returned early when no
  messages were waiting, so the summary row's wall clock froze during a stall.
  It now redraws every cycle, which is exactly when a moving clock is worth
  having.

## [0.13.3] - 2026-07-28

### Fixed

- **A resumed `clean` run shows the files it carried over.** The live table
  was built from the files left to process, so resuming a 29-file corpus with
  two already done rendered `2/29 files` above a table with 27 rows in it — the
  count and the rows disagreed, and the two finished files appeared nowhere.

  The missing rows were the visible half. Because the display only knew about
  the remaining files, the summary row's corpus size was the *remaining* bytes
  rather than the corpus, and its records, clean, and quarantined counters all
  started from zero — so the earlier run's work was absent from every total on
  screen, not just from the roster.

  Carried-over files now get their rows filled in from the checkpoint before
  the first frame, showing the records, clean, quarantined, and time that run
  actually measured, and their numbers join the run totals. Those rows render
  dimmed, matching the results table, which has always carried resumed files
  that way: complete, but measured by the earlier run rather than this one.

## [0.13.2] - 2026-07-28

### Fixed

- **The last silent stretches now show a spinner.** 0.13.1 gave `extract` and
  `diff` in-flight feedback but missed four places that still did real I/O with
  nothing on screen — three of them *before* a live table opens, which is the
  worst place for it, because the command looks hung before it has said
  anything at all.

  - `clean` clears the whole prior output tree at startup — tens of GB of
    unlinking on a corpus re-run — and did it before the roster printed.
  - `dedup` streams and parses a prior `verify` run's entire suspects chunk set
    before its table opens.
  - `extract` parses all of `broken-noradids.ndjson` before any per-catalog
    work, and its per-catalog chunk bisect sat outside the analysis spinner.
    The bisect now shares that spinner rather than getting its own: the locate
    and the read are one silent stretch to anyone watching, and two consecutive
    spinners would only flicker.

  `report`, `explain`, and the wizard stay bare deliberately — their work is
  instantaneous, and a spinner that flashes for 50 ms reads worse than none.
  `verify` needed nothing: its preflight is stat-only and every stage after the
  per-stem loop already relabels the pinned summary row.

- Spinner labels all end in an ellipsis now; `diff`'s was the one still using
  three ASCII dots.

## [0.13.1] - 2026-07-28

### Added

- **`extract` and `diff` show they are working.** Both ran silent — `extract`
  through a binary search and a verbatim byte copy per satellite, `diff` through
  two full streaming passes over each run's `report.jsonl` chunk set — and a
  long one was indistinguishable from a hang. Each now shows a spinner on
  stderr while it works: `extract` around the history analysis and around the
  copy/commit, `diff` around the per-file aggregation. `extract`'s spinner
  deliberately stops short of the gap-confirmation prompt, so the gap detail
  you need in order to answer still prints plainly and the prompt is never
  hidden behind a live region.

### Changed

- **One width-breakpoint policy instead of two.** The per-file tables broke at
  80 and 100 columns while the aggregate summary broke at 72 and 100, so a
  single terminal could show a narrow file table above a medium summary block.
  Both now share the one number line. **Terminals 72–79 columns wide now render
  the aggregate summary in its plain tier**, where they previously got the
  medium one.

- **Text columns read as text.** `extract`'s `status` and `diff`'s per-file
  `rule` and `change` columns were right-justified as if they held numbers,
  because the shared results-table chrome assumed everything past the name
  column was numeric. It now takes a per-column override, and those three are
  left-justified. Every other table is byte-identical to before.

### Fixed

- Removed `phase_bar`, an unused progress primitive with no call sites —
  `verify` and `dedup` grew live tables instead and never came back to it. The
  module docs that still described it, and `term`'s claim that stdout carried
  only the `report` view (`diff` has been a second consumer for a while), have
  been corrected to match the code.

## [0.13.0] - 2026-07-26

### Added

- **`clean` renders one live table, from discovery to results.** Every
  discovered file has a row before any work starts, so the first frame is the
  roster (index, file, size); work then updates rows in place — the byte bar
  fills, then the same row switches to its final records / clean / quarantined
  / time — and the finished table stays on screen as the results view with the
  aggregate panel under it. A run no longer prints new blocks as it goes. When
  the rows outnumber the terminal height the table windows around the active
  files with an `… N more` marker (a `rich` live region cannot scroll), and the
  complete static results table is printed afterwards so nothing the window hid
  is lost. Per-file completion lines print only off a TTY, where there is no
  table to update; failures print their error everywhere.

- **`verify` and `dedup` render the same live table**, over cleaned stems: size,
  progress, records, and each command's own result columns (hard/soft suspects
  for `verify`, records read and hard-suspect exclusions for `dedup`). Both were
  previously silent for the whole run — many minutes on a corpus-scale tree —
  and spoke only at the end. Every suspect is attributed to the stem it came
  from, including the contradiction and orbit findings raised after the
  streaming pass, so the columns sum to the verdict line. The stages after the
  per-stem loop relabel the pinned summary row rather than printing spinners,
  and the verdict prints under the finished table.

- **Per-file and per-id results tables elsewhere.** `clean` and `lintle report`
  render a per-file results table from the run envelope (records, clean,
  quarantined, repairs, time; resumed files dimmed, failed files dashed, and a
  total row whose time is the run's wall clock, never the column's sum).
  `extract` rosters the requested ids and closes with a per-id table of records,
  span, gaps and outcome, rendered from the sidecars it just wrote. `diff`
  renders its deltas as tables on a TTY while keeping its byte-exact plain text
  for pipes. All of them share one table chrome and one set of width tiers
  (narrow < 80 ≤ medium < 100 ≤ wide); columns drop whole, values are never
  truncated.

### Changed

- **`verify` and `dedup` rows show a real progress bar, and the summary row a
  heartbeat.** Their progress column held a percentage *string* where `clean`
  had a bar, so a row that sat on the same number for a minute read as frozen.
  Both now use the same `ProgressBar` renderable, and the summary row carries a
  spinner advanced by *work* — one step per redraw, and redraws only happen
  when records move — so a cycling glyph means progress and a frozen one means
  a genuine stall. Absent from the final frame and from piped output.

### Fixed

- **`UnitTable(drop=...)` crashed when omitted.** The parameter defaulted to an
  empty tuple but is used as a per-tier mapping; every current caller passes
  one, so it never fired in practice.
- **`verify`'s contradiction pass showed no progress.** The external merge over
  every cleaned record is the run's long tail — the better part of an hour on a
  200M-record corpus — and the summary row sat on a static label throughout,
  indistinguishable from a hang. It now counts the sorted stream against the
  known record total, so the label carries an exact fraction rather than a
  guess.
- **`verify --orbit` nested two live regions.** The orbit pass opened its own
  progress bar inside the results table's `rich.live.Live`, which cannot nest.
  It now reports through that table's summary row like every other post-stream
  stage.
- **Ctrl-C during `verify`, `dedup` or `extract` printed a traceback.** Only
  `clean` handled `SIGINT`, inside its worker pool; the single-process consumers
  had nothing catching it. `cli.main` now wraps the whole dispatch in a
  `KeyboardInterrupt` backstop — one `cancelled.` line, exit `130`, the out-dir
  lock released, and no partially-committed subtree, since those are written
  atomically at the end of a run.

## [0.12.0] - 2026-07-25

### Added

- **`05-dedup/manifest.jsonl`** — a new per-satellite corpus manifest: one
  compact JSON row per catalog (catalog-ascending), carrying record count,
  epoch span, median spacing, largest gap, and gap count. A single plain
  file, never chunked (unlike every other per-record output stream). The
  `jq | shuf | xargs lintle extract` substrate for random-satellite coverage
  queries — `lintle` itself owns no RNG.
- **`lintle verify`'s `epoch_distribution`** — a new top-level
  `{"YYYY-MM": count}` record-density histogram over valid records in
  `04-verify/summary.json` (a sibling of `checked`, not nested inside it),
  plus a matching `### Epoch distribution` section in `summary.md`.
  Informational only — never affects `exit_code` or suspects. Named "epoch
  distribution" / "record density," deliberately not "coverage."
- **`cleaned_fingerprint`** — a cheap stat-only structural fingerprint of
  `01-cleaned/` (`{"stems": [[stem, bytes], ...]}`, no file content read),
  stored by `dedup` in `05-dedup/summary.json`. `extract` recomputes it at
  run start and warns — never fails — when `01-cleaned/` has drifted since
  the `dedup` run it's reading from.
- **`lintle.history`** — the gap/median-spacing reduction shared by `extract`
  (its `<id>.json` sidecar) and `dedup` (the new manifest) is now one pure,
  I/O-free module (`HistoryStats`/`Gap`, `analyze_epochs`), lifted out of
  `extract._analyze` so the two callers can never compute divergent numbers
  for the same satellite.

## [0.11.1] - 2026-07-24

### Fixed

- **Fresh runs scrub the unnumbered root `verify/` and `dedup/` dirs every
  0.10.x wrote.** The 0.11.0 flat-layout scrub removed the `data/` tree and the
  ≤ 0.10.0 root layout but let these two legacy dirs survive beside
  `04-verify/`/`05-dedup/`, leaving stale results next to fresh ones (flagged
  in PR #180's final review; confirmed during the 0.11.0 corpus regeneration).
  Regression test: `test_scrub_removes_legacy_root_verify_and_dedup`.

## [0.11.0] - 2026-07-24

### Added

- **`lintle extract <noradID>…`** — one satellite's complete deduped TLE
  history as pure `<id>.txt` (epoch-ascending 2-line records) plus a
  deterministic `<id>.json` stats sidecar (record count, epoch range, largest
  gap, elset range, provenance). Binary search over the sorted fixed-width
  `dedup/import` chunks — no index artifact, works on any existing dedup
  output. `--dest` picks the destination (default: `<out-dir>/06-extract/`);
  missing ids exit 2.
- **`lintle extract` now warns when a satellite's history has reportable gaps
  (> 10× its median epoch spacing) or records quarantined during `clean`,
  shows the gaps, and — on a TTY — asks y/n before exporting (non-TTY runs
  warn and proceed; declining skips that satellite without an error). The
  `<id>.json` sidecar is schema v2: `median_spacing_days`, `gap_count`,
  `gaps`, `had_quarantined_records`.
- **Every out-dir step ships its own `README.md`.** `clean` writes the root
  overview plus `01-cleaned/`, `02-broken/`, `03-report/` READMEs; `verify`,
  `dedup`, and `extract` write their own into `04-verify/`, `05-dedup/`, and
  `06-extract/` respectively. Static, deterministic text — no counts or
  timestamps — describing each dir's files and the command that regenerates
  them.

### Changed

- **BREAKING (output layout): flat, pipeline-ordered out-dir.** The 0.10.1
  `data/` grouping is retired: `01-cleaned/`, `02-broken/`, `03-report/`,
  `04-verify/`, `05-dedup/`, `06-extract/` now sit directly under the
  out-dir, numbered in order of operations. Every step dir ships its own
  `README.md` describing its files and the command that regenerates it.
  `lintle extract` now defaults `--dest` to `<out-dir>/06-extract/`.
  Outputs from ≤ 0.10.3 must be regenerated by re-running the steps; a fresh
  run scrubs both legacy layouts.

## [0.10.3] - 2026-07-23

### Changed

- **`verify`'s source aligner is now a null object with the skip policy behind
  its seam.** `SourceAligner.open(source_dir, stem)` always returns an aligner —
  inert when the stem has no source file — and `feed(rec, revalidated=...)`
  no-ops for revalidate-failed records without consuming source lines. The
  caller-side `continue`/`is not None` conventions that produced the 0.10.2
  aligner bug class are gone; behavior is byte-identical (A/B-verified against
  a 2-file, 7.7M-record corpus subset through clean → verify → dedup).
- The #158 same-epoch clash rule has one implementation: `find_conflicts` and
  `has_epoch_clash` both route through a shared `_is_clash` predicate, ending
  the hand-synced twin implementations (#164).
- One `.partial` temp-suffix authority (`fsutil.PARTIAL_SUFFIX`), and the
  legacy root-level `report.*.jsonl` scrub now enumerates chunks through
  `ChunkedReader`'s anchored parse instead of a loose glob, so non-chunk
  bystander files survive a fresh-run scrub.
- Desloppify code-health pass (51 review findings): typed signatures across the
  writer/pipeline surface, `Counter` tallies, `WorkerRunResult` instead of a
  5-tuple, `match` dispatch in `cli.main` with lazy `verify`/`dedup` imports,
  and renames — `VrfyRule` → `VerifyRule`, `verify.run_verify` → `verify.run`,
  `orbit._TIERS` → public `TIERS`. CLI surface and all structured outputs are
  unchanged.

## [0.10.2] - 2026-07-22

### Fixed

- **`verify --source` no longer false-flags quarantined duplicates as interior
  mutations.** The aligner resyncs on an anchor of `(catalog, epoch-columns)`,
  which is not unique: the real corpus (tle2020) stores each satellite twice at
  one epoch — a `+`-signed 68-char *missing-checksum* copy clean drops, and the
  real space-signed 69-char copy it keeps — so the dropped copy shares the
  kept copy's anchor. When that quarantined shadow fell in the scan path it was
  reported as a hard `VRFY-INTERIOR-MUT`. The anchor branch now skips a
  same-anchor source pair that does not itself reduce to a valid, clean-able
  record (one clean would have quarantined) and keeps scanning for the real
  origin. A genuine interior mutation is unaffected — its origin is a valid
  record — so nothing is hidden. Regression test:
  `test_quarantined_duplicate_is_not_interior_mutation`.

- **`verify --source` no longer desyncs on long quarantine runs.** The
  `SourceAligner` used a fixed 4096-line resync window as a *search-distance*
  cap: when more than ~4096 dropped (quarantined) source lines separated two
  accepted records, it could not reach the later record's origin, so it
  advanced one line at a time and falsely flagged every subsequent cleaned
  record as `VRFY-ORIGIN-MISSING`. On the full corpus (e.g. `tle2020` cleaned
  without `--reconstruct-checksum`, which quarantines runs of 20k–28k
  consecutive 68-char missing-checksum records) this produced ~44M false
  suspects and ran ~31h, single-core-bound. The window is now only a
  memory bound on the read buffer; the forward scan slides it without limit —
  a cleaned record is always a sanctioned edit of a real source pair, so its
  origin exists ahead — crossing quarantine runs of any length in O(source
  lines) total. Interior-mutation and re-issue detection are unchanged.
  Regression test: `test_resyncs_across_long_quarantine_gap`.

## [0.10.1] - 2026-07-21

### Changed

- **BREAKING (output layout): one directory per pipeline step.** All of `lintle
  clean`'s output now lives under `<out-dir>/data/` — `data/cleaned/`,
  `data/broken/`, and `data/report/` (holding `report.md`, `report.json`, the
  chunked `report.NNNNN.jsonl` set, and `broken-noradids.ndjson`) — mirroring how
  `verify/` and `dedup/` already namespace their output. The out-dir root is now
  just `data/` · `verify/` · `dedup/` plus a self-describing `README.md`;
  transient run state (`.shards/`, `.clean-state.json`) stays at the root.
  `lintle report` and `lintle diff` read the new locations, and a fresh run scrubs
  a prior ≤ 0.10.0 root layout.

### Added

- Each `clean` run writes a static **`README.md`** at the out-dir root explaining
  the per-step layout, so the output is self-describing without external docs.

## [0.10.0] - 2026-07-21

### Changed

- **BREAKING (output layout): every record/line output stream is now split into
  fixed-count chunks.** Instead of one `cleaned/<stem>.cleaned.txt`,
  `broken/<stem>.broken.txt`, `dedup/import.txt`, `dedup/notes.jsonl`,
  `verify/suspects.jsonl`, or `report.jsonl`, `lintle` now writes an *always-indexed*
  chunk set — `<stem>.00001.cleaned.txt`, `<stem>.00002…`, `import.00001.txt`,
  `report.00001.jsonl`, etc. — of **`--chunk-records` records each (default
  1,000,000 ≈ 140 MB)**. Concatenating a set's chunks in index order is byte-identical
  to the old single file. This keeps no single output file huge (the worst,
  `dedup/import.txt`, was 28.7 GB) while preserving determinism, constant memory, and
  atomic-durable commits (each chunk commits the instant it fills). Aggregate summary
  documents (`report.md`, `report.json`, `verify/summary.*`, `dedup/summary.json`,
  `broken-noradids.ndjson`) are not streams and stay single files. Readers understand
  only the new layout, so **outputs from ≤ 0.9.0 must be regenerated by re-running the
  step** (everything under `output/` is reproducible from `source/`; no migration tool).
  The `.broken.txt` sidecar header dropped its `# N quarantined of M entries` line
  (those counts live in `report.json`/`report.md`) so the header can lead the first
  chunk without needing an end-of-stream count.

### Added

- **`--chunk-records N`** on `clean`, `dedup`, and `verify` sizes the output chunks
  (default 1,000,000; `0` = never roll, a single `.00001` chunk). A `clean --resume`
  with a changed `--chunk-records` restarts fresh (the value is part of the run
  identity, so a resume can't mix chunk sizes within one logical run).

## [0.9.0] - 2026-07-20

### Added

- `lintle verify --orbit` now **stratifies its satellite sample** toward dup-epoch
  catalogs (#163/#2) — those carrying more than one record at the same
  `(catalog, epoch)`, the cases most likely to hide an orbit inconsistency. The
  contradiction pass (`find_conflicts`) collects them (orbit-gated, so the default
  sgp4-free path pays nothing) and `sample_catalogs` keeps them all, spreading the
  rest of the budget evenly; when the priority stratum overflows the budget it is
  itself evenly spaced across the id range rather than truncated to the lowest ids.
  Deterministic (sorted sets, integer arithmetic, no RNG); an empty priority set
  reproduces the previous evenly-spaced sample byte-for-byte.

- `lintle verify --orbit --sensitivity {sensitive,strict}` (#163/#3): a two-tier
  dial on the orbit-outlier threshold. `sensitive` (the default) keeps today's
  behaviour (100 km floor, 10·MAD); `strict` raises both (200 km, 20·MAD) to surface
  fewer, higher-confidence outliers. The tiers are a fixed table (no RNG), scale only
  the global floor + MAD terms (the local-median multiplier and the min-epochs gate
  stay fixed), and the default is byte-identical to the previous release.

- `lintle verify --orbit` now applies **leave-one-out culprit isolation** (#163/#1):
  a lone corrupt interior record used to flag *two* pairs — the corrupt record and
  its innocent successor. When both a record's incoming and outgoing pairs are hot
  and re-propagating its neighbours *around* it (a leave-one-out probe) reconciles
  them, the finding is now attributed to the culprit alone with an `(isolated by
  leave-one-out …)` note. The probe is gated on twice the regime gap limit (it skips
  a record, so its gap is doubled) and judged against the global threshold only.
  Ambiguous cases — a genuine manoeuvre step, two adjacent corrupt records, a probe
  over the doubled gate, or an endpoint corruption — fall back to the previous
  per-pair attribution. Everything stays soft (`VRFY-ORBIT-OUTLIER`); isolation
  improves *attribution*, never certainty.

- `lintle verify --orbit` gap gate is now **regime-aware** (#163/#4): instead of a
  flat 3-day propagation-gap limit, GEO/geosync tracks (mean motion < 1.5 rev/day,
  re-issued less often) tolerate a 7-day gap while LEO/MEO/Molniya keep the tighter
  3-day gate. This recovers most GEO adjacent pairs a flat 3-day gate discarded. The
  regime is classified from the propagation source record's `sgp4` mean motion; a
  boundary object that flips class only swaps one *soft* gap gate for the other,
  never a verdict.

- `lintle verify --orbit` now applies a windowed **local-median** term to the
  outlier threshold (#163/#5): each pair's bar is `max(global, 20 · median of a
  time-local window)`, so a genuinely local spike must exceed 20× what is *locally*
  typical rather than being drowned in — or masked by — the whole-track median. The
  window spans the contiguous run of chain-adjacent, in-gate pairs around a pair (a
  skipped gap bounds it, keeping the median time-local) and is inactive below 10
  window points. The term only ever *raises* a bar (via `max`), so it removes false
  positives on uniformly-elevated segments (e.g. high-drag phases) and never adds a
  suspect; residuals and both threshold terms round to the 0.1 km quantum.

## [0.8.0] - 2026-07-15

### Added

- `lintle verify --orbit` — the opt-in sampled `sgp4` orbit-consistency pass
  (Increment 2, goal 2). For each sampled satellite's epoch-sorted track it
  propagates every cleaned TLE forward to its neighbour's epoch with `sgp4` and
  flags position-residual outliers over a robust per-satellite threshold
  (`max(100 km, median + 10·MAD)`). An outlier is **soft/inconclusive**
  (`VRFY-ORBIT-OUTLIER`) — a real manoeuvre is indistinguishable from a
  corruption over a single pair, so it never fails the run; only `sgp4` rejecting
  an element set as physically unphysical is hard (`VRFY-ORBIT-ERROR`, ~never on
  cleaned data). Residuals are rounded to a 0.1 km quantum before thresholding so
  the suspect set and exit code are byte-reproducible across platforms (locked by
  a golden fixture). Sampling is by satellite, deterministic, default
  `--sample 3000` / `--all`. Constant memory (streams through the same external
  sort as the contradiction pass, one track at a time). Promotes `sgp4` from a
  dev-only test oracle to a verify-scoped runtime dependency; the
  clean/validate/repair path stays walled off from it (import-graph test).

### Changed

- `verify` now streams its suspects to disk through an external merge-sort
  (`SuspectSink`) instead of accumulating them in a list, so peak memory is one
  chunk regardless of how many suspects are found — the last part of the verify
  path whose footprint scaled with the finding count (issue #156). A run's
  `suspects.jsonl` / `summary.{json,md}` bytes are unchanged: the serialization is
  shared with the list renderers and the k-way merge reproduces their exact
  stable sort order (locked by a byte-equivalence test).

### Fixed

- `dedup` no longer flags a *refined re-issue* (a new element-set carrying a
  different orbit at the same epoch) as a "genuine contradiction". It now shares
  `verify`'s #158 predicate — a contradiction is one element-set naming more than
  one orbit — so the two passes agree on what a same-epoch clash is (issue #164).
  Previously `dedup` used a broader group-level "more than one distinct orbital
  state" test: on the full corpus that reported 364,149 contradictions (exit 1)
  where `verify` found 5. Benign re-issues still collapse to the latest; only a
  true same-element-set clash is flagged and exits non-zero.

## [0.7.0] - 2026-07-11

### Added

- `lintle dedup` — emit a de-duplicated "latest re-issue only" import list from a
  clean run's cleaned output. Space-track republishes the same orbit at the same
  epoch with only a bumped element-set (or revolution) number; the faithful
  `cleaned/` archive keeps every copy, so `dedup` writes a separate
  `<out-dir>/dedup/import.txt` with one card per `(catalog, epoch)`, keeping the
  latest re-issue (highest element-set number). Benign re-issues — identical
  parsed orbital state — collapse silently; a genuine same-epoch orbit
  contradiction is kept-latest **and** flagged in `notes.jsonl` (exit 1), never
  resolved in silence. When a `verify` run's `suspects.jsonl` is present, hard
  suspects are excluded from the import list first. `cleaned/` is never modified;
  constant memory (records stream through the same external sort as `verify`,
  one `(catalog, epoch)` group held at a time); byte-deterministic output. Shares
  `verify`'s `orbital_state` / `element_set` so both agree on "same orbit" and
  "which is latest".
- `lintle verify` — post-run correctness auditing of a clean run's output
  (Increment 1, `sgp4`-free): re-validates every cleaned record against the one
  validator, flags any `(catalog, epoch, element-set)` contradiction — two
  records that share a satellite, epoch, *and* element-set number yet carry a
  different orbital state — and, when the source tree is available, confirms
  every cleaned line is a *sanctioned* edit of a real source line (no interior
  mutation) via a bounded-window source alignment. The contradiction check
  compares parsed orbital *values*, not raw bytes, so the many valid ASCII
  encodings space-track emits for one number never false-positive; benign
  same-epoch re-issues (a new element-set number) are counted in a summary
  census rather than flagged. Writes a deterministic suspects report under
  `<out-dir>/verify`; exit 1 on any hard suspect. Constant-memory: the
  group-by-satellite pass uses an external merge sort that spills to disk. The
  clean/validate/repair path is barred from importing `lintle.verify` (or
  `sgp4`) by an import-graph test.
- Interactive wizard: running `lintle` with no subcommand on a TTY opens a rich
  menu to configure paths and start a clean / verify / report. The chosen source
  and output directories are remembered in a project-local `./.lintle.json`
  (`lintle.config`, stdlib JSON), so `lintle clean` / `verify` / `report` run
  without repeating paths — precedence is always explicit CLI arg > stored
  config > built-in default, and stored paths are re-checked (and re-prompted)
  when they no longer exist. Off a TTY (scripts, CI, pipes) a bare `lintle`
  keeps the old behaviour: it prints help and exits 2, never blocking on a
  prompt.

## [0.6.0] - 2026-07-04

### Added

- Failed input files are now recorded in the run envelope (issue #83). When a worker
  raises, `run.failed_files` carries a `[{"file": basename, "error": str}]` list
  (sorted, always present — `[]` on a clean run) and `summary.failed_count` mirrors
  its length. `report.md` gains a `## Failures` table when any file failed (omitted on
  a clean run). Exit code 2 is unchanged for this case. Schema version bumped
  `"2"` → `"3"` because both new fields are required (not additive-optional).
- `clean --reconstruct-checksum` opts in to tier-2 missing-checksum reconstruction.

### Changed

- **`#120`/`#106` — the validator now returns a typed `tle.FieldError` instead of
  a bare error string.** `FieldError` subclasses `str` (so every consumer that
  treats an error as text — substring tests, `"; ".join(...)`, f-string interpolation
  — keeps working byte-for-byte) while carrying structured fields: `kind`
  (`"length"`/`"column"`/`"semantic"`/`"checksum"`/`"catalog"`), a 1-indexed
  inclusive `column_range`, and `observed`/`expected`. `repair` now routes on
  `FieldError.kind` rather than grepping the prose for `"checksum"` (the brittle
  contract #106 pinned as a tripwire), and populates `report.jsonl`'s
  `column_range`/`observed`/`expected` for **column, semantic, and catalog**
  findings — previously they were filled only for checksum mismatches. The
  `report.jsonl` line schema stays `"1"`: the field set and types are unchanged;
  only previously-`null` optional values are now filled in. Human-facing output
  (`report.md`, the `.broken.txt` sidecar, the `note` field) is byte-identical —
  pinned by the sgp4 oracle and the full existing suite.
- **Missing-checksum reconstruction is now opt-in (default off).** A checksumless 68-char
  line is quarantined by default rather than having a recomputed checksum appended: a dropped
  trailing *data* character is indistinguishable from a dropped checksum, so reconstructing it
  by default could silently emit wrong-but-valid data (Critical Rule #2, issue #82). Pass
  `--reconstruct-checksum` to restore the recompute. The flag is part of the resume
  run-identity, so changing it forces a re-run rather than folding mismatched outputs.

### Fixed

- **`#87` / `#99` — the out-dir lock had a TOCTOU reclaim race, a blind release, a
  post-reboot wedge, and a PID-reuse hostage.** The hand-rolled pidfile read the
  holder's PID, checked liveness with `os.kill(pid, 0)`, and `unlink`+retried to
  reclaim a dead lock — none of which re-verified the file it was deleting, so two
  runs could both reclaim and proceed (`#87`, P0, reproduced), and a run whose lock
  was raced away blind-unlinked the *current* holder's lock on exit. Identity also
  embedded Linux `boot_id`, so a crash-then-reboot left an unreclaimable "different
  host" lock (`#99`), and a recycled PID kept a dead lock alive. Replaced the whole
  scheme with an advisory **`fcntl.flock`** held for the run: the kernel releases it
  the instant the holder closes its fd, exits, is killed, or the host reboots, so
  liveness needs no PID check or boot-id and there is no reclaim step to race.
  Release is the bare `os.close` of *our own* fd — a run can only ever drop its own
  lock, never a successor's. The `.clean.lock` file is deliberately never unlinked
  (`flock` binds to the inode; unlinking would let a racing opener lock an orphaned
  inode), and now records `{host, pid, started}` only as informational text for the
  `LockHeldError` message, which names the file and the manual-removal escape hatch.
  POSIX-only; Windows is out of scope (use WSL). A shared out-dir across hosts over a
  network FS is documented as untested (relies on server-side `flock` propagation).
- **`#95` — a newline-free or CR-only multi-GB file was materialised as one giant `bytes`
  object, violating constant-memory (Critical Rule #3).** `iter_records` previously iterated
  over the binary handle with `for raw in handle`, which splits only on `\n`; a file with no
  `\n` (or only `\r` terminators) buffered the entire file as one `raw` chunk — a 3.2 GB file
  would load whole, OOM the worker, and then be pickled across the pool boundary. Fixed by
  replacing the iterator with `handle.readline(_MAX_LINE_BYTES)` (C-level, throughput
  unchanged for normal lines). A chunk of exactly `_MAX_LINE_BYTES = 4096` with no trailing
  `\n` is the start of an oversized line: the excerpt is kept as a bounded quarantine payload,
  the remainder is drained in fixed-size chunks (bytes still counted into `bytes_consumed`),
  and one `Orphan` with `RuleID.LINE_LENGTH` is emitted for the logical line. The raw bytes in
  the quarantine entry are noted as truncated — the one place byte-faithfulness yields to
  constant-memory, and only for a pathological input. Normal lines (the entire real corpus) are
  processed byte-identically. `stats.bytes_consumed` still reaches `st_size` at EOF, and
  `input_lines_seen` counts each logical line exactly once.
- **`#104` — `QuarantineSink.__enter__` was not exception-safe; `cleaned_handle` was opened
  outside the sink's `with` block.** `QuarantineSink.__enter__` entered its `BrokenFileWriter`
  and then its `JsonlFindingsWriter` sequentially: if the jsonl writer's `open` failed (disk
  full, unwritable `.shards`), the already-entered `BrokenFileWriter.__exit__` never ran,
  leaving a leaked body handle and `.broken.txt.body.partial` debris. In `pipeline._run`,
  `cleaned_handle = open(...)` happened before `with sink:` so a `sink.__enter__` failure
  leaked the cleaned `.partial`. Fixed with two changes: (1) `QuarantineSink.__enter__` now
  uses a `contextlib.ExitStack` — each sub-writer is entered onto the stack; on success
  `stack.pop_all()` transfers ownership to `self._stack` (closed by `__exit__`); a mid-enter
  failure unwinds already-entered writers via the stack's own cleanup. (2) In `pipeline._run`,
  `cleaned_handle = open(...)` is now opened inside the `with sink:` block (before the inner
  try/finally) so a `sink.__enter__` failure cannot leak it — the handle simply doesn't exist
  at that point.
- **`#101a` — broken sidecar excluded from resume integrity check when no records quarantined.**
  `resume.output_sizes` previously recorded the `.broken.txt` sidecar only when
  `stats.quarantined_count > 0`, but `pipeline` always writes a header-only sidecar even
  for a clean file. A file whose sidecar was deleted or truncated would not be detected
  on resume. The sidecar is now recorded unconditionally.
- **`#101b` — output naming convention duplicated across modules.** Suffix and dirname
  strings (`.cleaned.txt`, `.broken.txt`, `.findings.jsonl`, `cleaned`, `broken`, `.shards`)
  were re-encoded independently in `pipeline._clean_output_paths`, `resume.output_sizes`,
  `cli.discover_paths`, and `report_writers.concat_findings_shards`. They now live as
  module-level constants (`CLEANED_SUFFIX`, `BROKEN_SUFFIX`, `FINDINGS_SUFFIX`,
  `CLEANED_DIRNAME`, `BROKEN_DIRNAME`, `SHARDS_DIRNAME`) in `lintle/__init__.py` — the
  single source of truth — and all consumers import from there.
- **`#117` — `concat_findings_shards` silently skipped a missing shard, causing `report.jsonl`
  to underreport vs `report.json` on resume.** On a resumed run, completed files' stats come
  from the checkpoint (not reprocessed), so their findings shards are not regenerated. If a
  shard was deleted out-of-band, `report.jsonl` would omit those findings while `report.json`
  counted them — a silent disagreement. Fixed with two defenses: (1) the findings shard is now
  recorded in `resume.output_sizes`, so a missing or truncated shard on resume triggers
  reprocessing — regenerating the shard and keeping `report.jsonl` complete; (2)
  `concat_findings_shards` now returns the list of source filenames whose shard was missing but
  had quarantined records so the caller (`output_artifacts`) can surface a `warning:` on stderr.
- **`#105` — stale-checkpoint archives accumulated unboundedly.** `archive_checkpoint` now
  prunes older archives after creating a new one, keeping only the newest 3
  (`_STALE_ARCHIVE_KEEP`). The ISO-8601 timestamp suffix is lexicographically sortable so the
  oldest entries are reliably identified and removed.
- **`#94` — disk-space guard charged the wrong amount.** The 2× guard now runs at the
  right moment in each branch. For a `--resume` run it charges 2× the *remaining*
  (unprocessed) input bytes — so a nearly-complete resume on a tight disk is no longer
  wrongly refused. For a fresh run it runs *after* `scrub_outputs` so the freed prior
  outputs are already reflected in the available space before the guard fires.
- **`#93` — `scrub_outputs` had no ownership check.** A fresh run on a mistyped
  `--out-dir` pointing at a directory with user content (e.g. a `cleaned/` subdirectory)
  could silently destroy it. The preflight now refuses (exit 2, no data destroyed) when
  the out-dir is non-empty and carries no lintle-ownership signal (`.lintle-output`
  marker, checkpoint, or stale-checkpoint archive). A `.lintle-output` marker is written
  on every first fresh run so subsequent runs and scrubs recognise the directory.
- **`#102` — `scrub_outputs` left prior-run report artifacts.** An interrupted fresh run
  could leave a stale `report.json` (from the prior run) that `lintle report` would then
  render as current. `scrub_outputs` now also removes `report.md`, `report.json`,
  `report.jsonl`, and `broken-noradids.ndjson` so the out-dir is truly clean before a
  new run's workers write fresh outputs.
- Records whose lines carry leading whitespace now pair and repair via the `leading-trim`
  fix class instead of being quarantined as `BAD_PREFIX`. `iter_records` matches the
  `1 `/`2 ` prefix on a whitespace-trimmed view while carrying the raw bytes forward to the
  repairer (issue #88).

### Performance

- **`#109` — every accepted record was validated twice.** `repair_record` called
  `tle.validate_record(line1, line2)` after both lines had already passed
  `repair_line`'s full `validate_line`. The only new information for two
  individually-valid lines is the catalog-number cross-check. A new
  `tle.validate_record_catalog(l1, l2)` helper performs only that check,
  returning the byte-identical error string; `repair_record` now calls it instead.
  `validate_record` is unchanged. Property tests confirm equivalence for all
  matching and mismatched valid pairs.
- **`#110A` — `compute_checksum` hot-path: per-char membership tests replaced with
  a precomputed lookup table.** The original loop called `ch in _DIGIT` then
  `int(ch)` for every character. A module-level `_CHECKSUM_CONTRIB` dict (ASCII
  digits `'0'`–`'9'` → their integer value, `'-'` → 1, absent = 0) reduces the
  loop body to `sum(_CHECKSUM_CONTRIB.get(c, 0) for c in line[:68]) % 10` — one
  dict lookup per character. Byte-equivalent by construction; the existing
  checksum property tests confirm invariance.
- **`#123` — pipeline allocation micro-optimisations.**
  (a) `slots=True` added to `RecordCandidate`, `Orphan`, `_ProgressBatcher`
  (pipeline.py) and `Accepted`, `Quarantined` (repair.py) — eliminates
  per-instance `__dict__` allocation on every record; slotted dataclasses
  pickle correctly across the worker pool.
  (b) `_record_acceptance` now writes both cleaned lines in a single
  `handle.write(line1 + "\n" + line2 + "\n")` call — byte-identical output,
  half the Python-level write calls on the accepted-record hot path.
  (c) `_ProgressBatcher.enabled` was a `@property` re-evaluated every call;
  replaced with a `_enabled: bool` field computed once in `__post_init__`.

## [0.5.0] - 2026-06-08

### Added

- A clean run now persists its run envelope as `report.json` in the output directory —
  byte-identical to the `--report json` stdout output — alongside `report.md`,
  `report.jsonl`, and `broken-noradids.ndjson`.
- A new read-only `lintle report [out-dir]` subcommand re-renders a prior clean run's
  aggregate summary from its `report.json` (text → panel on stdout; `--report json` →
  the file's bytes verbatim). A missing or unreadable `report.json` exits `2`.
- New runtime dependency: **`humanize>=4,<5`** — human-readable durations and sizes in
  the human display (panel duration via `precisedelta`, roster sizes via
  `naturalsize(gnu=True)`). Pure-Python, zero transitive deps; confined to `summary.py`
  and `cli_progress.py` (stderr/stdout panel only — structured output is unaffected).
- New dev dependencies: **`hypothesis>=6,<7`** (property-based tests for the validator
  and repair logic) and **`pytest-xdist>=3,<4`** (parallel test execution — the default
  suite now runs with `-n auto`).

### Changed

- `clean` now renders a terminal-width-responsive **aggregate summary panel** to stderr at
  the end of every run (replacing the per-file stdout summary dump); text-mode stdout is now
  empty, and the per-file detail lives in `report.md` / `report.json`.
- The `clean` summary panel now shows elapsed time in human-readable form (e.g. "2 minutes
  and 4 seconds" instead of raw seconds) and the pre-run roster shows file sizes in
  `gnu`-unit notation (e.g. "3.0G") via `humanize`. Fixes a roster unit bug where the old
  hand-rolled `_format_size` used binary (1024-based) division but decimal labels — so
  3 GiB rendered as "3.0 GB" (binary value, wrong "GB" label) rather than the correct
  "3.0G"; `naturalsize(gnu=True)` is now used consistently. Display-format change only —
  structured outputs carry raw numbers as before.

### Removed

- The `lintle validate` subcommand (read-only audit mode) has been removed from the CLI.
  Use `lintle clean`; its `report.md`, `report.jsonl`, and `--report json` envelope cover
  all audit needs that `validate` previously addressed. The validator definition (`tle.py`)
  and the streaming pipeline are unchanged — this was a CLI-surface removal only.

## [0.4.1] - 2026-05-31

### Added

- The `clean` live progress block now shows, per in-flight file, a **byte
  throughput** (`rich.progress.TransferSpeedColumn`) and a **time-remaining ETA**
  (`TimeRemainingColumn`) — derived from the per-file byte total already supplied,
  so a multi-hour 30 GB run shows real per-file speed and ETA. The overall row
  gains a **files-done/total counter** (`MofNCompleteColumn`). These columns are
  gated by task kind (a small `_ForKind` wrapper) so the byte columns never render
  on the file-count overall row and the counter never renders raw bytes on a
  per-file row. TTY-only, additive UX — off a TTY the plain per-file summary lines
  are unchanged, and stdout / structured output are untouched.
- A **spinner** (`rich` status) now covers the otherwise-silent report
  finalization after the progress block exits — writing `report.md`,
  `broken-noradids.ndjson`, and concatenating the per-worker shards into
  `report.jsonl` (the slow part on a large corpus). TTY-only; a no-op context off
  a TTY, so piped/structured output is unaffected.

### Changed

- Upgraded the `rich` runtime dependency from the 13.x series to **15.x**
  (`rich>=15,<16`). No behavioural change — the stderr-only progress UI, roster,
  and `error:`/`warning:` rendering are unchanged (verified by the byte-exact
  `term` tests and the progress/roster suite); stdout and structured outputs
  never touched `rich`.
- **Dependency pinning policy:** every dependency (runtime and dev) is now pinned
  `>=current_major,<next_major` — minor/patch releases resolve automatically, but
  major upgrades are deliberate and manual, one at a time. Caps added to the dev
  group (`pytest<10`, `pytest-cov<8`, `ruff<0.16`, `sgp4<3`). See `ARCHITECTURE.md` §7.

### Fixed

- The `clean` cancel message no longer claims it will "continue where it stopped".
  Resume granularity is a whole file: re-running skips fully-completed files and
  restarts the file interrupted mid-stream, so a single-file run that is cancelled
  starts over from the beginning. The message now says so, and drops the dangling
  `--no-resume` hint when nothing had completed (there is no checkpoint to ignore).

### Documentation

- **README restructured for newcomers/evaluators** — it now leads with the pitch
  and the common commands, with the deeper design rationale moved to
  `ARCHITECTURE.md`. Reorganised for faster onboarding; no content lost.
- README "Cancelling and resuming" and ARCHITECTURE §5 now state the per-file
  resume granularity (completed files skipped, in-progress file restarted) upfront,
  rather than leaving it to be inferred.

## [0.4.0] - 2026-05-31

### Added

- `clean` gains a redesigned live progress UI (issue #53): a one-shot
  **size-only roster** of the files to be processed (printed instantly from
  `os.stat` — no pre-read of the corpus), a **multi-file per-worker progress
  block** showing each active file's byte progress and running record count,
  and exact per-file counts at completion. The `--jobs` default is now
  *CPU count − 1, capped at the file count* (reserving a core during the long
  run; an explicit `--jobs` is still honoured as-is). This adopts **`rich`**
  (`>=13,<14`) as the first runtime dependency, clearing the four-bar policy
  (authoritative spec §3.1): it replaces ~150 lines of hand-rolled ANSI in
  `cli.py`, is the de-facto standard live-display library (`pip`, `uv`, `pdm`),
  is pure-Python with a small transitive surface (`markdown-it-py`, `pygments`),
  and is confined to terminal rendering in `cli.py`.

- `clean` now prints a **borderline disk-space warning** when free space on
  the `--out-dir` volume sits between the 2× input-size abort floor and a
  2.5× ceiling. The abort path is unchanged — exit `2` below 2×, message
  unchanged — but a run that previously fell silent above the floor now
  surfaces a `warning:` line on stderr (`free space in <out-dir> is close to
  the 2× safety guard: N bytes free of ~M recommended; the run will proceed
  but may exhaust the disk`) when free is in the 2×-to-2.5× band, so users
  know they are cutting it close before commits start exhausting the disk.
  Internal: `cli._check_disk_space` now returns a `(severity, message)`
  tuple — `"error"` (caller aborts) or `"warn"` (caller prints and
  proceeds) — or `None` when free is comfortably above the warn ceiling.

- `--max-quarantined` (on both `validate` and `clean`) now accepts a trailing
  `%` to express the exit-code threshold as a **rate** rather than an absolute
  count. `--max-quarantined 1%` exits non-zero if more than 1% of routed
  records (`clean_count + quarantined_count`) were quarantined; the integer
  form (`--max-quarantined 100`) is unchanged and the default `0` still means
  "any quarantine fails". The two modes are mutually exclusive by construction
  — a single value is either a count or a rate, never both — which sidesteps
  the combination semantics that a separate `--max-quarantined-pct` flag would
  have forced. Comparison is strictly greater (`100*q > p*r`,
  cross-multiplied to avoid divide-by-zero on an empty corpus and float drift
  at the boundary); `0%` ≡ `0` and `100%` effectively never trips. Design at
  `docs/superpowers/archive/specs/2026-05-27-max-quarantined-percentage-design.md`.

- Host-aware out-dir lock: refuses to start a second concurrent `clean` against
  the same `--out-dir`.

### Changed

- **Terminology unified on "quarantine".** The codebase and outputs used "reject"
  and "quarantine" interchangeably; everything now says **quarantine** (the act of
  setting a bad record aside). The stdout summary label `rejects:` is now
  `quarantined:`, and `lintle explain` calls a rule a "quarantine rule". Internals
  renamed to match (`QuarantineSink`, `QuarantineEntry`, `Quarantined`, etc.).
  **Breaking change** to two machine-readable surfaces:
  - `--report json`: the per-rule map `reject_counts` is renamed `quarantine_counts`
    (in both `summary` and `files[]`), and `schema_version` bumps **`"1"` → `"2"`**.
    Consumers keying on `schema_version == "1"` or `reject_counts` must update.
  - The `clean --resume` checkpoint `SCHEMA_VERSION` bumps **`2` → `3`**; a checkpoint
    written by an older `lintle` is refused and the run restarts fresh (the existing
    refuse-on-change behaviour — no data loss, the prior outputs are archived).

  The `report.jsonl` findings stream and `lintle diff` are **unaffected** (they never
  carried `reject_counts`; their `schema_version` stays `"1"`). Stable `RuleID` wire
  tokens (`TLE-CHK-001`, …) are unchanged.
- All CLI stderr messages now route through `rich`: `error:` lines render
  bold-red and `warning:` lines yellow on a terminal, while status, prompt, and
  cancel notices share the one stderr `Console`. Output is unchanged off a TTY
  (pipes, CI, redirects) — no ANSI, no wrapping — so machine-readable stderr and
  stdout/result data stay plain. Internally a new `term.py` leaf owns the shared
  Console and the `error`/`warning`/`note`/`prompt` emitters, so the styled
  prefix lives in one place (used by both `cli.py` and `diff.py`).
- `clean` now resumes by default after an interruption: re-run the same command
  (same `--out-dir`, unchanged inputs) to continue where it stopped. Interactive
  terminals prompt; CI/non-TTY auto-resumes with a notice. `--no-resume` starts
  fresh (clearing prior outputs); `--resume` resumes without prompting.
- Cancelling (`Ctrl-C`, or `SIGTERM`/`SIGHUP` from a scheduler) prints how to
  continue or start over.
- **Breaking change.** Minimum Python is now **3.14** (was 3.11). `requires-python`,
  `tool.ruff.target-version`, `.python-version`, and the trove classifiers all
  bumped together; drops 3.11 / 3.12 / 3.13 support. Aligns lintle with the
  drunik-org Python stack standard (drunik / lintle / descent-engine all on
  Python 3.14, `line-length = 88`, `target-version = "py314"`, ruff rule set
  `["E","F","I","UP","B","SIM"]`, `pytest-cov` in the dev group).
- Every output file `clean` commits — the `cleaned/` files, `.broken.txt`
  sidecars, findings shards, `report.jsonl`/`report.md`/`broken-noradids.ndjson`,
  and the `--resume` checkpoint — is now committed **durably**, not just
  atomically: a new `lintle.fsutil.durable_replace` helper `fsync`s the file's
  data, `os.replace`s it into place, then `fsync`s the containing directory, so
  a committed file survives a hard power loss or kernel panic rather than only a
  clean Ctrl-C / sleep / crash. On macOS the true power-loss barrier is
  `F_FULLFSYNC` (plain `fsync` does not flush the drive's write cache); `fsutil`
  uses it there and plain `os.fsync` on Linux/other platforms. This closes the
  gap that mattered most for `clean --resume` (#56), which trusts a
  previously-committed output without reprocessing it: the worker now makes its
  outputs durable before the parent records the file `completed`, so the
  checkpoint can never name a file whose bytes are not yet on disk. Durability
  is always-on (no flag) — measured at roughly 1 second of overhead across a
  full ~120-commit run on the 30 GB corpus. Closes #58.
- **Breaking change.** `lintle validate` and `lintle clean` now accept exactly
  one positional input — a single file *or* a single directory — instead of
  zero-or-more. The default remains `data/source`. Scripts invoking
  `lintle clean dirA dirB` (or multiple explicit files) will now fail at
  argparse with a usage error. Run the tool once per input directory
  (`for d in dirA dirB; do lintle clean "$d"; done`), or stage the inputs
  into a single directory first (e.g. `mkdir merged && cp dirA/* dirB/*
  merged/ && lintle clean merged`). This trims speculative flexibility
  the documented workflow never exercised: the per-file output names are
  derived from each input's basename alone, so multi-input runs needed a
  defensive collision check whose existence was the only reason multi-input
  was risky in the first place. With single-input, basenames within one
  directory are unique by filesystem guarantee, so the failure mode and its
  guard disappear together.

### Removed

- `cli._detect_basename_collisions` and its `TestDetectBasenameCollisions`
  tests — no callers after the single-input `validate`/`clean` change above.
- The realpath dedup loop inside `cli.discover_paths` (a single input has
  nothing to dedup against). `discover_paths` and `check_paths` now take a
  single path string rather than a list.

## [0.3.0] - 2026-05-27

### Added

- New `clean --resume` flag for **single-run resume**: continue an interrupted
  `clean` (Ctrl-C, a closed laptop, a crash) so it processes only the files not
  yet completed, rather than restarting the whole corpus. Checkpointing is
  always-on — the parent fingerprints every input up front and atomically
  rewrites a `.clean-state.json` in `--out-dir` after each file commits,
  deleting it on full success, so the checkpoint's presence marks an interrupted
  run and a finished run leaves none behind. `--resume` validates refuse-on-
  change: any drift in the `lintle` version or an input's identity (size,
  `mtime_ns`, head/tail 64 KB hash) aborts with a specific message (exit `2`)
  rather than mixing outputs from two states. Completed files' findings shards
  survive the interruption, so a resumed run's `report.jsonl`, `report.md`, and
  `broken-noradids.ndjson` match a non-interrupted full run. This is *not* a
  cross-run cache (the rejected design §13, #12) — it is scoped to finishing one
  run and never skips re-validation of records it emits. New `lintle.resume`
  module; `report.stats_from_summary` reconstructs a `FileStats` from its JSON
  summary so reused files appear in the final report. Closes #56.
- New `lintle explain <TAG>` subcommand turns the validator into its own
  reference: it documents **both** public vocabularies lintle stamps on a
  report — the rejection rules (`RuleID`, e.g. `TLE-CHK-001`) and the repair
  tags (`FixClass`, e.g. `reconstructed-checksum`). For any tag it prints a
  plain-English definition (single-sourced from `RuleSpec`/`FixSpec`, never
  re-described), a good/bad or before/after example with the failing column
  marked, the repair-tier linkage, and a source-of-truth citation into the
  code. Read-only; an unknown tag exits `2` listing every valid tag. Every
  example is the *same object* the test suite validates against the live
  `tle.py`/`repair.py` across all classification layers (line, pairing,
  record), so the docs cannot silently drift from validator behaviour;
  import-time guards make explain-coverage and tag-namespace disjointness
  structural. A new `FixSpec`/`FIXES` registry gives each repair tag a
  canonical one-line definition, mirroring `RuleSpec`/`RULES`. The
  `reconstructed-checksum` entry carries an explicit safety note (the only
  sanctioned reconstruction: a deterministic recompute, re-validated in full
  before commit — never a guessed data character). Closes #11.
- New `lintle diff RUN-A RUN-B` subcommand compares two clean-run output
  directories by streaming each one's `report.jsonl` and printing the defect
  classes new in B, the classes fixed (present in A, absent in B), the
  per-rule count deltas, and a per-file (per-basename) breakdown — turning
  "eyeball two `report.md` files" into a focused delta of what the upstream
  export pipeline broke, fixed, or shifted between runs. Read-only; writes
  nothing. Counts the *primary* `rule_id` of each finding only — never the
  `related[]` array — mirroring `pipeline._record_reject` so the diff's
  per-rule totals agree with each run's own `report.md`. The corpus-level
  totals are derived by summing the per-file counts, so the two sections can
  never disagree. A mismatched (or missing) `schema_version`, a malformed
  line, non-UTF-8 bytes, or a missing `report.jsonl` is a hard error (exit
  `2`); a clean comparison exits `0`. The per-file breakdown is keyed by the
  `report.jsonl` `file` basename: because `clean` refuses inputs with
  colliding basenames (`_detect_basename_collisions`), each basename names
  exactly one file within a run, so the key is unambiguous. A basename present
  in only one run is *flagged* ("only in run A/B — fixed, removed, or
  renamed") rather than attributed, and never rendered as a misleading
  `N -> 0`, since `report.jsonl` lists only files that had findings. Memory is
  bounded by (distinct files × distinct rule IDs), not the number of findings.
  Decision recorded in `debates/010-lintle-diff-implementation/`. Closes #10.
- New `--max-quarantined N` flag on both `validate` and `clean` (issue #13).
  Exit code stays `0` when the total quarantined record count is at or below
  `N`; flips to `1` only when *more than* `N` records were quarantined. The
  default `N=0` preserves the historical "any quarantine fails" contract, so
  the flag is purely opt-in for CI/DataOps callers that need a tolerance
  budget. Unlike `lintle ... || true; jq -e '.summary.quarantined_count <= N'`,
  the flag preserves the meaningful `2` (operational error) and `130` (Ctrl-C)
  exit codes that a swallow-and-parse wrapper would mask. The two other
  thresholds floated in the original issue (`--threshold RATIO` and
  `--fail-on RULE-ID=N`) were intentionally NOT shipped: `--threshold` is
  redundant with `--max-quarantined` and adds denominator ambiguity, and
  `--fail-on` would promote `RuleID` strings from "report artifact" to
  "CI YAML public-forever contract" — a meaningfully bigger compatibility
  promise that should wait on real user demand. Decision recorded in
  `debates/013-fail-on-threshold-flags/`.
- `lintle validate --report json` (and `lintle clean --report json`) now
  emits a top-level versioned envelope object instead of the prior flat
  array of per-file summaries. The shape is
  `{schema_version, run, environment, summary, files}` —
  `run` carries the subcommand name, the ISO 8601 UTC start timestamp,
  and the parent-process wall-clock `elapsed_seconds`; `environment`
  carries `tool_version` and `python_version` (no env vars, paths, or
  hostnames); `summary` carries corpus-wide aggregates
  (`files_processed`, `paired_records`, `clean_count`,
  `quarantined_count`, `fix_counts`, `reject_counts`); `files` is the
  per-file array, where each entry is the existing `summary_dict()`
  shape extended with `elapsed_seconds`, `bytes`, and
  `records_per_sec`. The throughput field is always a stable float
  (denominator clamped to 1 ms) — never `null` — so statically-typed
  consumers can declare a single type without sentinel handling. Per-
  file timing is captured by each worker via `time.monotonic()`;
  `summary` aggregates are NOT summed worker durations (`--jobs N`
  parallelism would inflate that), so `run.elapsed_seconds` is the
  authoritative end-to-end duration. The contract is locked by
  `docs/superpowers/archive/specs/2026-05-25-report-json-envelope.md` and the
  golden fixture at `tests/fixtures/report-envelope-v1.golden.json` (the envelope was
  later bumped to schema `"2"` and the fixture renamed `-v2`; see the Unreleased section).
  Closes #20.
- New `lintle.diagnostics` module defines a stable, citable rule-ID registry
  (`TLE-COL-001`, `TLE-CHK-001`, `TLE-PAIR-001`, …) and a structured
  `Diagnostic` dataclass with `rule_id`, `source_line_nos`, `tier_attempted`,
  `column_range`, `observed`, `expected`, and `note` fields. Reject reasons
  are no longer free-form prose — they are now structured records keyed by a
  stable identifier that downstream consumers can pin in `report.md`, the
  `.broken.txt` sidecar, JSON output, and future tooling. Rule IDs follow
  the `TLE-<FAMILY>-<NNN>` shape (families: COL, CHK, PAIR, SEM, INT) and
  are never recycled — retired IDs stay readable forever. Includes a
  `RuleSpec` registry (`RULES`) with metadata about every rule, queryable
  for future `lintle explain TLE-XXX-NNN` tooling. Closes #8.
- The `report.md` run report now includes a "Rule reference" section,
  auto-generated from the `diagnostics.RULES` registry, listing every rule
  that fired in the run with its short title so the report is
  self-explanatory.
- `report.md` now ends with a `## Per-NORAD breakdown` table: one row per
  satellite catalog number whose records were quarantined, with the
  corpus-wide quarantine count, the per-rule defect breakdown, and the
  source filenames the satellite appeared in. Rows are sorted by
  quarantined-record count descending (NORAD ID ascending on ties); the
  Files column shows the first five filenames alphabetically followed by a
  `+N more` suffix when the satellite spans more files than that, keeping
  the cell bounded for persistent NORADs. The table caps at
  `format_run_report(all_stats, top_n=100)` rows by default with an
  italicised "...and N more — see broken-noradids.ndjson for the full list."
  footer when truncation activates; pass `top_n=None` to render every row.
  The richer per-NORAD data is the human-facing counterpart to
  `broken-noradids.ndjson`, whose `{"noradId":N}` contract stays minimal.
  Closes #40.
- Per-rule drop visibility everywhere `lintle` surfaces reject totals.
  `FileSample` gains a `dropped_count: dict[RuleID, int]` field, populated
  by `RejectSink.add` when the per-rule bucket is at cap (the bound that
  in-memory exemplars are capped at — full byte-faithful catalog reaches
  `.broken.txt` regardless). The new data threads through three surfaces:
  the `lintle validate` summary's per-rule heading switches from `(M):`
  to `(N of M hits, K dropped):` when `K > 0`; the JSON output
  (`lintle validate --report json`) gains a `dropped_counts` field
  parallel to `reject_counts`, keyed by stable rule IDs; and
  `report.md`'s "Records quarantined (by rule)" table gains a `Dropped`
  column summed across files. The trailing `...and X more` under each
  rule block in the validate summary stays, so the existing
  truncation-indicator stays visually anchored to the exemplars it
  applies to. Closes #46.
- `lintle clean` now emits a corpus-wide `report.jsonl` alongside
  `report.md` and `broken-noradids.ndjson`: one JSON object per
  quarantined record, citing the stable `RuleID` (`TLE-CHK-001`,
  `TLE-COL-003`, …) and carrying the structured fields downstream
  automation needs — `file`, `source_lines`, `tier_attempted`,
  `norad_id`, `column_range`, `observed`, `expected`, `note`, and
  `related` (secondary diagnostics). Every line carries
  `schema_version: "1"` and `outcome: "quarantined"` (the latter
  reserves space for future `"fixed"` emission without breaking
  consumers). The format is compact (`json.dumps(..., separators=(",", ":"))`),
  key-sorted (`sort_keys=True`), UTF-8, LF-terminated — byte-deterministic
  across runs on identical input, enabling content-hash caching and the
  `lintle diff` consumer (issue #10). Streaming is per-worker: each
  worker writes `<out_dir>/.shards/<stem>.findings.jsonl`; the main
  process concatenates shards in alphabetical `src_name` order at end
  of run and removes the shard directory. A pre-run shard-dir scrub
  prevents contamination from prior aborted runs. The byte-faithful
  catalog stays in `broken/<stem>.broken.txt`; `report.jsonl` is the
  structured-findings stream consumers can `jq` against. The
  `RejectEntry` dataclass gains a trailing optional `norad_id` field
  decoded once at quarantine time. Closes #9.

### Changed

- **Breaking — `--report json` output shape.** The flat array of
  per-file `summary_dict()` entries previously emitted by
  `lintle ... --report json` is replaced by the top-level envelope
  described under Added above. Consumers that did `payload[0]` to read
  the first file's stats now do `payload["files"][0]`; the per-file
  keys (`src_name`, `paired_records`, …) are unchanged but join three
  new ones (`elapsed_seconds`, `bytes`, `records_per_sec`). No legacy
  flag is provided; the schema is pinned by `schema_version: "1"` so
  future minor revisions stay additive within `"1"` and any breaking
  rename bumps to `"2"`.
- Internal: extracted `RejectSink` and `FileSample` from `FileStats` so
  the 5-per-rule exemplar cap is enforced by construction rather than by
  convention in a single caller. `pipeline.process_file` no longer
  juggles a separate `broken_writer` and exemplar dict — `RejectSink`
  owns both responsibilities and the cap is now a structural property of
  the sink type. `FileStats.reject_exemplars` is replaced by
  `FileStats.reject_sample: FileSample` (a frozen, per-rule bounded
  sample). `FileSample.from_bounded(cap=N, entries_by_rule={...})` is
  the test-fixture entry point; production code writes through
  `sink.add(entry)`. Renderers (`format_reject_lines`,
  `write_broken_file`) read from `stats.reject_sample.buckets`. No
  user-visible byte format changes (`.broken.txt`, JSON output, and
  `report.md` are byte-identical to the pre-refactor baseline). Closes
  #19.
- Internal: encapsulated `FileStats.quarantined_norad_ids` behind a
  `NoradTracker` type with a single `record(norad_id, rule_id)` mutation
  entry point. `pipeline._record_reject` no longer hand-rolls the
  `setdefault`/`get`/`+1` dance — future writers will find `.record(...)`
  by name instead of reinventing the pattern. Field name unchanged
  (`quarantined_norad_ids` preserved so the `summary_dict` JSON-key
  contract and `git log -S` history stay intact); only the type changed
  from `dict` to `NoradTracker`. Renderers (`summary_dict`,
  `_aggregate_per_norad`, `aggregate_broken_norad_ids`) read via
  `tracker.counts`. Sibling refactor to issue #19's `RejectSink`
  extraction, deliberately simpler — no cap, no file resource, no
  context-manager, no `merge`, no freeze boundary (half-encapsulation
  by deliberate choice so the per-NORAD data shape stays free to evolve
  toward per-satellite timestamps or provenance without breaking a
  monoid contract). No user-visible byte format changes
  (`broken-noradids.ndjson`, JSON output, and `report.md` are
  byte-identical to the pre-refactor baseline). Closes #47.
- Free-form short tags used across `repair.py`, `pipeline.py`, and tests
  are now defined in `lintle.categories` (for `FixClass`, the successful-repair
  taxonomy) and `lintle.diagnostics` (for `RuleID`, the rejection taxonomy)
  as `enum.StrEnum` classes, so typos and renames are caught rather than
  silently drifting across call sites. Closes #18.
- **Breaking — `.broken.txt` sidecar line format.** The per-entry headline
  now cites the rule ID and structured fields instead of a free-form
  sentence: `[N] source lines X-Y - rule: TLE-CHK-001 (tier-1) - col 69
  observed='7' expected='3'`. Related diagnostics on the same record
  (when both lines of a record fail) render on indented `    and: ...`
  continuation lines. The sidecar header (`# source: ... | generated:
  ... | lintle <version>`) is unchanged and already pins the format to
  a release, so downstream parsers can dispatch on version.
- **Breaking — JSON output via `lintle validate --report json`.** The
  per-file `"reject_categories"` field is renamed `"reject_counts"` and its
  inner keys change from free-form tags (`"checksum-mismatch"`) to stable
  rule IDs (`"TLE-CHK-001"`). `fix_counts` and its inner keys are
  unchanged. The per-file payload also gains `"quarantined_norad_ids"`
  carrying the per-satellite per-rule breakdown that backs the new
  Markdown per-NORAD section (see Added above), shaped as
  `{"<noradId>": {"TLE-CHK-001": count, ...}, ...}` — integer NORAD keys
  auto-stringify, `RuleID` keys serialise as their stable wire token.
- `FileStats.reject_categories` is renamed `FileStats.reject_counts` to
  match the new vocabulary; values are keyed by `diagnostics.RuleID`
  (which compares and hashes as its stable string value).
- `FileStats.quarantined_norad_ids` is now a `dict[int, dict[RuleID, int]]`
  instead of a `set[int]`: outer keys are still the satellite catalog
  numbers, but each value is a per-rule count dict tallying which
  diagnostics that satellite hit in this file. `pipeline._record_reject`
  records the rule ID alongside the satellite at quarantine time, feeding
  the new `## Per-NORAD breakdown` section. The `broken-noradids.ndjson`
  sidecar still emits one `{"noradId":N}` line per ID —
  `aggregate_broken_norad_ids` now iterates the dict's keys — so that
  downstream contract is byte-identical. The per-file map is O(IDs × 9),
  and the corpus-wide rollup adds an O(IDs × source files) term for the
  Files column; both are bounded by the satellite catalog and the small
  fixed number of source files, preserving the constant-memory invariant.
  Closes #40.
- `validate` mode now groups reject exemplars by rule ID (up to 5 per
  rule, sorted by descending occurrence count with alphabetic tiebreak),
  so a single noisy defect class can no longer hide rarer rules in the
  operator summary. `FileStats.reject_exemplars` is now
  `dict[RuleID, list[RejectEntry]]` capped at
  `_PER_RULE_EXEMPLAR_BOUND = 5` per rule; the per-file memory ceiling
  drops from 1000 entries to `|RuleID| × 5 = 45`. Each exemplar line
  reuses `_format_diagnostic` so column ranges, observed/expected, repair
  tier, and related-diagnostic continuations carry over. The on-disk
  `.broken.txt` streaming path is untouched — every reject still reaches
  the byte-faithful catalog. Closes #21.

### Removed

- `lintle.categories.RejectCategory` (replaced by
  `lintle.diagnostics.RuleID`). Call sites updated; `RejectCategory` was
  internal — no external API breakage beyond the JSON / `.broken.txt`
  changes noted above.

## [0.2.0] - 2026-05-24

### Fixed

- `pipeline.process_file` no longer conflates unpaired orphan lines with
  paired 2-line records in its counter. `FileStats.total_records` is replaced
  by three independent counters: `paired_records` (true 2-line entries),
  `orphan_entries` (unpaired single lines surfaced as findings), and
  `input_lines_seen` (every physical line read from the file). Per-file
  summary, JSON output (`--report json`), `.broken.txt` sidecar header, and
  `report.md` run report all surface the three counters in their own columns
  so percentages and breakdowns are unambiguous. `clean_count` /
  `quarantined_count` semantics are unchanged: orphans still go to
  `.broken.txt` and remain tallied under `reject_categories['orphan-line']`.
  Closes #5.
- `cli.main` now refuses to run when two distinct inputs share a basename,
  because their `cleaned/` and `broken/` sidecars would otherwise silently
  overwrite each other under `data/output/` — exactly the kind of
  wrong-but-valid-looking outcome the spec forbids. `discover_paths` also
  dedupes inputs by `os.path.realpath`, so the same canonical file listed
  twice (literally, via a parent directory, or through a symlink) is
  processed once. Closes #4.
- `cli.check_paths` no longer pre-checks readability via `os.access`. That
  call consults POSIX mode bits only and false-negatives on filesystems
  that grant read via ACLs (NFSv4, SMB, FUSE), producing a misleading
  "unreadable" verdict on inputs the worker can in fact open. The
  authoritative readability test is the worker's `open()`; a real
  permission failure surfaces through the per-file processing path with
  the same exit code 2. Landed alongside the basename-collision fix in
  commit `a898fb9`. Closes #7.

### Added

- `lintle clean` now emits a corpus-wide `broken-noradids.ndjson` at the
  `--out-dir` root, alongside `cleaned/`, `broken/`, and `report.md`. One
  `{"noradId":N}` object per line, deduplicated and sorted ascending,
  listing every NORAD catalog number whose records were quarantined
  anywhere in the run. Records whose line 1 is itself unreadable are
  omitted (no catalog number to recover). Intended for downstream
  consumers (e.g. descent-app) that need to flag affected satellites
  without parsing the human-readable `broken/*.txt` defect reports. The
  file is always written in `clean` mode — empty when nothing was
  quarantined — so the artifact is always present. Schema is deliberately
  minimal (one field); future releases can extend each record additively
  without breaking compat. Closes #2.
- `tle.extract_norad_id()` — recovers the 5-digit catalog number from a
  TLE line 1, used by the new NDJSON emitter.

- The live progress line on a TTY now reports throughput (`records/sec`) and
  the longest-running file currently in flight (with `+N more` when other
  files are also being processed). With `--jobs N` the oldest active file
  surfaces alone once peers finish — making a single slow file visible at a
  glance during long runs of the 29-file corpus. The progress queue now
  carries `("start", name)` / `("end", name)` lifecycle events alongside the
  existing integer record-count deltas; `process_file` always emits
  `("end", name)` from a `finally`, so a failed file is correctly cleared
  from the display's active set. Closes #24.

- `tests/test_pipeline_throughput.py` — an opt-in end-to-end throughput
  regression test for `pipeline.process_file()` that streams synthetic TLE
  records and fails on a severe slowdown. Gated by the new `slow` pytest
  marker (registered in `pyproject.toml`, excluded from the default suite via
  `addopts`), so the existing CI matrix is unaffected. Combines a within-run
  stability check (no timed run more than 30% slower than the median) with an
  opt-in per-machine stored baseline at `tests/.throughput_baseline.json`
  (git-ignored). Run with `uv run pytest -m slow -s`; refresh the baseline
  after intentional perf changes with
  `LINTLE_UPDATE_BASELINE=1 uv run pytest -m slow`. Closes #23.

## [0.1.2] - 2026-05-23

### Fixed

- `report.py` now streams the `.broken.txt` reject sidecar line-by-line instead
  of holding the full reject set in memory, so the constant-memory invariant
  survives files with a high reject ratio.

### Added

- `CLAUDE.md` § Worktree Workflow and `CONTRIBUTING.md` § Parallel development
  with git worktrees — how to iterate on several branches at once while sharing
  the ~30 GB corpus across worktrees via a symlink.
- `.gitignore` excludes `/.worktrees/`.

## [0.1.1] - 2026-05-22

### Fixed

- `lintle clean` (and `validate`) no longer crash with a `FileNotFoundError`
  traceback when the default input directory `data/source` does not exist on the
  host. A new input-validation step in `cli.main()` catches the situation
  upfront and prints a friendly hint that points the user at `--help` and
  explains how to pass paths or create the directory.
- `discover_paths` no longer silently treats a nonexistent path as a file;
  missing entries are dropped (and the new `check_paths` helper rejects them at
  the boundary with a clear `no such file or directory` message instead of a
  crash deeper in the pipeline).
- `--jobs 0` is rejected upfront instead of silently spinning up a zero-worker
  pool that hangs.

### Added

- `--version` / `-V` on the top-level `lintle` command.
- Top-level and per-subcommand help now include an `Examples:` block and an
  `Exit codes:` reference. Subcommands carry richer descriptions and metavars
  (`PATH`, `DIR`, `N`) so `lintle --help` and `lintle clean --help` are
  self-explanatory.
- `check_paths(paths, using_default)` — a small public helper in `cli.py` that
  returns a user-facing error string for missing or unreadable inputs, or
  `None` if everything is fine.

### Changed

- The `paths` positional argument's argparse default is now `None` (resolved to
  `data/source` inside `main()`) so the CLI can tell "user passed nothing" apart
  from "user explicitly passed `data/source`" and tailor the error wording.
- The version string is now tracked in **one place**: `pyproject.toml`. The
  `__version__` attribute in `src/lintle/__init__.py` is resolved at runtime via
  `importlib.metadata.version("lintle")` (falling back to `0.0.0+local` for
  uninstalled source checkouts). Future releases need only a single bump in
  `pyproject.toml` — see `CONTRIBUTING.md` for the release flow.

## [0.1.0] - 2026-05-22

### Added

- `lintle` console script with two modes: `validate` (read-only audit) and `clean`
  (writes corrected files plus quarantine sidecars).
- `tle.py` — the single TLE validator: column layout, mod-10 checksum, semantic range
  checks, and paired-record validation.
- `repair.py` — speculative, validated repairs: trailing-`\` stripping, CRLF
  normalisation, whitespace trimming, and deterministic checksum reconstruction.
- `pipeline.py` — constant-memory streaming with prefix-driven `1 `/`2 ` line pairing.
- `report.py` — per-file statistics, the byte-faithful `.broken.txt` quarantine sidecar,
  and the Markdown run report.
- `cli.py` — argument parsing, path globbing, per-file `ProcessPoolExecutor` parallelism,
  a live single-line progress display, and graceful Ctrl-C shutdown (exit code `130`).
- Test suite: 92 tests across 7 files, including an `sgp4` oracle cross-check and
  golden-output / idempotence integration tests; `cli.py` is fully covered.
- Project tooling: `ruff` for linting and formatting, `pytest-cov` for coverage.
- Documentation: `README.md`, `CONTRIBUTING.md`, and this changelog.
