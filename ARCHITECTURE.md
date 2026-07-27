# lintle architecture

This is the **living design reference** for `lintle` — the validator and cleaner for
Two-Line Element (TLE) corpus files exported from
[space-track.org](https://www.space-track.org/). It is the permanent successor to the
dated design specs, implementation plans, and corpus-run summaries that now live under
`docs/superpowers/archive/` as historical records (see [Design history](#9-design-history)).

It is **self-sufficient**: a reader — whether maintaining the code or consuming its
output — never needs the archive. `README.md` is the user guide; this document is the
design reference behind it. The code is the ultimate source of truth; where this document
describes a contract, that contract has been verified against what the tool actually emits.

---

## 1. Purpose & principles

`lintle` audits a ~30 GB TLE corpus against the standardized TLE specification, repairs the
systematic export defects, and emits a uniform, de-defected corpus that any SGP4 /
orbital-mechanics library can ingest directly. Records it cannot *safely* repair are
**quarantined** — never silently mangled — into a per-file sidecar detailed enough to file a
defect report with space-track.

**One validator.** A single module (`tle.py`) defines what a "perfect" TLE record is —
column layout, semantic ranges, the mod-10 checksum, and line pairing. The `clean` command
reuses that definition to emit only records that pass it. There is no second "perfect."

These four principles are the reason the design exists. An implementation that breaks one is
wrong, not merely suboptimal.

1. **Validated transformation.** Never apply a fix and trust it. The cleaner applies a
   candidate fix, re-runs *full* validation on the result, and commits it only if it now
   passes — otherwise the record is quarantined. Every line in `cleaned/` is therefore valid
   by construction; the cleaner cannot turn a bad record into a wrong-but-valid-looking one.
2. **Correctness over recovery.** Never emit a wrong-but-valid-looking record; when in doubt,
   quarantine. No reconstruction of missing *data* characters. The one sanctioned
   reconstruction is a missing *checksum* digit, which is deterministically recomputable from
   columns 1–68 — and even that is a distinct, weaker repair tier with its own reporting (see
   [§4](#4-repair-model)).
3. **Constant memory.** Files stream; the pairing state machine holds at most two lines at
   once. A 3.2 GB file must never be loaded whole, and no per-file structure grows with record
   count.
4. **One validator definition.** "Perfect" is defined once, in `tle.py`. There is never a
   second, divergent validation path — which is precisely why `sgp4` (a *permissive* parser) is
   walled out of the clean/validate/repair path. `sgp4` is a physics engine used only by `lintle
   verify` to measure orbit *consistency*, never a validity authority; an import-graph test
   enforces the wall.

---

## 2. Module map & data flow

All package code lives under `src/lintle/`. Module dependencies flow one way, so cycles are
structurally impossible.

```
cli.py ──▶ pipeline.py ──▶ repair.py ──▶ tle.py
  │             │
  │             ├──▶ report.py ──▶ report_aggregation.py
  │             └──▶ report_writers.py ──┘ (imports report.py one-way)
  │
  ├──▶ cli_progress.py  (rich live progress + roster; → pipeline's progress messages, summary's shared formatters)
  ├──▶ resume.py        (single-run checkpoint + run-stamp/output-size helpers; → __version__, fsutil, stem, naming-constants)
  ├──▶ run_planning.py  (disk-space guard + output scrub + resume/fresh-run decision; CleanConfig + RunPlan; → fsutil, report, resume, term)
  ├──▶ worker_pool.py   (process-pool dispatch + progress collection; → pipeline, cli_progress, process_control, report, resume, run_planning, term + stdlib futures/mp/signal)
  ├──▶ output_artifacts.py (clean-run report.md/json + NDJSON/JSONL finalization; → report, report_writers, cli_progress, term)
  ├──▶ thresholds.py    (--max-quarantined parsing + quality-gate exit policy; pure, no internal deps)
  ├──▶ process_control.py (worker SIGINT setup + fast pool termination; → term; also used by worker_pool)
  ├──▶ diff.py          (read-only consumer of report.jsonl)
  ├──▶ explain.py ──▶ explain_examples.py
  ├──▶ summary.py       (aggregate-panel renderer + read-only `lintle report` over report.json; → term)
  ├──▶ term.py          (stderr+stdout rich Consoles + error/warning/note/prompt + is_interactive/prompt_yes_no)
  ├──▶ verify/          (`lintle verify` auditor — own package; → tle, term; cli edge lazy, never in the clean path)
  ├──▶ dedup.py ──▶ verify/, history.py  (`lintle dedup` — latest-re-issue import list + per-satellite manifest; → verify.{checks,grouping,records}, history, chunking, fsutil, term; cli edge lazy)
  ├──▶ extract.py ──▶ dedup.py, verify/, history.py  (`lintle extract` — per-satellite TLE history; → dedup naming constants, verify.{checks,records}, history, chunking, fsutil, term; cli edge lazy)
  └──▶ wizard.py ──▶ config.py   (no-subcommand TTY menu; cli.main is injected — wizard never imports cli)

verify/   (lintle verify — Increment-1 core + opt-in --orbit physics pass; cli ──▶ verify, never the reverse)
  __init__.py (run) ──▶ checks.py ──▶ tle.py
                          ├──▶ grouping.py ──▶ records.py ──▶ epoch.py
                          ├──▶ report.py          (checks ──▶ records, report; records ──▶ __init__ naming constants; __init__ ──▶ term)
                          └──▶ orbit.py ──▶ sgp4   (Increment 2; lazy-imported only under --orbit)

fsutil.py    stdlib-only I/O leaf — durable_replace + out_dir_lock
diagnostics.py, categories.py, explain_examples.py    pure-data leaves (no I/O)
config.py    stdlib-JSON leaf — ./.lintle.json load/save (no internal deps)
history.py   pure history-reduction leaf — HistoryStats/Gap + analyze_epochs (gap/median
             math), lifted out of extract._analyze so extract and dedup share one
             definition; no I/O, no sgp4; imports only stdlib + verify.epoch.parse_epoch
```

| Module | Owns |
|--------|------|
| `tle.py` | The validator: column layout, mod-10 checksum, semantic ranges, record pairing. The single definition of "perfect." Pure functions, no I/O. |
| `repair.py` | Speculative fixes, each confirmed by `tle.py` before commit; the `Accepted` / `Quarantined` record outcomes. Pure functions. |
| `pipeline.py` | Streams a file in binary, pairs `1 `/`2 ` lines into records, routes each to clean output or quarantine. Owns the per-file `process_file` worker entry. |
| `report.py` | `FileStats` and its sibling dataclasses, the `summary_dict` / `build_run_envelope` JSON shapes, and the Markdown `report.md` / JSON `report.json` writers (`write_run_json` is the byte-identical twin of the `--report json` stdout envelope). |
| `report_aggregation.py` | Pure corpus aggregation helpers for run totals and per-NORAD rollups consumed by `report.py`. |
| `report_writers.py` | Structured-file writers leaf: the `.broken.txt` sidecar (`BrokenFileWriter`), the `report.jsonl` findings shards (`JsonlFindingsWriter`), the `QuarantineSink` (bounded sample + streaming), `broken-noradids.ndjson`, and shard concatenation. Imports `report.py` one-way. |
| `output_artifacts.py` | End-of-clean-run finalization for `report.md`, the machine-readable `report.json`, `broken-noradids.ndjson`, and corpus-wide `report.jsonl` — all committed in one place. |
| `resume.py` | The single-run `.clean-state.json` checkpoint for `clean --resume`: input fingerprinting, checkpoint build/load, the resume-decision matrix, the run-start timestamp, per-file output-size capture, and the typed `CompletedEntry` dataclass (issue #118). Imports only `__version__`, `fsutil`, and `stem`/naming-constants — an actual leaf: `CompletedEntry`'s `summary`/`outputs` dicts are built by its caller (worker_pool), so resume never imports `report`. |
| `run_planning.py` | Clean-run preflight: disk-space policy, resume classification, fresh-run output scrubbing, and the resolved `RunPlan` (slots=True). Also owns `CleanConfig` (issue #121) — the typed `clean`-command configuration snapshot built once in `cli.main` and passed to both leaf functions instead of a raw argparse `Namespace`. Imports `fsutil`, `report`, `resume`, `term`. |
| `worker_pool.py` | Process-pool dispatch, progress collection, per-file failure handling, checkpoint updates via `resume.CompletedEntry.from_stats` (issue #118), and interrupt shutdown. Now imports `run_planning` for the `CleanConfig` type (one-way, no cycle). |
| `fsutil.py` | `durable_replace` (the one atomic+fsync commit path) and `out_dir_lock` (the advisory-flock out-dir lock). Stdlib only. |
| `chunking.py` | Fixed-count output chunking: `ChunkedWriter` splits every record/line output stream into `<stem>.NNNNN.<suffix>` chunks of `--chunk-records` units (default 1,000,000), committing each chunk via `durable_replace` the instant it fills and scrubbing a stem's prior set on first open (invariant 5); `ChunkedReader` reassembles a stem's set in index order as one logical stream. Concatenating a set == the pre-chunking single file. Stdlib-only leaf; imports only `fsutil`; never `sgp4`. Depended on by `pipeline`, `report_writers`, `resume`, `verify/records`, `verify/report`, `dedup`, and `diff`. |
| `diff.py` | Read-only: per-rule delta between two runs' `report.jsonl` (`lintle diff`). |
| `explain.py` | Read-only: renders rule/fix documentation (`lintle explain`). |
| `summary.py` | Responsive aggregate-panel renderer over the `build_run_envelope` dict (plain/medium/wide tiers + ASCII-bar fallback), keyed off the target Console; backs `clean`'s end-of-run stderr panel and the read-only `lintle report` (renders `<out-dir>/report.json`: text → panel on stdout, json → file bytes verbatim). Also owns the phase-3 `render_files` per-file results table and the formatters both per-file tables share — `display_tier` (the narrow/medium/wide width boundaries), `format_clock` (M:SS durations), `format_size` (gnu byte units). Imports `humanize` for those and for human-readable panel durations (`precisedelta`). Styled UI, not byte-bound. |
| `diagnostics.py` | Stable `RuleID` registry + structured `Diagnostic` dataclass + `RepairTier`. Pure data. |
| `categories.py` | `FixClass` enum + `FixSpec` registry — the repair taxonomy. Pure data. |
| `explain_examples.py` | Validator-verified examples + citations backing `explain`. Pure data. |
| `thresholds.py` | Pure `--max-quarantined` parsing and quarantine-threshold exit-code policy. |
| `process_control.py` | Signal/worker shutdown helpers (SIGINT setup, fast pool termination, cancel/exit-code) used by `cli.py` and `worker_pool.py`. |
| `term.py` | Two shared `rich` Consoles — `stderr_console` for status/errors, `stdout_console` for the `report` result view — the `error:` / `warning:` / `note` / `prompt` emitters, and the `is_interactive` / `prompt_yes_no` stdin helpers. |
| `cli.py` | argparse, globbing, and top-level `clean` orchestration: delegates preflight to `run_planning`, dispatch to `worker_pool`, signal/shutdown to `process_control`, the quality-gate exit policy to `thresholds`, run finalization to `output_artifacts`, and the aggregate panel / `report` render to `summary`; owns the resulting process exit code. |
| `cli_progress.py` | Rich presentation leaf: the phase-1 `render_roster`, the phase-2 live `ProgressDisplay` (a `rich.live.Live` over a `rich.table.Table` — in-flight rows plus a pinned summary row, bounded so terminal height and resize cannot strand or crop it), and the `status` spinner for the single-process post-run phases (`verify`, `dedup`, `extract`, `diff`). Consumes `pipeline`'s typed progress messages and `summary`'s shared tier/duration/size formatters, so phases 2 and 3 cannot disagree about a boundary or a format. Every live block is disabled off a TTY. |
| `verify/` | The `lintle verify` post-run auditor (own package): `epoch` (epoch-key parsing), `records` (the `CleanedRecord` dataclass + cleaned-tree readers), `grouping` (the spill-to-disk `ExternalSorter` for the constant-memory contradiction pass), `checks` (`revalidate` / `find_conflicts` — both routing the #158 same-epoch clash through the one `_is_clash` predicate `has_epoch_clash` also uses — / the `SourceAligner` byte-diff: a null object built via `SourceAligner.open`, inert when a stem has no source, whose `feed(rec, revalidated=...)` internalizes the revalidate-skip policy so `run`'s loop carries no aligner guards), `report` (`VerifyRule`, `Suspect`, and the `SuspectSink` (which owns the exit-code rule) — an external merge-sort that streams suspects to disk and renders `suspects.jsonl` + `summary.{json,md}` at flat peak memory, byte-identical to its list renderers; #156), a `collections.Counter`-based `epoch_distribution` record-density histogram (`{"YYYY-MM": count}` over revalidated records only — a sibling of `checked` in `summary.json`, plus an `### Epoch distribution` section in `summary.md` — purely informational, never feeding `counts`/`hard`/`exit_code`), `__init__.run` (orchestration), and `orbit` (Increment 2 — the opt-in sampled `sgp4` orbit-consistency pass: adjacent-pair position residuals, soft `VRFY-ORBIT-OUTLIER` outliers, deterministic 0.1 km quantum; refined in #163 with regime-aware gap gates (GEO 7-day vs LEO/MEO 3-day), a windowed local-median threshold term, leave-one-out culprit isolation for lone interior spikes, a `--sensitivity {sensitive,strict}` dial, and dup-epoch stratified oversampling of the satellite sample). A pure *consumer* of `tle` (its sole validity authority) and `term`; `orbit` is the package's **only** `sgp4` importer, lazily imported by `run` only under `--orbit` so the default path stays `sgp4`-free. |
| `dedup.py` | The `lintle dedup` pass: reads `01-cleaned/` (never mutating it), excludes hard suspects from a prior `verify` run's `suspects.jsonl`, groups by `(catalog, epoch)` through `verify`'s `ExternalSorter`, and writes `<out-dir>/05-dedup/{import.txt,notes.jsonl,manifest.jsonl,summary.json}` — one card per group, the latest re-issue kept, benign re-issues (including refined orbits under a new element-set) collapsed and genuine same-element-set contradictions kept-latest-but-flagged. Reuses `verify.checks.orbital_state`/`element_set` and the shared `has_epoch_clash` #158 predicate (one definition of "same orbit" / "latest" / "same-epoch clash" — #164). Also streams each kept card's catalog/epoch/element-set into `history.analyze_epochs`, flushing one `manifest.jsonl` row per satellite on each catalog boundary (a single plain file, never chunked — see §6), and stores a stat-only `cleaned_fingerprint` of `01-cleaned/` in `summary.json` for `extract`'s staleness check. Constant memory; deterministic bytes. |
| `extract.py` | The `lintle extract` pass: reads a prior `dedup` run's sorted fixed-width `import.*` chunk set (140 bytes/record — guarded, never assumed) and binary-searches one catalog's contiguous run into `<dest>/<id>.txt` (verbatim byte slice, epoch-ascending) plus a deterministic `<id>.json` stats sidecar (schema v2: median spacing, the 10 largest reportable gaps — delta > 10× the satellite's median spacing, via the shared `history.analyze_epochs` — and a tri-state quarantine flag from `03-report/broken-noradids.ndjson`); warns and, on a TTY, asks y/n before exporting a gappy or quarantine-affected history (non-TTY: warn + proceed; decline = skip, not an error), where `<dest>` defaults to `<out-dir>/06-extract` (with its own README) and only an explicit `--dest` overrides it, undecorated. Also recomputes `verify.records.cleaned_fingerprint` at run start and warns (never fails) when it disagrees with the one `dedup` stored in `summary.json` — `01-cleaned/` drifted since that `dedup` run. Read-only, local, no index artifact; reuses `verify`'s `catalog_of`/`element_set` (epoch/gap math now behind `history.py`) so catalog, epoch, and gap definitions each stay singular. |
| `history.py` | Pure history reduction shared by `extract` (the `<id>.json` sidecar) and `dedup` (`manifest.jsonl`): `analyze_epochs(epochs, elsets) -> HistoryStats` given one satellite's epoch datetimes and element-set numbers in stream order — count, span, median spacing, and the `GAPS_CAP` (10) largest reportable gaps (delta > `GAP_FACTOR` (10) × median spacing), plus the `epoch_dt`/`iso` helpers. No I/O, no `sgp4`; imports only stdlib + `verify.epoch.parse_epoch`. Lifted out of `extract._analyze` so the two callers cannot compute divergent numbers for the same satellite. |
| `config.py` | Optional `./.lintle.json` project config: stdlib-JSON `load`/`save` of the two remembered keys (`source`, `output`). Never an authority over an explicit CLI arg. No internal deps. |
| `wizard.py` | The interactive rich menu shown when `lintle` runs with no subcommand on a TTY (configure / clean / verify / report / quit). A thin front-end that builds an argv and calls `cli.main`, reimplementing no orchestration; imports `config` + `term`. |

`tle.py` and the data leaves carry no I/O. `report_writers.py` depends on `report.py` (never
the reverse), so the structured writers and the renderers stay acyclic.

**`verify/` and the wizard stay acyclic too.** The `verify/` package flows one way —
`__init__ ──▶ {checks, grouping, records, report}`, `checks ──▶ tle` + `records` + `report`,
`grouping ──▶ records ──▶ epoch`, with `records` reaching back only to `__init__`'s
output-naming constants — and `cli ──▶ verify`, never the reverse. The clean/validate/repair
path (`pipeline`, `repair`, `tle`, `cli`'s clean path) never imports `lintle.verify` or
`sgp4`, so the auditor can never become a second validity definition; an import-graph test
enforces the wall (see §7). `dedup.py` is a second read-only consumer of the same package —
`cli ──▶ dedup ──▶ verify.{checks, grouping, records}` (+ `term`), one-way, and equally barred
from the clean path — so it reuses `verify`'s one definition of "same orbit" / "latest re-issue"
rather than spawning a divergent second one. `dedup.py` also imports `history.py` (the shared
gap/median reducer) to build `manifest.jsonl` — a fourth acyclic leaf, no new edge back into
`verify` or the clean path. `extract.py` is a third read-only consumer, one hop further
downstream — `cli ──▶ extract ──▶ dedup` (naming constants only) `+ verify.{checks, records}`
`+ history` (+ `chunking`, `fsutil`, `term`), also one-way and lazily dispatched, also barred
from the clean path, and also equally barred from ever reaching `sgp4`. `extract` no longer
imports `verify.epoch` directly — the epoch/gap reduction moved behind `lintle.history`, which
itself imports only `verify.epoch.parse_epoch` — so the import-graph closure test walks
`extract`'s transitive imports (reaching `history` and, through it, `verify`) and separately
`test_verify_submodules_are_sgp4_free_except_orbit` sweeps every `verify/*.py` module except
`orbit.py` (`checks`, `epoch`, `grouping`, `records`, `report`, `__init__`) for `sgp4`-freedom
unconditionally — so `epoch.py`'s own `sgp4`-freedom is checked independent of which caller
imports it, rather than being pinned to one caller's import list (see §7). The `cli ↔ wizard` cycle — `cli` launches the menu,
the menu re-enters `cli.main` — is broken by a lazy import on *both* sides (`cli` imports
`wizard` only inside the no-subcommand TTY branch; `wizard` imports `cli` only inside its
dispatch), so `wizard ──▶ config`/`term` are the only module-load-time edges.

**Output-naming constants.** `CLEANED_SUFFIX`, `BROKEN_SUFFIX`, `FINDINGS_SUFFIX`,
`CLEANED_DIRNAME`, `BROKEN_DIRNAME`, and `SHARDS_DIRNAME` live in `lintle/__init__.py` —
the single source of truth for the naming convention. `pipeline._clean_output_paths`,
`resume.output_sizes`, `cli.discover_paths`, and `report_writers.concat_findings_shards`
all import them from there rather than re-encoding the convention.

---

## 3. The validator (`tle.py`)

`tle.py` is the single definition of a perfect TLE record. It is pure (no I/O) and is the
correctness oracle the whole tool is built around. A record is two fixed-width lines.
Validation happens in escalating layers, each gating the next so the most fundamental error
surfaces first.

- **Line length.** Each line must be exactly **69 ASCII columns** (`LINE_LENGTH = 69`).
- **Column layout.** Columns 1–68 are checked against a fixed positional spec
  (`_LINE1_CHARS`/`_LINE1_FIELDS`, `_LINE2_CHARS`/`_LINE2_FIELDS`): single-character positions
  (line-number digit, classification, separators, signs, decimal points) and multi-character
  fields (catalog number, international designator, epoch, derivatives, B\*, inclination, RAAN,
  eccentricity, etc.) each carry an allowed-character set.
- **Semantic ranges.** Only checked once the column layout is sound. Epoch day-of-year in
  `(0, 367)`; inclination in `[0, 180]`; RAAN, argument of perigee, mean anomaly in `[0, 360)`;
  eccentricity in `[0, 1)`; mean motion strictly positive.
- **Checksum.** Column 69 is a **mod-10 checksum** of the first 68 characters — each digit adds
  its value, each `-` adds 1, every other character adds 0, result is `sum % 10`. Checked last,
  after the body, so a record with both a bad layout and a bad checksum is reported as a layout
  defect, not a checksum one.
- **Pairing.** `validate_record(line1, line2)` requires each line to validate *and* the
  satellite catalog numbers (columns 3–7) to match between the two lines.
- **NORAD extraction.** `extract_norad_id` recovers the 5-digit catalog ID from a line 1 for
  programmatic reporting of quarantined records. It deliberately returns `None` for Alpha-5
  letter-prefixed IDs, keeping the downstream contract a plain integer.

This is reference-level; the code is authoritative for the exact column offsets and ranges.

---

## 4. Repair model

The cleaner never guesses a data character. It applies a small, fixed-order set of
content-safe transformations, then re-validates the candidate and commits only on success
(principle #1). What it cannot make pass, it quarantines (principle #2).

### The redundancy paradox

`lintle` never invents data — it emits only information that was already in the record. The
**single exception** is the column-69 checksum, and it is an exception *precisely because* the
checksum carries no information of its own: it is a deterministic mod-10 function of columns
1–68, so recomputing a missing one asserts nothing the record didn't already say. The only
field safe to rebuild is the one field that was redundant to begin with. A mod-10 checksum has
a 1-in-10 chance of accepting a wrong line by luck, so inventing an orbital-data character would
risk emitting a record that *looks* valid but is silently wrong — the one outcome worse than
dropping it.

**Reconstruction is opt-in (default off).** Even the checksum exception carries a residual
risk: a 68-character line is ambiguous — it could be a record exported without its column-69
digit, *or* a 69-character record that lost its last *data* character, in which case the old
checksum digit has slid left into a data field and a freshly-appended checksum would certify
wrong data as clean (issue #82). Because the two cases are indistinguishable from the bytes
alone, `clean` **quarantines** a checksumless line by default (principle #2 — when in doubt,
quarantine). The `--reconstruct-checksum` flag opts in to the recompute for corpora where
dropped checksums are known to be the cause; it is threaded through `cli → pipeline → repair`
and is part of the resume run-identity, so flipping it makes an in-progress run re-process
rather than fold mismatched outputs together.

### The five fix classes

`FixClass` (in `categories.py`) is the single source of truth for the tags that appear in
`Accepted.fixes`, `stats.fix_counts`, and the `report.md` "Fixes applied" table. Listed in
decreasing order of safety:

| Class | Examples | Action |
|-------|----------|--------|
| Content-preserving | trailing `\` (`trailing-backslash`), CRLF (`crlf`), trailing whitespace (`trailing-ws`) | auto-fix (checksum survives as an independent check) |
| Reconstructed-checksum | a record exported without its column-69 digit (`reconstructed-checksum`) | recompute the checksum from intact columns 1–68 — **opt-in** via `--reconstruct-checksum`; otherwise quarantined (see *redundancy paradox*) |
| Content-shifting | leading whitespace (`leading-trim`) | trim, then re-validate; quarantine if it fails. `iter_records` matches the `1 `/`2 ` prefix on a leading-whitespace-trimmed *view* of the line, so an indented record still pairs; the raw bytes are carried forward so `repair_line` owns the trim. A leading BOM is **not** trimmed — it is a non-ASCII byte and is quarantined |
| Structural | blank / whitespace-only / CR-only lines | drop, resynchronise pairing |
| Corrupt | bad checksum, wrong length, orphan line, garbled columns, catalog mismatch | **quarantine** |

The concrete `FixClass` members are `crlf`, `leading-trim`, `trailing-ws`,
`trailing-backslash`, and `reconstructed-checksum`. Fix order inside `repair_line` is fixed:
strip CRLF → strip leading whitespace → strip trailing whitespace → strip a trailing backslash
→ build a 69-character candidate (reconstructing the checksum only if the line is 68 chars, its
body is valid, *and* `--reconstruct-checksum` was passed — otherwise the 68-char line is
quarantined as a length error) → a single full re-validation of the candidate.

Because trailing-whitespace stripping runs *before* the length is measured, a checksum-less
68-character line whose column 68 is a legitimately-allowed space (the `_DIGIT_SPACE`
element-set-number and revolution-number fields permit one) is normalized to 67 characters and
quarantined as a `LINE_LENGTH` error (`observed=67`) rather than taking the missing-checksum
path — *even under `--reconstruct-checksum`* (issue #108). This is intentional, not a gap: a
trailing space on a checksum-less line is genuinely ambiguous (last data column vs. junk
whitespace), so Critical Rule #2 dictates quarantine over a guessed reconstruction. The
conservative outcome (never wrong output) holds either way; only the diagnosis differs.

### Repair tiers

A `Diagnostic` records which **`RepairTier`** was attempted before it fired, so consumers can
downgrade trust on records that survived a stronger repair attempt:

- `none` — quarantined with no repair attempt.
- `tier-1` (`NORMALIZATION`) — CRLF / whitespace / trailing-backslash normalization.
- `tier-2` (`CHECKSUM_RECONSTRUCT`) — missing-checksum reconstruction. A `tier-2` record that
  still fails (e.g. a catalog mismatch after both lines survived checksum reconstruction) is a
  stronger corruption signal than one caught at first read.

### Outcomes

`repair.process_record` returns one of two dataclasses:

- `Accepted(line1, line2, fixes)` — valid after repair; `fixes` lists the `FixClass` tags
  applied across both lines.
- `Quarantined(raw_lines, source_lines, primary, related)` — routed to quarantine. `raw_lines`
  preserves the original bytes for byte-faithful sidecar output; `primary` is the headline
  `Diagnostic` used for aggregation and the visible diagnosis; `related` carries supporting
  diagnostics (e.g. when both lines of a record fail).

`RuleID` (in `diagnostics.py`) is the stable, citable identifier vocabulary — the string value
is the **public contract** and is never reused or recycled. Current families: `TLE-COL-*`
(layout: length, interior-char-missing, non-ASCII byte, invalid layout), `TLE-CHK-001`
(checksum mismatch), `TLE-PAIR-*` (orphan line, bad prefix, catalog mismatch), `TLE-SEM-*`
(semantic ranges, reserved), `TLE-INT-001` (the cleaner itself raised on a record). An
import-time guard fails fast if a `RuleID` lacks a matching `RuleSpec`.

---

## 5. Streaming, parallelism, durability, resume

### Constant-memory streaming

`pipeline.iter_records` opens each file in **binary** so `\r` and stray bytes are observed
exactly, reads it line by line via `handle.readline(_MAX_LINE_BYTES)` (C-level, throughput
equal to the iterator on normal lines), and pairs lines with a prefix-driven state machine that
holds **at most two lines** (a line-1 awaiting its line-2). Blank, whitespace-only, and CR-only
lines are dropped. Pairing resynchronises on every `1 ` line, so one missing line cannot cascade
into a run of mispaired records. Memory is constant regardless of file size — a 3.2 GB file
never loads whole.

**Line-length cap (issue #95).** A genuine TLE line is 69 bytes; `_MAX_LINE_BYTES = 4096`
is the cap applied to each `readline` call. A chunk of exactly that size with no trailing `\n`
is treated as (the start of) an oversized line: the excerpt is kept as a bounded quarantine
payload, the remainder is drained cheaply in fixed-size chunks (summing their lengths into
`stats.bytes_consumed` so the counter still reaches `st_size`), and one `Orphan` with
`RuleID.LINE_LENGTH` is emitted for the logical line. This prevents a CR-only or newline-free
multi-GB file from materialising as one giant `bytes` object in the worker process and across
the pool's pickle boundary. The raw bytes in the quarantine entry are truncated (noted in the
`Diagnostic.note`); this is the one place byte-faithfulness yields to constant-memory, and only
for a pathological input — every real corpus file has `\n`-terminated lines well under the cap.

### Per-file parallelism

Each input file is processed in its own worker process via a `ProcessPoolExecutor` (default
`--jobs` = CPU count − 1, capped at the file count, floored at 1). A `multiprocessing.Manager`
queue carries per-file byte/record progress deltas back to the parent for the live display.
Workers ignore `SIGINT`; only the parent sees Ctrl-C, catches it once, and terminates the
workers directly (rather than waiting on `shutdown(wait=True)`, which would block until an
in-flight multi-minute file finished).

### Durable, atomic commit

`fsutil.durable_replace(tmp, dest)` is the **one sanctioned commit path** for every output
(`cleaned/*`, `.broken.txt`, `report.md`, `report.jsonl`, `broken-noradids.ndjson`, the
checkpoint). It: **fsync the temp file's data → `os.replace` onto the destination → fsync the
containing directory**. `os.replace` alone gives atomicity (a reader sees the old name or the
new one, never a half-write) but not durability; the fsyncs close that gap so a committed file
survives a hard power loss. On **macOS** the true barrier is `fcntl(fd, F_FULLFSYNC)`, not plain
`os.fsync` (which does not flush the drive's own write cache); `fsutil` selects the right one
per platform at import time. Outputs are written to deterministic `.partial` temp files, so a
killed run leaves at most one stale `.partial` per file (truncated next run), never random
debris.

### Out-dir lock

`fsutil.out_dir_lock` prevents two concurrent `clean` runs from corrupting a shared
`--out-dir`. It holds an **advisory `fcntl.flock`** on a `.clean.lock` sidecar for the life of
the run and **refuses** (`LockHeldError`, exit 2) when another live run already holds it. The
kernel owns the lock's lifetime — it is released the instant the holder closes its fd, exits, is
killed, or the host reboots — so liveness needs no PID check or boot-id and there is no
stale-lock reclaim step. This is what makes the lock robust against the two failure modes a
hand-rolled pidfile suffered (issues #87/#99): a crashed run frees its lock automatically
(no reboot wedge, no PID-reuse hostage), and because release is just closing *our own* fd, a
refused or raced run can never delete a live holder's lock (no TOCTOU reclaim race, no blind
release). The file is **never unlinked** — `flock` binds to the inode, so removing the path
would let a concurrent opener lock an orphaned inode while a fresh file is created in its place;
the leftover sidecar is reused next run and `run_planning` already treats it as scrub noise. The
sidecar still records `{host, pid, started}`, but now purely as informational text for the
`LockHeldError` message (which also names the file and the manual-removal escape hatch).

Concurrent runs **on one host** (the common case) are fully serialized. A `--out-dir` written
concurrently from **multiple hosts** over a network filesystem relies on `flock` propagating
server-side (modern NFSv4) and is not a tested configuration — give each host its own
`--out-dir`. POSIX-only (`fcntl.flock`); Windows is out of scope (use WSL).

`verify`, `dedup`, and `extract` participate in the same lock (via `cli._locked_postrun`): all
three stream `<out-dir>/01-cleaned` (or, for `extract`, `<out-dir>/05-dedup`) and write their
own subtree, so a concurrent `clean` scrubbing the out-dir mid-read would corrupt them. A
missing out-dir skips the lock — the consumer's own "no cleaned output" exit-2 error is the
friendlier failure.

### Single-run resume

`clean` maintains a `.clean-state.json` checkpoint in `--out-dir`, written via
`durable_replace` as each file completes and **deleted on full success** — so its *presence*
marks an interrupted run, and a finished run leaves none behind. `--resume` (or the default
prompt) consults it. **The unit of resumption is a whole file:** completed files are skipped, but
the file in progress at interruption is reprocessed from the start — there is no intra-file
checkpoint, since the streaming pairing state machine keeps no rewindable position, so a
single-file run gains nothing from resume. This is scoped to **completing one run**, not a
cross-run skip cache: each resumed run still re-validates every record it emits. See [§6](#the-checkpoint-clean-statejson-schema_version-3)
for the on-disk shape and the resume-decision matrix.

---

## 6. Outputs & machine-readable contracts

This is the most important permanent section: downstream consumers rely on these shapes. Every
contract below was verified against the tool's actual output.

### Output-tree layout

A successful `clean` run (followed, optionally, by `verify`/`dedup`/`extract`) lays out
`--out-dir` as one flat level of directories, numbered in pipeline order so the directory
listing itself documents the order of operations (0.11.0 — the 0.10.1 `data/` grouping is
retired):

```
<out-dir>/
├── README.md          — overview: the six dirs, order of operations, regen note
├── 01-cleaned/        — <stem>.NNNNN.cleaned.txt chunk sets      + README.md
├── 02-broken/         — <stem>.NNNNN.broken.txt sidecars          + README.md
├── 03-report/         — report.md · report.json · report.NNNNN.jsonl ·
│                        broken-noradids.ndjson                    + README.md
├── 04-verify/         — suspects.NNNNN.jsonl · summary.{json,md}  + README.md
├── 05-dedup/          — import.NNNNN.txt · notes.NNNNN.jsonl ·
│                        manifest.jsonl · summary.json              + README.md
└── 06-extract/        — <id>.txt · <id>.json                      + README.md
```

**Per-step dirs (0.11.0).** The out-dir root holds one numbered directory per pipeline
step — `01-cleaned`, `02-broken`, and `03-report` (written by `clean`), `04-verify`
(written by `verify`), `05-dedup` (written by `dedup`), and `06-extract` (written by
`extract`, defaulting there when `--dest` is not given) — plus a self-describing root
`README.md`. Every populated step dir also carries its own static `README.md`, written by
the step that owns it, at the same point it writes that dir's artifacts. Transient run
state (`.shards/`, `.clean-state.json`, `.clean.lock`, `.lintle-output`) stays at the root
as machinery, not step output. The naming constants (`CLEANED_DIRNAME` … `EXTRACT_DIRNAME`)
live in `lintle/__init__.py`, the single source of truth every consumer imports from.
Outputs from ≤ 0.10.3 (the `data/` layout, plus the unnumbered root `verify/`/`dedup/`
every 0.10.x wrote) or ≤ 0.10.0 (bare `cleaned/`/`broken/` at the root) are not
migrated — regenerate them; a fresh `clean` run scrubs all legacy layouts from
`--out-dir` before writing the new one.

**Chunked output layout.** Every record/line output *stream* is split into an always-indexed
`<stem>.NNNNN.<suffix>` chunk set of `--chunk-records` units each (default 1,000,000 ≈ 140 MB),
so no single file is ever huge (the worst pre-chunking, `05-dedup/import.txt`, was 28.7 GB).
Concatenating a set's chunks in index order (`cat <stem>.*.<suffix>`) is byte-identical to the
old single file — the invariant that keeps Critical Rules #1/#2 intact. The six invariants
(`chunking.py`): per-stream counting never global; always-index (no rename-on-roll); concat-identity;
atomic commit per chunk; stale-chunk scrub on (re)run/resume; constant memory. Aggregate *summary*
documents (`report.md`, `report.json`, `04-verify/summary.*`, `05-dedup/summary.json`,
`broken-noradids.ndjson`) are not streams and stay single files. `05-dedup/manifest.jsonl` is a
partial exception: it is one row per satellite (a genuine per-record-family stream, not an
aggregate summary), yet it is written as a **single plain file, never chunked** — see below.

Transient run state lives alongside and is removed on success: `.shards/` (per-worker
`report.jsonl` shards — kept whole intermediates, concatenated into the chunked `report.NNNNN.jsonl`
set then `rmtree`'d) and `.clean-state.json` (the resume checkpoint, whose `outputs` map records
every cleaned/broken *chunk* by name). On an interrupted or failed run, both survive so a later
`--resume` can rebuild a complete `report.jsonl` set from the shards. `report.md`, `report.json`,
the `report.*.jsonl` set, and `broken-noradids.ndjson` are **always** written on a successful
clean run — empty/zeroed when nothing was quarantined — so the consumer artifact set is stable.
The persisted `report.json` is what the read-only `lintle report` command renders later.

- **`01-cleaned/tleYYYY.cleaned.txt`** — standard 2-line TLE text, every record verified valid: 69
  ASCII columns per line, `\n`-terminated, matching catalog numbers, valid checksums.
- **`02-broken/tleYYYY.broken.txt`** — the byte-faithful quarantine sidecar (see below).

### stdout / stderr discipline

A hard channel rule, so output is safely pipeable:

- **`clean` stdout** = pipeable data only. With `--report json`, stdout carries *only* the JSON
  envelope (byte-identical to the persisted `report.json`). In text mode, `clean` stdout is
  **empty** — the human aggregate panel goes to stderr and the per-file detail lives in
  `report.md` / `report.json`. Never styled.
- **`clean` stderr** = the three-phase live `rich` UI, `processing…` notices, the end-of-run
  aggregate panel (text mode), and `error:` / `warning:` lines.
- **`report` stdout** = the rendered result view: `report` text renders the phase-3 per-file
  table and the aggregate panel to stdout (via `term.stdout_console`); `report --report json`
  echoes `report.json` verbatim.

### The `clean` display: one table, updated in place

A `clean` run renders **one** live table on stderr, from discovery to results. Every
discovered file has a row from the first frame, so that frame *is* the roster — index,
basename, size, and empty progress cells. Work then updates rows in place: a file starts,
its bar fills, and on completion the same row switches to its final records / clean /
quarantined / time. Nothing is appended while the run proceeds, and the final frame stays
on screen (`transient=False`) as the results view, with the aggregate panel printed under
it. One column set serves every row state — a pending row is blank rather than zeroed, a
running row carries its bar, a finished row its counts — so a row never changes shape
under the reader.

A `rich.live.Live` region cannot scroll: it only moves the cursor up within the viewport,
so a table taller than the terminal strands its overflow (measured: 467 stranded rows,
and 114 lines plus 8 duplicate headers after a resize). The table therefore **windows**
when the rows outnumber the terminal height: it shows a run of rows starting at the first
unfinished one — the active files and everything still to come — plus an `… N more`
marker. Rows outside the window keep their state and slide back in; they are not dropped.
`ProgressDisplay.windowed` records that this happened, and the caller then prints the
complete static results table (`summary.render_files`) after the run, so nothing the
window could not show is lost. When every row fits, that table is redundant and is
skipped.

Per-file completion lines print only off a TTY, where there is no table to update and
they are a piped run's only progress record; a *failure* prints its error on every
stream, since a row cannot carry the reason. Off a TTY the run degrades to the static
roster, those completion lines, and the static results table.

`verify` and `dedup` render the same way, over their own unit of work: every cleaned stem
has a row before any work starts, the row fills in as that stem streams (size, progress,
records, and the command's own result columns — hard/soft suspects for `verify`, records
read and hard-suspect exclusions for `dedup`), and the finished table is the results view.
The stages that follow the per-stem loop — the contradiction pass, the optional orbit
pass, the write — report themselves by relabelling the pinned summary row, so they need
no spinner and print no line; `verify` rewrites every row's suspect columns before the
frame freezes, since contradiction and orbit findings are attributed to their stems only
after the stream. The verdict line prints after the table closes, so it lands under the
results rather than above a live region. Per-stem progress is exact rather than estimated:
a cleaned record is two 69-column lines plus newlines, so a record count converts to bytes
(clamped, in case a chunk is ever short).

`extract` keeps a static roster and results table instead: each id is a sub-second binary
search, and it prompts y/n mid-run for a gappy history — a live region would fight the
prompt. `diff` renders its two deltas as tables on a TTY and its byte-exact plain text off
one, because piped `diff` output is a grep target.

Every results table is built by `summary.results_table`, which fixes the chrome — `SIMPLE`
box, dim right-justified index, left-justified name, right-justified numbers — so no two
commands' results can drift apart.

Column tiers go through the one `summary.display_tier` (narrow < 80 ≤ medium < 100 ≤
wide): wide carries size, bar, %, records, clean, quarantined, time; medium drops size and
time; narrow also drops the bar, keeping the percentage and the result columns. Columns
disappear whole, values are never truncated, and widths are pinned from pre-dispatch
bounds so no column reflows mid-run.

- rich styling on either Console is applied only when that stream is a TTY; off a TTY (pipe,
  `capsys`, `NO_COLOR`) it degrades to plain literal text, so even the panels stay readable.
- The structured output **files** and the `--report json` stdout bytes are never routed through
  a `rich` Console — they go through plain `json` / file writers for byte-determinism.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Quarantine count (or rate) is at or below `--max-quarantined` (default `0`). |
| `1` | Quarantine count (or rate) **exceeded** `--max-quarantined` — the quality gate. |
| `2` | Operational/usage error: bad args, no input files, disk shortfall, lock held, a file that failed to process, or a stale/corrupt/declined resume (including EOF at the prompt). |
| `129` | Terminated by `SIGHUP` (128 + 1). |
| `130` | Interrupted by `SIGINT` / Ctrl-C (128 + 2). |
| `143` | Terminated by `SIGTERM` (128 + 15). |

Ctrl-C is a first-class exit for **every** subcommand, not just `clean`: `cli.main` wraps the
whole dispatch in a `KeyboardInterrupt` backstop that prints one `cancelled.` line and returns
`130`, so no command can ever hand the operator a traceback. `clean` still handles its own
SIGINT inside the worker pool first (it has resume guidance to give); the backstop covers the
windows outside that block and the single-process consumers (`verify`/`dedup`/`extract`), whose
subtrees are committed only through the atomic durable writes at the end — a cancelled consumer
leaves the previous tree intact and releases the out-dir lock on the way out.

`--max-quarantined` accepts a bare integer (absolute record count) or a value with a trailing
`%` (percentage of routed records = `clean + quarantined`, cross-multiplied to avoid
divide-by-zero and float drift). The default `0` means "any quarantine fails."

### The `--report json` envelope — `schema_version "3"`

A single top-level JSON object. Every field is **required and non-nullable**; empty maps render
`{}`, empty arrays `[]` — never omitted, never `null`. This is the verified shape (one valid
pair, one checksum-flipped quarantine):

```json
{
  "schema_version": "3",
  "run": {
    "command": "clean", "timestamp": "2026-05-31T15:34:44Z",
    "elapsed_seconds": 0.26, "failed_files": []
  },
  "environment": { "tool_version": "0.3.0", "python_version": "3.14.5" },
  "summary": {
    "files_processed": 1, "paired_records": 2, "orphan_entries": 0,
    "input_lines_seen": 4, "clean_count": 1, "quarantined_count": 1,
    "failed_count": 0, "fix_counts": {}, "quarantine_counts": { "TLE-CHK-001": 1 }
  },
  "files": [
    { "src_name": "tle_demo.txt", "elapsed_seconds": 0.028, "bytes": 280,
      "records_per_sec": 69.68, "paired_records": 2, "orphan_entries": 0,
      "input_lines_seen": 4, "clean_count": 1, "quarantined_count": 1,
      "fix_counts": {}, "quarantine_counts": { "TLE-CHK-001": 1 },
      "dropped_counts": {}, "quarantined_norad_ids": { "25544": { "TLE-CHK-001": 1 } } }
  ]
}
```

`schema_version` is a **string** to leave room for additive tags like `"3.1"`. Adding optional
fields stays under `"3"`; renaming or removing one bumps the major. History: `"1"` → `"2"` when
`reject_counts` was renamed `quarantine_counts`; `"2"` → `"3"` when `run.failed_files` and
`summary.failed_count` were added (issue #83) — both fields are **required** (not
additive-optional) so the bump is mandatory.

**Top-level / `run` / `environment` / `summary`:**

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | exactly `"3"` in this release |
| `run.command` | string | `"clean"` (the only CLI-emitted run command) |
| `run.timestamp` | string | ISO 8601 UTC, suffix `Z` |
| `run.elapsed_seconds` | float | parent-process wall-clock; `>= 0` |
| `run.failed_files` | array\<object\> | `[{"file": basename, "error": str}, ...]` sorted by file; `[]` when none |
| `environment.tool_version` | string | `lintle.__version__` |
| `environment.python_version` | string | `major.minor.micro` |
| `summary.files_processed` | int | `== len(files)` |
| `summary.paired_records` | int | corpus-wide sum |
| `summary.orphan_entries` | int | corpus-wide sum |
| `summary.input_lines_seen` | int | corpus-wide sum |
| `summary.clean_count` | int | corpus-wide sum |
| `summary.quarantined_count` | int | corpus-wide sum |
| `summary.failed_count` | int | `== len(run.failed_files)`; `0` when none |
| `summary.fix_counts` | object\<str,int\> | `FixClass` keys; `{}` when none |
| `summary.quarantine_counts` | object\<str,int\> | `RuleID` keys; `{}` when none |

**Per-file (`files[]`) — superset of `summary`'s per-file fields plus timing and breakdowns:**

| Field | Type | Notes |
|---|---|---|
| `src_name` | string | basename only |
| `elapsed_seconds` | float | per-file worker wall-clock; `>= 0` |
| `bytes` | int | `os.path.getsize(src_path)`; `>= 0` |
| `records_per_sec` | float | `paired_records / max(elapsed_seconds, 0.001)` — clamped, never `null` |
| `paired_records`, `orphan_entries`, `input_lines_seen`, `clean_count`, `quarantined_count` | int | per file |
| `fix_counts` | object\<str,int\> | `FixClass` keys |
| `quarantine_counts` | object\<str,int\> | `RuleID` keys |
| `dropped_counts` | object\<str,int\> | per-rule count of in-memory sample entries dropped at cap; `{}` when none |
| `quarantined_norad_ids` | object\<str,object\<str,int\>\> | NORAD ID → (`RuleID` → count) |

**Timing semantics (do not mix).** `run.elapsed_seconds` is the parent process's wall-clock
across the whole run. `files[i].elapsed_seconds` is one worker's duration on one file. With
`--jobs N` the per-file durations sum to **more** than parent wall-clock; **never** sum them to
derive a corpus total — use `run.elapsed_seconds` for end-to-end and `records_per_sec` for
per-file throughput.

**Privacy.** The envelope contains only: tool/Python version, the subcommand name, the
timestamp, file **basenames**, and numeric counts. No env vars, hostnames, usernames, or
absolute paths.

### The `report.jsonl` per-record findings stream — `schema_version "1"`

One compact JSON object per quarantined record (sorted keys, LF, UTF-8), used by `lintle diff`.
This stream stayed `"1"` through the envelope's `"2"` and `"3"` bumps. Verified line:

```json
{"column_range":[69,69],"expected":"7","file":"tle_demo.txt","norad_id":25544,"note":null,"observed":"0","outcome":"quarantined","related":[],"rule_id":"TLE-CHK-001","schema_version":"1","source_lines":[3],"tier_attempted":"tier-1"}
```

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | `"1"` |
| `outcome` | string | always `"quarantined"` in v1 (reserved for future `"fixed"`) |
| `file` | string | source basename |
| `norad_id` | int \| null | catalog ID decoded at quarantine time; `null` when line 1 is unreadable |
| `rule_id` | string | the primary `RuleID` (e.g. `"TLE-CHK-001"`) |
| `source_lines` | array\<int\> | 1-indexed source line number(s) |
| `tier_attempted` | string | `"none"` / `"tier-1"` / `"tier-2"` |
| `column_range` | array\<int\> \| null | `[start, end]` 1-indexed, or `null` |
| `observed`, `expected` | string \| null | bounded to 16 chars |
| `note` | string \| null | bounded to 80 chars, non-printables sanitized; `null` when empty |
| `related` | array\<object\> | secondary diagnostics, each the same nested shape (minus the envelope fields) |

> `column_range`/`observed` are populated for **column, semantic, catalog, and
> checksum** findings (issue #120) — the validator returns a typed
> `tle.FieldError` (a `str` subclass carrying `kind` + the column span), so
> `repair` routes on `FieldError.kind` instead of grepping the prose and records
> the structured fields. `expected` is filled where the rule has a single
> expected token (a checksum digit, a semantic bound like `[0, 180]`, the other
> line's catalog number, a single-char column's allowed set); it stays `null` for
> a *multi-character* column field, whose constraint is a charset best left to the
> prose `note` rather than a 16-char-truncated value. All three are `null` for rules with
> no single column locus (e.g. `BAD_PREFIX`, `ORPHAN_LINE`). The `note` still
> carries the full prose. The line schema stays `"1"`: the field set and types are
> unchanged — only previously-`null` optional values are now filled in.

### The `.broken.txt` sidecar

Byte-faithful quarantine catalog, one per input file. A three-line ASCII header (title, source
+ timestamp + tool version, `N quarantined of M entries`) followed by one entry per quarantined
record. Each entry is a header line citing the primary diagnostic (`[index] source line(s) N -
rule: <id> (<tier>) col <range> observed=... expected=... - <note>`), any related diagnostics
on `    and:` continuation lines, then the **original raw bytes** of the offending line(s)
verbatim. Verified:

```
# tle_demo.broken.txt - quarantined records
# source: tle_demo.txt | generated: 2026-05-31T15:35:02Z | lintle 0.3.0
# 1 quarantined of 2 entries

[1] source lines 3-4 - rule: TLE-CHK-001 (tier-1) col 69 observed='0' expected='7'
1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2920
2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537
```

The `M entries` denominator is `paired_records + orphan_entries`. The full catalog is streamed
to disk regardless of the in-memory sample cap, so `.broken.txt` is never truncated.

### `broken-noradids.ndjson`

Corpus-wide newline-delimited JSON, one `{"noradId":N}` object per line, deduplicated and
sorted ascending across the whole run. Records whose line 1 is itself unreadable are omitted
(no catalog number to recover). The minimal one-field shape is deliberately additive —
consumers ignore unknown fields. Empty file when nothing was quarantined. Verified:

```
{"noradId":25544}
```

### The checkpoint `.clean-state.json` — `schema_version 4`

The single-run resume checkpoint. Note `schema_version` here is an **integer** (`4`), unlike
the string schema versions on the JSON surfaces above. It bumped `3 → 4` in 0.11.0 when the
flat numbered layout retired `data/`: recorded chunk basenames resolve against the new dirs,
so an old (schema-3) checkpoint must classify `STALE` rather than resume against paths that no
longer exist. Compact JSON, sorted keys. Verified shape:

```json
{
  "schema_version": 4,
  "lintle_version": "0.3.0",
  "run_identity": { "max_quarantined": "0" },
  "inputs": {
    "<path>": { "size": 280, "mtime_ns": ..., "ctime_ns": ..., "inode": ...,
                "head_sha256": "...", "tail_sha256": "..." }
  },
  "completed": {
    "<path>": { "summary": { ...summary_dict... }, "outputs": { "tle_demo.cleaned.txt": 140 } }
  }
}
```

- **`inputs`** maps each discovered input to a cheap identity fingerprint: size, integer
  `mtime_ns` / `ctime_ns`, inode, and a SHA-256 of the first and last 64 KB. The bounded
  head+tail window catches any append (tail changes) or truncation (size changes) in one seek
  while staying O(1) — the interior is never read (principle #3). `ctime_ns` + inode catch
  metadata-preserving copies and replace-by-rename. Residual: an interior edit that also
  preserves size+mtime+ctime+inode is not detected.
- **`completed`** maps each fully-processed input to `{summary, outputs}`, where `outputs`
  records each output basename's on-disk size at completion — backing an integrity
  re-verification on resume (a SIGKILL/disk-full truncation that bare existence wouldn't catch).
  Recorded unconditionally: the cleaned file (`tleYYYY.cleaned.txt`), the broken sidecar
  (`tleYYYY.broken.txt` — header-only when nothing was quarantined, but always written), and the
  findings shard (`.shards/tleYYYY.findings.jsonl`). A missing or truncated shard detected on
  resume triggers full reprocessing of that file so `report.jsonl` stays complete (issue #117).
- **`run_identity`** pins output-affecting configuration (today: `max_quarantined`) so a
  changed run cannot validate-through a checkpoint.

**Resume-decision matrix.** A checkpoint classifies as `ABSENT`, `CORRUPT` (present but
unparseable — never silently treated as absent), `VALID`, or `STALE` (version, run identity, or
any input identity drifted). Resume is **all-or-nothing**: any drift invalidates the whole
checkpoint. Resolution:

- `VALID`: `--resume` resumes; `--no-resume` starts fresh; non-interactive auto-resumes;
  interactive prompts `[Y/n]`.
- `STALE`: `--no-resume` starts fresh; `--resume` aborts (exit 2) with the reason;
  non-interactive aborts with a `--no-resume` hint; interactive prompts `[y/N]`.
- `CORRUPT`: `--no-resume` starts fresh; otherwise aborts (exit 2).
- `ABSENT`: `--resume` aborts ("no interrupted run to resume"); otherwise fresh.

A fresh run **archives** any existing checkpoint to `.clean-state.json.stale-<timestamp>`
(never destroying a recoverable run), then scrubs the `01-cleaned/`, `02-broken/`, `.shards/`
trees and the four report artifacts (`report.md`, `report.json`, `report.jsonl`,
`broken-noradids.ndjson`) so no orphans from a differently-scoped prior run linger and no stale
report is left for `lintle report` to render if the fresh run is itself interrupted (issue #102).
After archiving, `archive_checkpoint` prunes older stale archives keeping only the newest 3
(`_STALE_ARCHIVE_KEEP`); the ISO-8601 timestamp suffix is lexicographically sortable so `sorted()`
orders them chronologically (issue #105).

**Preflight ordering.** `resolve_clean_plan` executes in this order: build `inputs` +
`run_identity` → classify checkpoint → resolve resume action → branch:

- **RESUME branch:** disk-space guard runs on `2×` the *remaining* (unprocessed) input bytes,
  computed after completing-file integrity re-verification. A nearly-complete resume is never
  refused on a tight disk that would comfortably hold the fraction still to process (issue #94).
- **FRESH branch:** ownership check (`_is_safe_to_scrub`) → archive checkpoint → scrub (trees +
  report artifacts) → disk-space guard on `2×` full corpus (now that freed space is reflected) →
  write `.lintle-output` ownership marker.

**Ownership marker.** `run_planning._OUTPUT_MARKER` (`.lintle-output`) is written into the
out-dir by the first fresh run. A scrub refuses (exit 2, no data destroyed) when the out-dir is
non-empty, carries no `.lintle-output` marker, no checkpoint (`.clean-state.json`), and no
stale-checkpoint archive — indicating it is not a lintle output directory and may contain
user-owned content (issue #93). Effectively-empty out-dirs (only the lock file present) are
always safe to use.

### `05-dedup/manifest.jsonl` — the per-satellite corpus manifest

`dedup` also writes `<out-dir>/05-dedup/manifest.jsonl`: one compact ASCII JSON row per
satellite, catalog-ascending, built by feeding each kept card's catalog/epoch/element-set into
`history.analyze_epochs` as `dedup`'s external sort emits them in `(catalog, epoch)` order —
`_ManifestBuilder` holds one satellite's epoch/element-set lists at a time and flushes a row on
every catalog boundary (Critical Rule #3: bounded to one satellite's history, never the whole
corpus). Unlike every other per-record output stream, it is a **single plain file, never
chunked** — tens of thousands of satellites, not billions of records, so the chunk-set
machinery buys nothing here. Row shape (fixed key order, one line):

```json
{"norad_id":25544,"records":1234,"first_epoch":"2017-01-01T00:00:00Z","last_epoch":"2026-07-01T00:00:00Z","span_days":3468.0,"median_spacing_days":0.72,"largest_gap_days":41.3,"gap_count":2}
```

`median_spacing_days` is `null` below 3 records (the trivially-gapless case). This is the
corpus-coverage substrate: a query like `jq | shuf | xargs lintle extract` picks a random
well-covered (or gappy) satellite for spot-checking — `lintle` itself owns no RNG, so `shuf`
supplies the randomness.

### `epoch_distribution` — verify's record-density histogram

`lintle verify` tallies a `{"YYYY-MM": count}` histogram of every revalidated record's epoch
month while it revalidates (a `collections.Counter`, converted to a plain `dict` — sorted by
key — at the output boundary), and writes it as a new top-level `epoch_distribution` key in
`04-verify/summary.json` — a **sibling of `checked`, not nested inside it** — plus a matching
`### Epoch distribution` Markdown section in `summary.md` (one `- YYYY-MM  N` line per month,
emitted only when non-empty). Only records that pass revalidation are binned; a month with no
valid records is simply an absent key, never a zero. Deliberately named "epoch distribution" /
"record density," not "coverage" — it says nothing about gaps or completeness, only where
records cluster in time (`history.py`'s gap analysis is the coverage-shaped signal). It is
purely informational: it never feeds `counts`, `hard`, `soft`, or `exit_code`.

### `cleaned_fingerprint` — the dedup/extract staleness guard

`dedup` computes a stat-only structural fingerprint of `01-cleaned/` —
`{"stems": [[stem, total_chunk_bytes], ...]}`, sorted, built entirely from `Path.stat().st_size`
calls (no file content ever read) — and stores it as `cleaned_fingerprint` in
`05-dedup/summary.json`. `extract` recomputes the same fingerprint at run start and **warns,
never fails,** when it disagrees with the stored one: `01-cleaned/` changed since the `dedup`
run `extract` is reading from, so its results may be stale (re-run `lintle dedup`). Cheap enough
to run on every `extract` invocation — a handful of `stat` calls, not a hash over ~30 GB — so it
catches drift (a `clean --resume` filling in more records, say) without re-hashing the corpus;
it detects staleness, not bit-rot.

---

## 7. Runtime-dependency policy

The runtime is lean by policy, not dogma. The current runtime dependencies are **`rich>=15,<16`**
(terminal rendering for the `clean` progress UI), **`humanize>=4,<5`** (human-readable
durations and sizes in the human display — `precisedelta` for the panel duration, `naturalsize`
for the roster sizes), and **`sgp4>=2.25,<3`** (the physics engine for `lintle verify --orbit`).
`humanize` is confined to the human stderr/stdout display and never
touches structured or byte-deterministic output (`report.*`, the `.broken.txt` sidecar, the
checkpoint, `cleaned/*`, the `--report json` envelope, `broken-noradids.ndjson`). `pytest` is
dev-only. `sgp4` is the test oracle **and** the physics engine for `lintle verify`; it is never
imported by the clean/validate/repair path (the hard invariant below). It was promoted from a
dev-only test oracle to a verify-scoped runtime dependency with the `verify --orbit` physics
pass (Increment 2) — imported only by `verify/orbit.py`, lazily, and only when `--orbit` runs.

**The bar is relaxed.** A third-party runtime dependency may be added when it advances the aim
of a stable, maintainable, easy-to-understand app — i.e. when it is **popular, actively
maintained, and genuinely reduces the code we would otherwise own**, *and* it violates none of
the hard correctness invariants below. Four signals **favour** adoption but are **not**
necessary conditions and none is a veto:

1. **Popular / widely deployed** — but a `left-pad` one-liner earns no tilt.
2. **Actively maintained & mature.**
3. **Reduces our maintenance burden** — deletes real code (~100 lines, rule of thumb, *or* a
   gotcha-prone domain: terminal control, parsing, compression). A parity-only swap barely tilts.
4. **Sensible operational shape** — pure-Python or prebuilt wheels, small transitive surface,
   acceptable license, clean audit history, no heavy import-time side effects.

**Hard correctness invariants (the only vetoes — immovable however popular a library is).** A
dependency is rejected if it would:

- form a **second validation path** (principle #4 — why `sgp4` is walled out of the clean path);
- **load a file whole** or make any per-file structure grow with record count (principle #3);
- import **`sgp4` or another orbital parser into the clean/validate/repair path** — `sgp4` is
  permitted only inside `lintle verify`, as a consistency *measurer*, never a validity authority;
  the clean path is barred from importing it or `lintle.verify` by an import-graph test;
- make any **structured/machine-readable output or stdout-pipeable data non-byte-deterministic
  or styled** — `report.md`, `report.json`, `report.jsonl`, `broken-noradids.ndjson`, the
  `.broken.txt` sidecar, the `--report json` envelope, the `.clean-state.json` checkpoint, and
  `cleaned/*.txt` all stay exactly as their contracts assert (`report.json` is byte-identical to
  the `--report json` stdout envelope); `rich` styling is confined to the stderr/stdout panel UI;
- weaken the **atomic + durable commit** (`durable_replace`) or the **advisory-flock out-dir
  lock** semantics; or
- violate **validated transformation / correctness over recovery** (principles #1/#2).

These gate a dependency's *behaviour*, not its file location — there is deliberately no layering
rule. Adoption lands with a `CHANGELOG.md` entry.

**Version-pinning policy (every dependency, runtime *and* dev).** Each dependency is pinned
`>=current_major,<next_major` (e.g. `rich>=15,<16`, `pytest>=9.0,<10`, `sgp4>=2.25,<3`). Minor and
patch releases resolve automatically; **major upgrades are deliberate and manual, taken one at a
time** with a re-review (run the suite — for `rich`, the byte-exact `test_term.py` + the progress
/roster tests are the tripwire — and skim the upstream changelog for anything tests would miss).
For a `0.x` dependency the leftmost non-zero component is treated as the major, because that is
where the breaking changes land: `ruff>=0.15,<0.16` (a `0.16` bump can add rules / reflow code and
silently fail `ruff format --check`, so it is taken deliberately, not auto). `uv.lock` is the
lockfile of record and is committed with each change.

### Considered & deferred (canonical record)

Two reject grades: **Reject (hard invariant)** is immovable; **Reject (not worth it)** is a
judgement under the relaxed bar that can be revisited.

| Tool | Disposition | Reason |
|---|---|---|
| TLE/orbital libs (`sgp4`, `Skyfield`, `tletools`, `astropy`) | Reject in the clean path (hard invariant) | A parser/validator in the clean path would be a second validation path (#4). `sgp4` is permitted only inside `lintle verify` (consistency metrics, never validity) and as the test oracle; other orbital libs stay out. |
| `pydantic` | Reject (hard invariant) | Second coercion/validation path (#4); would drift byte-deterministic outputs (#1/#2); `pydantic-core` native at scale. |
| `orjson` / `ujson` / `msgspec` | Reject (hard invariant) | Changes on-disk bytes (`sort_keys`, separators, `ensure_ascii=False`, LF) the diff contract + resume round-trip assert. |
| `tabulate` | Reject (hard invariant) | `report.md` is asserted byte-for-byte; padding rules rewrite every byte. |
| `filelock` | Reject (lean runtime) | The lock is now a ~25-line `fcntl.flock` wrapper (issues #87/#99); `filelock` is itself a flock/lockfile wrapper, so it would add a runtime dependency without removing meaningful burden. Its cross-platform layer targets Windows, which is out of scope. |
| atomic-write libs (`atomicwrites`, `boltons`) | Reject (hard invariant) | None implements the macOS `F_FULLFSYNC` + dir-fsync ordering `durable_replace` needs; `atomicwrites` is unmaintained. |
| file-hashing libs (`dirhash`, `xxhash`) | Reject (hard invariant) | The resume fingerprint is a bounded head+tail 64 KB window; whole-file hashing would read the full 3.2 GB (#3). |
| `click` / `typer` | Reject (not worth it) | `argparse` is stdlib with zero supply-chain surface; ~0 net lines deleted; would change `--help`/error text the e2e tests assert. |
| `polars` / `pandas` | Reject (not worth it) | `diff` is per-rule counters; a `dict[str,int]` suffices; huge native tree. |
| `structlog` / `loguru` | Reject (not worth it) | No logging; the 3-channel output covers it; net-negative LOC. |
| `joblib` / `loky` / `tenacity` | Reject (not worth it) | Bespoke exact-cancel, `128+signo` codes, and checkpoint ordering are `lintle` policy no executor deletes. |
| `platformdirs` | Reject (not worth it) | No user config/cache dirs to resolve. |
| config parsing (`tomli`, …) | Reject (not worth it) | `tomllib` is stdlib; `argparse`/`json` cover the rest. |
| caching (`diskcache`, `cachetools`) | Reject (not worth it) | One-pass streaming tool; a `dict` suffices. |
| `tqdm` | Reject (not worth it) | Can't render a dynamic block of N concurrent bars; `rich` already covers progress. |
| `textual` | Reject (not worth it) | Full TUI framework; we want a progress block, not an app. |
| `blessed` / `prompt_toolkit` | Reject (not worth it) | Lower-level; still ~50 lines of glue. `rich` fits better. |
| **`rich`** | **Adopted (issue #53)** | Popular, well-maintained terminal renderer; drives the `clean` stderr progress UI, replacing ~150 lines of hand-rolled ANSI. Pure-Python; imported only by the `cli_progress.py` and `term.py` presentation leaves — callers such as `cli.py` and `output_artifacts.py` reach it through them, never directly — and every byte goes to stderr, so no streaming, memory, or structured-output impact. |
| **`humanize`** | **Adopted (2026-06-07)** | Human-display durations (`precisedelta`) and roster sizes (`naturalsize(gnu=True)`); pure-Python, zero transitive deps; confined to `summary.py` and `cli_progress.py` — stderr/stdout panel only, never structured output. A 2026-06-07 re-audit re-confirmed all other candidates as rejected or deferred for the reasons already tabled. |
| `zstandard` | Defer (trigger-gated) | Only on a *measured* output-size / transfer bottleneck; until then stdlib `gzip`. |

Dev-only (exempt; record purpose if nontrivial): `pytest`, `pytest-cov`,
`ruff`, `hypothesis` (property-based validator/repair tests), `pytest-xdist` (parallel suite —
default run is `pytest -n auto`). (`sgp4` is now a verify-scoped **runtime** dependency — see
the §7 intro — though it still doubles as the `test_oracle.py` acceptance cross-check.)

---

## 8. Terminology

- **Quarantine** — the act of setting a bad record aside instead of repairing it: it is written
  byte-faithfully to the `broken/*.broken.txt` sidecar, counted in `quarantined_count` and
  `quarantine_counts`, and never emitted to `cleaned/`. Quarantining is the safe default
  whenever a repair cannot be validated (principle #2).
- **Routed records** — `clean_count + quarantined_count`: every record (and orphan) goes to
  exactly one destination, never both. This is the denominator for `--max-quarantined N%`.
- **Orphan** — a line that could not be paired into a record (a lone `1 ` or `2 ` line, or one
  followed by a non-TLE line). Orphans are quarantined as `TLE-PAIR-001`.
- **`reject` → `quarantine` (historical).** An earlier codebase used `reject` for this concept:
  `reject_counts`, `reject_sample`, a `--report json` `reject_counts` key, and `RejectCategory`.
  The terminology was unified to **`quarantine`** project-wide. The `--report json` envelope was
  bumped `schema_version "1"` → `"2"` for the `reject_counts` → `quarantine_counts` rename; the
  `report.jsonl` findings stream and `lintle diff` stayed `"1"`. **`reject_counts` is not a
  current key anywhere.** Readers of older commits or archived specs will see `reject*` and
  should read it as today's `quarantine*`.

---

## 9. Design history

The dated design specs, implementation plans, and corpus-run summaries now live under
`docs/superpowers/archive/` (`specs/`, `plans/`, `runs/`) as **historical records** — kept for
design *rationale* only. They include the authoritative cleaner design
(`2026-05-21-tle-corpus-cleaner-design.md`, whose §3.1 first stated the dependency policy now
consolidated in [§7](#7-runtime-dependency-policy)), the
`--report json` envelope design (`2026-05-25-report-json-envelope.md`, schema now `"3"`), the
structured findings design (`2026-05-25-report-jsonl-structured-findings.md`), the
runtime-dependency-policy rationale (`2026-05-28-runtime-dependency-policy-design.md`), and the
resume-by-default design (`2026-05-30-resume-by-default-design.md`), among others.

**This document and the code are the current truth.** Where the archive and this document
disagree, this document (verified against the code) wins; where this document and the code
disagree, the code wins. A reader never needs the archive to understand or consume `lintle`.
