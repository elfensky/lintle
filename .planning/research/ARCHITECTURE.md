# Architecture Research

**Domain:** Offline Python CLI that validates and cleans TLE corpora (lintle) - brownfield, subsequent milestone
**Researched:** 2026-11-02
**Confidence:** HIGH for #226, #227, #228, #223 (issue prototypes plus code read). MEDIUM for #222 (spike ran on a 4-stem fixture, not on the 30 GB corpus).

Scope: how the five open issues (#226, #227, #228, #222, #223) fit the existing module graph and the sgp4 wall. The existing system is not re-described. See `.planning/codebase/ARCHITECTURE.md` and repo-root `ARCHITECTURE.md` (§2, §5) for that.

## Standard Architecture

### System Overview (target state after the milestone)

New or changed modules are marked `[NEW]` or `[CHG]`. Arrows point from importer to imported. No arrow points back.

```
                     lintle / python -m lintle
                              |
                          cli.py [CHG: shrinks 1040 -> ~664 lines]
         parser, _dispatch, _locked_postrun, --jobs flag on verify+dedup
          |                 |                          |
          | clean arm       | lazy                     | lazy
          v                 v                          v
  clean_run.py [NEW]   verify/__init__ [CHG]       dedup.py [CHG]
  run(args)->int            |                          |
  join_exit_code()          +-----------+--------------+
  _finalize_run             |  verify/scan.py [CHG]    (shared cleaned-scan driver:
  resolve_jobs (see below)  |  serial path kept; parallel path added)
     |    |    |            |        |
     |    |    |            v        v
     |    |    |      pool_core.py [NEW, name provisional]
     |    |    |      generic pool: items + worker fn + in-order collect
     |    |    |      signal choreography, fast teardown, failure capture
     |    |    |            ^
     |    |    v            | (clean adapter)
     |    |  worker_pool.py [CHG: run_workers = clean adapter over pool_core;
     |    |                  no resume/report imports after #227]
     |    v
     |  resume.py [CHG: + CheckpointSession]
     v
  run_planning.py, output_artifacts.py, thresholds.py, process_control.py (unchanged API)

  Leaves:  tle.py [CHG: + RECORD_BYTES]   history.py [CHG: + span_days, stats_row]
           report_writers.py [CHG: header drops "generated:"]   (#223)
           report.py [CHG: format_run_report takes generated_at]  (#223)
```

The sgp4 wall is untouched. `sgp4` stays imported only by `verify/orbit.py`, lazily, under `--orbit`.

### Component Responsibilities

| Component | Responsibility | Boundary rule |
|-----------|----------------|---------------|
| `clean_run.py` [NEW, #226] | Owns the whole `clean` run: input checks, lock lifecycle, plan, pool run, envelope, artifact-vs-render branch, teardown, exit code. Public surface: `run(args) -> int` and pure `join_exit_code(...)`. | Imports `run_planning`, `worker_pool`, `output_artifacts`, `thresholds`, `process_control`, `resume`, `report`, `summary`, `fsutil`, `term`, `cli_progress`. Never imports `cli`, `verify`, `dedup`, `extract`, `sgp4`. `cli` imports it, never the reverse. |
| `join_exit_code` [NEW, #226] | Pure fold of five outcome sources into one int. Precedence: plan verdict, operational error (2), interrupt (128+signo), failed file (2), quarantine gate (0 or 1). | No I/O, no imports beyond stdlib and `process_control.signal_exit_code`. Table-tested. |
| `resume.CheckpointSession` [NEW, #227] | Sole owner of the checkpoint write side: `open(out_dir, inputs, completed, run_identity, summarize=...)`, `record(path, stats)`, `delete()`. | `resume.py` stays a leaf: it must not import `report`. The stats-to-summary contract rides in the injected `summarize` callable (caller passes `report.summary_dict`). `write_checkpoint` is looked up at call time so tests can still monkeypatch it. |
| `worker_pool.run_workers` [CHG, #227 then #222] | Clean adapter: submits `pipeline.process_file` per file, drives `ProgressDisplay`, calls `session.record(path, stats)`. | After #227: imports neither `resume` nor `report`. After #222: delegates pool mechanics to `pool_core`. |
| `pool_core` [NEW, #222] | Generic process-pool core: items, worker function, in-order result collection, SIGINT/SIGTERM/SIGHUP choreography, `terminate_workers` teardown, per-item failure capture, `resolve_jobs`. | Stdlib + `process_control` + `term` only. No knowledge of `pipeline`, `verify`, `resume`, `report`, `sgp4`. Must be importable from both the clean path and `verify/`. |
| `verify/scan.py` [CHG, #222] | Shared cleaned-scan driver. Gains a parallel per-stem path next to the serial one; owns `Scan`, `UnitTable` wiring, fingerprint. | May import `pool_core` and `verify.*`. Must stay `sgp4`-free (swept by `test_verify_submodules_are_sgp4_free_except_orbit`). |
| per-stem worker function [NEW, #222] | Module-level picklable function in `verify/` (for example `verify/stem_scan.py`): streams one stem, runs the caller's per-record step, spills sorted runs and suspect runs into a parent-owned spill dir, returns a small `StemResult`. | `sgp4`-free. Never imports `orbit`. Never returns record lists. |
| `ExternalSorter` [CHG, #222] | Gains cross-process handoff: parent-owned spill dir, `seal()` that spills the in-memory tail and returns run paths, and a merge entry point that accepts run paths from several sorters in stem order. | Equal-key tie-break stays "add order", extended to "stem order, then add order". |
| `history.stats_row` and `HistoryStats.span_days` [CHG, #228] | One reduction and one 8-key row builder for `dedup` manifest and `extract` sidecar. | Pure leaf. Imports stdlib and `lintle.epoch` only. |
| `tle.RECORD_BYTES` [CHG, #228] | One home for `2 * (LINE_LENGTH + 1)`. | `tle.py` is pure and importable from both sides of the wall. `verify/scan.py` and `extract.py` import it. No new edge into `verify`. |
| `.broken.txt` header [CHG, #223] | Becomes `# source: <name> \| lintle <ver>`. No clock inside worker processes. | `report.md` takes its stamp from the envelope's `run.timestamp`, injected by `output_artifacts`. |

### Where `resolve_jobs` lives

#226 moves `resolve_jobs` into `clean_run.py`. #222 needs it from `verify`/`dedup`. `verify` must not import `clean_run` (that would pull the whole clean orchestration into the auditor). Two ways out:

1. **Recommended:** at #226 time, put `resolve_jobs` in `worker_pool.py` (pure, pool-related) and re-export from `cli` for the parser side. At #222 it moves once more into `pool_core` with a re-export.
2. Accept one extra move at #222: keep it in `clean_run`, move to `pool_core` later.

Either is cheap. Pick at #226 plan time. Do not let `verify` import `clean_run`.

## Recommended Project Structure

```
src/lintle/
  cli.py              # parser + dispatch only; clean arm = `return clean_run.run(args)`
  clean_run.py        # [NEW #226] run(args) -> int, join_exit_code, _finalize_run
  worker_pool.py      # clean adapter over pool_core (after #222); no resume/report (after #227)
  pool_core.py        # [NEW #222] generic pool; name provisional
  resume.py           # + CheckpointSession (#227)
  tle.py              # + RECORD_BYTES (#228)
  history.py          # + span_days in HistoryStats, stats_row (#228)
  report_writers.py   # header without "generated:" (#223)
  report.py           # format_run_report(generated_at=...) (#223)
  verify/
    scan.py           # serial path + parallel path (#222); RECORD_BYTES import (#228)
    stem_scan.py      # [NEW #222] picklable per-stem worker (name provisional)
    grouping.py       # ExternalSorter seal()/merge-of-runsets (#222)
    report.py         # SuspectSink needs a per-stem spill variant (#222)
```

### Structure Rationale

- **`clean_run.py` next to `worker_pool.py`, not inside `cli.py`:** the clean run has sequencing invariants of its own (lock, plan, pool, finalize, teardown). A named module gives a test seam. `cli.py` keeps argparse and routing.
- **`pool_core.py` as a top-level module, not under `verify/`:** both adapters import it. A top-level stdlib-only module sits on neither side of the wall.
- **Worker function inside `verify/`:** workers import `verify.checks` and `tle`. They need no `sgp4`. Keeping them in `verify/` means the existing sweep test covers them with no test change.

## Architectural Patterns

### Pattern 1: Adapter over a generic core (for #222)

**What:** `pool_core` knows items, a worker function and an ordered result stream. `clean` and `verify` each supply a worker function, a progress sink, and a post-result hook.
**When to use:** only when the second adapter exists. #222 is that second adapter. The seam is extracted inside #222, not before it (#222 comment, point 3).
**Trade-offs:** one more module and one level of indirection. The gain is that signal handling and teardown (issues #89, #100) stay in one place and are not copied.

```python
# pool_core (sketch, not final API)
def run_pool(items, worker, *, jobs, on_result, on_failure, initializer=None) -> PoolOutcome: ...
# clean adapter: on_result = progress.file_done + session.record
# verify adapter: on_result = store StemResult in stem-order slot
```

### Pattern 2: Merge in stem order, never completion order (for #222)

**What:** workers finish in any order. The parent stores each `StemResult` in a slot indexed by the stem's position in `stems`. The final k-way merge receives run sets in that stem order.
**Why it is byte-safe:** `heapq.merge` breaks ties by iterable order, and `ExternalSorter` defines equal keys as "add order". Serial add order is stem 1 fully, then stem 2. Merging run sets in stem order reproduces that.
**Evidence:** spike with workers forced to finish in reverse stem order: `diff -r` empty, exit codes equal (#222 comment).
**Pin it:** keep the #204 B tie-stability test across a spill boundary. Add a variant with equal `(catalog, epoch)` in different stems and a forced reverse completion order.

### Pattern 3: Parent-owned spill directory

**What:** the parent creates one spill root and gives each worker a subdirectory. Workers write runs there and return paths. The parent deletes the root in a `finally`.
**Why:** `ExternalSorter` today owns a `tempfile.TemporaryDirectory`. If a worker owns it, the runs vanish when the worker object is collected or the worker exits, before the parent reads them. Interrupts end workers with `terminate_workers`, so parent-side cleanup is the only reliable one.
**Trade-offs:** one more parameter on `ExternalSorter`. No new dependency.

### Pattern 4: Injected callable to keep a leaf a leaf (for #227)

**What:** `CheckpointSession.open(..., summarize=report.summary_dict)`. `resume.py` never imports `report`.
**Trade-off, recorded in #227:** the stats-to-summary contract moves from an import edge a grep finds to a callable passed at one call site. Acceptable. Add a test that pins the checkpoint bytes against one stats object (not two pipeline runs: per-file timing floats differ).

## Data Flow

### `lintle clean` after #226 and #227

```
cli.main -> _dispatch -> clean_run.run(args)
   |
   +-- input checks (jobs, threshold, path, discover, stat)        -> return 2 on error
   +-- mkdir out_dir; fsutil.out_dir_lock (ExitStack)              -> return 2 on LockHeldError/OSError
   +-- run_planning.resolve_clean_plan -> RunPlan                  -> plan.exit_code -> return
   +-- session = resume.CheckpointSession.open(out_dir, plan.inputs, plan.completed,
   |                                           plan.run_identity, summarize=report.summary_dict)
   +-- worker_pool.run_workers(config, files, plan, jobs, console, sizes, session)
   |      per finished file: progress.file_done(stats); session.record(path, stats)
   |      session.record raising OSError -> operational_error -> KeyboardInterrupt path
   +-- result.operational_error -> join_exit_code -> 2             (no finalize)
   +-- result.interrupted       -> join_exit_code -> 128+signo     (no finalize, state kept)
   +-- _finalize_run: sort stats, envelope, output_artifacts.write_clean_artifacts,
   |                  render (json | files table + panel)
   +-- if not failed_files: session.delete() + remove .shards
   +-- return join_exit_code(plan verdict, op error, interrupt, failed_files, quarantine code)
```

Two points for planners:

- **Order of effects is not order of precedence.** Operational error and interrupt return before `_finalize_run`. Failed-file and quarantine verdicts are decided after it. `join_exit_code` maps outcomes to a code. `run()` keeps the early returns. Table-test the join. Keep integration tests for the effect order (artifacts written on failed-file runs, state kept on failure and interrupt, state deleted only on full success).
- **Exit 1 stays reserved for the quarantine gate.** A `session.record` failure must still end as exit 2 (issue #89). Add a test that injects `OSError` from `write_checkpoint` and asserts 2.

Candidate shape of the pure function:

```python
def join_exit_code(*, plan_exit: int | None, operational_error: bool,
                   interrupted_signo: int | None, failed_files: bool,
                   quarantine_code: int) -> int: ...
```

### `lintle verify` and `dedup` after #222

```
cli (verify/dedup arm, --jobs) -> _locked_postrun -> verify.run / dedup.run
   scan.cleaned_stems_or_error -> stems (sorted order = merge order)
   scan.scan_cleaned: fingerprint (stat-only), UnitTable (parent)
   jobs == 1 ?  -> serial path (today's code, kept for tests and small runs)
   else:
     parent: spill_root = mkdtemp; slots = [None] * len(stems)
     pool_core.run_pool(stems, stem_scan.scan_stem, jobs=jobs, ...)
        worker (per stem, own process):
           aligner = SourceAligner.open(source_dir, stem)   # verify only
           for rec in records.iter_file(out_dir, stem):
               verify: revalidate -> suspect sink (worker-local, spills) | sorter.add + histogram + population + aligner.feed
               dedup : skip if (src_file, index) in this stem's hard subset, else sorter.add
               progress msg -> manager queue -> parent UnitTable.update
           sorter.seal() -> run paths in spill_root/<stem>/ ; return StemResult
        parent on_result: slots[index_of(stem)] = result; table.finish(stem, ...)
     parent after all stems (serial, unchanged semantics):
        merge run sets in stem order -> find_conflicts (verify) | _write_import_set (dedup)
        merge commutative accumulators: histogram Counter, population set, missing_source, per-stem hard/soft
        SuspectSink: suspect run sets merged in stem order, then conflicts, then orbit pass
     finally: shutil.rmtree(spill_root)
```

What stays serial: contradiction pass, the orbit pass, dedup's collapse-and-write, the `SuspectSink` write. Speedup is Amdahl-bounded by the serial tail and by the largest stem (3.2 GB of 30.4 GB, so about 9.5x ceiling at best).

### State Management

- No shared in-memory state between commands. Unchanged.
- `RunPlan.completed` (mutable dict mutated by `worker_pool` today) moves under `CheckpointSession`. `worker_pool` stops mutating the plan. The cancel message count (`len(plan.completed)`) reads from the session.
- Worker progress for `verify`/`dedup` needs a worker-to-parent channel. Reuse the clean path's shape: a `multiprocessing.Manager` queue drained in the parent. `UnitTable.update(stem, ...)` is the sink.

## Scaling Considerations

The relevant scale is corpus size and core count, not users. Measured baseline: 30.4 GB, 232,380,554 records, `verify --source --orbit` 2 h 05 m on 1 core, `dedup` 48 m 50 s on 1 core.

| Concern | Today (1 core) | After #222 (N workers) |
|---------|----------------|------------------------|
| Sorter memory | one chunk, 200,000 records, RSS about 165 MB | N chunks. Per-worker chunk must shrink to about 200,000 / N so total stays flat (PROJECT constraint). |
| Spill run count | about 1,160 runs (232M / 200k) | More runs if chunks shrink. See fd-limit risk below. |
| Spill disk | `$TMPDIR`, outside the disk guard (CONCERNS.md) | Same total volume, more parallel writes. Guard gap stays open. |
| Largest-stem tail | not applicable | One 3.2 GB stem is about 10% of bytes. It bounds the parallel phase. |
| Serial tail | included in 2 h 05 m | Contradiction pass, orbit pass and writes stay serial. Share not measured. |

### Scaling Priorities

1. **File-descriptor limit in the final merge.** `ExternalSorter.sorted_records` opens one handle per run at once. With per-worker chunks shrunk by N, run count grows by about N. This machine reports `ulimit -n` 1,048,576, so no failure is visible here. A default macOS shell is typically 256 (assumption, not verified on the maintainer's other Macs). Recommended design: each worker merges its own spills into one sorted run per stem before returning, so the parent merges about 29 files. Cost: one extra read and write pass per worker, done in parallel. Needs a spike to measure against the plain approach. Treat as the main open design question of #222.
2. **Size the serial share before promising a speedup.** The issue measured one core at 97% during the scan phase, but did not split `verify --source --orbit` into scan, contradiction, orbit and write. The orbit pass is sampled and is a separate pass. If it is a large share of the 2 h 05 m, #222 alone will not move the headline number. Profile phases first. The same holds for `dedup`: its per-record scan work is light (a set lookup), so the gain may be small there.

## Anti-Patterns

### Anti-Pattern 1: Returning record lists from workers

**What people do:** `return [rec for rec in stem]` or return a populated sorter by pickle.
**Why it's wrong:** a 3.2 GB stem holds about 23M records. It breaks Critical Rule #3 and the pickle boundary.
**Do this instead:** workers spill to the parent-owned dir and return run paths plus small counters.

### Anti-Pattern 2: Merging in completion order

**What people do:** `as_completed` and append results as they arrive.
**Why it's wrong:** ties between equal `(catalog, epoch)` in different stems then depend on timing. `suspects.*.jsonl` and `import.*.txt` stop being byte-deterministic.
**Do this instead:** index results by stem position. Merge in that order.

### Anti-Pattern 3: Reusing `worker_pool.run_workers` unchanged for verify

**What people do:** pass a different function into the existing pool.
**Why it's wrong:** it hardcodes `pipeline.process_file`, `FileStats`, `ProgressDisplay` and (until #227) checkpoint writes. Verify would import `pipeline`, `report` and `resume` for no reason.
**Do this instead:** extract `pool_core` in #222, after #227 removes the checkpoint knowledge.

### Anti-Pattern 4: Putting orbit work in the workers

**What people do:** move the `--orbit` pass into the per-stem workers to use the cores.
**Why it's wrong:** `sgp4` would then be imported in a worker module that the pool shares with the default path. It also needs the global sorted satellite view, which a per-stem worker does not have.
**Do this instead:** keep `orbit.py` as the only `sgp4` importer, lazy, in the parent, after the scan. Parallelising the orbit pass is a separate issue.

### Anti-Pattern 5: Injecting the run stamp into the sidecar (#223)

**What people do:** thread the run timestamp through `CleanConfig` to `BrokenFileWriter`.
**Why it's wrong:** it makes stamps uniform inside one run, but two runs over the same input still differ. The invariant stays false. Cost measured in #223: 5 files, about 8 signatures.
**Do this instead:** drop `generated:` from the sidecar header. Keep the clock only in `report.json` and `report.md`, both fed by the envelope's `run.timestamp`.

### Anti-Pattern 6: `verify` importing `clean_run`

**What people do:** import `resolve_jobs` from wherever #226 put it.
**Why it's wrong:** the auditor then depends on the full clean orchestration. It blurs the one-way graph.
**Do this instead:** put `resolve_jobs` in a pool module (see above).

## Integration Points

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| `cli` -> `clean_run` | `run(args) -> int` | Module-level import. `clean_run` joins the clean-path closure. Add it to the seeds of `test_clean_path_closure_never_imports_sgp4_or_verify` so the walk is explicit, not incidental. |
| `clean_run` -> `worker_pool` | `run_workers(config, files, plan, jobs, console, sizes, session)` | `session` is the one new argument (#227). |
| `worker_pool` -> `resume` | None after #227 | Import removed. Check with grep. |
| `clean_run` -> `resume` | `CheckpointSession.open` / `delete` | `summarize=report.summary_dict` passed here. |
| `worker_pool` -> `pool_core` | function call | After #222. |
| `verify.scan` -> `pool_core` | function call | `verify -> pool_core` is allowed. `pool_core -> verify` is not. |
| `pool_core` -> anything lintle | `process_control`, `term` only | Keeps it on both sides of the wall. |
| `verify.stem_scan` -> `verify.checks`, `records`, `grouping`, `report`, `tle`, `repair` | direct | Same edges `verify.run` has today. |
| `dedup` -> `verify.scan` | `scan_cleaned` + parallel path | Dedup passes a per-stem hard-suspect subset to each worker, not the whole set, to keep the pickle small. |
| `dedup`, `extract` -> `history.stats_row`, `tle.RECORD_BYTES` | direct | Both leaves are `sgp4`-free. `extract` reaches `tle` already via `verify.checks`. |
| `output_artifacts` -> `report.format_run_report` | `generated_at` from `envelope["run"]["timestamp"]` | #223. No change to `cli`/`clean_run` call sites. |

### Import-graph tests

| Test | Effect of this milestone |
|------|--------------------------|
| `test_clean_path_closure_never_imports_sgp4_or_verify` | `clean_run` and `pool_core` fall inside the closure. Both must stay free of `verify`, `extract`, `sgp4`. Add them to the seeds. |
| `test_verify_submodules_are_sgp4_free_except_orbit` | Globs `verify/*.py`. A new `stem_scan.py` is covered with no edit. It must not import `orbit`. |
| `test_extract_closure_never_imports_sgp4` | Unchanged. `extract -> history`, `tle` stay clean. |
| `test_epoch_leaf_is_stdlib_only` | Unchanged. `history` change must not make `epoch` import anything. |
| New (recommended) | Pin `pool_core` to imports from `{process_control, term, __init__}` plus stdlib. Pin `resume` and `history` leaf status: `resume` never imports `report`. |

Caveat on the existing walker: `_module_level_imports` collapses any `lintle.verify.X` import to the single name `verify`. It cannot see which `verify` submodule a clean-path module reached. The sweep test is the only guard for submodules.

## Build Order

Dependency facts:

- #226 and #227 both edit the clean tail. #226 moves it verbatim. #227 edits it in a few places. Moving first and editing second gives the smaller merge conflict surface.
- #227 removes `resume`/`report` knowledge from `run_workers`. #222's pool split depends on that being gone.
- #226 gives #222 a place for `--jobs` finalize work and removes the 297-line `_finalize_run`/tail from the file #222 also touches (`cli.py` parser arms).
- #228 and #222 both edit `verify/scan.py` (the `RECORD_BYTES` line) and `dedup.py`. The #228 edit is tiny. Land it first.
- #223 touches `report_writers.py`, `report.py`, `output_artifacts.py`, `tests/test_freeze.py`, `ARCHITECTURE.md` §6. It does not touch `cli.py`, because `write_clean_artifacts` already receives the envelope. It is independent of the other four.
- The #223 decision (qualify the invariant vs drop the stamp) must be taken before coding. The #223 comment gives a verified resolution: drop the stamp. A checkpoint from an older lintle cannot be resumed anyway (`validate_run_identity` checks `lintle_version`), so no mixed-header resume case exists.

Suggested order:

| Step | Issue | Why here | Size | Parallel with |
|------|-------|----------|------|---------------|
| 1 | #228 | Leaf-only, byte-identical, touches `scan.py` before #222 does. | small | #223 |
| 1 | #223 | Independent. Needs the drop-vs-qualify decision first (recommend drop). Narrow the freeze test canonicalisation so the clock cannot return unseen. | small | #228 |
| 2 | #226 | Largest mechanical move. Everything on the clean tail then lands in a named module. | medium | none (touches `cli.py` heavily) |
| 3 | #227 | Edits `clean_run.py`, `worker_pool.py`, `resume.py`. Tests retarget to `clean_run`/`resume`. | small-medium | none |
| 4 | #222 | Needs #227 (clean `run_workers`) and #226 (named home for jobs). Contains the `pool_core` extraction. | large | none |
| 5 | Doc drift | CONTRIBUTING.md (Python 3.14, `sgp4` verify-scoped), README.md, and the `ARCHITECTURE.md` §2 module map rows for every new module. Update `ARCHITECTURE.md` in the same PR as each change, per CLAUDE.md. | small | any step |

Steps 1a and 1b are independent worktrees. Steps 2 to 4 are strictly sequential. Land each as its own PR (rebase-and-merge, no version bump).

If #227 must land before #226: it still works. The #226 prototype then moves a slightly different tail. Expect a manual conflict on the teardown lines (`cli.py:598-603`).

### Per-step test seams

| Step | New direct tests | Regression guard |
|------|------------------|------------------|
| #228 | `stats_row` unit test; `span_days` on empty and single-epoch input; `RECORD_BYTES == 140` | `diff -r` over `04-verify/`, `05-dedup/`, `06-extract/` before and after |
| #223 | Sidecar header equals `# source: X \| lintle V`; two runs give byte-equal `02-broken/` | Freeze golden regenerates once with `LINTLE_FREEZE_UPDATE=1`; canonicalisation no longer applies under `02-broken/` |
| #226 | `join_exit_code` table (all precedence pairs); `clean_run.run` via fake pool | Existing 49 `cli.main` call sites untouched; 4 monkeypatch sites retarget |
| #227 | Checkpoint bytes pinned to one stats object; `record` raising `OSError` gives exit 2 | `write_checkpoint` monkeypatch still intercepts |
| #222 | Serial vs parallel `diff -r` on a multi-stem fixture with cross-stem duplicates; forced reverse completion; pool-core cancellation and failure; memory bound (worker chunk = budget / jobs) | #204 B tie-stability pin; both `jobs=1` and `jobs>1` run in CI |

## Open Design Questions for #222 (needs phase research)

1. Final-merge shape: parent merges all worker runs (simple, fd-heavy) or workers pre-merge to one run per stem (extra I/O, about 29 handles). Measure both. Decide before coding.
2. `SuspectSink` split: workers need a local sink with per-stem spill. The merged output must equal serial arrival order. `hard_by_stem`/`soft_by_stem` counters must merge into the parent sink for the table cells.
3. Progress channel for `UnitTable`: drain a manager queue in the parent poll loop or in a thread. Reuse what `ProgressDisplay` does. Not designed yet.
4. Default `--jobs` for `verify`/`dedup` and the interaction with `--orbit` memory. Recommend the same default as `clean` (CPU count - 1, capped by stem count).
5. Spill-dir location and the disk guard. Out of scope today (CONCERNS.md), but N parallel writers make the gap more visible.

## eli14 summary

**Analogy:** a post office sorting room with one clerk who sorts all the mail of 29 trucks, one truck at a time.

- **#226, #227, #228, #223** are housekeeping in the office: put a name on the "closing the office" checklist (#226), give the logbook to one clerk instead of three (#227), keep one copy of the label-size rule (#228), and stop stamping the clock time on parcels that should look identical every day (#223). Today's risk if skipped: small, mostly slower and riskier future changes. Nothing is wrong with the output now.
- **#222** hires more clerks. Each clerk sorts one truck into sealed bags and hands the bags over. One head clerk then merges the bags, always in truck order, so the final pile is identical to the one-clerk result. The risks are two: too many bags for the head clerk to hold at once (file handles), and the head clerk's own part staying slow (the serial tail). If the first is ignored it can crash a run on a machine with a low limit. If the second is ignored the work is done but the wait barely shrinks.

## Sources

- GitHub issues #222 (with its feasibility-spike comment), #223 (with its verified-resolution comment), #226, #227, #228 on `elfensky/lintle` (read 2026-11-02). Confidence: HIGH for the facts they state; prototypes were run by the issue author.
- Code read in this worktree: `src/lintle/worker_pool.py`, `verify/scan.py`, `verify/__init__.py`, `verify/grouping.py`, `dedup.py`, `history.py`, `resume.py`, `process_control.py`, `cli.py` (clean tail and `_finalize_run`), `tests/test_verify.py` (`TestImportGuard`). Confidence: HIGH.
- Repo-root `ARCHITECTURE.md` §2, §5, §6 and `.planning/codebase/ARCHITECTURE.md`. Confidence: HIGH.
- Not verified: fd-limit behaviour on the maintainer's other machines; the share of `verify --source --orbit` wall clock spent outside the scan phase; any speedup for `dedup`.

---
*Architecture research for: lintle subsequent milestone (#222, #223, #226, #227, #228)*
*Researched: 2026-11-02*
