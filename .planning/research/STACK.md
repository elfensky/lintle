# Technology Stack

**Project:** lintle (milestone: #222 parallel cleaned scan, plus #226/#227/#228 refactors and #223 determinism claim)
**Dimension:** Stack for a process pool over per-file scans feeding a deterministic k-way merge
**Researched:** 2026-11-02
**Overall confidence:** MEDIUM-HIGH (stdlib behaviour confirmed in official docs and in this repo's own code; performance gain not measured)

## Verdict

Add **zero new dependencies**. Everything #222 needs is in the Python 3.14 standard library, and the repo already uses the same pieces in `worker_pool.py` (clean path). Reuse that pattern.

- Pool: `concurrent.futures.ProcessPoolExecutor` (same class `worker_pool.py` uses).
- Worker output: sorted run files on disk plus small tally objects. Workers never return records.
- Merge: `heapq.merge` in the parent, over a **total sort key**, with bounded fan-in.
- Determinism: results indexed by stem rank, never by completion order.
- Budget: the existing `chunk_size` divided by the worker count, with a floor.

**eli14 summary.** Think of 29 piles of index cards (the stems). Today one clerk sorts every pile alone. The plan hires several clerks. Each clerk sorts whole piles into neat sorted stacks and puts them on a shared shelf. One head clerk then merges the stacks into one final stack. The final stack must come out in the same order every time, so the head clerk reads the stacks by pile number, not by who finished first. Each clerk gets a smaller desk when more clerks work, so the room never gets fuller. Today's risk: the merge order rule relies on an undocumented habit of `heapq.merge`. Future risk: too many stacks open at once can hit the open-file limit on Linux.

## Recommended Stack

### Core (stdlib only, all Python 3.14)

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| `concurrent.futures.ProcessPoolExecutor` | stdlib 3.14 | Run one cleaned stem per task in a worker process | CPU-bound pure-Python parse/validate/sort (issue #222: one core at 97%). Processes bypass the GIL. Raises `BrokenProcessPool` when a worker dies (the older `multiprocessing.Pool` can hang). `worker_pool.py` already uses it with `initializer=process_control.ignore_sigint` and `terminate_workers()`. One pattern, one set of tests. | HIGH |
| `Executor.map(fn, items)` or `submit` + index-keyed result list | stdlib 3.14 | Collect per-stem results in stem order | `map` yields in input order regardless of completion order. `as_completed` yields in completion order, so it must not decide aggregation or emission order. | HIGH (docs; probe confirmed order) |
| `ProcessPoolExecutor.terminate_workers()` | new in 3.14 | Fast shutdown on SIGINT/SIGTERM | Already used in `worker_pool.py`. Reuse for the scan pool. After it returns, the parent removes the spill directory. | HIGH (in repo code) |
| `heapq.merge(*runs, key=...)` | stdlib | Parent-side k-way merge of per-stem sorted runs | Lazy, constant memory, already the `ExternalSorter` merge. | HIGH |
| `tempfile.TemporaryDirectory` | stdlib | One parent-owned spill root with a private subfolder per worker task | Parent cleans up in one place, including after `terminate_workers()`. Pass `dir=` to move the spill off `$TMPDIR` (see CONCERNS.md). | HIGH |
| `multiprocessing.Manager().Queue()` | stdlib | Worker-to-parent progress messages for the `UnitTable` | Same mechanism as `worker_pool.py`. Proxy objects pickle through `submit`, which works under `spawn` and `forkserver`. | HIGH (in repo code) |
| `os.process_cpu_count()` | stdlib 3.13+ | Default worker count | Honours CPU affinity. `cli.py` already passes it to `resolve_jobs`. Reuse the same rule (CPU count minus 1, capped by stem count). | HIGH |
| `resource.setrlimit(RLIMIT_NOFILE, ...)` | stdlib, POSIX | Optional stopgap for the open-file ceiling | See design rule 2 below. The project is POSIX-only already (`fcntl`). | MEDIUM |

### Database

None. lintle is an offline CLI over local files (PROJECT.md, Out of Scope).

### Infrastructure

No change. CI stays Ubuntu with Python 3.14; local dev on macOS. The two platforms use **different default start methods** (see below), so the pair already tests both.

### Supporting Libraries

No new library. Existing runtime deps stay: `rich>=15,<16`, `humanize>=4,<5`, `sgp4>=2.25,<3` (verify `--orbit` only). The `sgp4` wall holds: scan workers import `lintle.verify.records`, `lintle.verify.checks` and `lintle.epoch`. They never import `lintle.verify.orbit`.

## Design rules this stack must carry

### 1. Make the sort key total (removes reliance on undocumented behaviour)

`ExternalSorter` documents "equal keys come back in add order" because `heapq.merge` breaks ties by iterable order. The 3.14 `heapq` documentation does **not** promise this. CPython's implementation does it (each heap entry is `[key, order, value, next]`), but it is not a contract.

With workers, the tie order depends on how runs are assigned to the merge. The safe fix is a key with no ties:

- Sort key for cleaned records: `(catalog, epoch_key, src_file, index)`.
- `src_file` compares like stem rank, because `records.cleaned_stems` returns `sorted(stems)`.
- This gives exactly today's order (ties fall back to stem order, then in-file order). No output byte changes.
- Keep `record_key` = `(catalog, epoch_key)` as the **group** key for dedup and the contradiction pass. Add a separate `sort_key`. Merging the two would regroup the corpus.
- `SuspectSink` already sorts on `(rule, catalog, epoch_key, src_file, index)`. Ties across stems are impossible there, so its stem-order merge is safe once each worker adds in file order.

Confidence: HIGH (code read plus docs). Needs one regression test: `jobs=1` vs `jobs=N` outputs compared byte by byte, with a tiny `chunk_size` to force spills.

### 2. Two-level merge bounds open files and moves work into the workers

Today `ExternalSorter.sorted_records` opens **every** run at once: `[p.open() for p in self._runs]`. At 232 M records and `chunk_size=200_000` that is about 1,160 handles. macOS here allows 1,048,576. A default Linux soft limit is often 1,024, so the current code can already hit `EMFILE` there. Parallelism makes it worse: a smaller per-worker chunk means more runs.

Recommended shape:

1. Each worker scans one stem, spills sorted runs, merges its own runs into **one sorted run file per stem**, deletes the spill runs, and returns `(stem_rank, run_path, tallies)`.
2. The parent merges at most `len(stems)` files (about 29).
3. If a single stem still has more runs than a safe fan-in (suggest 256), add one intermediate merge pass inside the worker. A `resource.setrlimit` raise toward the hard limit is an acceptable stopgap, not the design.

The total record count is known before the scan: `stem_size / RECORD_BYTES` (140 bytes per record). The planner can compute the exact run count up front.

Confidence: MEDIUM. The fan-in cap and the pass count are design decisions for the phase plan.

### 3. Per-worker spill budget

Today one process holds up to two sorters (record sorter and suspect sink) of 200,000 items each. RSS was flat at 165 MB (issue #222). The rule from PROJECT.md is "shrink per-worker spill budgets, not multiply them".

- `worker_chunk = max(FLOOR, BASE_CHUNK // jobs)` with `BASE_CHUNK = 200_000` and a floor near 25,000 records.
- The floor caps the run count, which caps the fan-in in rule 2.
- Pass `chunk_size` through `record_sorter(chunk_size)` and `SuspectSink(chunk_size)`. Both already accept it.
- Total in-RAM buffer across the pool then stays near today's single-process figure.

Confidence: MEDIUM. The floor value needs one measurement on the real corpus.

### 4. Order of results and of aggregates

- Submit stems **largest first** (shortest wall clock; the largest file is 3.2 GB). Store each result under its stem rank. Merge and aggregate in rank order.
- Merge worker tallies (`histogram`, `hard_by_stem`, `soft_by_stem`, `counts`) with `Counter.update` **in stem order**. JSON output keeps first-seen key order, so merge order is a byte-level concern (CLAUDE.md, Code Style).
- Workers return sets of ints (`population`). The parent sorts before use. Never return sets of `str`: `PYTHONHASHSEED` differs per process.
- Workers write spill files with `encoding="ascii"` and `repr(epoch_key)` (shortest round-trip float). Both are deterministic across processes.
- Never use `as_completed` order for anything that reaches an output file.

### 5. Start methods

Python 3.14 changed the default start method on Linux from `fork` to `forkserver`. macOS and Windows keep `spawn`. Consequences for the scan worker:

- Worker functions are module-level, in an importable module (for example `lintle/verify/scan.py`). No lambdas or closures. Note that `Scan` takes `cells`/`totals` lambdas today; those stay in the parent.
- Arguments and return values are picklable plain data: strings, ints, `Counter`, small dataclasses with `slots=True`. `CleanedRecord` is a frozen slotted dataclass and pickles, but records should never cross the process boundary.
- Do not pass `mp_context="fork"`: `fork` after threads (rich's live display uses a thread) triggers a `DeprecationWarning` since 3.12 and is a latent deadlock.
- Do not set `max_tasks_per_child` unless needed. A hang with queued tasks was fixed only in 3.14.7 (gh-115634). If memory fragmentation shows up in a corpus run, `max_tasks_per_child=1` is the fix, with a 3.14.7 floor.

Confidence: HIGH (official multiprocessing and concurrent.futures docs; the clean path already runs under this default on CI).

## What NOT to use

| Do not use | Why | Use instead |
|------------|-----|-------------|
| `ThreadPoolExecutor` / `threading` | The work is pure-Python parse, validate and sort. The GIL keeps it on one core. Zero gain. | `ProcessPoolExecutor` |
| `InterpreterPoolExecutor` (new in 3.14) | Per-interpreter GIL gives real parallelism, but the 3.14 notes list high per-interpreter memory, unoptimised startup, and many incompatible third-party extension modules. It is a `ThreadPoolExecutor` subclass: no `terminate_workers()`, and a hard crash in a subinterpreter takes down the parent. The only gain (cheap start) is irrelevant for 29 tasks that each run for minutes. | `ProcessPoolExecutor`. Revisit when the ecosystem catches up. |
| Free-threaded build (`python3.14t`) | Officially supported (PEP 779), but single-thread overhead is 5-10%. Users install lintle from PyPI on the default build, so the project cannot assume it. `sgp4` is a C extension. The repo pins `3.14`. | Default build, processes |
| `asyncio` | The bottleneck is CPU, not I/O wait. | Processes |
| `multiprocessing.Pool` | A dead worker can hang `imap`. No `terminate_workers()` semantics. Diverges from `worker_pool.py`. | `ProcessPoolExecutor` |
| `as_completed` to build output order | Completion order differs run to run. It breaks byte-determinism (Critical constraint). Fine for driving the progress table only. | `map`, or an index-keyed result list |
| Relying on `heapq.merge` tie order | Behaviour exists in CPython but the 3.14 docs do not promise it. | Total sort key (rule 1) |
| `Executor.map(..., buffersize=...)` as a memory fix | `buffersize` (new in 3.14) bounds in-flight results. Scan results are a path plus small tallies, so there is nothing to bound. | Return small results only |
| Returning `CleanedRecord` lists from workers | Pickling millions of records through the result pipe breaks constant memory and costs more CPU than the scan. | Write runs to disk; return paths |
| `joblib` / `dask` / `ray` / `mpire` | New dependency for a job `ProcessPoolExecutor` does in about 40 lines. `joblib` adds its own pickling layer and loky worker reuse, which complicates the spawn-safety and signal handling already solved in `process_control.py`. | stdlib |
| `duckdb` / `polars` / `pyarrow` external sort | Heavy dependency. Sort order for ties holds only with a total key. The hot loop (`revalidate`, aligner, `epoch_dt`) stays Python, so the sort is not the bottleneck. Vetoed in spirit by the determinism invariant (#1/#2) and by the lean-runtime policy. | `ExternalSorter` + `heapq.merge` |
| `sortedcontainers` | Already in the lock only through dev tooling. It is an in-memory structure, not an external sort. | `list.sort` per chunk |
| `compression.zstd` for spill runs (new in 3.14) | Plausible later win for disk use. It adds CPU on a CPU-bound job and changes nothing in the milestone's scope. | Plain ASCII TSV runs. Defer. |

## Alternatives Considered

| Category | Recommended | Alternative | Why Not |
|----------|-------------|-------------|---------|
| Parallel unit | One task per cleaned stem | One task per chunk file | A stem is the natural unit: its records pair across chunk boundaries and the `SourceAligner` is per stem. Chunk-level tasks break pairing and the aligner. |
| Merge topology | Per-stem run in worker, then parent merge | Parent opens all spill runs | Fan-in grows with the number of workers. Hits `EMFILE` on Linux defaults. |
| Tie order | Total key `(catalog, epoch_key, src_file, index)` | Keep "iterable order" tie-break | Undocumented; depends on assigning runs to the merge in the right order forever. |
| Progress channel | `Manager().Queue()` (as `worker_pool.py`) | `mp_context.Queue` via `initargs` | Slightly cheaper (no manager process), but a second pattern to maintain. Not worth it. |
| Result collection | `map` over stems sorted by size, re-indexed by rank | `as_completed` + sort afterwards | Equivalent if re-indexed. `map` fails fast and reads simpler. |

## Roadmap-relevant ordering notes

- `resolve_jobs` lives in `cli.py`. #226 moves clean orchestration into `clean_run.py`. Put the shared job-count rule in a leaf (`worker_pool.py` or `process_control.py`) **before** #222 needs it from `verify`/`dedup`.
- #227 (`CheckpointSession`) is described as prerequisite-shaped for #222. This research does not cover why. Confirm with the #227 text before ordering.
- #222 touches `verify/scan.py`, `verify/grouping.py`, `verify/__init__.py`, `dedup.py` and `verify/report.py` (`SuspectSink`). #228 touches `history.py`, `dedup.py`, `extract.py` and moves `RECORD_BYTES` out of `scan.py`. Land #228 first to avoid a conflict on `RECORD_BYTES`.
- The final merge stays serial in the parent: it decodes every record (232 M rows) and feeds the contradiction or dedup pass. Expect Amdahl's limit there. Measure before promising a speed-up. Issue #222 states the merge "is not where the time goes", but no measurement is on file.

## Installation

```bash
# No new dependencies.
uv sync
```

## Test stack (existing tools only)

| Test | Tool | Purpose |
|------|------|---------|
| Output identity | `pytest`, `cmp`-style byte compare of every chunk set | `jobs=1` equals `jobs=N` for `verify` and `dedup` |
| Spill boundary | tiny `chunk_size` parameter | Forces many runs, so ordering across spill boundaries is exercised |
| Merge property | `hypothesis` | Merge of per-stem sorted runs equals `sorted()` of all records under the total key |
| Start method | CI (Linux `forkserver`) and dev machine (macOS `spawn`) | Picklability of every worker argument and result |
| Fan-in | fake low `RLIMIT_NOFILE` in a subprocess | Confirms the merge never exceeds the cap |

## Confidence notes

| Claim | Basis | Level |
|-------|-------|-------|
| `ProcessPoolExecutor` default start method is `forkserver` on Linux in 3.14 | docs.python.org multiprocessing and whatsnew 3.14 | HIGH |
| `terminate_workers()`, `kill_workers()`, `Executor.map(buffersize=)`, `InterpreterPoolExecutor` are 3.14 features | docs.python.org concurrent.futures and whatsnew 3.14; `terminate_workers` also used in `worker_pool.py` | HIGH |
| `Executor.map` returns results in input order | Local probe (ran on CPython 3.15.2, not 3.14, because only 3.15 is on the PATH) plus docs wording "similar to map()" | MEDIUM |
| `heapq.merge` tie order is not documented | docs.python.org heapq; implementation read in local CPython | HIGH |
| `ExternalSorter` opens all runs at once | Read `grouping.py` lines 99-107 | HIGH |
| Linux soft file limit of 1,024 is common | General knowledge; this macOS host reports 1,048,576 | LOW (verify on the CI runner) |
| Parallel scan gives a useful speed-up | Not measured; only the single-core profile in issue #222 | LOW |

Note on tooling: the GSD `classify-confidence` seam rates the `webfetch` provider LOW by default, with or without `--verified`. The HIGH ratings above rest on the official Python documentation cross-checked against this repository's code, not on the provider tier.

## Sources

- [concurrent.futures, Python 3.14](https://docs.python.org/3.14/library/concurrent.futures.html) · [🔖 2026-11-02](https://linkding.lav.ren/bookmarks?details=4367)
- [multiprocessing, Python 3.14](https://docs.python.org/3.14/library/multiprocessing.html) · [🔖 2026-11-02](https://linkding.lav.ren/bookmarks?details=4368)
- [heapq, Python 3.14](https://docs.python.org/3.14/library/heapq.html) · [🔖 2026-11-02](https://linkding.lav.ren/bookmarks?details=4365)
- [What's New in Python 3.14](https://docs.python.org/3.14/whatsnew/3.14.html) · [🔖 2026-11-02](https://linkding.lav.ren/bookmarks?details=4372)
- Repo code read: `src/lintle/verify/scan.py`, `verify/grouping.py`, `verify/records.py`, `verify/__init__.py`, `verify/report.py`, `verify/orbit.py`, `dedup.py`, `worker_pool.py`, `process_control.py`
- Issue #222 body (via `gh issue view 222`)
