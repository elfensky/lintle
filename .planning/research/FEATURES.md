# Feature Research

**Domain:** Offline Python data-cleaning/audit CLI over a ~30 GB, 232 M-record corpus. This milestone covers parallel audit passes (`verify`/`dedup`) and reproducible, byte-deterministic artifacts.
**Researched:** 2026-11-02
**Confidence:** MEDIUM. External practice (SOURCE_DATE_EPOCH, ordered parallel map) is from official or primary sources. The mapping onto lintle comes from reading its code. No prototype of #222 was run.

**Scope note:** This file classifies the Active list in `.planning/PROJECT.md`. It proposes no new product feature. "Anti-features" are things to refuse while delivering that list.

## Feature Landscape

### Table Stakes (Users Expect These)

#### A. Parallel audit scan (#222)

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| `--jobs N` on `verify` and `dedup`, same meaning as on `clean` | `clean` already has `--jobs` (default CPU count - 1). Users expect one spelling and one default across commands. GNU parallel uses `-j`/`--jobs` the same way. | LOW | `--jobs 1` must run the serial path, not a one-worker pool. The README already advises lowering `--jobs` on a slow disk. Say the same for `verify`/`dedup`. |
| Output byte-identical for every N (1, 2, many) and identical to the pre-#222 serial output | Core invariant #1/#2. GNU parallel `-k` (keep order) is the standard contract: jobs run in parallel, output stays in input order regardless of which job finishes first. | MEDIUM | Emit per-stem results in stem order. Use a reorder buffer keyed by stem index and yield only when the next expected index is ready (the gevent `imap` pattern). Never emit in completion order. |
| Bounded in-flight window | Unbounded prefetch exhausts memory when input is cheap and work is slow (CPython bug tracker, josh.r). Rule #3 forbids it. | MEDIUM | Cap in-flight stems at about N, not at the stem count. Worst case is a reorder buffer holding N-1 finished stems. Keep per-stem results small (counts, suspects) and never whole record lists. |
| Per-worker spill budgets shrink as N grows | PROJECT.md constraint: "parallel work must shrink per-worker spill budgets, not multiply them". `ExternalSorter` spills at 200,000 items today. | MEDIUM | Divide the item threshold by N (or sort by bytes). Spill still lands in `$TMPDIR`, outside the disk guard (CONCERNS.md). Parallelism multiplies that exposure, so say so in the run output or docs. |
| Live progress still works | `UnitTable` rows per stem already exist. Parallel scanning means several rows are active at once, as in `clean`'s `ProgressDisplay`. Off-TTY per-unit lines (#221) must remain. | MEDIUM | Progress goes to stderr and is outside the byte-determinism contract. Completion-order lines are acceptable there. `Scan.units()` is a generator that opens and closes one row at a time, so it needs a concurrent shape. |
| Same exit codes, same failure semantics | Documented exit codes (0/1/2). A worker crash or Ctrl-C must not leave a partial `04-verify/` or `05-dedup/`. | MEDIUM | Reuse `process_control` (fast pool termination, signal exit codes) and `fsutil.durable_replace`. Do not write a second shutdown path. |
| A jobs-matrix determinism test | Without it the byte-identical claim is untested for N > 1. | LOW | Run `verify`/`dedup` with `--jobs 1` and `--jobs N` on the fixture tree. Compare the whole output tree bytewise. `04-verify` and `05-dedup` write no timestamps today, so no allow-list is needed. |

#### B. Reproducible, byte-deterministic artifacts (#223)

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| The invariant is stated precisely, and docs, code and tests agree | README says "same input -> identical bytes every run". ARCHITECTURE section 7 lists `report.json` and the `.broken.txt` sidecar as byte-deterministic. Both are false today. | LOW | The `.broken.txt` header carries `generated: <utc_stamp()>` (`report_writers.py` line 113). That is #223. See the wider-scope finding below. |
| Artifact bytes are a pure function of (input bytes, flags, tool version) | Reproducible-builds practice: no wall-clock, hostname, username, absolute path, locale or env in a contracted artifact. | LOW | Already true for the `.broken.txt` body, `broken-noradids.ndjson`, cleaned chunks and the checkpoint. ARCHITECTURE line 675 says outputs carry basenames, counts and a timestamp only. The timestamp is the only gap. |
| Pick one resolution: drop the volatile field, or enumerate it as run metadata | #223 is "decision needed first". Both outcomes are legitimate practice. A hybrid with no named list is the only bad outcome. | LOW | See "Decision input for #223" below. |
| A run-to-run determinism test (same input twice, compare trees) | An invariant with no test drifts. | LOW | Compare the trees from two `clean` runs. If any field stays volatile, apply an explicit named allow-list (the diffoscope idea: normalise the known-volatile fields, then compare). |
| Chunk-set determinism preserved | `cat <stem>.*.<suffix>` must reproduce the single file. A header only in chunk 00001 already shapes this. | LOW | Dropping the stamp makes chunk 00001 identical across runs. |

