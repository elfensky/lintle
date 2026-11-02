---
last_mapped_commit: bf834f78234eaa5b6f879c0bff96587181b4d723
last_mapped_at: 2026-11-02
---
# Codebase Structure

**Analysis Date:** 2026-11-02

## Directory Layout

```
night-gsd-standard/                 # repo root (git worktree)
├── src/lintle/                     # all package code (src layout)
│   ├── verify/                     # `lintle verify` auditor package (sole sgp4 importer: orbit.py)
│   └── *.py                        # ~40 flat modules, one responsibility each
├── tests/                          # pytest suite, one test_<module>.py per unit
│   └── fixtures/                   # sample TLE inputs
├── docs/                           # superpowers/ (archived specs, plans, runs), workflows/, space-track-defect-report.md
├── .github/workflows/              # ci.yml, publish.yml, version.yml (+ dependabot.yml)
├── .planning/codebase/             # GSD codebase maps (this file lives here)
├── ARCHITECTURE.md                 # living design reference (root, 1022 lines)
├── AGENTS.md  (CLAUDE.md -> AGENTS.md)  # agent and contributor instructions
├── CONTRIBUTING.md, README.md, CHANGELOG.md, LICENSE
├── pyproject.toml                  # build, deps, ruff, pytest, console script
├── uv.lock                         # locked dependency set
├── .python-version                 # Python pin for uv
├── scorecard.png                   # project image asset
└── data/                           # git-ignored ~42 GB corpus (absent in worktrees; symlink if needed)
```

## Directory Purposes

**`src/lintle/`:**
- Purpose: The whole package. Flat layout, one module per responsibility.
- Contains: `.py` modules. Pure data leaves, streaming core, writers, CLI helpers, presentation leaves.
- Key files: `cli.py` (entry), `pipeline.py` (per-file core), `tle.py` (validator), `repair.py`, `worker_pool.py`, `run_planning.py`, `fsutil.py`, `chunking.py`, `__init__.py` (naming constants and `__version__`).

**`src/lintle/verify/`:**
- Purpose: `lintle verify` auditor, its own package.
- Contains: `__init__.py` (`run` orchestration), `checks.py` (revalidate, conflicts, `SourceAligner`), `scan.py` (shared cleaned-scan driver, also used by dedup), `records.py` (`CleanedRecord`, cleaned-tree readers), `grouping.py` (`ExternalSorter`), `report.py` (`SuspectSink`, suspects and summary writers), `orbit.py` (opt-in sgp4 pass).
- Key files: `src/lintle/verify/__init__.py`, `src/lintle/verify/orbit.py`.

**`tests/`:**
- Purpose: Pytest suite. Grouped into `Test*` classes, one per behaviour.
- Contains: `test_<module>.py` for almost every module (`test_pipeline.py`, `test_repair.py`, `test_verify.py`, `test_dedup.py`, `test_extract.py`, and so on). Import-graph and wall tests live here too (`test_freeze.py`, `test_integration.py`, `test_oracle.py`).
- Key files: `tests/conftest.py` (shared fixtures), `tests/test_integration.py` (end-to-end), `tests/test_pipeline_throughput.py`.
- Fixtures: `tests/fixtures/`.

**`docs/`:**
- Purpose: Human-facing and historical material. Not read by the tool at runtime.
- Contains: `docs/superpowers/archive/{specs,plans,runs}/` (dated, point-in-time, not maintained), `docs/workflows/`, `docs/space-track-defect-report.md`.

**`.github/workflows/`:**
- Purpose: CI and release automation.
- Key files: `ci.yml` (test and lint), `publish.yml` (package publish), `version.yml`.

**`.planning/`:**
- Purpose: GSD planning artifacts. `codebase/` holds these maps.
- Generated: Yes (by GSD mapper runs).
- Committed: Yes, by the orchestrator.

**`data/` (runtime, not in repo):**
- Purpose: Corpus. `data/source/` holds inputs. `data/output/` holds the `clean` output tree.
- Generated: Output is generated. Source is provided.
- Committed: No. Git-ignored. Never read whole. Never written from parallel worktrees.

## Key File Locations

**Entry Points:**
- `src/lintle/cli.py`: `main` and `_dispatch`. Console script `lintle`.
- `src/lintle/__main__.py`: `python -m lintle`.
- `src/lintle/wizard.py`: no-subcommand menu.

**Configuration:**
- `pyproject.toml`: dependencies (`rich`, `humanize`, `sgp4` runtime; `pytest`, `ruff`, `hypothesis`, `pytest-xdist`, `pytest-cov` dev), ruff rules, pytest options, `[project.scripts]`.
- `src/lintle/config.py`: reads and writes `./.lintle.json` (source and output dirs).
- `.python-version`: Python pin.

**Core Logic:**
- `src/lintle/tle.py`: the validator. The single definition of "perfect."
- `src/lintle/repair.py`: `normalize_edges`, `is_content_free`, `process_record`.
- `src/lintle/pipeline.py`: `iter_records`, `process_file`, `_route_candidate`.
- `src/lintle/worker_pool.py`: `run_workers`.
- `src/lintle/run_planning.py`: `CleanConfig`, `RunPlan`, preflight.
- `src/lintle/fsutil.py`: `durable_replace`, `out_dir_lock`, JSON seam.
- `src/lintle/chunking.py`: `ChunkedWriter`, `ChunkedReader`.
- `src/lintle/resume.py`: `.clean-state.json` checkpoint.
- `src/lintle/verify/checks.py`, `src/lintle/dedup.py`, `src/lintle/extract.py`: post-run logic.

