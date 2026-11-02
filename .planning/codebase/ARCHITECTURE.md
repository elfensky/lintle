---
last_mapped_commit: bf834f78234eaa5b6f879c0bff96587181b4d723
last_mapped_at: 2026-11-02
---
<!-- refreshed: 2026-11-02 -->
# Architecture

**Analysis Date:** 2026-11-02

## System Overview

```text
┌──────────────────────────────────────────────────────────────────────┐
│                 CLI entry: `lintle` / `python -m lintle`             │
│        src/lintle/cli.py (main, _dispatch)  · wizard.py (no-args)    │
├───────────┬───────────┬────────────┬────────────┬─────────┬──────────┤
│  clean    │  verify   │   dedup    │  extract   │  diff   │ explain/ │
│           │           │            │            │         │ report   │
│ run_plan- │ verify/   │ dedup.py   │ extract.py │ diff.py │ explain. │
│ ning.py   │ __init__  │            │            │         │ py/      │
│ worker_   │           │            │            │         │ summary. │
│ pool.py   │           │            │            │         │ py       │
└─────┬─────┴─────┬─────┴──────┬─────┴──────┬─────┴────┬────┴────┬─────┘
      │           │            │            │          │         │
      ▼           ▼            ▼            ▼          ▼         ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Clean pipeline (streaming):  pipeline.py ─▶ repair.py ─▶ tle.py     │
│  (the ONE validator lives in tle.py; the clean path never imports    │
│   sgp4 or lintle.verify)                                             │
└───────────┬──────────────────────────────────────────────┬───────────┘
            ▼                                              ▼
┌───────────────────────────────┐      ┌──────────────────────────────┐
│ Output writers & report       │      │ Durability & state           │
│ report.py, report_writers.py, │      │ fsutil.py (durable_replace,  │
│ report_aggregation.py,        │      │   out_dir_lock), chunking.py,│
│ output_artifacts.py           │      │   resume.py (.clean-state)   │
└───────────────────────────────┘      └──────────────────────────────┘
            ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Output tree under --out-dir: 01-cleaned … 06-extract (see STRUCTURE)│
└──────────────────────────────────────────────────────────────────────┘
```

