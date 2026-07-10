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

On the bundled 29-file corpus (~232 M records), run with `--reconstruct-checksum`:
**99.96 % cleaned, 0.044 % quarantined** — every quarantined record fell into an anticipated
defect category. (Missing-checksum reconstruction is opt-in as of 0.6.0; without the flag the
71.3 M checksumless records are quarantined instead of cleaned — see
[Results](#results-on-the-bundled-corpus).)

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

# Audit a clean run's output for corruption and contradictions
uv run lintle verify [out-dir] --source [src-dir]

# Re-render a prior clean run's aggregate summary from its report.json
uv run lintle report [out-dir]

# Explain a rule ID or fix tag — definition, examples, source citation
uv run lintle explain <TAG>

# Compare two clean runs' findings (per-rule deltas)
uv run lintle diff <run-a> <run-b>
```

**Arguments and options:**

| Option | Default | Meaning |
|--------|---------|---------|
| `path` | `data/source` | A single file or directory. A directory is globbed for `tle*.txt` (tool output `*.cleaned.txt` / `*.broken.txt` is excluded). |
| `--out-dir DIR` | `data/output` | Where `clean` writes its output. Created if absent. |
| `--jobs N` | CPU count − 1 | Files processed in parallel. Lower it if a slow disk causes I/O contention. |
| `--report text\|json` | `text` | Summary format. |
| `--max-quarantined N[%]` | `0` | Exit non-zero only if MORE than N records were quarantined; or, with a trailing `%`, more than `N%` of routed records (`clean + quarantined`). Default `0` ≡ "any quarantine fails". |
| `--resume` / `--no-resume` | — | (`clean` only) Resume an interrupted run without prompting / ignore any checkpoint and start fresh. See [Cancelling and resuming](#cancelling-and-resuming). |
| `--reconstruct-checksum` | off | (`clean` only) Opt in to tier-2 missing-checksum reconstruction: recompute and append a dropped column-69 checksum instead of quarantining the 68-char line. Off by default because a dropped trailing *data* character is indistinguishable from a dropped checksum. Part of the resume run-identity, so toggling it forces a fresh run. |

**Examples:**

```bash
# Clean the whole corpus
uv run lintle clean data/source

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

### Auditing a clean run — `lintle verify`

`clean` promises byte-faithful, always-valid output; `verify` is the independent second
opinion that checks the promise held. It reads a finished run's `<out-dir>/cleaned/` (never
mutating it) and runs three sgp4-free checks:

- **re-validate** — every cleaned record must still pass the one validator (`tle.py`);
- **contradiction** — no two records may share a `(catalog, epoch)` *and* an element-set
  number yet carry a different orbital state (benign same-epoch re-issues, which carry a new
  element-set number, are counted in a census, not flagged);
- **source byte-diff** — with `--source`, every cleaned line must be a *sanctioned* edge
  edit of a real source line; any interior-column change is corruption.

```bash
# Audit the default run against its source
uv run lintle verify data/output --source data/source

# Structural checks only (re-validate + contradiction), no source needed
uv run lintle verify data/output --no-source-diff
```

| Option | Default | Meaning |
|--------|---------|---------|
| `out-dir` | stored config, else `data/output` | The finished clean run to audit (reads `<out-dir>/cleaned/`). |
| `--source DIR` | stored config, else `data/source` | Original source tree for the byte-diff (goal 1). |
| `--no-source-diff` | off | Skip the byte-diff; re-validate and check contradictions only. |

The suspects report is written under `<out-dir>/verify/` (`suspects.jsonl` +
`summary.{json,md}`). Exit codes: **`0`** clean, **`1`** at least one hard suspect
(a re-validation failure, an element-set contradiction, or an interior mutation), **`2`**
operational error (no cleaned output to audit).

### Interactive wizard & remembered paths — `.lintle.json`

Run `lintle` with **no subcommand** on a terminal and it opens a small menu — configure,
clean, verify, report, quit — instead of printing help. The source and output directories you
pick are remembered in a project-local **`./.lintle.json`** so the everyday commands run
without repeating paths. Precedence is always **explicit CLI arg → stored config →
built-in default**, and a stored path that has since vanished is re-prompted. Off a TTY
(scripts, CI, pipes) a bare `lintle` keeps the old behaviour — prints help and exits `2` — so
nothing that pipes `lintle` ever blocks on a prompt.

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

Even the checksum recompute is **opt-in** as of 0.6.0 (`--reconstruct-checksum`): by
default a checksumless 68-char line is quarantined, because a dropped trailing *data*
character is indistinguishable from a dropped checksum, so reconstructing one by default
could silently emit wrong-but-valid data.

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
├── report.jsonl            — corpus-wide structured findings stream
└── report.md               — corpus-wide run report
```

- **`cleaned/tleYYYY.cleaned.txt`** — standard 2-line TLE text, every record verified valid
  and ready for downstream ingestion.
- **`broken/tleYYYY.broken.txt`** — the quarantine sidecar: source line number(s), a
  human-readable reason, and the offending line(s) copied **byte-faithfully**, with a header
  formatted to paste into a space-track defect report.
- **`broken-noradids.ndjson`** — one `{"noradId":N}` per line, the deduplicated, sorted set
  of NORAD catalog numbers quarantined anywhere in the run (for programmatic consumers).
- **`report.md`** — human-readable run report: corpus totals, % cleaned/quarantined, fix
  counts, the per-rule defect breakdown, a per-file table, a per-NORAD breakdown, and — when
  any input file failed to process — a `## Failures` table naming each failed file and its error.
- **`report.json`** — the machine-readable run envelope, **byte-identical** to the
  `--report json` stdout output. Persisted on every clean run so `lintle report` can
  re-render the summary later without re-processing the corpus.

At the end of a clean run an **aggregate summary panel** is rendered to **stderr** —
corpus totals, % cleaned/quarantined, and the top fix / quarantine rules — sized to the
terminal width (with an ASCII-bar fallback off a TTY). Text-mode stdout stays empty; the
full machine summary is `report.json` (or `--report json` on stdout). `records` counts
paired 2-line entries; `clean` are those that passed and were written; `quarantined` is
everything routed to `broken/` (failed records **and** every orphan line). The invariant is
`records + orphan == clean + quarantined`. Defects key by the stable `RuleID` registry
(`TLE-CHK-001`, `TLE-PAIR-001`, …) so one identifier names a defect across every artifact.

`lintle report [out-dir]` re-renders that panel to **stdout** from a prior run's
`report.json` (or echoes the JSON verbatim with `--report json`); a missing or unreadable
`report.json` exits `2`.

Live progress during a long run is also written to **stderr** (so it never pollutes the
stdout `--report json` pipe): a size roster up front, per-file byte/record progress with
throughput and ETA, and an `[k/N]` line as each file finishes.

→ Machine-readable contracts (`--report json` envelope, `report.jsonl`, the `.broken.txt`
format, the checkpoint): [`ARCHITECTURE.md` §6](ARCHITECTURE.md#6-outputs--machine-readable-contracts).

## Results on the bundled corpus

A full run over the 29-file corpus (`tle2004`–`tle2025`, ~232 million records), **with
`--reconstruct-checksum`**:

- **99.96 % cleaned** — 187.9 M trailing-`\` artifacts stripped, 71.3 M missing checksums
  reconstructed.
- **0.044 % quarantined** (103,228 records) as genuinely corrupt — every quarantined record
  fell into an anticipated category; no unknown defect type surfaced.

Since 0.6.0 the missing-checksum recompute is opt-in (default off). A **default** run over the
same corpus quarantines those 71.3 M checksumless records instead of reconstructing them, so
the default-mode cleaned rate is correspondingly lower — pass `--reconstruct-checksum` to
reproduce the figures above.

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