**Shared Constants:**
- `src/lintle/__init__.py`: `__version__`, `stem()`, and naming constants (`CLEANED_SUFFIX`, `BROKEN_SUFFIX`, `CLEANED_DIRNAME`, `REPORT_JSONL_NAME`, `EXTRACT_DIRNAME`, and others). Import these; never re-spell them.

**Testing:**
- `tests/test_<module>.py`: unit tests for that module.
- `tests/conftest.py`: shared fixtures.
- `tests/test_integration.py`: end-to-end run.

## Naming Conventions

**Files:**
- Modules: `snake_case.py`, one noun or concept each (`run_planning.py`, `report_writers.py`).
- Tests: `test_<module>.py`, matching the module it covers (`test_verify_stream.py` for streaming verify).
- Output files: `<stem>.NNNNN.<suffix>` for chunk sets (`cleaned.txt`, `broken.txt`, `jsonl`). Single-file outputs use fixed names (`report.md`, `report.json`, `summary.json`, `manifest.jsonl`).
- Output step dirs: numbered, `NN-name` (`01-cleaned`, `02-broken`, `03-report`, `04-verify`, `05-dedup`, `06-extract`).

**Directories:**
- Package subdirectories: lowercase single word (`verify/`).
- Output step dirs: `NN-name`, as above.

**Identifiers:**
- Functions: `snake_case`. Private helpers start with `_`.
- Classes and dataclasses: `PascalCase`. Dataclasses use `@dataclasses.dataclass(slots=True)`, with `frozen=True` where immutable.
- Rule IDs: `TLE-COL-*`, `TLE-CHK-001`, `TLE-PAIR-*`, `TLE-SEM-*`, `TLE-INT-001`, `VRFY-*`, `DEDUP-*`. Values are public and never reused.
- Constants: `UPPER_SNAKE_CASE` (`LINE_LENGTH`, `_MAX_LINE_BYTES`).

## Where to Add New Code

**New validation rule or range check:**
- Primary code: `src/lintle/tle.py` (the one validator). Add a `RuleID` in `src/lintle/diagnostics.py` and a `RuleSpec`.
- Tests: `tests/test_tle.py`, `tests/test_diagnostics.py`.

**New repair (fix class):**
- Primary code: `src/lintle/repair.py` (`normalize_edges` or `process_record`). Add the tag to `FixClass` in `src/lintle/categories.py`.
- Tests: `tests/test_repair.py`, `tests/test_categories.py`.
- Note: `verify` undoes repairs through `normalize_edges`. Keep the change in that one place.

**New clean output stream:**
- Primary code: `src/lintle/chunking.py` (use `ChunkedWriter`). Add the naming constant in `src/lintle/__init__.py`. Route from `src/lintle/pipeline.py`.
- Tests: `tests/test_chunking.py`, `tests/test_pipeline.py`.

**New `verify` check:**
- Primary code: `src/lintle/verify/checks.py` (or `src/lintle/verify/orbit.py` for physics-based checks, which may import `sgp4`).
- Suspect type: `src/lintle/verify/report.py`.
- Tests: `tests/test_verify.py`, `tests/test_orbit.py`.

**New subcommand:**
- Parser: `_add_<name>_subparser` and an entry in `build_parser()` in `src/lintle/cli.py`.
- Implementation: new `src/lintle/<name>.py`. Import lazily inside `_dispatch` (see `dedup`, `extract`, `verify`).
- Post-run work: wrap with `_locked_postrun` if it reads `01-cleaned/`.
- Tests: `tests/test_<name>.py`, plus cases in `tests/test_cli.py`.

**New report or JSON contract:**
- Primary code: `src/lintle/report.py` (run envelope) or `src/lintle/report_writers.py` (structured files). Write through `fsutil.durable_replace`.
- Tests: `tests/test_report.py`, `tests/test_report_writers.py`.

**New CLI rendering or table:**
- Primary code: `src/lintle/summary.py` (shared tables and formatters) or `src/lintle/cli_progress.py` (live tables).
- Tests: `tests/test_summary.py`, `tests/test_cli_progress.py`.

**New shared utility (durable I/O, JSON):**
- Primary code: `src/lintle/fsutil.py` (stdlib only).
- Tests: `tests/test_fsutil.py`.

**New pure helper with no I/O:**
- Place it in a pure leaf module (`src/lintle/history.py`, `src/lintle/epoch.py`, `src/lintle/thresholds.py`). Do not import anything from the clean path into it.

**Import rules to keep:**
- Clean path (`pipeline`, `repair`, `tle`, clean part of `cli`) must not import `lintle.verify` or `sgp4`.
- `epoch.py` and `fsutil.py` stay stdlib-only.
- `verify` may import `repair`. `repair` must not import `verify`.
- Lazy-import `wizard` in `cli`, and `cli` in `wizard`.

## Special Directories

**`.planning/`:**
- Purpose: GSD planning and codebase maps.
- Generated: Yes.
- Committed: Yes.

**`.worktrees/` (outside this repo's tree, under `~/Developer/.worktrees/`):**
- Purpose: Git worktrees for this repo.
- Generated: Yes (by `git worktree add`).
- Committed: No.

**`.venv/`:**
- Purpose: Local virtual environment (`uv sync`).
- Generated: Yes.
- Committed: No.

**`data/`:**
- Purpose: Corpus and clean output tree.
- Generated: Output is generated. Source is provided.
- Committed: No (git-ignored).

**Output tree (`<out-dir>`, runtime):**
- `01-cleaned/`, `02-broken/`, `03-report/`, `04-verify/`, `05-dedup/`, `06-extract/`, each with its own `README.md`. Root holds `README.md` and transient state (`.shards/`, `.clean-state.json`, `.clean.lock`, `.lintle-output`).
- Generated: Yes. Committed: No.

---

*Structure analysis: 2026-11-02*