#### C. Refactors with byte-identical outputs (#226, #227, #228)

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Byte-identical acceptance gate: suite green plus output-tree comparison | Each issue is a pure refactor and was prototyped green. The contract is "no observable change". | LOW per issue | Compare before/after trees on the fixture corpus. For #227 compare `.clean-state.json` bytes. For #228 compare `manifest.jsonl` and the extract sidecar `<id>.json`. |
| #228: one `stats_row` for the 8 keys, `span_days` computed once, one `RECORD_BYTES` in `tle.py` | Removes duplicated key lists in dedup and extract. This fits the "one definition" rule. | LOW | `RECORD_BYTES` is `140` in `verify/scan.py` today. `extract` also guards 140 bytes per record. |
| #227: `CheckpointSession` owns open/record/delete | One owner for the checkpoint write side. | MEDIUM | Checkpoint bytes must not change. The resume decision matrix stays untouched. |
| #226: `clean_run.run(args) -> int` plus a pure `join_exit_code(...)` with a table test | `cli.py` mixes parsing and orchestration. A pure exit-code join is cheap to table-test. | MEDIUM | Exit-code meaning (0/1/2/129/130/143) is documented in the README, so the table test pins it. Highest merge-conflict surface because `cli.py` is large. |
| Doc drift fixed: CONTRIBUTING says Python 3.11+, README and CONTRIBUTING call `sgp4` dev-only | Wrong install facts mislead the one user who reads them. | LOW | README line 43-45 and CONTRIBUTING.md need edits. CLAUDE.md and ARCHITECTURE section 7 already say verify-scoped runtime. `rich`, `humanize` and `sgp4` are the runtime deps. README also omits `humanize`. |

### Differentiators (Competitive Advantage)

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| One parallel driver shared by `verify` and `dedup` | `verify/scan.py` already merges the two commands' scan scaffolding. Parallelising once gives both commands the same flag, progress and determinism. | MEDIUM | This is the shape #222 names. Keep the per-record work a pure function of one stem so it can run in a worker. |
| Measured speedup, reported against the baseline | PROJECT.md baselines: `verify --source --orbit` 2 h 05 m on 1 core, `dedup` 48 m 50 s on 1 core, `clean` 21 m 23 s on 7 workers. A stated before/after number makes the result checkable. | LOW | Amdahl's law applies. The `ExternalSorter` merge and the `--orbit` pass are sequential. Only the per-file scan parallelises. Set expectations from the scan's share of wall clock, not from N. |
| Determinism that survives any worker count (not only same-N) | Many parallel tools are deterministic only for a fixed N. Output independent of N makes CI runs comparable across machines. | MEDIUM | Follows from stem-order emission plus a worker-count-independent sort key. The external sort already returns equal keys "in add order". Add order must therefore come from stem order, not completion order. |
| Enumerated volatile-field list ("modulo run metadata") in the contract | Reproducible-builds practice: name what varies, test everything else bytewise. | LOW | Applies only if a timestamp stays. The list is `run.timestamp`, per-file `elapsed_seconds` and `records_per_sec` in `report.json`, and the `report.md` stamp. |
| `SOURCE_DATE_EPOCH` honoured for any retained timestamp | Standard env var. Read it if set, else use the current time, in UTC. Gives bit-reproducible reruns without a new flag. | LOW | Only worth doing if a timestamp is kept. The spec discourages embedding at all where possible. It also discourages inventing separate override switches. Not needed if timestamps are dropped. |
| Prerequisite ordering captured: #227 before #222 | PROJECT.md calls #227 "prerequisite-shaped" for #222. | n/a | See dependencies. |

