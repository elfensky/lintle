# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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

### Changed

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