Source of truth for the design is the repo root `ARCHITECTURE.md` (1022 lines). This document summarises it for planners and executors. Where the two differ, the code and the root `ARCHITECTURE.md` win.

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| CLI dispatch | argparse build, subcommand routing, exit codes, `--out-dir` lock wrapper (`_locked_postrun`) | `src/lintle/cli.py` |
| Entry point | `python -m lintle` shim; console script `lintle = lintle.cli:main` | `src/lintle/__main__.py`, `pyproject.toml` |
| Clean preflight | Disk-space guard, fresh-run output scrub, resume decision, `CleanConfig`, `RunPlan` | `src/lintle/run_planning.py` |
| Process pool | Per-file dispatch to worker processes, progress collection, checkpoint updates, interrupt shutdown | `src/lintle/worker_pool.py` |
| Signal control | SIGINT setup in workers, fast pool termination | `src/lintle/process_control.py` |
| Streaming pipeline | Binary line reads, 1/2-line pairing state machine, routing to clean or quarantine | `src/lintle/pipeline.py` |
| Repair | Content-safe normalisation, optional checksum reconstruction, re-validation before commit | `src/lintle/repair.py` |
| Validator | The single definition of a "perfect" TLE record (layout, ranges, mod-10 checksum, pairing, catalog decode) | `src/lintle/tle.py` |
| Diagnostics registry | Stable `RuleID`, `Diagnostic`, `RepairTier` (pure data) | `src/lintle/diagnostics.py` |
| Repair taxonomy | `FixClass` enum and `FixSpec` registry (pure data) | `src/lintle/categories.py` |
| Epoch definition | Normalised epoch parse, key, instant, ISO (stdlib-only leaf) | `src/lintle/epoch.py` |
| Run report | `FileStats`, run envelope, `report.md` / `report.json` writers | `src/lintle/report.py` |
| Corpus aggregation | Run totals and per-NORAD rollups (pure) | `src/lintle/report_aggregation.py` |
| Structured writers | `.broken.txt` sidecar, `report.jsonl` shards, `broken-noradids.ndjson`, shard concat | `src/lintle/report_writers.py` |
| End-of-run finalisation | Writes report and NDJSON/JSONL artifacts in one place | `src/lintle/output_artifacts.py` |
| Durable commit | `durable_replace` (fsync, rename, dir fsync), JSON seam, out-dir `flock` lock | `src/lintle/fsutil.py` |
| Chunking | `ChunkedWriter` / `ChunkedReader` for `<stem>.NNNNN.<suffix>` chunk sets | `src/lintle/chunking.py` |
| Resume checkpoint | `.clean-state.json` build/load, resume matrix, fingerprints | `src/lintle/resume.py` |
| Live progress | Rich live tables for clean (`ProgressDisplay`) and post-run commands (`UnitTable`), `status` spinner | `src/lintle/cli_progress.py` |
| Terminal IO | Shared rich Consoles, `error`/`warning`/`note`/`prompt` emitters, TTY helpers | `src/lintle/term.py` |
| Summary rendering | Aggregate panel, results tables, `lintle report` view, shared formatters | `src/lintle/summary.py` |
| Thresholds | `--max-quarantined` parsing and quality-gate exit policy (pure) | `src/lintle/thresholds.py` |
| Verify auditor | Post-run re-validation, source byte-diff, contradiction rules, orbit physics pass | `src/lintle/verify/` (`__init__.py`, `checks.py`, `scan.py`, `records.py`, `grouping.py`, `report.py`, `orbit.py`) |
| Dedup | Latest-re-issue import list and per-satellite manifest from `01-cleaned/` | `src/lintle/dedup.py` |
| History reducer | Gap and median math shared by `extract` and `dedup` (pure) | `src/lintle/history.py` |
| Extract | One satellite's history from a dedup run (binary search, no index) | `src/lintle/extract.py` |
| Diff | Read-only per-rule delta between two `report.jsonl` files | `src/lintle/diff.py` |
| Explain | Read-only rule and fix documentation; examples in pure data | `src/lintle/explain.py`, `src/lintle/explain_examples.py` |
| Config | Optional `./.lintle.json` remembering source and output dirs | `src/lintle/config.py` |
| Wizard | No-argument interactive menu; builds argv and calls `cli.main` | `src/lintle/wizard.py` |

## Pattern Overview

**Overall:** Layered, one-way module pipeline with a streaming core, a process pool for per-file parallelism, and post-run read-only auditors that consume the on-disk output tree.

**Key Characteristics:**
- Module dependencies flow one way. The documented chain is `cli.py → pipeline.py → repair.py → tle.py`. Cycles are avoided by design. The two known cycles are broken with lazy imports (see Architectural Constraints).
- Data leaves (`tle.py`, `diagnostics.py`, `categories.py`, `explain_examples.py`, `epoch.py`, `history.py`, `thresholds.py`) carry no I/O.
- Output on disk is the contract. `verify`, `dedup`, `extract`, and `diff` read the files that `clean` writes. They do not share in-memory state with it.
- The clean path is walled off from `sgp4` and from `lintle.verify`. An import-graph test enforces this (see `ARCHITECTURE.md` §7 at repo root).
- Every output file is written through one commit path, `fsutil.durable_replace`, so a file is either old or new, never half-written.

## Layers