### Anti-Features (Commonly Requested, Often Problematic)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Completion-order (unordered) result merge for speed | `imap_unordered` is simpler and avoids idle workers | Breaks byte-determinism. Output then depends on timing and N. | Reorder buffer keyed by stem index with a bounded window. |
| Unbounded prefetch / `Executor.map` over all stems | One line of code | `Executor.map` consumes the whole input at once and queues all futures. Rule #3 violation at 29 stems x multi-GB. | Fixed in-flight window of about N stems. |
| Threads or free-threaded concurrency for the scan | Avoids process-pool pickling | The scan is CPU-bound parsing and validation. `clean` already uses processes, and slotted dataclasses are kept picklable. A second concurrency model doubles the shutdown and signal paths. | Reuse the process-pool pattern from `worker_pool`/`process_control`. |
| Parallelising the sort/merge or the `--orbit` pass inside #222 | Those passes dominate wall clock | Different problem. The sort is I/O- and spill-bound, and the orbit pass needs adjacent-pair context inside a group. Scope creep beyond "per-file cleaned scan". | Leave sequential. Record the Amdahl limit in the issue. |
| Auto-tuned or "smart" job count | Convenience | Hides a determinism-irrelevant knob and complicates tests. The README already documents the I/O-contention trade-off. | Match `clean`'s default (CPU count - 1) and keep `--jobs` explicit. |
| Caching the sorted order across runs (via `cleaned_fingerprint`) | CONCERNS.md lists it as an improvement path | Not on the Active list. It adds cache invalidation to a correctness tool. | Out of this milestone. Leave it recorded in CONCERNS.md. |
| A new `--deterministic` / `--reproducible` flag | Seems explicit | A mode that can be off means the default output is the non-reproducible one. The tool's promise is that it is always on. | Make the default output reproducible. Do not add a toggle. |
| A custom override switch such as `FORCE_SOURCE_DATE` | Seems helpful for CI | The reproducible-builds page discourages such switches. | If a timestamp is kept, honour only `SOURCE_DATE_EPOCH`. |
| Checksum manifests, signing or a provenance file for outputs | Looks like "reproducible-builds done fully" | Not requested. New artifact, new contract, new test surface. `lintle diff` and bytewise `cat` comparison already cover the user's need. | Out of scope. |
| Embedding the build time to "prove freshness" | Mirrors `generated:` stamps | The timestamp is exactly what breaks reproducibility. `report.json` already records `run.timestamp` for provenance. | Keep run time in one place only, and mark that place volatile. |
| Behaviour changes bundled into #226/#227/#228 | Refactor PRs invite "while I'm here" fixes | Destroys the byte-identical acceptance gate. | One concern per PR. Behaviour changes get their own issue. |

## Feature Dependencies

```
#228 (RECORD_BYTES -> tle.py, history.stats_row)
    └──touches same file──> verify/scan.py  (also edited by #222)

#227 (CheckpointSession)
    └──prerequisite for──> #222 (parallel scan; checkpoint write side must have one owner)

#222 (parallel scan)
    ├──requires──> bounded in-flight window + stem-order reorder buffer
    ├──requires──> per-worker spill-budget shrink (ExternalSorter threshold)
    └──requires──> jobs-matrix determinism test

#223 (timestamp decision)
    ├──decides──> whether the determinism test needs a volatile-field allow-list
    └──also covers──> report.json run.timestamp / elapsed_seconds, report.md stamp

#226 (clean_run.py extraction)
    └──independent of #222 logic, but conflicts textually with any cli.py edit
         (--jobs wiring for verify/dedup also lands in cli.py)

Doc drift fix ──independent──> all
```

### Dependency Notes

- **#222 requires #227:** PROJECT.md records #227 as prerequisite-shaped for #222. Do #227 first.
- **#228 before #222:** #228 moves `RECORD_BYTES` out of `verify/scan.py`, and #222 rewrites that module. Landing #228 first avoids a rebase conflict.
- **#223 before the full determinism test:** The test's allow-list depends on the #223 decision. The #222 jobs-matrix test is independent, because `04-verify` and `05-dedup` carry no timestamp. The fingerprint is size-only, with no mtime.
- **#226 vs `cli.py` edits:** `--jobs` on `verify`/`dedup` also touches argument wiring in `cli.py`. Sequence #226 before or after #222, never in parallel branches.
- **Parallel scan conflicts with unordered merge:** the two cannot coexist under the determinism rule.

## Decision Input for #223 (the open decision)

Facts from the code (read 2026-11-02):

- The `.broken.txt` header line is `# source: <name> | generated: <utc_stamp()> | lintle <version>` (`report_writers.py` line 113). The stamp is a wall-clock read.
- `report.md` also embeds `utc_stamp()` (`report.py` line 455).
- `report.json` carries `run.timestamp` (set from `report.utc_stamp()` in `cli.py` line 969). It also carries per-file `elapsed_seconds` and the derived `records_per_sec` (`report.py` lines 251-253). Those are timings and differ on every run.

So the wider claim "byte-deterministic `report.json`" is false for more than the sidecar. A fix that handles only the sidecar leaves ARCHITECTURE section 7's list of "byte-deterministic" artifacts inaccurate. The decision must name all four sites, or the invariant must be scoped explicitly.

Practice and recommendation:

- The SOURCE_DATE_EPOCH page says embedding a timestamp is discouraged where possible. It says a tool reads the variable if set and falls back to the current time otherwise.
- **Sidecar (`.broken.txt`):** drop the `generated:` stamp. It is the cheapest fix and `report.json`'s `run.timestamp` already gives provenance. It keeps the chunk-set `cat` equivalence clean. The `lintle <version>` field stays, because the version is an input to the function.
- **`report.json` / `report.md`:** these exist to record a run, so timing is their content. Qualify the invariant here ("byte-deterministic modulo `run.timestamp`, `elapsed_seconds`, `records_per_sec`") and list the fields in ARCHITECTURE section 6.
- **Cross-check:** the `--report json` stdout twin must still equal `report.json` byte for byte. That is a same-run property and is unaffected.

