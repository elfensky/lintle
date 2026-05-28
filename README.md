# lintle

A validator and cleaner for **Two-Line Element (TLE)** corpus files exported
from [space-track.org](https://www.space-track.org/).

It audits a TLE file against the standardized TLE specification, repairs the
systematic export defects, and emits a **uniform, de-defected** corpus that any
SGP4 / orbital-mechanics library can ingest directly. Records it cannot safely
repair are quarantined — never silently mangled — into a per-file sidecar
detailed enough to file a defect report with space-track.

---

## What problem it solves

A TLE record is two fixed-width lines, each *exactly* 69 ASCII columns, with a
mod-10 checksum in column 69. Bulk historical exports from space-track carry
two systematic, era-specific defects:

- **Trailing `\` artifact** — almost every `Line 1` has an extra `\` byte
  appended before the newline.
- **Missing checksum digit** — many records were exported without their
  column-69 checksum, leaving 68-column lines.

These appear independently and in combination, and a small fraction of records
are genuinely corrupt (garbled columns, orphaned lines, wrong lengths).
`lintle` distinguishes the safely-repairable from the genuinely-corrupt and
treats each correctly.

## What lintle will and won't reconstruct

**lintle never invents data.** It emits only information that was already in the
record. The single exception is the column-69 checksum — and it is an exception
*precisely because* the checksum carries no information of its own: it is a
deterministic mod-10 function of columns 1–68, so recomputing a missing one
asserts nothing the record didn't already say. This is the **redundancy paradox**
at the heart of the tool: the only field safe to rebuild is the one field that
was redundant to begin with.

Every other defect that could only be "fixed" by guessing a real data character
is **quarantined, never repaired**. A mod-10 checksum has a 1-in-10 chance of
accepting a wrong line by luck, so inventing an orbital-data character risks
emitting a record that *looks* valid but is silently wrong — the one outcome
worse than dropping it. When in doubt, lintle quarantines.

See [How it works](#how-it-works) below for the fix-class table that
operationalises this principle.

## How it works

**One validator, used two ways.** A single module (`tle.py`) defines what a
"perfect" TLE record is — column layout, semantic ranges, and the mod-10
checksum. The `validate` command reports defects against that definition; the
`clean` command reuses the *exact same* validator and only emits records that
pass it.

**The validated-transformation principle.** The cleaner never applies a fix and
hopes. It applies a candidate fix, then re-runs full validation on the result,
and commits the fix *only if it now passes*. Consequently the cleaner cannot
turn a bad record into a wrong-but-valid-looking one, and every line in the
output is valid by construction.

**Five fix classes, in decreasing order of safety:**

| Class | Examples | Action |
|-------|----------|--------|
| Content-preserving | trailing `\`, CRLF, trailing whitespace | auto-fix (checksum survives as an independent check) |
| Reconstructed-checksum | a record exported without its column-69 digit | recompute the checksum from intact columns 1–68 |
| Content-shifting | leading whitespace / BOM | trim, then re-validate; quarantine if it fails |
| Structural | blank / whitespace-only lines | drop, resynchronise pairing |
| Corrupt | bad checksum, wrong length, orphan line, garbled columns | **quarantine** |

**Streaming and parallel.** Files are read in binary, line by line, in constant
memory — a 3 GB file never loads into RAM. Records are paired by a prefix-driven
state machine that resynchronises on every `1 ` line, so one missing line
cannot cascade into mispaired records. Each input file is processed in its own
worker process.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) for environment and dependency management

`lintle` itself has **no runtime dependencies** — it is pure standard library.
`sgp4` is a dev-only dependency, used as a test oracle.

## Installation

```bash
uv sync
```

This creates the virtual environment and installs the dev dependencies. No
build step is needed to run the tool.

## Usage

The console script is `lintle`:

```bash
# Audit only — report defects, write nothing
uv run lintle validate [path]

# Produce cleaned output + quarantine sidecars
uv run lintle clean [path]

# Explain a rule ID or fix tag — definition, examples, source citation
uv run lintle explain <TAG>
```

`python -m lintle ...` is equivalent to `uv run lintle ...`.

**Arguments and options:**

| Option | Default | Meaning |
|--------|---------|---------|
| `path` | `data/source` | A single file or directory. A directory is globbed for `tle*.txt` (tool output `*.cleaned.txt` / `*.broken.txt` is excluded). |
| `--out-dir DIR` | `data/output` | Where `clean` writes its output. Created if absent. |
| `--jobs N` | CPU count | Number of files processed in parallel. Lower it if a slow disk causes I/O contention. |
| `--report text\|json` | `text` | Summary format. |
| `--max-quarantined N[%]` | `0` | Exit non-zero only if MORE than N records were quarantined; or, with a trailing `%`, more than `N%` of routed records (`clean + quarantined`) were quarantined. Default `0` ≡ "any quarantine fails". |
| `--resume` | off | (`clean` only) Continue an interrupted run in `--out-dir`: skip files already completed and process only the rest. Refuses if the `lintle` version or any input changed since the interrupted run. |

**Examples:**

```bash
# Validate the whole corpus
uv run lintle validate data/source

# Clean one file
uv run lintle clean data/source/tle2022.txt --out-dir data/output

# Clean the corpus, capture a machine-readable summary
uv run lintle clean data/source --report json > run-summary.json

# CI gate: tolerate up to 100 quarantined records before failing the job
uv run lintle clean data/source --max-quarantined 100 --report json > run-summary.json

# CI gate (scale-invariant): tolerate up to 1% of routed records
uv run lintle clean data/source --max-quarantined 1% --report json > run-summary.json

# Look up what a rule ID or fix tag means, with a verified example
uv run lintle explain TLE-CHK-001
uv run lintle explain reconstructed-checksum

# Resume an interrupted run (e.g. the laptop slept mid-corpus) — finish only what's left
uv run lintle clean data/source --out-dir data/output --resume
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Quarantine count (or rate) is at or below `--max-quarantined` (default `0`). |
| `1` | Quarantine count (or rate) exceeded `--max-quarantined`. |
| `2` | Operational error — no input files, disk shortfall, or a file that failed to process. |

Repairable defects (including the near-universal trailing `\`) do **not** raise
the exit code above 0 — almost every raw file contains them. `--max-quarantined`
preserves the meaningful `2` (operational error) and `130` (Ctrl-C) signals that
a `lintle ... || true` pipe would swallow.

## Resuming an interrupted run

A long `clean` over the full corpus can be interrupted — Ctrl-C, a closed
laptop, a crash. Re-run the **same command with `--resume`** to finish it: files
already completed are skipped (their `cleaned/`, `broken/`, and report
contributions reused) and only the remainder is processed.

While a run is in flight it maintains a small `.clean-state.json` checkpoint in
`--out-dir`, deleted on successful completion — so its presence marks an
interrupted run, and a finished run leaves none behind. `--resume` **refuses**
(exit `2`) if the `lintle` version or any input file changed since the
interruption, rather than silently mixing outputs from two different states;
re-run without `--resume` for a clean full pass. This is single-run resume, not
a cross-run cache: each run still re-validates every record it emits.

## Output

A `clean` run lays `--out-dir` out like this:

```
<out-dir>/
├── cleaned/                tleYYYY.cleaned.txt   — one per input file
├── broken/                 tleYYYY.broken.txt    — one per input file
├── broken-noradids.ndjson  — corpus-wide list of quarantined NORAD IDs
└── report.md               — corpus-wide run report
```

- **`cleaned/tleYYYY.cleaned.txt`** — standard 2-line TLE text, every record
  verified valid: 69 ASCII columns per line, `\n`-terminated, matching
  satellite catalog numbers, valid checksums. World-readable, ready for
  downstream ingestion.

- **`broken/tleYYYY.broken.txt`** — the quarantine sidecar. Each entry records
  the source line number(s), a human-readable reason, and the offending line(s)
  copied **byte-faithfully**. The header carries totals, a timestamp, and the
  tool version — formatted to paste into a space-track defect report.

- **`broken-noradids.ndjson`** — newline-delimited JSON, one
  `{"noradId":N}` object per line, listing every NORAD catalog number whose
  records were quarantined anywhere in the run, deduplicated and sorted
  ascending. Records whose line 1 is itself unreadable are omitted —
  there's no catalog number to recover. Intended for programmatic
  downstream consumers (e.g. a satellite catalog flagging archive gaps)
  that want the affected IDs without parsing `broken/*.txt`. The schema
  is deliberately minimal; future releases may extend each record with
  additional fields, which consumers can ignore safely. Empty file when
  nothing was quarantined.

- **`report.md`** — a Markdown run report aggregating the whole run: corpus
  totals, the percentage cleaned and quarantined, corpus-wide fix counts, the
  defect-rule breakdown (keyed by stable `RuleID` tokens like `TLE-CHK-001`),
  a per-file table, a "Rule reference" section auto-generated from the
  `diagnostics.RULES` registry naming every rule that fired, and a per-NORAD
  breakdown table listing each satellite whose records were quarantined with
  its per-rule counts and the source files it appeared in (sorted by
  quarantined-record count descending, capped at the top 100 with a remainder
  footer pointing at `broken-noradids.ndjson` for the long tail).

A run summary is also printed per file to stdout (and as JSON with
`--report json`):

```
tle2022.txt   8,412,066 records   8,412,064 clean   3 quarantined   (1 orphan, 16,824,134 lines)
  fixes:   trailing-backslash 8,412,064 | reconstructed-checksum 195,293
  rejects: TLE-CHK-001 1 | TLE-PAIR-001 1 | TLE-COL-001 1
```

The header line separates three input tallies that an earlier version
conflated into a single "records" number (issue #5): `records` is **paired
records** (proper 2-line TLE entries — orphans are not counted here); `clean`
is paired records that passed validation and were written to `cleaned/`;
`quarantined` is everything routed to `broken/`, which includes both failed
paired records and every orphan (an unpaired line is quarantined as
`TLE-PAIR-001`); the parenthetical `orphan` counts unpaired single lines and
`lines` counts every physical line read (including blanks dropped by the
pairing loop). The invariant is `records + orphan == clean + quarantined`,
so `clean + quarantined` can exceed `records` by the orphan count.

Reject counts key by the stable `RuleID` registry (e.g. `TLE-CHK-001` for
checksum mismatch, `TLE-PAIR-001` for orphan lines, `TLE-COL-001` for wrong
length) — the same handles cited in `report.md` and the `.broken.txt`
sidecar so a defect surfaces under one identifier across every artifact a
run emits. `reconstructed-checksum` is reported separately from
content-preserving fixes: those records are format-conformant, but their
checksums are *computed*, not independently verified.

`validate` writes nothing — it only prints the per-file summary and the
locations of defective records to stdout.

### Progress

A 30 GB run is not silent. Live progress is written to **stderr** as it goes —
so it never pollutes the stdout summary or a `--report json` pipe:

```
processing 29 file(s) with 10 worker(s)...
  tle2004_7of8.txt: 5,000,000 records...
[3/29] tle2004_3of8.txt — 2,527,820 clean, 183 quarantined
```

A worker emits a record-count line every 1,000,000 records; the main process
prints an `[k/N]` line as each file finishes.

## Disk-space requirements

Every record is **routed** to exactly one of `cleaned/` or `broken/` — never
duplicated — so the finished output is roughly the same total size as the
input, plus negligible metadata (`report.md`, `report.jsonl`,
`broken-noradids.ndjson`, and the transient `.shards/` findings, all tiny
next to the records).

Outputs are written to temporary `.partial` files and atomically renamed on
completion. As a conservative guard, lintle checks free space up front and
**requires roughly twice the total input size** on the `--out-dir` volume
before it starts, aborting with exit `2` if short:

```
error: insufficient disk space in data/output: need ~63,000,000,000 bytes, have 40,000,000,000
```

When free space sits in the borderline band — at or above the 2× floor but
below 2.5× the input size — the run proceeds, but lintle prints a warning
to stderr so you know you are cutting it close:

```
warning: free space in data/output is close to the 2× safety guard: 70,000,000,000 bytes free of ~63,000,000,000 recommended; the run will proceed but may exhaust the disk
```

Rule of thumb: for the bundled ~30 GB corpus, keep **~60 GB free** to clear
the abort floor and **~75 GB free** to clear the warning band. (The 12 GB
`TLEs.zip` is not an input and is never read.)

## Results on the bundled corpus

A full run over the 29-file corpus (`tle2004`–`tle2025`, ~232 million records):

- **99.96 % cleaned** — 187.9 M trailing-`\` artifacts stripped, 71.3 M missing
  checksums reconstructed
- **0.044 % quarantined** (103,228 records) as genuinely corrupt — every reject
  fell into an anticipated category; no unknown defect type surfaced

## Development

```bash
uv sync                          # install dev dependencies
uv run pytest                    # run the test suite
uv run pytest --cov=lintle       # with a coverage report
uv run ruff check                # lint
uv run ruff format               # auto-format
```

The suite includes unit tests per module, an asymmetric cross-check against the
trusted `sgp4` parser (a known-good TLE must be accepted by both), and
end-to-end integration tests (golden output, idempotence, re-validation).

Code quality is enforced with [`ruff`](https://docs.astral.sh/ruff/) (lint rule
sets `E`, `F`, `I`, `UP`, `B`, `SIM`; 88-column lines) and coverage is measured
with `pytest-cov`.

## Project layout

```
src/lintle/
  tle.py          # core: defines a "perfect" TLE record (pure, no I/O)
  diagnostics.py  # stable RuleID registry + structured Diagnostic dataclass
  categories.py   # FixClass enum + FixSpec registry — the repair taxonomy
  explain_examples.py # validator-verified examples + citations backing `explain`
  repair.py       # speculative, validated repairs
  pipeline.py     # streaming reader, prefix-driven pairing, per-file routing
  report.py       # quarantine sidecar + run-summary rendering
  fsutil.py       # durable_replace — the one atomic+fsync commit path
  resume.py       # single-run checkpoint for `clean --resume`
  diff.py         # read-only: per-rule delta between two runs (`lintle diff`)
  explain.py      # read-only: renders rule/fix documentation (`lintle explain`)
  cli.py          # argument parsing, parallelism, exit codes
tests/            # pytest suite
docs/superpowers/
  specs/          # the design specification
  plans/          # the implementation plan
  runs/           # corpus-run summaries
```

`diagnostics.py` and `categories.py` are pure-data leaves of the dependency
graph — they hold enums and frozen dataclasses, no logic; `explain_examples.py`
is pure data too, composing those leaves into documented examples. `repair`,
`pipeline`, and `report` depend on them; `fsutil.py` is a stdlib-only I/O leaf
(the durable file-commit helper) that `pipeline`, `report`, and `resume` route
every output through; `diff.py` and `explain.py` are read-only consumers reached
only through `cli.py`; `tle.py` remains the single source of truth for what
counts as a valid TLE record.

## Further reading

The full design rationale — the defect model, the TLE column specification,
the fix policy, and the architecture — is in
[`docs/superpowers/specs/2026-05-21-tle-corpus-cleaner-design.md`](docs/superpowers/specs/2026-05-21-tle-corpus-cleaner-design.md).