**CLI / orchestration:**
- Purpose: Parse arguments, decide the run plan, route subcommands, own exit codes.
- Location: `src/lintle/cli.py`, `src/lintle/run_planning.py`, `src/lintle/worker_pool.py`, `src/lintle/process_control.py`, `src/lintle/thresholds.py`, `src/lintle/output_artifacts.py`
- Contains: argparse subparser builders, `discover_paths`, `_finalize_run`, `_locked_postrun`, `main`, `_dispatch`.
- Depends on: pipeline, reporting, fsutil, term, cli_progress, verify (lazily), dedup, extract, diff, explain, summary.
- Used by: `lintle` console script, `python -m lintle`, `wizard.py`.

**Streaming per-file core:**
- Purpose: Turn a raw corpus file into validated records, or quarantine entries, in constant memory.
- Location: `src/lintle/pipeline.py`, `src/lintle/repair.py`, `src/lintle/tle.py`
- Contains: `iter_records`, `process_file`, `_route_candidate`, `_clean_output_paths`, `RecordCandidate`, `Orphan`, progress message dataclasses.
- Depends on: `diagnostics`, `categories`, `report`, `report_writers`, `chunking`, `fsutil`, `__init__` (naming constants).
- Used by: `worker_pool.py` (process entry), tests.

**Reporting and writers:**
- Purpose: Turn per-file stats into run totals, the Markdown and JSON report, and NDJSON/JSONL artifacts.
- Location: `src/lintle/report.py`, `src/lintle/report_aggregation.py`, `src/lintle/report_writers.py`, `src/lintle/output_artifacts.py`, `src/lintle/summary.py`
- Depends on: `fsutil`, `chunking`, `term`, `humanize`, `rich`.
- Used by: `cli`, `pipeline`, `worker_pool`, `verify.report`, `diff`, `dedup`, `extract`.

**Durability and state:**
- Purpose: Atomic writes, out-dir locking, chunk sets, resume checkpoints.
- Location: `src/lintle/fsutil.py`, `src/lintle/chunking.py`, `src/lintle/resume.py`
- Depends on: stdlib only (`fsutil`), `fsutil` plus `__version__` and naming constants (`resume`, `chunking`).
- Used by: almost every writer.

**Post-run auditors and consumers (read-only with respect to `01-cleaned/`):**
- Purpose: Re-validate, cross-check, deduplicate, and export satellite histories from `01-cleaned/`.
- Location: `src/lintle/verify/`, `src/lintle/dedup.py`, `src/lintle/extract.py`, `src/lintle/history.py`, `src/lintle/diff.py`
- Depends on: `tle`, `repair` (`normalize_edges`, `is_content_free` only), `epoch`, `history`, `chunking`, `fsutil`, `cli_progress`, `summary`, `term`.
- Used by: `cli` (via lazy dispatch).

**Presentation:**
- Purpose: Rich terminal output for live progress, panels, tables, prompts.
- Location: `src/lintle/cli_progress.py`, `src/lintle/summary.py`, `src/lintle/term.py`, `src/lintle/wizard.py`
- Depends on: `rich`, `humanize`, `pipeline` progress messages.
- Used by: `cli`, `worker_pool`, `output_artifacts`, `verify`, `dedup`, `extract`, `diff`.

**Pure data leaves:**
- Purpose: Stable vocabularies with no I/O.
- Location: `src/lintle/diagnostics.py`, `src/lintle/categories.py`, `src/lintle/explain_examples.py`, `src/lintle/epoch.py`, `src/lintle/history.py`, `src/lintle/thresholds.py`, `src/lintle/tle.py`
- Used by: everything above them.

## Data Flow

### Primary Request Path: `lintle clean`