Confidence: MEDIUM. The recommendation follows the cited practice and the code. The maintainer owns the decision.

## MVP Definition

### Launch With (this milestone)

- [ ] #228 - smallest, unblocks a clean `scan.py` for #222
- [ ] #227 - prerequisite for #222
- [ ] #223 - decide and implement, including the invariant wording and the determinism test
- [ ] #222 - `--jobs`, stem-order reorder buffer, bounded window, spill budget scaled by N, jobs-matrix test
- [ ] #226 - mechanical extraction, with the `join_exit_code` table test
- [ ] Doc drift fix (Python 3.14, `sgp4` verify-scoped runtime, `humanize` listed)

### Add After Validation

- [ ] Report the measured `verify`/`dedup` speedup on the real corpus - once #222 lands
- [ ] Warn about expected spill size and `$TMPDIR` free space for parallel runs - trigger: a spill-related failure (already recorded in CONCERNS.md)

### Future Consideration

- [ ] Reuse of sorted order across runs via `cleaned_fingerprint` - not in the Active list
- [ ] Parallel sort/merge - different problem, different issue

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| #222 parallel scan, order-stable and bounded | HIGH (2 h 05 m and 48 m single-core runs) | MEDIUM | P1 |
| #223 resolve the determinism claim | HIGH (core promise is currently false) | LOW | P1 |
| #227 CheckpointSession | MEDIUM (enables #222) | MEDIUM | P1 |
| #228 history/`RECORD_BYTES` cleanup | LOW-MEDIUM | LOW | P1 (first, to unblock) |
| #226 clean_run extraction | MEDIUM (maintainability) | MEDIUM | P2 |
| Doc drift | MEDIUM (wrong install facts) | LOW | P1 (cheap) |
| `SOURCE_DATE_EPOCH` support | LOW | LOW | P3 (only if a timestamp stays) |

**Priority key:** P1 must have this milestone. P2 should have. P3 only if triggered.

## Comparable Practice (not competitors)

No direct competitor exists: lintle is a bespoke offline tool. The table lists the practices its design mirrors.

| Concern | GNU parallel | Python executors | Reproducible-builds | lintle approach |
|---------|--------------|------------------|---------------------|-----------------|
| Ordered output from parallel work | `-k` / `--keep-order`: still parallel, output in input order | `Executor.map` yields in order but eagerly queues all input | n/a | Reorder buffer keyed by stem index, bounded window |
| Job count | `-j N`, default one per core | `max_workers` | n/a | `--jobs N`, same default as `clean` |
| Timestamps in artifacts | n/a | n/a | Discouraged; if used, read `SOURCE_DATE_EPOCH`, else now; UTC | Drop from sidecar; enumerate where retained |
| Proving determinism | Output "always the same regardless of load" | n/a | Rebuild and compare | Jobs-matrix and run-twice tree comparison |

## Sources

- [SOURCE_DATE_EPOCH - reproducible-builds.org](https://reproducible-builds.org/docs/source-date-epoch/) · [🔖 2026-11-02](https://linkding.lav.ren/bookmarks?details=4340) - definition, read-if-set else now, UTC, discourages embedding and override switches (MEDIUM: page summary, spec itself not read)
- [GNU parallel book - `--keep-order`, `--jobs`](https://dyn.manpages.debian.org/trixie/parallel/parallel_book.7.en.html) - ordered output while still running in parallel (MEDIUM, via search excerpt)
- [CPython bug tracker, josh.r, msg322390](https://bugs.python.org/msg322390) - unbounded prefetch memory risk (MEDIUM)
- [gevent `_imap.py`](https://mitra.stanford.edu/kundaje/marinovg/oak/anaconda3/lib/python3.7/site-packages/gevent/_imap.py) - index-keyed reorder buffer pattern (LOW-MEDIUM: third-party copy)
- Repo files read: `.planning/PROJECT.md`, `README.md`, `ARCHITECTURE.md` sections 6-7, `.planning/codebase/CONCERNS.md`, `src/lintle/verify/scan.py`, `src/lintle/verify/records.py`, `src/lintle/report_writers.py`, `src/lintle/report.py`, `src/lintle/cli.py` (HIGH for what the code does today)
- Not found: authoritative pigz or `ProcessPoolExecutor` `chunksize`-ordering guidance. The search returned nothing usable.

---
*Feature research for: parallel audit passes and byte-deterministic artifacts in an offline data-cleaning CLI*
*Researched: 2026-11-02*
