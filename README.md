# tlekit

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
`tlekit` distinguishes the safely-repairable from the genuinely-corrupt and
treats each correctly.

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

`tlekit` itself has **no runtime dependencies** — it is pure standard library.
`sgp4` is a dev-only dependency, used as a test oracle.

## Installation

```bash
uv sync
```

This creates the virtual environment and installs the dev dependencies. No
build step is needed to run the tool.

## Usage

The console script is `tle-clean`, with two subcommands:

```bash
# Audit only — report defects, write nothing
uv run tle-clean validate [paths...]

# Produce cleaned output + quarantine sidecars
uv run tle-clean clean [paths...]
```

`python -m tlekit ...` is equivalent to `uv run tle-clean ...`.

**Arguments and options:**

| Option | Default | Meaning |
|--------|---------|---------|
| `paths` | `data/source` | Files or directories. A directory is globbed for `tle*.txt` (tool output `*.cleaned.txt` / `*.broken.txt` is excluded). |
| `--out-dir DIR` | `data/output` | Where `clean` writes its output. Created if absent. |
| `--jobs N` | CPU count | Number of files processed in parallel. Lower it if a slow disk causes I/O contention. |
| `--report text\|json` | `text` | Summary format. |

**Examples:**

```bash
# Validate the whole corpus
uv run tle-clean validate data/source

# Clean one file
uv run tle-clean clean data/source/tle2022.txt --out-dir data/output

# Clean the corpus, capture a machine-readable summary
uv run tle-clean clean data/source --report json > run-summary.json
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | No records quarantined — clean (or every defect repaired). |
| `1` | At least one record was quarantined. |
| `2` | Operational error — no input files, disk shortfall, or a file that failed to process. |

Repairable defects (including the near-universal trailing `\`) do **not** raise
the exit code above 0 — almost every raw file contains them.

## Output

A `clean` run lays `--out-dir` out like this:

```
<out-dir>/
├── cleaned/   tleYYYY.cleaned.txt   — one per input file
├── broken/    tleYYYY.broken.txt    — one per input file
└── report.md  — corpus-wide run report
```

- **`cleaned/tleYYYY.cleaned.txt`** — standard 2-line TLE text, every record
  verified valid: 69 ASCII columns per line, `\n`-terminated, matching
  satellite catalog numbers, valid checksums. World-readable, ready for
  downstream ingestion.

- **`broken/tleYYYY.broken.txt`** — the quarantine sidecar. Each entry records
  the source line number(s), a human-readable reason, and the offending line(s)
  copied **byte-faithfully**. The header carries totals, a timestamp, and the
  tool version — formatted to paste into a space-track defect report.

- **`report.md`** — a Markdown run report aggregating the whole run: corpus
  totals, the percentage cleaned and quarantined, corpus-wide fix counts, the
  defect-category breakdown, and a per-file table.

A run summary is also printed per file to stdout (and as JSON with
`--report json`):

```
tle2022.txt   8,412,067 records   8,412,064 clean   3 quarantined
  fixes:   trailing-backslash 8,412,064 | reconstructed-checksum 195,293
  rejects: checksum-mismatch 1 | orphan-line 1 | wrong-length 1
```

`reconstructed-checksum` is reported separately from content-preserving fixes:
those records are format-conformant, but their checksums are *computed*, not
independently verified.

`validate` writes nothing — it only prints the per-file summary and the
locations of defective records to stdout.

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
uv run pytest --cov=tlekit       # with a coverage report
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
src/tlekit/
  tle.py        # core: defines a "perfect" TLE record (pure, no I/O)
  repair.py     # speculative, validated repairs
  pipeline.py   # streaming reader, prefix-driven pairing, per-file routing
  report.py     # quarantine sidecar + run-summary rendering
  cli.py        # argument parsing, parallelism, exit codes
tests/          # pytest suite
docs/superpowers/
  specs/        # the design specification
  plans/        # the implementation plan
  runs/         # corpus-run summaries
```

## Further reading

The full design rationale — the defect model, the TLE column specification,
the fix policy, and the architecture — is in
[`docs/superpowers/specs/2026-05-21-tle-corpus-cleaner-design.md`](docs/superpowers/specs/2026-05-21-tle-corpus-cleaner-design.md).