1. Entry: `lintle` console script calls `lintle.cli:main` (`src/lintle/cli.py`, `main` at ~line 696). With no subcommand on a TTY, `wizard.py` runs and re-enters `cli.main` through an injected callable.
2. `_dispatch` builds the `CleanConfig` (`src/lintle/run_planning.py`), then runs preflight. Preflight covers the disk-space guard, the fresh-run output scrub, and the resume decision (`src/lintle/resume.py`, `.clean-state.json`).
3. `src/lintle/cli.py` takes the out-dir lock (`fsutil.out_dir_lock`, `.clean.lock`). A second live run exits with code 2.
4. `src/lintle/worker_pool.py` (`run_workers`) submits one job per input file to a `ProcessPoolExecutor`. Each job calls `pipeline.process_file`. A `multiprocessing.Manager` queue carries progress deltas to the parent for `cli_progress.ProgressDisplay`.
5. `src/lintle/pipeline.py` (`iter_records`) reads each file in binary, at most one line at a time, with `readline(_MAX_LINE_BYTES)`. It pairs `1 `/`2 ` lines into candidates. Blank and continuation-only lines are dropped by `repair.is_content_free`.
6. For each candidate, `repair.process_record` tries content-safe fixes (`normalize_edges`), then optional checksum reconstruction (only with `--reconstruct-checksum`), then a single full re-validation through `tle.validate_record`.
7. `pipeline._route_candidate` writes an accepted record to a `ChunkedWriter` stream under `01-cleaned/`. A quarantined record goes to `02-broken/<stem>.NNNNN.broken.txt` and to the structured writers in `report_writers.py`.
8. Each chunk commits through `fsutil.durable_replace` when it fills. Per-file stats return as `FileStats` (`report.py`) and feed `resume.CompletedEntry.from_stats` for the checkpoint.
9. `src/lintle/output_artifacts.py` writes `03-report/report.md`, `report.json`, `report.NNNNN.jsonl`, and `broken-noradids.ndjson`. `summary.py` renders the end-of-run panel. The `.clean-state.json` checkpoint is deleted on full success.

### Secondary Flow: `lintle verify` → `dedup` → `extract`

1. `cli` lazy-imports `lintle.verify` and calls `verify.run`. `verify/scan.py` streams `01-cleaned/` back as records (`verify/records.py`).
2. `verify/checks.py` re-validates each record through `tle.validate_record`, runs the source byte-diff (`SourceAligner`), and applies contradiction rules. Suspects stream to disk through `verify/report.py` (`SuspectSink`, `ExternalSorter` from `verify/grouping.py`). Output goes to `04-verify/`.
3. `dedup.py` reads `01-cleaned/` and excludes hard suspects from `04-verify/suspects`. It groups by `(catalog, epoch)` via `verify.grouping`. It writes `05-dedup/import.NNNNN.txt` (fixed 140-byte records, sorted), `notes`, `manifest.jsonl`, and `summary.json`. `history.analyze_epochs` builds the per-satellite stats.
4. `extract.py` runs a preflight over `05-dedup/import.*`. It binary-searches the sorted chunk set for one catalog number. It writes `06-extract/<id>.txt` (verbatim byte slice) and `<id>.json` (stats sidecar).

### State Management

- No long-lived in-memory state across commands. Each command reads its inputs from disk.
- Within a `clean` run, state lives in three places: the worker's per-file `FileStats`, the parent's progress aggregation, and the on-disk `.clean-state.json` checkpoint.
- `.clean-state.json` is durable (written via `durable_replace`). Its presence marks an interrupted run. Resume unit is a whole file, not a record.

## Key Abstractions

**`validate_record` (the one validator):**
- Purpose: Defines "perfect" for a TLE record.
- Examples: `src/lintle/tle.py`
- Pattern: Pure function over two lines. Layered checks: line length, column layout, semantic ranges, mod-10 checksum, pairing.

**`Accepted` / `Quarantined` (repair outcomes):**
- Purpose: The only two results a record can have after repair.
- Examples: `src/lintle/repair.py` (`process_record`)
- Pattern: Frozen slotted dataclasses. `Accepted` carries the fixed lines and `FixClass` tags. `Quarantined` keeps raw bytes and a primary `Diagnostic`.

