# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
