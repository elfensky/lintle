# Domain Pitfalls

**Domain:** offline Python 3.14 CLI (lintle) with a deterministic external-sort pipeline; adding a parallel cleaned-tree scan (#222), resolving a determinism claim (#223) and three byte-identical refactors (#226, #227, #228).
**Researched:** 2026-11-02
**Overall confidence:** HIGH for pitfalls anchored in the repo code (read directly); MEDIUM for platform behaviour (Python 3.14 start methods, fd limits), which the plans should confirm with a one-line check.

Scope note: only pitfalls for the Active requirements. Generic multiprocessing advice is left out. Every item names the file or test that makes it real here.

---

## Critical Pitfalls

Mistakes that cause a silent output change, a constant-memory violation, or a rewrite.

### Pitfall 1: The freeze test cannot see the bugs #222 will introduce
**What goes wrong:** `tests/test_freeze.py` is the only whole-tree byte guard. It runs one source file (`tle2099.txt`, 4 records) with `--jobs 1`. That gives one stem, no spill, no cross-stem tie, no completion-order race. A parallel scan that merges in completion order, or breaks ties wrongly across stems, passes the freeze test unchanged. The same test also canonicalises timestamps, elapsed and versions with regexes, so it masks any new wall-clock leak.
**Why it happens:** The fixture was built to freeze schema and key order, not ordering under concurrency.
**Consequences:** `04-verify/suspects.*.jsonl`, `05-dedup/import.*.txt` and `manifest.jsonl` change only on a real corpus (multi-stem, many spills), in a way that looks valid. No test fails. The corpus run is a 3-hour job.
**Prevention:**
- Before any pool code, add a multi-stem fixture: at least 3 stems, the same catalog in several stems, 3-way and 4-way equal `(catalog, epoch_key)` ties split across stems, and `chunk_size` forced to 2-3 so each stem spills and ends with an in-memory tail.
- Assert `diff -r` equality (or the freeze map) of the whole `04-verify/` and `05-dedup/` trees for `jobs` in {1, 2, 4} crossed with two chunk sizes. Output must be a function of the input only: not of `--jobs`, not of chunk size, not of completion order.
- Force reverse completion order in a test (the #222 spike did this by hand). Use a latch or per-stem delay so the last stem finishes first.
- Keep the existing pin `test_equal_keys_keep_add_order_across_a_spill_boundary` (`tests/test_verify.py:487`) unchanged and green. It guards one sorter, not the cross-worker merge, so it is necessary but not sufficient.
**Detection:** A parallel PR that adds no new fixture with more than one stem. A diff to `freeze-e2e.golden.json` in a PR that is meant to be throughput-only.
**Phase:** #222 (test fixture is the first commit, before any pool code).

### Pitfall 2: Merging worker results in completion order, or ordering spill runs by anything but (stem index, run index)
**What goes wrong:** Tie stability today rests on one rule: equal keys come back in add order, because `heapq.merge` breaks ties by iterable order and runs merge in spill order, in-memory tail last (`verify/grouping.py:55-57`). With N workers, each stem has its own run list. The parent's final merge must feed `heapq.merge` the iterables in global add order: stem order first, then run order within the stem, then that stem's tail. Using `as_completed`, a directory glob, or a lexical sort of paths from several temp dirs breaks the rule. A lexical sort also breaks once run numbers pass the zero-pad width.
**Why it happens:** `as_completed` is the pattern already in `worker_pool.py`, and it is natural to copy. Output looks right whenever stems have no shared `(catalog, epoch_key)`, which is the common case.
**Consequences:** For re-issued records at the same epoch (17.5 M collapsed in the measured run), dedup picks a different "latest" record, or `SuspectSink` ties reorder. Both outputs stay valid-looking, which violates Critical Rule #2's spirit.
**Prevention:**
- A worker returns an ordered `list[Path]` of runs per stem. The parent keeps `futures` indexed by stem and consumes them with `future.result()` in stem order. Do not use `as_completed` for results.
- A worker must spill its final in-memory tail to a run file as the last run. The tail cannot be returned (see Pitfall 3).
- Sort runs by an explicit `(stem_index, run_index)` tuple, never by filename.
- Do not change the sorter's tie rule (add order). A new secondary key would change bytes for existing outputs.
**Detection:** Test from Pitfall 1 fails only with reverse-completion injection.
**Phase:** #222.

### Pitfall 3: Spill ownership: `TemporaryDirectory` finalizers delete runs before the parent reads them, and SIGTERM leaks them
**What goes wrong:** `ExternalSorter.__init__` creates `tempfile.TemporaryDirectory`. When a worker function returns, that object is garbage-collected and its finalizer removes the directory. The parent then opens run paths that no longer exist. The opposite failure also happens: `executor.terminate_workers()` (SIGTERM to workers) runs no finalizers, and `process_control.ignore_sigint` means Ctrl-C reaches only the parent. Worker spill dirs then leak under `$TMPDIR`, outside the disk guard, up to the full corpus size.
**Why it happens:** The sorter was written for one process that owns its temp dir for its whole life. Spill location is already an open concern (`CONCERNS.md`: spill under `$TMPDIR`, no disk guard).
**Consequences:** `FileNotFoundError` late in the run, or tens of GB left on the boot volume after an interrupt.
**Prevention:**
- The parent creates one spill root (one `TemporaryDirectory`, or a `<out-dir>/.spill-<pid>/` tree) and hands each worker a sub-path. Workers write runs there and never delete them. The parent removes the root in a `finally` on every exit path: success, `KeyboardInterrupt`, operational error.
- Give `ExternalSorter` an explicit "adopt this dir / do not clean up" mode rather than patching around the finalizer.
- Make the same change resolve the "spill outside the disk guard" concern: put the root next to the output tree and add its estimated size to `check_disk_space`.
- Test: interrupt a real 2-worker pool mid-scan, then assert the spill root is gone and no `lintle-verify-sort-*` directory remains in `tempfile.gettempdir()`.
**Detection:** Leftover `lintle-verify-sort-*` or `lintle-verify-suspects-*` directories after a test or a Ctrl-C.
**Phase:** #222.

### Pitfall 4: Per-worker spill budgets that multiply memory, then shrink so far that the merge fan-in explodes
**What goes wrong:** Two linked failures. (a) Each worker keeps `chunk_size=200_000` records and a second `SuspectSink` sorter, so peak memory becomes N times the current 165 MB (Critical Rule #3 and the milestone constraint "shrink, not multiply"). (b) Dividing the budget by N cures that but multiplies the run count by N. At `jobs=7`, 232 M records / 28 K per run is about 8,100 runs. The final `heapq.merge` keeps one open file handle plus one buffered decoder per run, so memory and file descriptors grow with the run count.
**Why it happens:** The budget is counted in records, not bytes (`CONCERNS.md`, "Chunk size is counted in records"), and fan-in is unbounded.
**Consequences:** Serial is already about 1,160 runs (232 M / 200 K). That exceeds a stock Linux soft limit of 1024 (`EMFILE` mid-merge) although it works on this Mac (`ulimit -n` here: 1,048,576). Fan-in of 8,000+ would fail on many hosts. Per-handle read buffers (about 8 KB each) then count against the memory budget too.
**Prevention:**
- Define the budget as a total (`TOTAL_SPILL_RECORDS`), with per-worker size = total / jobs, and write the formula down in `ARCHITECTURE.md` §Constant memory.
- Bound the merge fan-in: either a multi-pass merge (merge groups of K runs into one, K about 256-512) or `resource.setrlimit(RLIMIT_NOFILE, ...)` raised to the hard limit plus a clear error on `EMFILE`. A multi-pass merge temporarily doubles spill disk use, so count it in the disk estimate. Pick the multi-pass merge: it works on every host.
- Add a test with `chunk_size=2` over enough records to produce more runs than a lowered `RLIMIT_NOFILE` (set it low inside the test process) to prove the fan-in cap holds.
- Measure peak RSS (`resource.getrusage` for children) for jobs=1 and jobs=N on the multi-stem fixture and assert a ceiling that does not scale with N.
**Detection:** `OSError: [Errno 24] Too many open files`; RSS growing with `--jobs`.
**Phase:** #222 (design the budget before the pool).

### Pitfall 5: Shipping per-run state to workers by pickle: the `hard` set, closures, and anything not module-level
**What goes wrong:** `dedup` consults `hard = _load_hard_positions(out_dir)`, a set of `(src_file, index)` for every hard suspect, inside the record loop. Passed as a task argument to each worker it is pickled once per task. With 29 tasks it multiplies across workers and breaks constant memory when a verify run found many suspects. The `cells=`/`totals=` lambdas in `scan_cleaned` are also unpicklable, so a refactor that "just submits `sc.stream`" fails, but only under a real pool.
**Why it happens:** Python 3.14 uses `spawn` on macOS and `forkserver` on Linux (not `fork`). Nothing is inherited from the parent. Everything crosses by pickle and workers re-import modules. A fake in-process executor (the style used in 18 places in `test_cli.py`) hides every pickling failure.
**Consequences:** Works in tests, crashes or balloons on the corpus; or works on Linux CI and fails on macOS (or the reverse) if any code relied on fork inheritance.
**Prevention:**
- The worker function is a module-level function in a `verify/` module that imports only `tle`, `epoch`, `chunking`, `verify.records`, `verify.checks`, `verify.grouping`. It takes plain data (paths, ints, small dicts).
- Load shared read-only state once per worker through the pool `initializer`, or give each worker only its stem's slice of `hard`. Never send the whole set per task.
- Add at least one test with a real `ProcessPoolExecutor(max_workers=2)` per command (verify, dedup), tagged so it is a handful, not the bulk. Inline fakes stay for the rest. Run these on the platform's default start method (do not pin `fork`).
- CI is Ubuntu only (`CONCERNS.md`). The project owner develops on macOS, where the default is `spawn`. Add a macOS job for the parallel tests, or accept and document that macOS is covered only by manual runs.
**Detection:** `PicklingError`/`AttributeError: Can't get attribute` in a real-pool test; memory growth proportional to workers in dedup.
**Phase:** #222.

### Pitfall 6: A second, copy-pasted copy of `run_workers` signal choreography
**What goes wrong:** `worker_pool.run_workers` encodes hard-won rules (issue #100): save all three handlers before installing, convert SIGTERM/SIGHUP to `KeyboardInterrupt`, silence all three signals the moment teardown starts, `terminate_workers()` outside the handler, restore in `finally`, and `ignore_sigint` in workers. Cloning it for verify/dedup produces two versions that drift. The #222 comment already says the right shape is a generic pool core with `clean` and `verify` as adapters, extracted as part of #222 and after #227.
**Why it happens:** `run_workers` hardcodes `pipeline.process_file`, `FileStats`, `ProgressDisplay` and checkpointing, so reuse looks harder than copy.
**Consequences:** A second signal during teardown aborts cleanup in one command only. Exit-code semantics leak between commands: clean's `1` means "quality gate", verify's `1` means "hard suspects", and `operational_error` currently converts to a `KeyboardInterrupt` path with `interrupted=True` set as well.
**Prevention:**
- Extract the pool core only after #227 lands, and only the parts that do not depend on the command: signal save/trap/restore, executor lifecycle, ordered result collection, failure capture. Pass the command's differences in (worker fn, result handler, progress adapter).
- Keep exit-code policy out of the core. Each command maps `(interrupted, signo, operational_error, failed)` to its own code. Define the interrupted-verify exit code deliberately (`128+signo`, as clean does) and test it.
- Re-run the existing issue #100 tests unchanged against the core. Add the same interrupt test for verify and dedup.
**Detection:** Two files containing `signal.signal(signal.SIGHUP`. A PR that edits `worker_pool.py` tests to pass.
**Phase:** #222, sequenced after #227 and #226.

### Pitfall 7: Parallelising the wrong phase (Amdahl and unmeasured claims)
**What goes wrong:** The measured 2 h 05 m is `verify --source --orbit` as a whole. The archived run has no per-phase split. It contains the per-stem scan (revalidate, source aligner), a serial k-way merge plus `find_conflicts`, the serial sgp4 orbit pass, and the writes. The issue's own spike says phases C and D stay serial and speedup is bounded by the largest stem (3.2 GB of 30 GB). For dedup the per-record scan work is a set lookup and `sorter.add`, so the 48 m is likely dominated by sort/merge and chunk writes, which stay serial.
**Why it happens:** "97% CPU on one core during the scan phase" was observed, but the scan phase's share of the total was not recorded.
**Consequences:** A large refactor that moves verify from 2 h 05 to perhaps 1 h 30, and dedup barely at all, with fresh complexity (spill ownership, fan-in, signals) to maintain.
**Prevention:**
- First commit of the phase: add per-phase wall-clock timing to the unstyled stderr phase lines (they already exist from #221) and take one corpus measurement, with and without `--orbit`.
- Decide per command from the numbers. Start with verify's scan only. Treat dedup as optional until its split is known.
- Keep `--orbit` out of workers: the sgp4 wall and the "separate pass" statement in the issue stand.
- Consider whether splitting only the largest stem is worth the cost; default is no.
**Detection:** The first profile shows the merge or the orbit pass above 50% of wall time.
**Phase:** #222 (measure first).

### Pitfall 8: `--jobs` default changes behaviour, runs, and tests for everyone
**What goes wrong:** If `verify`/`dedup` default to CPU-1 workers (as `clean` does), every `cli.main(["verify", ...])` in the suite (`test_freeze.py`, `test_verify.py`, others under `-n auto`) spawns pools inside pytest-xdist workers. Spawn start-up imports `rich`, `humanize`, `lintle` per process (hundreds of ms), and CPU is oversubscribed.
**Prevention:** Default the new flag conservatively (serial, or parallel only above a stem-count/size threshold) and let `--jobs 1` run the same code path in-process, not a second serial implementation. One code path is what makes serial-versus-parallel byte equality testable. Reuse `resolve_jobs` (move it with #226) rather than a second parser. Keep fixtures with one stem on the serial path.
**Phase:** #222.

---

## Refactor Pitfalls (#226, #227, #228): byte identity and the import-graph tests

### Pitfall 9: Passing tests that no longer test anything after a move (`monkeypatch` retargeting)
**What goes wrong:** #226 says 4 monkeypatch sites plus 1 import in `tests/test_cli.py` need retargeting, "only because cli stops importing those modules". If `cli` still imports `shutil`/`os` for another reason, `monkeypatch.setattr(cli.shutil, ...)` still succeeds. It patches the module attribute globally (`shutil` is one object), so it may still intercept. But patches of `cli.<name>` for a name that moved raise `AttributeError` only with `raising=True`. Where a patch targets a name now imported by value into `clean_run`, the patch succeeds on `cli` and `clean_run` keeps the real function. The test then exercises the real filesystem or the real pool and passes.
**Prevention:** For every retargeted patch, first make it fail: run the test with the patch pointing to the old location and confirm failure, or assert the fake was called (`calls == [...]`). Patch with `raising=True` always. Patch where the name is looked up (`clean_run.worker_pool.run_workers`), not where it is defined. The #227 prototype note that `CheckpointSession` looks `write_checkpoint` up at call time keeps `resume.write_checkpoint` patches working; keep that property and test it.
**Phase:** #226, #227.

### Pitfall 10: The exit-code join silently changes precedence because the inputs overlap
**What goes wrong:** #226 lists precedence as plan verdict, operational error (2), interrupt (128+signo), failed file (2), quarantine gate. The inputs are not independent. In `run_workers`, a parent-side operational error is raised as `KeyboardInterrupt`, so `interrupted=True` and `interrupted_signo=SIGINT` are set whenever `operational_error` is set. A pure `join_exit_code(plan, op_error, interrupted, signo, failed, gate)` that tests `interrupted` before `op_error`, or that drops the "both set" case, returns 130 where today returns 2. Five tests built from one-flag-at-a-time rows will not catch it.
**Prevention:** The table test must be exhaustive over the cross product of the booleans (2^5 rows is cheap) and assert against values recorded from the pre-refactor `main` (generate the table by running the old code, not by reasoning). Include the row `operational_error and interrupted(SIGINT)` and the row `interrupted(SIGTERM) and failed_files`.
**Phase:** #226.

### Pitfall 11: Moving orchestration changes the order of side effects
**What goes wrong:** The clean tail has sequencing invariants with no named owner: preflight, then out-dir lock, then scrub (and marker write), then plan resolution, then pool run, then envelope, then artifact-or-render branch, then checkpoint and `.shards` teardown only on full success (`cli.py:598-603`), then lock release. Moving verbatim is safe. Tidying while moving ("early return", pulling the lock into a context manager, reordering teardown before render) alters what survives an interrupt. Teardown that runs on an interrupted or failed run deletes the checkpoint, so `--resume` has nothing to resume. This pairs with the known open items: `scrub_outputs` swallows errors, and the ownership marker is written before the run.
**Prevention:** Commit 1 is a pure move with no edits beyond imports (`git diff --color-moved` should show it as moved). Reformatting and renames are separate commits. Add tests that pin the post-state per exit class: after interrupt the checkpoint exists; after full success it is gone; after a failed file it is kept. Do not fix the scrub or marker concerns inside this refactor; each changes behaviour and belongs in its own PR.
**Phase:** #226.

### Pitfall 12: The import-graph test cannot see what it is supposed to guard
**What goes wrong:** `test_clean_path_closure_never_imports_sgp4_or_verify` (`tests/test_verify.py:708`) walks module-level imports from the seeds `{"cli","pipeline","repair","tle"}`. Three blind spots matter here:
1. A new `clean_run.py` is covered only because `cli` imports it at module level. If it is imported inside a function (lazy), the walk never reaches it and the wall is unguarded. The `_module_level_imports` helper is named for this.
2. The walker collapses any `lintle.verify.X` import to the name `verify`. It stops there, so a clean-path module importing `verify.orbit` is caught only because `verify` itself is forbidden by name. A new parallel worker module under `verify/` is checked only by the separate sweep (`test_verify_submodules_are_sgp4_free_except_orbit`), which covers module-level `sgp4` imports only.
3. Moving `RECORD_BYTES` into `tle.py` (#228) is safe, but moving `resolve_jobs`/`discover_paths` between `cli`, `clean_run` and a shared pool module can create `verify -> cli` or `worker_pool -> verify` edges that no test forbids. The wall is `clean -> sgp4/verify`; the new generic pool core is used by both sides and must import neither.
**Prevention:** Add `clean_run` (and any new pool core module) to the seeds explicitly. Add one assertion that the pool core and `clean_run` exist in the walked set (a test that fails if the seed file is missing, so a rename does not make it vacuous). Keep new imports at module level. Extend the `verify/` sweep to forbid a module-level import of `verify.orbit` from any other `verify/` module. Run the wall test as the first check after every moved line, not at the end.
**Phase:** #226 (seeds), #222 (pool core and worker module).

### Pitfall 13: "Byte-identical" proved on a toy fixture, then regenerating the golden to make it pass
**What goes wrong:** The prototypes were verified by `diff -r` over `04-verify/`, `05-dedup/` and `06-extract/`. #228 rewrites rounding and None-guard ladders. Edge classes decide bytes: a satellite with one record (no median spacing, span 0), count 0, two records at identical epochs (gap 0, span 0), and values that round to `-0.0` or print as `1e-06`. The freeze fixture has 3-4 records of one satellite and none of these. When a refactor trips the freeze test the quickest "fix" is `LINTLE_FREEZE_UPDATE=1`, which erases the guard.
**Prevention:**
- Rule for #226/#227/#228 PRs: `tests/fixtures/freeze-e2e.golden.json` must show zero diff. Make the PR template or review checklist say so.
- Before refactoring `history`/`dedup`/`extract` rows, add a fixture with each edge class and pin the manifest line and sidecar JSON bytes, taken from the pre-refactor code (tests first, as the repo workflow says).
- In #228 keep raw values in `HistoryStats` and round only inside `stats_row`. If `span_days` is stored rounded, other consumers of the stats see a different number.
- Preserve `stats_row` key order exactly as the manifest authors it. The manifest serialises by insertion order; only the sidecar is protected by `sort_keys`. Add a test that asserts `list(stats_row(...))` equals the 8 keys in the current order.
**Phase:** #228 first (smallest, sets the pattern), then #227, #226.

### Pitfall 14: `CheckpointSession` loses the shared-mutable `plan.completed` side channel
**What goes wrong:** Today `run_workers` mutates `plan.completed` in place, and the cancel message uses `len(plan.completed)` as the "files done" count that tells the operator whether resume will skip work. If `CheckpointSession.open(..., completed, ...)` copies the dict (a natural "defensive" change), `plan.completed` stops growing and the message reports `0/N done` while the checkpoint holds many. Order also matters: today the entry is added before the write, so a failing `write_checkpoint` (ENOSPC) leaves `plan.completed` ahead of disk and raises the operational-error path. Reversing the order changes which state survives.
**Prevention:** The session must own the dict that `RunPlan` exposes (share the reference, or make `RunPlan.completed` a read view of the session). Keep "mutate then write". Pin the bytes with one stats object (as #227 says, not two pipeline runs: timing floats differ). Add tests for the cancel-message count after a mid-run interrupt, and for the ENOSPC path returning exit 2. Keep `delete()` teardown only on the success path (Pitfall 11). Do not change the read side or add resumability to verify.
**Phase:** #227.

### Pitfall 15: A summary-builder callable hides a contract the import test used to show
**What goes wrong:** #227 injects `report.summary_dict` into `CheckpointSession.open` because `resume.py` must not import `report`. The stats-to-summary contract then lives in a callable that grep and the import graph cannot see. A later `FileStats` field change alters the checkpoint bytes with no failing import.
**Prevention:** The bytes-pin test from Pitfall 14 is the guard, so it must use a `FileStats` with every field non-default (including a repaired and a quarantined entry). Record the callable contract in `ARCHITECTURE.md` next to the module map row.
**Phase:** #227.

---

## Determinism Pitfalls (#223)

### Pitfall 16: Fixing the sidecar stamp but leaving other wall-clock sources and half-edited docs
**What goes wrong:** The invariant is stated in `CLAUDE.md`/`AGENTS.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`, `CHANGELOG.md` and code comments (`dedup.py` docstring: "Output bytes are deterministic"). Option 1 (qualify) edited in one place leaves the others claiming absolute determinism. Option 2 (drop the stamp) fixes `<stem>.NNNNN.broken.txt` only. `utc_stamp()` also feeds `report.md` (`- Generated:`) and `report.json` (`run.timestamp`), the checkpoint holds a run-start stamp, and `freeze` canonicalises `elapsed_seconds` and `records_per_sec`. In the sidecar the stamp is taken per file at open time, so it differs between files inside one run, and a resumed run differs from a fresh run. Dropping the stamp also changes the header size, which is stored in the checkpoint's per-chunk `output_sizes`; check how a checkpoint from an older version is treated on `--resume`.
**Why it happens:** A property stated absolutely in several documents, with an exception encoded in a test helper (`_strip_generated` in `tests/test_run_planning.py`) and a regex list in `test_freeze.py` instead of in the written rule.
**Prevention:**
- Decide first (it is a decision gate in PROJECT.md). Recommendation: option 2 plus a one-line qualification. Drop the stamp from the sidecar header, keep `report.json`/`report.md` as the only places with a run clock, and state the invariant as "byte-deterministic except `report.json run.timestamp`, `report.md Generated`, and timing fields". This makes the stronger claim true for the sidecar and the written rule exact.
- Grep for every statement of the invariant (`grep -rn "byte-determin"` outside `data/`, `.planning/` and `docs/superpowers/archive/`) and edit all hits in one commit.
- Land #223 first and alone: its golden diff (the stamp line only, in each sidecar) is then a clean, reviewable change, and the later refactor PRs can claim "zero golden diff".
- Add a test that runs `clean` twice on one input and compares the whole tree after only the documented timing/timestamp canonicalisation. Remove `_strip_generated` if `report.md` stays the only user. Also test a resume-vs-fresh sidecar equality.
- Check checkpoint compatibility explicitly: `lintle_version` is in the checkpoint, and the sidecar size is recorded. Write down what happens on `--resume` across the version that drops the stamp.
**Detection:** Two identical runs whose `diff -r` shows more than the documented files.
**Phase:** #223 (first, before refactors).

### Pitfall 17: Introducing new nondeterminism through the parallel scan's own bookkeeping
**What goes wrong:** Merging worker results touches several accumulators. `Counter` first-seen key order follows merge order (completion order if merged as results arrive). `verify/report.py` guards `summary.json` with `sort_keys=True`, but `CLAUDE.md` warns that other output boundaries rely on first-seen order. `missing_source`, `population`, the month histogram and the per-stem suspect counts all merge commutatively as values. The hazard is in anything serialised in insertion order. Progress lines are another source: #221's off-TTY per-unit lines would print in completion order, and the "unstyled stdout/stderr is byte-deterministic" rule would then break for those lines.
**Prevention:** Merge every accumulator in stem order, not arrival order, even when addition is commutative. Emit per-stem progress lines in stem order off a TTY (buffer until the earlier stems finish) or print them with the stem index and document that the per-unit lines are exempt. Keep wall-clock and rate values out of any structured output. Cover with the Pitfall 1 tree-equality test, including `summary.json`, `summary.md`, and the captured off-TTY progress text.
**Phase:** #222.

---

## Moderate Pitfalls

### Pitfall 18: `Scan` is a serial generator API and verify's work is interleaved inside the loop
**What goes wrong:** `Scan.units()` / `Scan.stream()` yield records into the caller's loop while updating `n_records`, `_file_records` and table rows. `verify` runs `revalidate`, the aligner, histogram and sorter adds in that loop body. Making it parallel means the per-record body moves into a worker, so the `Scan` interface changes shape (units in, per-stem results out). Two drivers (the old generator for `--jobs 1`, a new pool driver) double the surface and let the serial path rot.
**Prevention:** Change `Scan` once: per-stem result objects (`StemResult`: run paths, counts, suspects spill, histogram, population, missing flag) produced either in-process or in a worker by the same function. Both `verify` and `dedup` consume it. Update `RECORD_BYTES` use (progress math) to read from `tle.py` after #228 so the two do not diverge.
**Phase:** #222, after #228.

### Pitfall 19: Suspects crossing the process boundary unbounded
**What goes wrong:** Returning a worker's suspect list by pickle breaks constant memory on a badly corrupted corpus; today `SuspectSink` spills at 200 K. The `Suspect` dataclasses pickle fine (the spike checked), but size is the issue.
**Prevention:** Each worker owns a `SuspectSink`-style sorter spill and returns run paths plus counters. The parent replays the add into the final sink in stem order, or merges runs with the same `(stem index, run index)` rule as Pitfall 2. Attribution counters (`hard_by_stem`, `soft_by_stem`, `hard`, `total`) merge by value.
**Phase:** #222.

### Pitfall 20: Live table and progress from several processes
**What goes wrong:** `clean` uses `multiprocessing.Manager().Queue()` and a drain thread that swallows all exceptions (`CONCERNS.md`, silent drain loop). On Ctrl-C the Manager server process shares the terminal's process group and may die, so the proxy raises inside the drain thread and progress goes dark without a message. Copying this for `UnitTable` carries the silent failure over.
**Prevention:** Use the same message shapes where possible; log the first drain failure once through `term.warning`; test that progress stops cleanly after an interrupt. Per-stem rows need worker-to-parent messages at the `REFRESH_EVERY` (50 000) cadence only, not per record.
**Phase:** #222.

### Pitfall 21: Doc and CI drift gets re-touched by refactors
**What goes wrong:** The "doc drift" item (Python 3.11+ in CONTRIBUTING.md; `sgp4` called dev-only) is easy to fold into a refactor PR and mix with module-map edits in `ARCHITECTURE.md` §2 (new rows for `clean_run`, `CheckpointSession`, `stats_row`, `RECORD_BYTES`, the pool core). Mixed commits make the "zero behaviour change" review harder.
**Prevention:** Do the doc-drift fix as its own docs commit with #223's invariant wording, since both touch the same files. Each refactor PR updates only its own `ARCHITECTURE.md` rows, in the same PR (the repo rule: keep it current).
**Phase:** #223 batch; each refactor updates its own row.

---

## Minor Pitfalls

### Pitfall 22: `RECORD_BYTES` derived constant loses its pin
**What goes wrong:** `RECORD_BYTES = 2 * (LINE_LENGTH + 1)` is correct only for LF line ends. It also feeds `extract`'s torn-chunk check (`size % RECORD_BYTES`) and the progress math. If anyone changes `LINE_LENGTH`, the value moves with it, which is correct for the validator but silently wrong for existing chunk sets on disk.
**Prevention:** Add `assert tle.RECORD_BYTES == 140` as a pinned test next to the existing line-length tests, and keep one import path (`from lintle.tle import RECORD_BYTES`) in both `verify/scan.py` and `extract.py`. Do not leave a re-export alias in the old homes, or a third "definition" appears.
**Phase:** #228.

### Pitfall 23: Dependabot PRs (#238, #239) landing mid-series
**What goes wrong:** A `rich`/`humanize` or `setup-uv` bump merged between the byte-identity refactors changes terminal output, and a failing freeze test is then misattributed to the refactor. Structured output is unaffected, but the live-table tests are not.
**Prevention:** Merge or park the two Dependabot PRs before starting the refactor series, so each later diff has one cause.
**Phase:** before #228.

### Pitfall 24: Verification claims
**What goes wrong:** Reporting "byte-identical" from `diff -r` on a toy tree, or "all tests pass" without output.
**Prevention:** Per `CLAUDE.md`, report the real output of `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`. For #222, one corpus-scale equality check (serial vs parallel `diff -r` of `04-verify/` and `05-dedup/`, about 3 hours serial) is the real acceptance test, not an optional extra. If skipped, say so.
**Phase:** all.

---

## Phase-Specific Warnings

| Phase topic | Likely pitfall | Mitigation |
|-------------|---------------|------------|
| #223 sidecar stamp / invariant wording | Half-edited docs; other timestamps remain; checkpoint size compatibility (P16) | Decide first, grep every statement of the invariant, land alone with a golden-only diff |
| #228 history reduction | Edge-class bytes (single record, count 0, equal epochs); key order; rounding location (P13); constant pin (P22) | Pin edge fixtures from old code first; round in `stats_row` only; assert key order |
| #227 CheckpointSession | Lost shared `plan.completed`; write/mutate order; hidden summary contract (P14, P15) | Session owns the shared dict; bytes-pin on a full `FileStats`; cancel-message count test |
| #226 clean_run extraction | Vacuous monkeypatch retargets; precedence of overlapping inputs; side-effect order; unguarded new module (P9-P12) | Exhaustive `join_exit_code` table from old code; pure-move commit; add seeds to import-graph test |
| #222 measurement | Parallelising a phase that is not the bottleneck (P7) | Per-phase timing and one corpus profile before pool code |
| #222 test scaffolding | Freeze test blind to ordering (P1) | Multi-stem, forced-spill, reverse-completion fixtures first |
| #222 spill design | Finalizer deletes/leaks runs; budget multiplication; fan-in; disk guard (P3, P4) | Parent-owned spill root; total budget / jobs; bounded multi-pass merge; extend disk guard |
| #222 pool core | Copied signal choreography; exit-code semantic leak; default `--jobs` (P6, P8) | Extract core after #227; per-command exit policy; one code path for `--jobs 1` |
| #222 workers | Unpicklable state, `hard` set copies, start-method differences (P5) | Module-level worker; initializer for shared state; real-pool tests on default start method |
| #222 merge/progress | Completion-order merge; insertion-ordered output; off-TTY progress order (P2, P17) | Stem-order merge for every accumulator and progress line |

## Recommended Ordering Implied by the Pitfalls

1. Park Dependabot PRs; do the doc-drift and #223 decision as one small docs/code PR (golden-only diff).
2. #228, then #227, then #226 (each with zero golden diff, each starting with tests pinned from old code).
3. #222: measure, fixtures, spill ownership and budget, then generic pool core, then the parallel scan for verify; do dedup only if the measured split justifies it.

## Sources

- Repo code read directly (HIGH): `src/lintle/worker_pool.py`, `process_control.py`, `verify/grouping.py`, `verify/scan.py`, `verify/__init__.py`, `verify/report.py`, `verify/records.py`, `dedup.py`, `extract.py`, `report_writers.py`, `report.py`, `resume.py`, `run_planning.py`.
- Tests read directly (HIGH): `tests/test_freeze.py`, `tests/test_verify.py` (import-graph walk at line 708; tie pin at line 487), `tests/test_run_planning.py` (`_strip_generated`).
- GitHub issues #222 (incl. spike comment), #223, #226, #227, #228 via `gh issue view` (HIGH for stated facts; the prototypes' claims are the issues' own and were not re-run).
- `.planning/codebase/CONCERNS.md`, `.planning/PROJECT.md` (HIGH).
- `docs/superpowers/archive/runs/2026-08-11-v0.14.1-full-corpus-e2e.md`: timings only; no per-phase split (HIGH for the gap).
- Python 3.14 start-method defaults (macOS `spawn`, Linux `forkserver`), `ProcessPoolExecutor.terminate_workers`, `TemporaryDirectory` finalizer behaviour, and stock Linux fd soft limit of 1024: from training knowledge, not re-checked online this session (MEDIUM). The plan should confirm each with a one-line check (`multiprocessing.get_start_method()`, `ulimit -n`) before relying on it.