**`FixClass` / `FixSpec` (repair taxonomy):**
- Purpose: Single source of truth for fix tags in `fix_counts` and the report.
- Examples: `src/lintle/categories.py`
- Pattern: Enum plus registry. `RuleID` values in `src/lintle/diagnostics.py` are a public, never-recycled contract.

**`ChunkedWriter` / `ChunkedReader`:**
- Purpose: Fixed-count output streams split into `<stem>.NNNNN.<suffix>` chunks. Concatenation of the chunks equals the single-file bytes.
- Examples: `src/lintle/chunking.py`
- Pattern: Commit per chunk through `durable_replace`. Scrub prior chunks on first open.

**`ExternalSorter` (verify grouping):**
- Purpose: Spill-to-disk sort with generic key, encode, and decode functions. Equal keys keep add order.
- Examples: `src/lintle/verify/grouping.py`
- Pattern: Used by contradiction checks, dedup, orbit, and `SuspectSink`.

**`SourceAligner` (verify checks):**
- Purpose: Byte-diff of cleaned records against source lines.
- Examples: `src/lintle/verify/checks.py`
- Pattern: Null object. Built through `SourceAligner.open`. Inert when a stem has no source.

**`CleanConfig` / `RunPlan`:**
- Purpose: Typed snapshot of `clean` config and resolved preflight plan. Replace a raw argparse `Namespace`.
- Examples: `src/lintle/run_planning.py`
- Pattern: Slotted dataclasses (`@dataclasses.dataclass(slots=True)`).

**`CompletedEntry` (resume):**
- Purpose: Typed checkpoint entry for one completed file.
- Examples: `src/lintle/resume.py`
- Pattern: Built by the caller (`worker_pool`), so `resume` never imports `report`.

## Entry Points

**`lintle` console script:**
- Location: `src/lintle/cli.py` (`main`), declared in `pyproject.toml` under `[project.scripts]`.
- Triggers: user shell.
- Responsibilities: all subcommands (`clean`, `verify`, `dedup`, `extract`, `diff`, `explain`, `report`).

**`python -m lintle`:**
- Location: `src/lintle/__main__.py`
- Triggers: module execution. Calls `lintle.cli.main`.

**Wizard (no-subcommand TTY menu):**
- Location: `src/lintle/wizard.py`
- Triggers: `lintle` run with no subcommand on a TTY. `cli` imports it only in that branch.
- Responsibilities: build argv and call `cli.main`. It reimplements no orchestration.

**Per-file worker entry:**
- Location: `src/lintle/pipeline.py` (`process_file`)
- Triggers: `src/lintle/worker_pool.py` via `ProcessPoolExecutor`.

## Architectural Constraints

- **Threading:** Process-based parallelism only. `ProcessPoolExecutor` runs one file per worker. The parent process owns progress display and checkpoint writes. Workers ignore SIGINT. Within a process the code is single-threaded, apart from the locked progress table in `cli_progress.py`.
- **Global state:** The two shared rich Consoles in `src/lintle/term.py` (`stderr_console`, `stdout_console`) are module-level singletons. `src/lintle/__init__.py` holds the output-naming constants (`CLEANED_SUFFIX`, `BROKEN_DIRNAME`, and others). These are the only shared constants; there are no mutable module-level caches.
- **Circular imports:** None in the module graph, by design. Two known cycles are broken with lazy imports:
  - `cli ↔ wizard`. `cli` imports `wizard` only inside the no-subcommand branch. `wizard` imports `cli` only inside its dispatch.
  - `cli_progress` and `summary` are presentation leaves. `cli_progress` imports `summary` one way.
