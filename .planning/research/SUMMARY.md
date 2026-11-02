# Project Research Summary

**Project:** lintle (milestone: #222 parallel cleaned scan, #223 determinism claim, #226/#227/#228 refactors, doc drift)
**Domain:** Offline Python 3.14 CLI that validates and cleans a ~30 GB TLE corpus; brownfield, subsequent milestone
**Researched:** 2026-11-02
**Confidence:** MEDIUM-HIGH

## Executive Summary

This milestone is five small-to-medium changes to a mature tool. Four are housekeeping with byte-identical outputs (#223, #226, #227, #228). One is a real feature: a parallel per-stem scan for `verify` and `dedup` (#222). All four research files agree on the core approach. Add zero dependencies. Reuse the `ProcessPoolExecutor` pattern from `worker_pool.py`. Workers scan one stem each, spill sorted runs to disk, and return paths plus small tallies. The parent merges in stem order, never in completion order. Output must be a function of the input only, not of `--jobs`, chunk size or timing.

The decisions are mostly made. #223 is resolved: drop the `generated:` stamp from the `.broken.txt` header and inject the run stamp into `report.md` (the choice recorded in the 2026-08-16 issue comment). Do not thread the stamp through `CleanConfig` (that leaves two runs still different). Qualify the invariant in the docs for `report.json` and `report.md`, which keep a run clock by design. The build order is #228 and #223 first, then #226, then #227, then #222.

The main risks sit in #222. First, `tests/test_freeze.py` uses one stem with four records, so it cannot see ordering bugs under concurrency. A multi-stem, forced-spill, reverse-completion fixture must be the first commit of that phase. Second, spill ownership and fan-in. Today the final merge already opens about 1,160 run files at once. A smaller per-worker chunk multiplies that. Workers should pre-merge to one run per stem, so the parent holds about 29 handles. Third, the speedup is unmeasured. The 2 h 05 m figure has no per-phase split, and the contradiction pass, orbit pass and writes stay serial. Measure before building.

## Key Findings

### Recommended Stack

No new dependencies. All pieces are in the Python 3.14 standard library and already used by the clean path. See `STACK.md`.

**Core technologies:**
- `concurrent.futures.ProcessPoolExecutor`: CPU-bound work; same class as `worker_pool.py`, with `terminate_workers()` (3.14) for fast shutdown
- `heapq.merge` over a total sort key `(catalog, epoch_key, src_file, index)`: removes reliance on its undocumented tie order; keep `record_key` as the separate group key
- `tempfile.TemporaryDirectory` owned by the parent: one spill root, one subfolder per task, removed in `finally`
- `multiprocessing.Manager().Queue()`: worker-to-parent progress, same as `clean`
- `os.process_cpu_count()`: default workers = CPU count minus 1, capped by stem count

Rejected: threads, `InterpreterPoolExecutor`, free-threaded build, `asyncio`, `joblib`/`dask`, `duckdb`/`polars`, `as_completed` for output order, returning records from workers. Python 3.14 uses `spawn` on macOS and `forkserver` on Linux: worker functions must be module-level and arguments picklable. Never pass `mp_context="fork"`.

### Expected Features

See `FEATURES.md`.

**Must have (table stakes):**
- `--jobs N` on `verify` and `dedup`, same meaning and default as `clean`; `--jobs 1` runs the same code path in-process
- Output byte-identical for every N and equal to the pre-#222 serial output; stem-order emission, bounded in-flight window
- Per-worker spill budget = total / jobs, with a floor
- Same exit codes and shutdown semantics via `process_control`; no partial `04-verify/` or `05-dedup/`
- A jobs-matrix determinism test and a run-twice determinism test for `clean`
- #223 resolved with the invariant wording consistent in every document
- #228, #227, #226 as pure refactors with zero golden diff
- Doc drift: CONTRIBUTING says Python 3.14, `sgp4` is a verify-scoped runtime dependency, README lists `humanize`

**Should have (competitive):**
- One parallel driver shared by `verify` and `dedup`
- A measured before/after speedup on the real corpus
- Determinism independent of worker count

**Defer (v2+):**
- Parallel sort/merge and parallel `--orbit` pass (separate issues; keep `sgp4` in the parent)
- Caching sorted order via `cleaned_fingerprint`
- `SOURCE_DATE_EPOCH` support (only needed if a timestamp stays in a contracted artifact)
- A `--deterministic` flag, checksum manifests or provenance files (anti-features)

### Architecture Approach

`cli.py` shrinks to parser and dispatch. `clean_run.py` (#226) owns the clean run with `run(args) -> int` and a pure `join_exit_code`. `resume.CheckpointSession` (#227) owns the checkpoint write side, with the summary callable injected so `resume.py` stays a leaf. In #222, a stdlib-only `pool_core.py` is extracted as the second adapter appears, so signal choreography (issues #89, #100) lives in one place. A module-level per-stem worker in `verify/` keeps the `sgp4` wall intact. `ExternalSorter` gains parent-owned spill dirs, `seal()` and a merge over run sets in stem order. See `ARCHITECTURE.md`.

**Major components:**
1. `clean_run.py`: clean orchestration and exit-code join (#226)
2. `resume.CheckpointSession`: open/record/delete of the checkpoint (#227)
3. `pool_core.py`: generic pool, in-order collection, teardown, `resolve_jobs` (#222)
4. `verify/stem_scan.py`: picklable per-stem worker, never imports `orbit`
5. `ExternalSorter`: cross-process handoff, stem-order merge
6. `history.stats_row`, `tle.RECORD_BYTES`: single definitions (#228)

### Critical Pitfalls

See `PITFALLS.md` for all 24.

1. **Freeze test is blind to concurrency bugs**: add a multi-stem fixture (cross-stem ties, tiny `chunk_size`, forced reverse completion) and assert tree equality for jobs in {1,2,4} before any pool code.
2. **Completion-order merge**: index results by stem, consume with `future.result()` in stem order, order runs by `(stem_index, run_index)`, merge every accumulator and progress line in stem order.
3. **Spill ownership and fan-in**: the parent owns the spill root and removes it on every exit path; budget = total / jobs; pre-merge to one run per stem; test with a lowered `RLIMIT_NOFILE`.
4. **Parallelising an unmeasured phase**: add per-phase timing and take one corpus measurement first; treat `dedup` as optional until its split is known.
5. **Refactors that pass vacuously**: exhaustive `join_exit_code` table generated from old code (note `operational_error` implies `interrupted`); `CheckpointSession` must share the `plan.completed` dict; no `LINTLE_FREEZE_UPDATE=1` to make a refactor pass; add new modules to the import-graph seeds.

## Implications for Roadmap

Four research files agree on the order: #228 and #223 first, then #226, #227, #222, with doc drift alongside. Steps 2 to 4 are strictly sequential because they edit the same files.

### Phase 1: Leaf cleanups and determinism claim (#228 + #223 + doc drift)
**Rationale:** Both are leaf-only and independent. #228 edits `verify/scan.py` (`RECORD_BYTES`) before #222 rewrites it. #223 first makes later refactor PRs claim "zero golden diff". Park Dependabot #238/#239 first so each diff has one cause.
**Delivers:** `history.stats_row`, `span_days`, `tle.RECORD_BYTES`; sidecar header `# source: X | lintle V`; `report.md` stamp from the envelope; invariant wording fixed in every document; CONTRIBUTING/README drift fixed; run-twice tree test.
**Addresses:** #228, #223, doc drift.
**Avoids:** P13 (edge-class fixtures pinned from old code, round only in `stats_row`, assert key order), P16 (grep every statement of the invariant, one commit), P22 (pin `RECORD_BYTES == 140`), P21 (docs in their own commit), P23.

### Phase 2: Extract clean orchestration (#226)
**Rationale:** Largest mechanical move, touches `cli.py` heavily, so it runs alone. Gives #222 a named home for jobs work.
**Delivers:** `clean_run.py`, pure `join_exit_code` with an exhaustive table test; `resolve_jobs` placed in `worker_pool.py` (never in `clean_run`).
**Avoids:** P9 (make retargeted patches fail first, `raising=True`), P10 (table from pre-refactor code, include `operational_error and interrupted`), P11 (pure-move commit first), P12 (add `clean_run` to import-graph seeds).

### Phase 3: CheckpointSession (#227)
**Rationale:** Removes `resume` and `report` knowledge from `run_workers`, which #222's pool split needs.
**Delivers:** `resume.CheckpointSession` with injected `summarize`; checkpoint bytes identical.
**Avoids:** P14 (session shares the `plan.completed` dict, keep mutate-then-write, ENOSPC gives exit 2), P15 (bytes-pin with a fully populated `FileStats`).

### Phase 4: Parallel cleaned scan (#222)
**Rationale:** Needs #227 (clean `run_workers`), #226 (named home for jobs) and #228 (stable `scan.py`). Largest, riskiest, least measured.
**Delivers:** `--jobs` on `verify`/`dedup`; `pool_core`; per-stem worker; sorter handoff; total sort key; jobs-matrix tests; measured speedup.
**Uses:** `ProcessPoolExecutor`, `heapq.merge` with a total key, parent-owned spill root.
**Implements:** `pool_core`, `verify/stem_scan`, `ExternalSorter.seal()`.
**Avoids:** P1-P8, P17-P20. Internal order: (a) per-phase timing and one corpus profile; (b) multi-stem fixtures; (c) spill ownership, budget and fan-in; (d) pool-core extraction; (e) `verify` first, `dedup` only if the split justifies it. Corpus-scale serial vs parallel `diff -r` is the real acceptance test (P24); say so if skipped.

### Phase Ordering Rationale

- #228 before #222 avoids a conflict on `RECORD_BYTES` in `scan.py`.
- #223 first gives the later phases a clean golden file.
- #226 before #227: move first, edit second, for the smaller conflict surface. (If #227 goes first it still works, with a manual conflict on the teardown lines.)
- Both #226 and #227 retire knowledge from `run_workers` before #222 extracts the pool core.
- Default `--jobs` for `verify`/`dedup`: STACK and FEATURES say match `clean`; PITFALLS (P8) warns that a parallel default spawns pools in every test under pytest-xdist. Decide at plan time. Recommended: match `clean` in the CLI, and keep single-stem fixtures on the serial path of the same code.

### Research Flags

Needs deeper research during planning:
- **Phase 4 (#222):** open design questions: final-merge shape (parent merges all runs vs worker pre-merge, measure both); `SuspectSink` split with per-stem spill; progress channel for `UnitTable`; default `--jobs` and `--orbit` memory; spill-dir location and the disk guard. Also needs the per-phase profile before commitment.

Standard patterns (skip research-phase):
- **Phase 1:** #228 and #223 are prototyped leaf changes; #223 resolution is already chosen.
- **Phase 2:** #226 is a prototyped mechanical move; the table test is generated from old code.
- **Phase 3:** #227 is prototyped; the risk is in tests, not in unknowns.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | MEDIUM-HIGH | Stdlib behaviour confirmed in 3.14 docs and repo code. `Executor.map` order probe ran on 3.15, not 3.14. Parallel speedup unmeasured. |
| Features | MEDIUM | Scope comes from PROJECT.md. External practice from primary but partly excerpted sources. |
| Architecture | HIGH for #226/#227/#228/#223, MEDIUM for #222 | Issue prototypes plus code read. The #222 spike ran on a 4-stem fixture, not the corpus. |
| Pitfalls | HIGH for repo-anchored items, MEDIUM for platform items | Start methods and fd limits partly from training knowledge. |

**Overall confidence:** MEDIUM-HIGH

### Gaps to Address

- Share of `verify --source --orbit` and `dedup` wall time spent in the scan phase: measure first in Phase 4; it decides whether `dedup` is worth parallelising.
- Linux default soft file limit (1,024 assumed) and the maintainer's other Macs: check `ulimit -n` on the CI runner and in the plan.
- `Executor.map` order and 3.14 start-method defaults: confirm with a one-line check on Python 3.14 (`multiprocessing.get_start_method()`).
- Spill budget floor (about 25,000 records) and fan-in cap (about 256): need one measurement on the corpus.
- Checkpoint compatibility across the version that drops the sidecar stamp: the sidecar size is stored per chunk. Research says an older checkpoint cannot be resumed anyway (`validate_run_identity` checks `lintle_version`); confirm with a test.
- Why #227 is "prerequisite-shaped" for #222: ARCHITECTURE explains it (clean `run_workers` before the pool split); STACK did not. Treat ARCHITECTURE as authoritative.
- CI is Ubuntu only: add a macOS job for the parallel tests, or document manual coverage.
- Default `--jobs` for `verify`/`dedup` (see Phase Ordering Rationale).

## Sources

### Primary (HIGH confidence)
- Python 3.14 docs: concurrent.futures, multiprocessing, heapq, What's New in 3.14
- Repo code and tests: `worker_pool.py`, `process_control.py`, `verify/*`, `dedup.py`, `report_writers.py`, `resume.py`, `tests/test_freeze.py`, `tests/test_verify.py`
- GitHub issues #222 (with spike), #223 (with resolution), #226, #227, #228 on `elfensky/lintle`
- `.planning/PROJECT.md`, `.planning/codebase/CONCERNS.md`, repo-root `ARCHITECTURE.md`

### Secondary (MEDIUM confidence)
- reproducible-builds.org SOURCE_DATE_EPOCH page (summary, not the full spec)
- GNU parallel `--keep-order` documentation (search excerpt)
- CPython bug tracker on unbounded `map` prefetch

### Tertiary (LOW confidence)
- gevent `_imap.py` reorder-buffer pattern (third-party copy)
- Linux soft fd limit of 1,024 and parallel speedup claims (not measured)

---
*Research completed: 2026-11-02*
*Ready for roadmap: yes*
