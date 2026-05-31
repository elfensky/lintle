# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
  `docs/superpowers/specs/2026-05-27-max-quarantined-percentage-design.md`.

- Host-aware out-dir lock: refuses to start a second concurrent `clean` against
  the same `--out-dir`.

### Changed

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
  `docs/superpowers/specs/2026-05-25-report-json-envelope.md` and the
  golden fixture at `tests/fixtures/report-envelope-v1.golden.json`.
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