- **Sgp4 wall:** `sgp4` is imported only in `src/lintle/verify/orbit.py`, lazily, and only under `--orbit`. Import-graph tests in `tests/` enforce that the clean path never reaches `sgp4` or `lintle.verify`.
- **Dependency direction for verify:** `verify → repair` (for `normalize_edges` and `is_content_free`) is allowed. `repair → verify` is not. `repair`'s own closure stays `sgp4`-free.
- **Epoch leaf:** `src/lintle/epoch.py` must stay stdlib-only. An import-graph test pins it.
- **Constant memory:** Every streaming path holds at most two lines of a file, or one bounded spill buffer (verify). Line length is capped at `_MAX_LINE_BYTES = 4096` in `pipeline.py`.
- **POSIX-only locking:** `fcntl.flock` in `fsutil.out_dir_lock`. Windows is out of scope.

## Anti-Patterns

### Importing `sgp4` or `lintle.verify` from the clean path

**What happens:** A new helper in `pipeline.py`, `repair.py`, `tle.py`, or the clean path of `cli.py` pulls in `sgp4` or `lintle.verify`.
**Why it's wrong:** `sgp4` is permissive. It would create a second validity definition (Critical Rule #4). The import-graph test fails.
**Do this instead:** Put validity logic in `src/lintle/tle.py`. Put orbit consistency logic in `src/lintle/verify/orbit.py`.

### Writing outputs without `durable_replace`

**What happens:** A writer calls `open(dest, "w")` or `os.rename` directly.
**Why it's wrong:** A crash can leave a half-written file in `01-cleaned/`, `03-report/`, or the checkpoint. Durability is also lost.
**Do this instead:** Write to a deterministic `.partial` path, then call `fsutil.durable_replace` (see `src/lintle/fsutil.py`). For chunked streams, use `src/lintle/chunking.py`.

### Redefining validation or repair rules in a consumer

**What happens:** `verify`, `dedup`, or `extract` restates the edge-normalisation sequence or the "is this line droppable" predicate.
**Why it's wrong:** Source-diff alignment desynchronises when the two definitions drift (see the `#155` and `VRFY-ORIGIN-MISSING` history in the root `ARCHITECTURE.md` §4).
**Do this instead:** Import `normalize_edges` and `is_content_free` from `src/lintle/repair.py`. Import `catalog_of` and `element_set` from `src/lintle/verify/checks.py` and `records.py`.

### Loading a corpus file whole

**What happens:** Code calls `read()` or `readlines()` on a `data/source/` file.
**Why it's wrong:** The largest file is 3.2 GB. Critical Rule #3 forbids it.
**Do this instead:** Stream with `handle.readline(_MAX_LINE_BYTES)` as `src/lintle/pipeline.py` does.

## Error Handling

**Strategy:** Per-record errors become quarantine entries with a `RuleID`. Per-file errors become `FileStats` failures handled in `worker_pool.py`. Command-level preconditions exit with code 2 and write nothing.

**Patterns:**
- Records: `repair.process_record` returns `Accepted` or `Quarantined`. It never raises for bad data.
- Garbage at the boundary: `epoch.parse_epoch` raises `ValueError`. Consumers (`dedup`, `verify.revalidate`) catch it and report it as a finding, not a crash.
- Command preflight: `extract` validates the whole `05-dedup/import.*` set once before writing. Failures exit 2 and leave the prior output intact.
- Interrupts: parent catches SIGINT once and terminates workers (`process_control.py`). The checkpoint keeps completed files.
- Lock contention: `fsutil.LockHeldError` maps to exit 2.

## Cross-Cutting Concerns

**Logging:** No logging framework. Status and errors go through `term.py` (`error:`, `warning:`, `note`). Live progress goes through `cli_progress.py`. Non-TTY output is plain lines.

**Validation:** Single definition in `src/lintle/tle.py`. Consumers call `tle.validate_record` and do not restate rules.

**Authentication:** Not applicable. The tool is local and file-based.

**Determinism:** Structured and stdout outputs are byte-deterministic. JSON writers use `fsutil.json_document` (sorted keys) or `report.render_run_json` (insertion order). Dict and `Counter` order is preserved at output boundaries.

---

*Architecture analysis: 2026-11-02*
