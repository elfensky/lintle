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
   second, divergent validation path — which is precisely why `sgp4` is a dev-only test oracle
   and must never be imported at runtime.

---

## 2. Module map & data flow

All package code lives under `src/lintle/`. Module dependencies flow one way, so cycles are
structurally impossible.

```
cli.py ──▶ pipeline.py ──▶ repair.py ──▶ tle.py
  │             │
  │             ├──▶ report.py ──┐
  │             └──▶ report_writers.py ──┘ (imports report.py one-way)
  │
  ├──▶ resume.py        (single-run checkpoint; depends only on __version__ + fsutil)
  ├──▶ diff.py          (read-only consumer of report.jsonl)
  ├──▶ explain.py ──▶ explain_examples.py
  └──▶ term.py          (stderr-only rich Console + error/warning/note/prompt)

fsutil.py    stdlib-only I/O leaf — durable_replace + out_dir_lock
diagnostics.py, categories.py, explain_examples.py    pure-data leaves (no I/O)
```

| Module | Owns |
|--------|------|
| `tle.py` | The validator: column layout, mod-10 checksum, semantic ranges, record pairing. The single definition of "perfect." Pure functions, no I/O. |
| `repair.py` | Speculative fixes, each confirmed by `tle.py` before commit; the `Accepted` / `Quarantined` record outcomes. Pure functions. |
| `pipeline.py` | Streams a file in binary, pairs `1 `/`2 ` lines into records, routes each to clean output or quarantine. Owns the per-file `process_file` worker entry. |
| `report.py` | `FileStats` and its sibling dataclasses, the `summary_dict` / `build_run_envelope` JSON shapes, and the Markdown `report.md` writer. |
| `report_writers.py` | Structured-file writers leaf: the `.broken.txt` sidecar (`BrokenFileWriter`), the `report.jsonl` findings shards (`JsonlFindingsWriter`), the `QuarantineSink` (bounded sample + streaming), `broken-noradids.ndjson`, and shard concatenation. Imports `report.py` one-way. |
| `resume.py` | The single-run `.clean-state.json` checkpoint for `clean --resume`: input fingerprinting, checkpoint build/load, the resume-decision matrix. |
| `fsutil.py` | `durable_replace` (the one atomic+fsync commit path) and `out_dir_lock` (the host-aware out-dir lock). Stdlib only. |
| `diff.py` | Read-only: per-rule delta between two runs' `report.jsonl` (`lintle diff`). |
| `explain.py` | Read-only: renders rule/fix documentation (`lintle explain`). |
| `diagnostics.py` | Stable `RuleID` registry + structured `Diagnostic` dataclass + `RepairTier`. Pure data. |
| `categories.py` | `FixClass` enum + `FixSpec` registry — the repair taxonomy. Pure data. |
| `explain_examples.py` | Validator-verified examples + citations backing `explain`. Pure data. |
| `term.py` | The single stderr `rich` Console and the `error:` / `warning:` / `note` / `prompt` emitters. |
| `cli.py` | argparse, globbing, parallel workers, live progress, Ctrl-C handling, exit codes. |

`tle.py` and the data leaves carry no I/O. `report_writers.py` depends on `report.py` (never
the reverse), so the structured writers and the renderers stay acyclic.

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

### The five fix classes

`FixClass` (in `categories.py`) is the single source of truth for the tags that appear in
`Accepted.fixes`, `stats.fix_counts`, and the `report.md` "Fixes applied" table. Listed in
decreasing order of safety:

| Class | Examples | Action |
|-------|----------|--------|
| Content-preserving | trailing `\` (`trailing-backslash`), CRLF (`crlf`), trailing whitespace (`trailing-ws`) | auto-fix (checksum survives as an independent check) |
| Reconstructed-checksum | a record exported without its column-69 digit (`reconstructed-checksum`) | recompute the checksum from intact columns 1–68 |
| Content-shifting | leading whitespace / BOM (`leading-trim`) | trim, then re-validate; quarantine if it fails |
| Structural | blank / whitespace-only / CR-only lines | drop, resynchronise pairing |
| Corrupt | bad checksum, wrong length, orphan line, garbled columns, catalog mismatch | **quarantine** |

The concrete `FixClass` members are `crlf`, `leading-trim`, `trailing-ws`,
`trailing-backslash`, and `reconstructed-checksum`. Fix order inside `repair_line` is fixed:
strip CRLF → strip leading whitespace → strip trailing whitespace → strip a trailing backslash
→ build a 69-character candidate (reconstructing the checksum if the line is 68 chars and its
body is valid) → a single full re-validation of the candidate.

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
exactly, reads it line by line, and pairs lines with a prefix-driven state machine that holds
**at most two lines** (a line-1 awaiting its line-2). Blank, whitespace-only, and CR-only lines
are dropped. Pairing resynchronises on every `1 ` line, so one missing line cannot cascade into
a run of mispaired records. Memory is constant regardless of file size — a 3.2 GB file never
loads whole.

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

### Host-aware out-dir lock

`fsutil.out_dir_lock` prevents two concurrent `clean` runs from corrupting a shared
`--out-dir`. It writes a JSON sidecar `.clean.lock` carrying host id, PID, and start time. It
**refuses** (`LockHeldError`, exit 2) when the lock is held by a live process on this host or by
*any* process on a different host; it **reclaims** only a same-host lock whose PID is dead.
Cross-host locks are never reclaimed (so a dead PID on host A is never falsely reclaimed from
host B). Host identity is `hostname` plus Linux `boot_id` where available.

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

A successful `clean` run lays out `--out-dir`:

```
<out-dir>/
├── cleaned/                 tleYYYY.cleaned.txt    — one per input file
├── broken/                  tleYYYY.broken.txt     — one per input file (sidecar)
├── report.md                — corpus-wide Markdown run report
├── report.jsonl             — corpus-wide structured findings (one JSON object per line)
└── broken-noradids.ndjson   — corpus-wide list of quarantined NORAD IDs
```

Transient run state lives alongside and is removed on success: `.shards/` (per-worker
`report.jsonl` shards, concatenated then `rmtree`'d) and `.clean-state.json` (the resume
checkpoint). On an interrupted or failed run, both survive so a later `--resume` can rebuild a
complete `report.jsonl` from the shards. `report.md`, `report.jsonl`, and
`broken-noradids.ndjson` are **always** written on a successful clean run — empty when nothing
was quarantined — so the consumer artifact set is stable.

- **`cleaned/tleYYYY.cleaned.txt`** — standard 2-line TLE text, every record verified valid: 69
  ASCII columns per line, `\n`-terminated, matching catalog numbers, valid checksums.
- **`broken/tleYYYY.broken.txt`** — the byte-faithful quarantine sidecar (see below).

### stdout / stderr discipline

A hard three-channel rule, so output is safely pipeable:

- **stdout** = pipeable data + the plain per-file summary. With `--report json`, stdout carries
  *only* the JSON envelope. Never styled.
- **stderr** = the live `rich` UI (roster, progress block), `processing…` notices, and
  `error:` / `warning:` lines. rich styling is applied only when stderr is a TTY; off a TTY
  (pipe, `capsys`, `NO_COLOR`) it degrades to plain literal text, so even stderr stays
  machine-readable.
- The structured output **files** are never routed through the stderr Console.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Quarantine count (or rate) is at or below `--max-quarantined` (default `0`). |
| `1` | Quarantine count (or rate) **exceeded** `--max-quarantined` — the quality gate. |
| `2` | Operational/usage error: bad args, no input files, disk shortfall, lock held, a file that failed to process, or a stale/corrupt/declined resume (including EOF at the prompt). |
| `129` | Terminated by `SIGHUP` (128 + 1). |
| `130` | Interrupted by `SIGINT` / Ctrl-C (128 + 2). |
| `143` | Terminated by `SIGTERM` (128 + 15). |

`--max-quarantined` accepts a bare integer (absolute record count) or a value with a trailing
`%` (percentage of routed records = `clean + quarantined`, cross-multiplied to avoid
divide-by-zero and float drift). The default `0` means "any quarantine fails."

### The `--report json` envelope — `schema_version "2"`

A single top-level JSON object. Every field is **required and non-nullable**; empty maps render
`{}`, empty arrays `[]` — never omitted, never `null`. This is the verified shape (one valid
pair, one checksum-flipped quarantine):

```json
{
  "schema_version": "2",
  "run":   { "command": "clean", "timestamp": "2026-05-31T15:34:44Z", "elapsed_seconds": 0.26 },
  "environment": { "tool_version": "0.3.0", "python_version": "3.14.5" },
  "summary": {
    "files_processed": 1, "paired_records": 2, "orphan_entries": 0,
    "input_lines_seen": 4, "clean_count": 1, "quarantined_count": 1,
    "fix_counts": {}, "quarantine_counts": { "TLE-CHK-001": 1 }
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

`schema_version` is a **string** to leave room for additive tags like `"2.1"`. Adding optional
fields stays under `"2"`; renaming or removing one bumps the major — which is exactly why the
`reject_counts` → `quarantine_counts` rename took it from `"1"` to `"2"`.

**Top-level / `run` / `environment` / `summary`:**

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | exactly `"2"` in this release |
| `run.command` | string | `"validate"` or `"clean"` |
| `run.timestamp` | string | ISO 8601 UTC, suffix `Z` |
| `run.elapsed_seconds` | float | parent-process wall-clock; `>= 0` |
| `environment.tool_version` | string | `lintle.__version__` |
| `environment.python_version` | string | `major.minor.micro` |
| `summary.files_processed` | int | `== len(files)` |
| `summary.paired_records` | int | corpus-wide sum |
| `summary.orphan_entries` | int | corpus-wide sum |
| `summary.input_lines_seen` | int | corpus-wide sum |
| `summary.clean_count` | int | corpus-wide sum |
| `summary.quarantined_count` | int | corpus-wide sum |
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
This stream stayed `"1"` through the envelope's `"2"` bump. Verified line:

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

### The checkpoint `.clean-state.json` — `schema_version 3`

The single-run resume checkpoint. Note `schema_version` here is an **integer** (`3`), unlike
the string schema versions on the JSON surfaces above. Compact JSON, sorted keys. Verified
shape:

```json
{
  "schema_version": 3,
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
(never destroying a recoverable run) and scrubs the `cleaned/`, `broken/`, and `.shards/` trees
so no orphans from a differently-scoped prior run linger.

---

## 7. Runtime-dependency policy

The runtime is lean by policy, not dogma. The current runtime dependency is **`rich>=15,<16`**
(terminal rendering for the `clean` progress UI). `sgp4` and `pytest` are dev-only; `sgp4` is a
test oracle and must never be imported at runtime.

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

- form a **second validation path** (principle #4 — why `sgp4` is dev-only);
- **load a file whole** or make any per-file structure grow with record count (principle #3);
- import **`sgp4` or another orbital parser at runtime**;
- make any **structured/machine-readable output or stdout-pipeable data non-byte-deterministic
  or styled** — `report.md`, `report.jsonl`, `broken-noradids.ndjson`, the `.broken.txt`
  sidecar, the `--report json` envelope, the `.clean-state.json` checkpoint, and `cleaned/*.txt`
  all stay exactly as their contracts assert; `rich` styling is confined to stderr ephemera;
- weaken the **atomic + durable commit** (`durable_replace`) or the **host-aware out-dir lock**
  semantics; or
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
| TLE/orbital libs (`sgp4`, `Skyfield`, `tletools`, `astropy`) | Reject (hard invariant) | A parser/validator would be a second validation path (#4); `sgp4` is fine as a dev-only test oracle. |
| `pydantic` | Reject (hard invariant) | Second coercion/validation path (#4); would drift byte-deterministic outputs (#1/#2); `pydantic-core` native at scale. |
| `orjson` / `ujson` / `msgspec` | Reject (hard invariant) | Changes on-disk bytes (`sort_keys`, separators, `ensure_ascii=False`, LF) the diff contract + resume round-trip assert. |
| `tabulate` | Reject (hard invariant) | `report.md` is asserted byte-for-byte; padding rules rewrite every byte. |
| `filelock` | Reject (hard invariant) | Cannot express the host-aware lock (cross-host refuse + same-host dead-PID reclaim); unsafe on a shared network out-dir. |
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
| **`rich`** | **Adopted (issue #53)** | Popular, well-maintained terminal renderer; drives the `clean` stderr progress UI, replacing ~150 lines of hand-rolled ANSI. Pure-Python; confined to `cli.py`/`term.py` stderr — no streaming, memory, or structured-output impact. |
| `zstandard` | Defer (trigger-gated) | Only on a *measured* output-size / transfer bottleneck; until then stdlib `gzip`. |

Dev-only (exempt; record purpose if nontrivial): `sgp4` (test oracle), `pytest`, `pytest-cov`,
`ruff`; candidates `hypothesis`, `pytest-xdist`.

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
`--report json` envelope design (`2026-05-25-report-json-envelope.md`, schema now `"2"`), the
structured findings design (`2026-05-25-report-jsonl-structured-findings.md`), the
runtime-dependency-policy rationale (`2026-05-28-runtime-dependency-policy-design.md`), and the
resume-by-default design (`2026-05-30-resume-by-default-design.md`), among others.

**This document and the code are the current truth.** Where the archive and this document
disagree, this document (verified against the code) wins; where this document and the code
disagree, the code wins. A reader never needs the archive to understand or consume `lintle`.
