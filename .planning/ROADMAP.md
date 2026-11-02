# Roadmap: lintle

## Overview

lintle 0.14.1 is shipped and in use. This roadmap covers the five open GitHub issues (#228, #223, #226, #227, #222) and the documentation drift found by the codebase map. Four of the five issues are pure refactors or claim fixes with byte-identical output. One issue (#222) is a real feature: a parallel per-stem scan for `verify` and `dedup`. The phases run in strict order. Phases 2, 3 and 4 edit the same files, so they cannot run in parallel. Every phase keeps the four Critical Rules in `AGENTS.md`.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Leaf cleanups and determinism claim** - Make the byte-determinism claim true (#223), finish the `history` reduction (#228), fix the doc drift
- [ ] **Phase 2: Clean orchestration extract** - Move the clean run out of `cli.py` into `clean_run.py` with a pure exit-code join (#226)
- [ ] **Phase 3: Checkpoint session** - Give the checkpoint write side one owner, `CheckpointSession` (#227)
- [ ] **Phase 4: Parallel cleaned scan** - Run the per-file cleaned scan of `verify` and `dedup` in parallel, measured first (#222)

## Phase Details

### Phase 1: Leaf cleanups and determinism claim
**Goal**: The byte-determinism claim is true and stated exactly, the shared history code has one definition per fact, and the docs match the code.
**Mode:** mvp
**Depends on**: Nothing (first phase)
**Requirements**: DET-01, DET-02, DET-03, DET-04, HIST-01, HIST-02, HIST-03, DOC-01, DOC-02
**Issues**: #223, #228, plus doc drift (no issue)
**Success Criteria** (what must be TRUE):
  1. Two `lintle clean` runs over the same inputs give `02-broken/` trees with an empty `diff -r`, and each `.broken.txt` header reads `# source: <name> | lintle <version>`.
  2. The freeze test compares `02-broken/` with no timestamp canonicalisation, so a clock reintroduced into the sidecar makes the test fail.
  3. `report.md` shows the same run stamp as `report.json` `run.timestamp`, and every statement of the determinism invariant in `AGENTS.md`, `ARCHITECTURE.md` and `CONTRIBUTING.md` names the artifacts that carry run metadata.
  4. `lintle verify`, `dedup` and `extract` write output byte-identical to their output before the change, while `span_days`, the 8 shared stats keys and `RECORD_BYTES` (140) each have one definition (`history.py`, `tle.py`).
  5. `CONTRIBUTING.md` says Python 3.14, and `README.md` and `CONTRIBUTING.md` list `rich`, `humanize` and `sgp4` (verify-scoped) as runtime dependencies.
**Plans**: TBD

### Phase 2: Clean orchestration extract
**Goal**: The clean run has its own module, and one pure function decides the exit code.
**Mode:** mvp
**Depends on**: Phase 1
**Requirements**: CLEAN-01, CLEAN-02, CLEAN-03
**Issues**: #226
**Success Criteria** (what must be TRUE):
  1. `lintle clean` gives the same output trees and the same exit codes as before the move, and the full suite passes with no golden file regenerated.
  2. `cli.py` reaches the clean run only through `clean_run.run(args) -> int`, and `clean_run` is a seed of the import-graph test, so `sgp4` and `lintle.verify` stay out of it.
  3. A table test pins `join_exit_code(...)` for every combination of plan verdict, operational error, interrupt, failed file and quarantine gate, with the expected values generated from the pre-refactor code.
**Plans**: TBD

### Phase 3: Checkpoint session
**Goal**: One object owns the checkpoint write side, and `worker_pool` no longer knows how a checkpoint is written.
**Mode:** mvp
**Depends on**: Phase 2
**Requirements**: CKPT-01, CKPT-02, CKPT-03
**Issues**: #227
**Success Criteria** (what must be TRUE):
  1. An interrupted `lintle clean`, resumed with `--resume`, ends with the same outputs and the same exit code as before the change.
  2. The checkpoint file written after a finished file is byte-identical to the file the four-call path wrote, pinned by a test with one fully populated stats object.
  3. `worker_pool` records a finished file with one `session.record(path, stats)` call and imports neither `resume` nor `report`.
  4. `resume.py` still never imports `report`; the summary builder is passed in at `open()`.
**Plans**: TBD

### Phase 4: Parallel cleaned scan
**Goal**: `verify` and `dedup` scan the cleaned files in parallel, with unchanged output, constant memory and a timing measurement taken first.
**Mode:** mvp
**Depends on**: Phase 3 (also Phase 1, which stabilises `verify/scan.py`)
**Requirements**: PAR-01, PAR-02, PAR-03, PAR-04, PAR-05
**Issues**: #222
**Success Criteria** (what must be TRUE):
  1. Per-phase timings for `verify` and `dedup` (scan, merge/conflicts, orbit, writes) from a real-corpus run exist before the parallel code is built and again after it, and a recorded decision says whether `dedup` is parallelised too.
  2. `lintle verify --jobs N` (and `lintle dedup --jobs N`, if the timings justify it) is accepted with the same meaning as in `clean`, `--jobs 1` runs serially, and on the corpus the scan phase is faster with more jobs.
  3. `04-verify/` (and `05-dedup/`) are byte-identical for `--jobs` 1, 2 and 4 and equal to the pre-change serial output, proven by a multi-stem fixture with cross-stem ties, forced spills and forced reverse completion; a corpus-scale serial vs parallel `diff -r` is run, or its omission is stated.
  4. Memory stays flat: workers return spilled run paths and small tallies, never record lists; the per-worker spill budget shrinks with the worker count; the final merge keeps a bounded number of files open, shown by a test with a lowered file-descriptor limit.
  5. Ctrl-C, exit codes and per-stem progress rows behave as in the serial scan, no partial output directory or spill file remains after an abort, `--orbit` stays a separate serial pass, and `sgp4` stays out of the clean path.
**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Leaf cleanups and determinism claim | 0/TBD | Not started | - |
| 2. Clean orchestration extract | 0/TBD | Not started | - |
| 3. Checkpoint session | 0/TBD | Not started | - |
| 4. Parallel cleaned scan | 0/TBD | Not started | - |
