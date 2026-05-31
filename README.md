# lintle

**Validate and clean Two-Line Element (TLE) satellite corpora — correctness-first.**

`lintle` audits TLE files exported from [space-track.org](https://www.space-track.org/)
against the standardized TLE spec, repairs the systematic export defects, and emits a
uniform, **de-defected** corpus that any SGP4 / orbital-mechanics library can ingest
directly. Records it cannot *safely* repair are quarantined — never silently mangled —
into a per-file sidecar detailed enough to file a defect report with space-track.

- **Correctness over recovery** — every emitted record is re-validated and valid *by
  construction*; on any doubt a record is quarantined, never guessed.
- **Constant memory** — streams a 3 GB file line-by-line; the whole ~30 GB corpus never
  loads into RAM.
- **Byte-deterministic output** — same input → identical bytes every run (diff-able,
  CI-friendly).

On the bundled 29-file corpus (~232 M records): **99.96 % cleaned, 0.044 % quarantined** —
every quarantined record fell into an anticipated defect category.

---

## What problem it solves

A TLE record is two fixed-width lines, each *exactly* 69 ASCII columns, with a mod-10
checksum in column 69. Bulk historical exports from space-track carry two systematic,
era-specific defects:

- **Trailing `\` artifact** — almost every `Line 1` has an extra `\` byte appended before
  the newline.
- **Missing checksum digit** — many records were exported without their column-69
  checksum, leaving 68-column lines.

These appear independently and in combination, and a small fraction of records are
genuinely corrupt (garbled columns, orphaned lines, wrong lengths). `lintle` distinguishes
the safely-repairable from the genuinely-corrupt and treats each correctly.

## Installation

Requires **Python 3.14+** and [`uv`](https://docs.astral.sh/uv/). The only runtime
dependency is **`rich`** (`>=15,<16`, terminal rendering for the `clean` progress UI);
everything else is standard library. (`sgp4` is a dev-only test oracle.)

```bash
uv sync
```

No build step is needed to run the tool.

## Usage

The console script is `lintle` (`python -m lintle …` is equivalent):

```bash
# Produce cleaned output + quarantine sidecars
uv run lintle clean [path]

# Render the aggregate summary from the last clean run
uv run lintle report [out-dir]

# Explain a rule ID or fix tag — definition, examples, source citation
uv run lintle explain <TAG>

# Compare two clean runs' findings (per-rule deltas)
uv run lintle diff <run-a> <run-b>
```

**Arguments and options:**

| Option | Default | Meaning |
|--------|---------|---------|
| `path` | `data/source` | (`clean`) A single file or directory. A directory is globbed for `tle*.txt` (tool output `*.cleaned.txt` / `*.broken.txt` is excluded). |
| `out-dir` | `data/output` | (`report`) The `--out-dir` written by a prior `clean` run. Reads `report.json`; exits 2 if absent or wrong schema. |
| `--out-dir DIR` | `data/output` | (`clean`) Where `clean` writes its output. Created if absent. |
| `--jobs N` | CPU count − 1 | Files processed in parallel. Lower it if a slow disk causes I/O contention. |
| `--report text\|json` | `text` | Summary format. For `clean`, `text` renders the aggregate panel on stderr; `json` emits the envelope on stdout. For `report`, `text` renders the panel on stdout; `json` re-emits `report.json` verbatim on stdout. |
| `--max-quarantined N[%]` | `0` | Exit non-zero only if MORE than N records were quarantined; or, with a trailing `%`, more than `N%` of routed records (`clean + quarantined`). Default `0` ≡ "any quarantine fails". |
| `--resume` / `--no-resume` | — | (`clean` only) Resume an interrupted run without prompting / ignore any checkpoint and start fresh. See [Cancelling and resuming](#cancelling-and-resuming). |

**Examples:**

```bash
# Clean one file to a custom location
uv run lintle clean data/source/tle2022.txt --out-dir data/output

# Clean the corpus, capture a machine-readable summary
uv run lintle clean data/source --report json > run-summary.json

# CI gate: fail only if more than 100 records (or 1% of routed records) are quarantined
uv run lintle clean data/source --max-quarantined 100 --report json > run-summary.json
uv run lintle clean data/source --max-quarantined 1%  --report json > run-summary.json

# Look up what a rule ID or fix tag means, with a verified example
uv run lintle explain TLE-CHK-001
uv run lintle explain reconstructed-checksum
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Quarantine count (or rate) is at or below `--max-quarantined` (default `0`). |
| `1` | Quarantine count (or rate) exceeded `--max-quarantined`. |
| `2` | Operational error — no input files, disk shortfall, lock held, stale/corrupt/declined resume, or a file that failed to process. |
| `129` / `130` / `143` | Killed by `SIGHUP` / `Ctrl-C` (`SIGINT`) / `SIGTERM`. |

Repairable defects (including the near-universal trailing `\`) do **not** raise the exit
code above 0 — almost every raw file contains them. `--max-quarantined` preserves the
meaningful `2` (operational error) and `130` (Ctrl-C) signals that a `lintle … || true`
pipe would swallow.

## Correctness guarantees & limits

This is the heart of the tool. The cleaner never applies a fix and hopes: it applies a
candidate fix, re-runs the *full* validator, and commits **only if the result passes** — so
the output cannot contain a wrong-but-valid-looking record. **One validator** (`tle.py`)
defines what "perfect" means; `clean` checks every candidate repair against it before
committing — so correctness is structural, not assumed.

**lintle never invents data.** The single sanctioned reconstruction is the column-69
checksum — safe *only because* it is a deterministic mod-10 function of columns 1–68, so
recomputing a missing one asserts nothing the record didn't already say (the **redundancy
paradox**: the only field safe to rebuild is the one that was redundant to begin with). A
mod-10 checksum accepts a wrong line 1-in-10 times by luck, so guessing an orbital-data
character risks a record that *looks* valid but is silently wrong — the one outcome worse
than dropping it. So anything requiring such a guess (bad checksum, wrong length, orphan
line, garbled columns) is **quarantined**, not repaired.

Fixes fall into five classes in decreasing order of safety — content-preserving (trailing
`\`, CRLF, trailing whitespace), reconstructed-checksum, content-shifting (leading trim),
structural (drop blanks), and corrupt (quarantine).

→ Full fix-class table, repair tiers, and the stable rule registry:
[`ARCHITECTURE.md` §1](ARCHITECTURE.md#1-purpose--principles) and
[§4](ARCHITECTURE.md#4-repair-model).

## Output

A `clean` run lays `--out-dir` out like this:

```
<out-dir>/
├── cleaned/                tleYYYY.cleaned.txt   — one per input file
├── broken/                 tleYYYY.broken.txt    — one per input file
├── broken-noradids.ndjson  — corpus-wide list of quarantined NORAD IDs
├── report.json             — persisted run envelope (schema_version "2")
├── report.jsonl            — corpus-wide structured findings stream
└── report.md               — corpus-wide run report (per-file detail)
```

- **`cleaned/tleYYYY.cleaned.txt`** — standard 2-line TLE text, every record verified valid
  and ready for downstream ingestion.
- **`broken/tleYYYY.broken.txt`** — the quarantine sidecar: source line number(s), a
  human-readable reason, and the offending line(s) copied **byte-faithfully**, with a header
  formatted to paste into a space-track defect report.
- **`broken-noradids.ndjson`** — one `{"noradId":N}` per line, the deduplicated, sorted set
  of NORAD catalog numbers quarantined anywhere in the run (for programmatic consumers).
- **`report.json`** — the persisted run envelope: byte-identical to `--report json` stdout
  (`indent=2`, trailing newline, insertion-order keys). Read by `lintle report`.
- **`report.md`** — human-readable run report: corpus totals, % cleaned/quarantined, fix
  counts, the per-rule defect breakdown, a per-file table, and a per-NORAD breakdown.

At the end of a run, `clean` renders a **responsive aggregate panel** to stderr (corpus
totals, Fixes applied, and Quarantined-by-rule counts). The panel is width-tiered: plain
output when piped or on a narrow terminal, richer sections and progress bars at wider widths.
Per-file detail (record counts, fix breakdown per file) remains in `report.md`. With
`--report json`, the JSON envelope is emitted on stdout and the panel still renders on stderr
when a TTY is attached.

`lintle report [out-dir]` re-renders this panel at any later time from `report.json`
(default `data/output`). It exits 2 if `report.json` is missing or carries a
`schema_version` other than `"2"`. With `--report json` it re-emits `report.json` verbatim
on stdout instead.

Live progress during a long run is written to **stderr**: a size roster up front, per-file
byte/record progress with throughput and ETA, and a files-done/total counter. Stdout stays
silent in text mode, so `--report json > run-summary.json` captures only the clean envelope.

→ Machine-readable contracts (`--report json` envelope, `report.jsonl`, the `.broken.txt`
format, the checkpoint): [`ARCHITECTURE.md` §6](ARCHITECTURE.md#6-outputs--machine-readable-contracts).

## Results on the bundled corpus

A full run over the 29-file corpus (`tle2004`–`tle2025`, ~232 million records):

- **99.96 % cleaned** — 187.9 M trailing-`\` artifacts stripped, 71.3 M missing checksums
  reconstructed.
- **0.044 % quarantined** (103,228 records) as genuinely corrupt — every quarantined record
  fell into an anticipated category; no unknown defect type surfaced.

## Operational notes

### Cancelling and resuming

A long `clean` can be interrupted (Ctrl-C, a closed laptop, `SIGTERM`/`SIGHUP`). Re-run
**the same command** (same `--out-dir`, unchanged inputs) to resume; on a TTY it prompts,
in CI it auto-resumes. **Resume granularity is a whole file:** completed files are skipped
and the file in flight at the interruption restarts from the beginning — so a multi-file
corpus run benefits, but a single-file run gains nothing. `--no-resume` discards the
checkpoint and starts fresh (clearing prior outputs).

→ Checkpoint shape and the resume-decision matrix:
[`ARCHITECTURE.md` §5](ARCHITECTURE.md#5-streaming-parallelism-durability-resume).

### Disk space

Every record is **routed** to exactly one of `cleaned/` or `broken/` — never duplicated — so
the output is roughly the input's size plus tiny metadata. As a guard, lintle requires
**~2× the total input size** free on the `--out-dir` volume before starting, aborting with
exit `2` if short (and warning on stderr in the 2×–2.5× borderline band). Rule of thumb for
the ~30 GB corpus: keep **~60 GB free** to clear the abort floor, **~75 GB** to clear the
warning. (The 12 GB `TLEs.zip` is not an input and is never read.)

## Development

```bash
uv sync                          # install dev dependencies
uv run pytest                    # run the test suite
uv run pytest --cov=lintle       # with a coverage report
uv run ruff check                # lint
uv run ruff format               # auto-format
```

The suite includes per-module unit tests, an asymmetric cross-check against the trusted
`sgp4` parser (a known-good TLE must be accepted by both), and end-to-end integration tests
(golden output, idempotence, re-validation). See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for setup, testing, and the git workflow.

## Further reading

[`ARCHITECTURE.md`](ARCHITECTURE.md) is the living design reference — the validator
definition, the module map and data flow, the repair model, streaming/durability/resume, the
machine-readable output-format contracts, and the runtime-dependency policy. Dated design
specs, plans, and corpus-run summaries are kept for historical rationale under
[`docs/superpowers/archive/`](docs/superpowers/archive/).
