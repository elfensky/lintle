# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/).

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
