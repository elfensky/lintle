# TLE Corpus Validator & Cleaner — Design

- **Date:** 2026-05-21
- **Status:** Implemented; §9 output layout revised post-build (2026-05-22)
- **Revision:** §1/§6 defect model corrected against a full-corpus scan (§1.1 measured
  distribution added); §6.2 reconstructed-checksum repair added; §5.4 gains semantic/range
  validation; §4.1 correctness claim downgraded to validation-conformance; §8 pairing made
  prefix-driven with the same-satellite mispair disclosed; §4.2/§7 updated for the
  `data/source/` + `data/output/` corpus layout. **2026-05-22:** §9 — `clean` now splits
  output into `cleaned/` and `broken/` subdirectories and writes a `report.md` run report.
  **2026-05-22:** project renamed `tlekit` → `lintle` — the package, PyPI distribution, and
  console script are all now `lintle` (no behavioural change).
  **2026-05-24:** §9 — `FileStats.total_records` split into three independent
  counters (`paired_records`, `orphan_entries`, `input_lines_seen`) so the
  per-file and run-report columns no longer conflate a half-record (a single
  orphan line) with a full 2-line record. Fix counts and reject categories
  are unchanged; report wording adjusted (issue #5).
  **2026-05-24:** §6 / §9 — the rejection model (`RejectCategory` + free-form
  `reason: str`) is superseded by a stable `RuleID` registry and a structured
  `Diagnostic` dataclass; `.broken.txt` line format is rewritten under a 0.3.0
  minor bump. See companion spec
  [`2026-05-24-stable-rule-id-registry-design.md`](2026-05-24-stable-rule-id-registry-design.md)
  (issue #8). Other sections of this design (validator definition, repair tiers,
  streaming/constant-memory, pairing) remain authoritative.
  **2026-05-24:** §9.4 — `report.md` gains a `## Per-NORAD breakdown`
  section at the bottom: per-satellite quarantine count, per-rule defect
  rollup, and source filenames the satellite appeared in, sorted by count
  descending and capped at top-N (default 100) with a remainder footer.
  `FileStats.quarantined_norad_ids` changes type from `set[int]` to
  `dict[int, dict[RuleID, int]]` to carry the per-rule context; the
  `broken-noradids.ndjson` sidecar contract is unchanged (issue #40).
  **2026-05-24:** §13 added — "Resumable runs" specifies a `manifest.json`
  artifact in `--out-dir` with a version-pinned skip predicate, per-file
  atomicity tied to the existing `os.replace` commit point, end-of-run
  manifest write semantics, and `--no-skip` / `--force` CLI overrides.
  Designed, not yet implemented; codifies the contract for issue #12. The
  prior §13 (Open considerations) renumbers to §14.
  **2026-05-25:** §4.3 / §8 / §11 / §13 — folded the reject-sink refactor
  (issue #19) into the design. §4.3 names `RejectSink` and `FileSample` in
  `report.py`'s responsibilities. §8 routes rejects through `RejectSink`
  (which owns `BrokenFileWriter`) and refreshes the `iter_records` /
  `repair.process_record` references. §11 gains a structural-invariant test
  bullet for the cap-by-construction guarantee. §13.1 clarifies the
  `FileStats` snapshot excludes the bytes-bearing `reject_sample`; §13.3
  line references shift for the refactored pipeline (`os.replace` at
  `pipeline.py:263` followed by `sink.finalize`, both inside `with sink:`)
  and disclose the narrow crash-between-replace-and-finalize partial-outcome
  window the refactor introduced.
  **2026-05-25:** §9 — `--report json` output replaced (breaking) by a versioned
  envelope object (`{schema_version, run, environment, summary, files}`) with
  per-file timing (`elapsed_seconds`, `bytes`, `records_per_sec`), tool/Python
  version, and corpus-wide aggregates. See companion spec
  [`2026-05-25-report-json-envelope.md`](2026-05-25-report-json-envelope.md)
  (issue #20). `FileStats` gains `elapsed_seconds: float` and `bytes: int`;
  `pipeline.process_file` captures both. The prior flat-array output is
  removed; no legacy flag.
  **2026-05-27:** §13 — the "Resumable runs" `manifest.json` design is **considered
  and rejected**: issue #12 closed wontfix. A cross-run skip cache trades the project's
  correctness-over-recovery principle for a rarely-realised payoff, the version-pin
  guard is defeated by the "no per-merge version bumps" workflow, and a skip-run would
  silently under-fill `report.jsonl` (breaking `diff`, #10). The section is retained,
  annotated, for the reasoning. Superseded by **single-run resume** (`--resume`), a
  durable checkpoint scoped to finishing one interrupted run (issue #56). §13's status
  line updated; section bodies and numbering otherwise unchanged.
  **2026-05-27:** §15 added — "Single-run resume (`--resume`)" specifies the implemented
  checkpoint that lets an interrupted `clean` be finished without redoing completed files:
  an always-on `.clean-state.json` in `--out-dir` (deleted on success), refuse-on-change
  validation against `lintle_version` + per-input identity, reused-file `FileStats`
  reconstruction for a complete report, and `.shards` preservation so `report.jsonl` stays
  complete. The correctness-preserving replacement for the rejected §13 (issue #56).
  **2026-05-27:** §15.4 — the durability *limit* is resolved (issue #58): every committed
  file routes through `fsutil.durable_replace` (fsync data → `os.replace` → fsync directory),
  with `F_FULLFSYNC` as the macOS power-loss barrier and `os.fsync` elsewhere. Durability is
  always-on (no flag) — measured at ~1 s over a ~120-commit, multi-minute run on the 30 GB
  corpus — and the worker fsyncs its outputs before the parent records `completed`, so
  `--resume` can never trust a non-durable output. §15.4 retitled "Durability limit" →
  "Durability".
  **2026-05-28:** §10 — `clean`'s preflight disk-space check gains a
  borderline-warning band: free space below 2× input still aborts with exit
  `2` (unchanged); 2× to 2.5× now prints a warning to stderr and proceeds;
  above 2.5× is silent (unchanged). Internal: `cli._check_disk_space` returns
  `(severity, message) | None` instead of `string | None` (PR #64).
  **2026-05-28:** §3 — added §3.1 "Runtime dependency policy & considered
  dependencies": the flat pure-stdlib runtime rule becomes a goal-led
  four-MUST-bar test (earns-its-weight · mature · small surface · operational
  fit; the aim is veto-only) with a canonical considered/deferred table. This
  spec is the canonical home; `CLAUDE.md` / `CONTRIBUTING.md` point here. Runtime
  stays zero-dependency. Companion:
  [`2026-05-28-runtime-dependency-policy-design.md`](2026-05-28-runtime-dependency-policy-design.md).
- **Topic:** A tool to validate and clean a multi-gigabyte corpus of Two-Line Element (TLE) files exported from space-track.org

## 1. Problem statement

The corpus is 29 `.txt` files of TLE data spanning years 2004–2025 — the 2004 data
is split into 8 parts (`tle2004_1of8.txt` … `tle2004_8of8.txt`), each split on a record boundary —
totalling roughly 30 GB, plus a separate 12 GB `TLEs.zip`. All of it lives in `data/source/`. A full-corpus scan reveals two
systematic export-pipeline defects, which appear both independently and in combination:

- **Trailing `\` on Line 1** — Line 1 records carry an extra trailing `\` byte. The scan shows
  this on **every Line 1 of every file except `tle2019`, `tle2023`, and `tle2025`** — years
  2004–2024 with three exceptions, not the 2004-only quirk originally assumed. A systematic
  export artifact, not sporadic corruption.
- **Missing checksum digit** — many records were exported *without* their column-69 checksum
  digit. Both Line 1 and Line 2 then carry 68 data columns rather than 69, with columns 1–68
  intact and well-formed — only the derived checksum is absent. Prevalence varies enormously by
  file (~95% of `tle2004_1of8.txt`, ~75% of `tle2017.txt`, under 1% of the cleanest files);
  corpus-wide it is ~15% of records — not an edge case.

The two defects combine into the observed line shapes:

| Line 1 bytes | Meaning |
|--------------|---------|
| 70, ends `\` | 69 valid columns (checksum present) + trailing `\` artifact |
| 69, ends `\` | 68 columns (checksum *missing*) + the `\` artifact occupying column 69 |
| 69, clean    | a correct record |
| 68           | one character missing — the checksum if columns 1–68 still validate, otherwise an interior character (quarantined). The 2025 file shows this in ~0.1–0.2% of lines. |

Line 2 never carries the `\` artifact; it is 69 bytes (checksum present) or 68 bytes (checksum
missing).

### 1.1 Measured defect distribution

A full-corpus length-and-backslash scan (≈232 M records / ≈465 M lines) gives the real
distribution — the design is sized against these measured numbers, not against the two defects
originally observed by eye:

| Record category | Fix | Share | Count |
|------------------|-----|-------|-------|
| Variant A — Line 1 carries its checksum and a trailing `\` | strip `\` (§6.1) | ~67% | ~156.6 M |
| Checksum-less — Line 1 and Line 2 both lack column 69 | reconstruct checksum (§6.2) | ~15% | ~35.6 M |
| Already clean — 69/69, no `\` | none | ~17% | ~40.1 M |
| Genuinely corrupt — odd length, bad prefix, orphan | quarantine (§6.5) | <0.01% | ~10⁴ |

The trailing `\` is the corpus-wide dominant defect; the missing checksum is dominant only
*within specific files*. The scan also surfaced the small genuinely-corrupt fraction the catalog
must quarantine: ~1,250 lines of length 71, a handful of length-1 lines, ~2,300 mis-prefixed
lines in `tle2020.txt`, ~3,200 blank/CR lines in `tle2019.txt`, and a `1 `/`2 ` count mismatch of
2 in `tle2017.txt` (orphans). No file uses `\r\n` line endings at scale; `1 `/`2 ` prefix counts
match exactly in 26 of 29 files, so mispairing risk is low in practice.

The `validate` discovery pass (Section 12) still runs the *full* validator — column layout,
semantic ranges, and checksum — so it remains the authoritative defect catalog; this scan sized
only the length/backslash defects.

We need:

1. A **validator** that audits a file, reports which lines are defective, where, and why, and
   confirms when a file is perfect.
2. A **cleaner** that emits a corrected, uniformly formatted file suitable for ingestion by other
   applications, and isolates records it cannot safely fix.

The desired output is a *unified, clean, easily parsable* TLE text format — standard 2-line TLE,
de-defected — so any downstream SGP4/orbital library can ingest it directly.

## 2. Goals and non-goals

### Goals

- Validate any TLE file against the standardized TLE specification (column layout + mod-10 checksum).
- Produce a cleaned file in which **every record is guaranteed valid** — verified, not assumed.
- Quarantine records that cannot be safely fixed into a per-source-file sidecar that is detailed
  enough to file a defect report with space-track.org.
- Stream multi-gigabyte files in constant memory.
- One validator definition of "perfect," reused by both the audit and the cleaning paths.

### Non-goals

- **No orbit propagation or analysis.** Output is clean TLE text; downstream apps do the science.
- **No structured/columnar output** (JSON, CSV, Parquet). Output stays in native TLE text format.
- **No aggressive reconstruction** of corrupt records — no brute-forcing a missing *data*
  character. A mod-10 checksum has a 1-in-10 false-accept rate, so guessing an orbital-data
  character risks silently producing plausible-but-wrong data. Such records are quarantined for
  space-track to fix. (Recomputing a *missing checksum digit* from intact columns 1–68 is **not**
  reconstruction in this sense — the checksum is a deterministic function of known-good data — and
  is an explicit, separately-tracked repair; see Section 6.2.)
- **No handling of the 12 GB `TLEs.zip`** — assumed to be an archive of the same `.txt` files.
- **No name/`0` lines.** The corpus is 2LE format (alternating `1 `/`2 ` lines only); the cleaned
  format preserves this.

## 3. Existing tools — why build

Surveyed: `python-sgp4`, `tletools`, `Skyfield`, `pyorbital`, `orbit-predictor`, `spacetrack`,
and assorted checksum gists. Every one of them is built to *consume* a TLE, not to *audit and
repair* a corrupt corpus. None classifies defects by type and location, quarantines unfixable
records, applies checksum-verified repairs, or streams multi-gigabyte files. The build-it decision
stands.

Two adjustments taken from the survey:

- **Do not reinvent the spec.** The TLE column layout and mod-10 checksum are fully standardized
  (CelesTrak / space-track documentation). They are implemented directly — no runtime library — but
  not guessed.
- **Use an existing parser as a test oracle.** `sgp4` (and optionally `tletools`) are added as
  **dev-only** dependencies and used in the test suite to cross-check our validation against a
  trusted implementation. The runtime carries no third-party dependencies today; when that
  changes it is governed by the policy in §3.1.

### 3.1 Runtime dependency policy & considered dependencies

The runtime is lean by policy, not by dogma. **This subsection is the canonical source for
the runtime-dependency rule** — `CLAUDE.md` and `CONTRIBUTING.md` carry only a pointer to it,
and the rationale + the four-model debate behind it are in
[`2026-05-28-runtime-dependency-policy-design.md`](2026-05-28-runtime-dependency-policy-design.md).

The aim is a stable, maintainable, easy-to-understand app. A third-party **runtime**
dependency may be added only when it advances that aim and **clears all four bars below —
each a necessary condition (MUST).** The aim is a **veto, never a waiver**: it can reject a
dependency that clears every bar, but never admit one that fails a bar. A genuine exception
requires an explicit row in the table below naming which bar fails and why.

1. **Earns its weight** — replaces real code we would otherwise maintain (~100 lines, rule of
   thumb, *or* a gotcha-prone domain: terminal control, parsing, compression). A `left-pad`
   one-liner never qualifies; an `axios`-class domain does.
2. **Mature & widely deployed** — used by major CLIs, active upstream, healthy releases.
3. **Small transitive surface** — its *direct* runtime dependencies number ≤ 3, each itself
   well-known. A single call that drags a large tree fails here.
4. **Operational fit** — packaging and behaviour must not threaten our invariants:
   pure-Python or widely-prebuilt wheels (no surprise native toolchain at install); bounded,
   streaming-friendly memory (Critical Rule #3); deterministic, locale-independent output
   (Critical Rules #1/#2); no heavy import-time side effects; an acceptable license; a clean
   `pip-audit` history.

**Recording requirement (not a bar):** adoption lands with a `CHANGELOG.md` entry beside the
`pyproject.toml` edit. **Maintenance:** pin to exclude the next major (e.g. `rich>=13,<14`);
`uv.lock` is the lockfile of record; re-run all four bars on any major-version bump. **The
Critical Rules gate a dependency's *behaviour*, not just our own code** — a dep that would
form a second validator (#4), load a file whole (#3), or make output nondeterministic
(#1/#2) is out regardless of which module imports it. **Dev-only** deps (test oracles,
tooling) are exempt from the bars but record purpose/scope if nontrivial.

There is deliberately **no layering rule**: we gate on a dependency's value and behaviour,
never its file location. The core stays auditable because bar 1 rejects most core deps on
their own (the validator is simple stdlib code) and bar 4 + the Critical Rules cover the
behavioural risks.

**Current runtime dependencies: none** (`dependencies = []`).

#### Considered & deferred (canonical record)

| Tool | Disposition | Reason |
|---|---|---|
| TLE/orbital libs (`sgp4`, `Skyfield`, `tletools`, `astropy`) | **Reject (runtime)** | Using one as a parser/validator is a second validation path (**Critical Rule #4**) — this is *why* `sgp4` is a test-oracle dev dep. Dev-only oracle use is fine. |
| `click` / `typer` | **Reject** | `argparse` covers the small CLI surface; ~0 net lines. Fails bar 1. |
| `pydantic` | **Reject** | We own our formats (`report.jsonl`, resume checkpoint); dataclasses + `json` cover them (bar 1); `pydantic-core` is native (bar 4). As TLE validation it is a second validator (#4). |
| `structlog` / `loguru` | **Reject** | No logging; a report + progress UI cover output. Nothing to save. |
| `orjson` / `ujson` | **Reject** | Replaces ~zero code; JSON isn't the bottleneck; native (bar 4). Fails bar 1. |
| `polars` / `pandas` | **Reject** | `diff` is per-rule counters; a `dict[str,int]` does it (bar 1); huge native tree (bars 3+4). |
| config parsing (`tomli`, …) | **Reject** | `tomllib` is stdlib (3.11+); `configparser` / `json` / `argparse` cover the rest. |
| caching (`diskcache`, `cachetools`) | **Reject** | One-pass streaming tool; a `dict` suffices for bounded state. Fails bar 1. |
| `tqdm` | **Reject** | Can't render a dynamic block of N concurrent bars whose set changes; we'd rebuild it ourselves. |
| `textual` | **Reject** | Full TUI framework; we want a progress block, not an app. |
| `blessed` / `prompt_toolkit` | **Reject** | Lower-level; still ~50 lines of layout glue. `rich` fits better. |
| `rich` | **Candidate (pending issue #53 evidence)** | Plausibly clears all four bars for the issue-53 progress UI (~150 lines of gotcha-prone ANSI replaced; mature; pure-Python; transitive `markdown-it-py` + `pygments`). **Not approved; `dependencies = []` holds.** Approval is evidence-driven, in the adopting PR. A parity-only swap fails bar 1. |
| `zstandard` | **Defer (trigger-gated)** | Only if output size / transfer time becomes a *measured* bottleneck (compressing sidecars/shards). Trigger: file a ticket with the measurement; until then stdlib `gzip`. Native ext → must clear bar 4. |

Dev-only (exempt from the bars; record purpose/scope; land any time): `hypothesis`
(property-based tests for `tle.py` / `repair.py` — strongest candidate), `pytest-xdist`
(parallel test runs).

## 4. Architecture (Approach B)

A single `uv`-managed Python project with a **zero-dependency runtime today** (governed by
§3.1). The two user-facing asks become **one validator** used in two modes: the validator
defines "perfect"; the cleaner reuses it and emits only records that pass it.

### 4.1 The validated-transformation principle

The cleaner never applies a fix and hopes. It treats the validator as an oracle:

1. Apply a candidate transformation (strip the trailing `\`, normalize line endings, …).
2. Re-run *full* validation on the result — column layout **and** the mod-10 checksum.
3. Commit the fix **only if the result now passes.** Otherwise the record is quarantined.

Consequences: every line in the cleaned file is valid *by construction* — it passed full
validation on the way out. This is a strong guarantee, but not an absolute one: it proves
conformance to the validator, not the truth of the original record. Two residual risks remain and
are accepted explicitly: (1) the mod-10 checksum has a 1-in-10 false-accept rate (Section 2), so a
content-shifting fix (Section 6.3) could pass by coincidence; (2) a record paired from two
same-satellite epochs is undetectable (Section 8). Content-*preserving* fixes (Section 6.1) carry
neither risk. The cleaner therefore cannot *casually* turn a bad record into a wrong-but-valid
one — but "provably cannot" would overstate it.

### 4.2 Project layout

```
TLEs/
├── data/                   # git-ignored — multi-gigabyte corpus, not version-controlled
│   ├── source/             # the 29 raw tle*.txt files + TLEs.zip (inputs)
│   └── output/             # cleaned/, broken/, report.md (the cleaner writes here — §9)
├── pyproject.toml          # uv project; console script "lintle"; dev deps: pytest, sgp4
├── src/lintle/
│   ├── __init__.py
│   ├── tle.py              # CORE: defines a "perfect" TLE record (pure, no I/O)
│   ├── repair.py           # candidate fixes — speculative, validated by tle.py (pure, no I/O)
│   ├── pipeline.py         # streaming I/O: read → pair into records → route
│   ├── report.py           # writes .broken.txt sidecar + per-file summary
│   ├── cli.py              # argparse: `validate` and `clean` subcommands
│   └── __main__.py         # `python -m lintle`
├── tests/
│   ├── fixtures/           # tiny hand-built files, one per defect class
│   └── test_*.py
└── docs/superpowers/specs/2026-05-21-tle-corpus-cleaner-design.md
```

### 4.3 Modules

| Module | Responsibility | Depends on |
|--------|---------------|------------|
| `tle.py` | Defines validity: checksum, column layout, record pairing. Pure functions. Single source of truth. | nothing |
| `repair.py` | Conservative transformations. Each applied speculatively, confirmed by `tle.py`; committed only if valid. | `tle.py` |
| `pipeline.py` | Streams a file in **binary**, pairs `1 `/`2 ` lines into record candidates, routes each to cleaned/broken, tallies stats. | `tle.py`, `repair.py` |
| `report.py` | Renders the `.broken.txt` reject file and the run summary. Owns the `RejectSink` (single mutation entry point, per-rule cap enforced by construction, streaming sidecar lifecycle) and the immutable `FileSample` value object handed back to `FileStats` (issue #19). | nothing |
| `cli.py` | Globs paths, dispatches `validate` vs `clean`, drives parallelism, prints summary. | all of the above |

Dependencies point one way (`cli → pipeline → repair → tle`); each layer is testable without the
layers above it.

## 5. The TLE specification, as encoded by `tle.py`

A valid TLE record is two lines, **each exactly 69 ASCII columns**, `\n`-terminated.

### 5.1 Line 1 column layout

| Columns | Field |
|---------|-------|
| 1 | Line number — `1` |
| 2 | Space |
| 3–7 | Satellite catalog number (numeric, or **Alpha-5** alphanumeric for modern objects) |
| 8 | Classification — `U`, `C`, or `S` |
| 9 | Space |
| 10–11 | International designator — launch year (may be blank for older objects) |
| 12–14 | International designator — launch number |
| 15–17 | International designator — piece |
| 18 | Space |
| 19–20 | Epoch year |
| 21–32 | Epoch — day of year and fractional day (literal `.`) |
| 33 | Space |
| 34–43 | First derivative of mean motion (literal `.`, optional leading sign) |
| 44 | Space |
| 45–52 | Second derivative of mean motion (exponential notation, decimal assumed) |
| 53 | Space |
| 54–61 | B\* drag term (exponential notation, decimal assumed) |
| 62 | Space |
| 63 | Ephemeris type |
| 64 | Space |
| 65–68 | Element set number |
| 69 | Checksum |

### 5.2 Line 2 column layout

| Columns | Field |
|---------|-------|
| 1 | Line number — `2` |
| 2 | Space |
| 3–7 | Satellite catalog number (must match Line 1) |
| 8 | Space |
| 9–16 | Inclination (degrees) |
| 17 | Space |
| 18–25 | Right ascension of ascending node (degrees) |
| 26 | Space |
| 27–33 | Eccentricity (decimal point assumed) |
| 34 | Space |
| 35–42 | Argument of perigee (degrees) |
| 43 | Space |
| 44–51 | Mean anomaly (degrees) |
| 52 | Space |
| 53–63 | Mean motion (revolutions per day, literal `.`) |
| 64–68 | Revolution number at epoch |
| 69 | Checksum |

### 5.3 Checksum

Sum columns 1–68: each digit `0`–`9` adds its value, a minus sign `-` adds `1`, every other
character (letters, spaces, `.`, `+`) adds `0`. The checksum is `sum % 10` and must equal
column 69.

### 5.4 Validation levels

- **Line level (column layout):** length is exactly 69; correct line-number prefix; required
  separator columns contain spaces; literal decimal points are in position (epoch fraction, first
  derivative, mean motion); variable fields contain only their permitted character set, including
  the exact exponential-field grammar for columns 45–52 and 54–61 (a signed mantissa and a signed
  single-digit exponent, `±NNNNN±N`); checksum matches.
- **Semantic level (range checks):** numeric fields fall in their physically valid ranges —
  eccentricity in `[0, 1)`, inclination in `[0, 180]`, RAAN / argument of perigee / mean anomaly
  in `[0, 360)`, mean motion strictly positive, epoch day-of-year in `(0, 367)`. This level is
  **load-bearing** for records repaired under Section 6.2: once a missing checksum is
  self-computed, the checksum re-validation is circular, so the column-layout and semantic checks
  are the *only* non-circular evidence that the record is not garbled.
- **Record level:** a Line 1 immediately followed by a Line 2, with **matching satellite catalog
  numbers** (columns 3–7).

The checksum is one integrity check among several — primary only for records whose checksum digit
is genuinely present; for reconstructed-checksum records (Section 6.2) the column-layout and
semantic levels carry the verification.

Edge cases the validator must tolerate as valid: a blank international designator on older objects;
optional leading sign or space in numeric fields; Alpha-5 alphanumeric catalog numbers (letters
are permitted in columns 3–7 only — the digit-only rule still applies to every other numeric
field).

## 6. Defect catalog and fix policy

Five fix classes, in decreasing order of safety. Every fix is **speculative**: applied, then
re-validated (Section 4.1); committed only if the result passes full validation.

### 6.1 Content-preserving auto-fix

These transformations never touch columns 1–69 of the record, so they are *provably*
content-preserving — the checksum survives the fix as an independent integrity check.

| Defect | Action |
|--------|--------|
| Trailing `\` on a 70-byte Line 1 (artifact on a checksum-bearing line) | strip the byte → 69 columns |
| `\r\n` or lone `\r` line endings | normalize to `\n` |
| Trailing spaces / tabs after column 69 | trim |

A 70-byte line ending in `\` whose columns 1–69 still fail the checksum (the `\` was not the only
problem) is **not** committed — that record is quarantined.

### 6.2 Reconstructed-checksum repair

A major defect (Section 1) — ~15% of the corpus, and the majority of several files: a record
exported without its column-69 checksum digit. Line 1 appears as a 69-byte line ending in `\`
(the `\` artifact in column 69) or as a plain 68-byte line; Line 2 appears as a plain 68-byte
line.

Repair: strip the `\` if present, leaving 68 characters; confirm those 68 characters pass the
column-layout **and** semantic checks (Section 5.4) as columns 1–68; append the computed checksum
`sum(cols 1–68) % 10`; re-validate the full 69-character line.

This is a **distinct, weaker repair tier**, tracked separately in all reporting. After the
checksum is self-computed, the checksum re-validation is *circular* — it cannot fail — so
verification rests entirely on the column-layout and semantic checks of columns 1–68. This is
*not* the "aggressive reconstruction" ruled out in Section 2: the checksum is a deterministic
function of known-good columns, not a guessed data character. But it is honest to state that a
reconstructed checksum is **format conformance, not verified integrity** (see "perfect file"
below). A 68-byte line whose columns 1–68 fail the layout/semantic checks is *not* repaired —
the missing character is interior, not the checksum, and the record is quarantined.

### 6.3 Content-shifting fix — apply only if re-validation passes

| Defect | Action |
|--------|--------|
| Leading whitespace / UTF-8 BOM before column 1 | trim, then full re-validation |

Trimming a leading byte **shifts every fixed-width column**, so unlike Section 6.1 the result is
a structurally different record — safe only to the ~90% confidence of the mod-10 checksum plus
the column-layout/semantic backstop. It is therefore *not* a cosmetic fix: if the trimmed line
does not pass full validation it is quarantined, never force-fixed.

### 6.4 Structural — safe drop

| Defect | Action |
|--------|--------|
| Blank / empty line between records | drop, then resynchronise pairing on the next `1 ` line (Section 8) |

### 6.5 Corrupt — quarantine (→ `.broken.txt`)

| Defect | Action |
|--------|--------|
| Wrong-length line not matching a known repair (6.1–6.3) | quarantine record |
| Checksum mismatch on a full 69-char line whose checksum digit is present | quarantine record |
| 68-char line whose columns 1–68 fail layout/semantic checks (interior character missing) | quarantine record |
| Line does not start with `1 ` / `2 ` | quarantine |
| Orphan Line 1 or Line 2 (no valid pair) | quarantine |
| Catalog-number mismatch between paired lines | quarantine both |
| Non-ASCII / control character inside a record body | quarantine |
| Column-layout or semantic violation | quarantine |

### 6.6 Fix ordering

When several fixes apply to one record they are applied in a fixed order — line-ending
normalization, then leading-trim (6.3), then trailing-trim / backslash-strip (6.1), then
checksum reconstruction (6.2) — and full re-validation runs once, on the final candidate. Fix
counts are reported per class, so a run reports e.g. "stripped 8,412,064 trailing backslashes"
**and** "reconstructed 195,293 missing checksums."

### Definition of a "perfect" cleaned file

Pairs of records — Line 1 then Line 2 — each exactly 69 ASCII characters, `\n`-terminated, no
blank lines, matching catalog numbers, both lines passing the column-layout and semantic checks,
and both checksums valid. A record repaired under 6.2 satisfies "checksum valid" only *by
construction*; the run summary and per-record provenance distinguish **verified** checksums from
**reconstructed** ones. The cleaned file is uniformly shaped and directly parsable — it is
*format-conformant* — but a downstream consumer must not read a reconstructed checksum as an
independent integrity guarantee.

## 7. CLI

A console script `lintle` with two subcommands matching the two asks:

```
lintle validate [paths…]    # ask #1 — read-only audit, mutates nothing
lintle clean    [paths…]    # ask #2 — produces cleaned + broken files
```

Options:

- `paths` — files or a directory. A directory is globbed for `tle*.txt`, **excluding any
  `*.cleaned.txt` and `*.broken.txt`** so that re-running the tool on a directory that already
  contains output does not re-process its own results. Defaults to `data/source/`.
- `--out-dir DIR` — destination for cleaned/broken files. Default `data/output/`, keeping the
  `data/source/` inputs pristine. The output directory is created if it does not exist.
- `--jobs N` — number of files processed in parallel. Default = CPU count.
- `--report text|json` — summary format. Default `text`.

`validate` is read-only: it streams each file and reports per-file totals, defect counts by type,
and the source line numbers of defects. `clean` performs the same validation and additionally
writes output files.

## 8. Data flow

Per file, fully streaming:

```
read bytes ─▶ line state-machine ─▶ pair into record candidates ─▶ repair.process()
   (binary)     (drops blank lines)    (1-line + 2-line + src line #s)     │
                                                              ┌────────────┴────────────┐
                                                      speculative strip → tle.validate
                                                              │                         │
                                                          passes?                     fails?
                                                              ▼                         ▼
                                                   <name>.cleaned.txt          <name>.broken.txt
```

1. `pipeline.iter_records(path)` opens the file in **binary** mode (to observe `\r`, `\`, and
   encoding precisely) and iterates lines. A small state machine consumes raw lines, drops blank
   lines, and yields record candidates: `(raw_line1, raw_line2, source_line_numbers)`. Lines that
   cannot be paired (orphans) are yielded as orphan candidates.
2. `repair.process_record(line1, src1, line2, src2)` decodes, applies cosmetic strips
   speculatively, runs `tle.validate_record`, and returns either `Accepted(line1, line2,
   fixes_applied)` or a `Rejected` value carrying a primary `Diagnostic` (issue #8).
3. `pipeline` routes Accepted records to `<name>.cleaned.txt` and Rejected records into a
   per-file `report.RejectSink`. The sink owns the byte-faithful `BrokenFileWriter` (in
   `clean` mode) and the bounded in-memory sample used by the `validate` summary; its per-rule
   cap is enforced by construction so over-cap entries cannot be inserted (issue #19). On
   finalize, the sink yields an immutable `FileSample` attached to the file's `FileStats`.
4. After each file, a summary is emitted.

The pairing state machine is **prefix-driven**: it expects a `1 ` line, then a `2 ` line. A `1 `
seen while a `1 ` is already held orphans the held line and starts over; a `2 ` with no held `1 `
is an orphan; a blank line is dropped without breaking the expectation. Pairing therefore
**resynchronises on every `1 ` prefix** — a single missing or dropped line cannot cascade into a
run of mispaired records.

One residual risk is accepted explicitly and cannot be eliminated: if a record's Line 2 *and* the
next record's Line 1 are both missing, the state machine can pair a Line 1 and a Line 2 drawn
from two *different epochs of the same satellite*. Both lines are individually valid and their
catalog numbers match, so the validator accepts the pair. The TLE format carries no cross-line
redundancy beyond the catalog number, so this same-satellite mispair is **undetectable** — it is
the reason Section 4.1 claims validation conformance, not absolute correctness. It is rare in
catalog-then-epoch-sorted exports; it is disclosed here rather than silently assumed away.

The state machine holds at most two lines, so the largest single file (~3.2 GB) streams in
constant memory. Each of the 29 files is independent, so `cli.py` runs them through a
`concurrent.futures.ProcessPoolExecutor`. `--jobs` defaults to the CPU count; on a single slow
disk, processing 29 multi-gigabyte files concurrently can cause I/O contention — operators may
lower `--jobs` if throughput suffers.

**Idempotence:** running `clean` on an already-clean file yields byte-identical output with zero
fixes and zero rejects; running `validate` on a `.cleaned.txt` reports it perfect.

## 9. Output formats

A `clean` run organises `--out-dir` into two subdirectories plus a run report:

```
<out-dir>/
├── cleaned/    <name>.cleaned.txt   — one per input file (§9.1)
├── broken/     <name>.broken.txt    — one per input file (§9.2)
└── report.md   — corpus-wide run report (§9.4)
```

### 9.1 Cleaned file — `cleaned/<name>.cleaned.txt`

Standard 2-line TLE text, every record guaranteed valid (Section 6). Modern objects may carry
Alpha-5 alphanumeric catalog numbers; these are preserved verbatim. Most SGP4 implementations
accept them, but a few older consumers do not — the cleaned output is standard TLE, not a
lowest-common-denominator dialect.

### 9.2 Reject file — `broken/<name>.broken.txt`

Per source file, formatted to be detailed enough to file a report with space-track.org:

```
# tle2022.broken.txt — quarantined records
# source: tle2022.txt | generated: 2026-05-24T14:03:00Z | lintle 0.3.0
# 3 quarantined of 8,412,067 entries

[1] source lines 14820-14821 - rule: TLE-CHK-001 (tier-1) col 69 observed='3' expected='7'
1 43210U 18014A   22045.12345678  .00001234  00000-0  12345-4 0  9991
2 43210  53.0123 211.4567 0001234  90.1234 270.9876 15.12345678123453

[2] source line 99102 - rule: TLE-PAIR-001 - orphan line 1 at end of file
1 51234U 21001A   22045.12345678  .00001234  00000-0  12345-4 0  9991

[3] source line 250011 - rule: TLE-COL-002 - 68-char line where columns 1-68 fail layout —
    missing character is interior, not the checksum, so not reconstructible (Section 6.2)
1 27497U 01055E   0415 .01279831  .00005767  00000-0  41216-3 0 7230
```

Each entry: index, source filename + line number(s), structured diagnostic
(stable `RuleID` token, optional `(tier-N)` repair attempt, optional `col`/`cols`
range, optional `observed=` / `expected=` fields, optional free-text note),
then the raw line(s) verbatim. When both lines of a record failed, related
diagnostics fold onto indented `    and: rule: TLE-XXX-NNN ...` continuation
lines. The header carries totals, an ISO-8601 timestamp, and the tool version
— pinning the sidecar's line format to a release so downstream parsers can
dispatch on `lintle 0.3.0`. See companion spec
[`2026-05-24-stable-rule-id-registry-design.md`](2026-05-24-stable-rule-id-registry-design.md)
for the full rule registry.

### 9.3 Run summary

Printed to stdout (and as JSON with `--report json`):

```
tle2022.txt   8,412,066 records   8,412,064 clean   3 quarantined   (1 orphan, 16,824,135 lines)
  fixes:   trailing-backslash 8,412,064 | reconstructed-checksum 195,293 | crlf 0 | trailing-ws 0
  rejects: TLE-CHK-001 1 | TLE-PAIR-001 1 | TLE-COL-001 1
```

Reject counts key by the stable `RuleID` registry (`TLE-CHK-001` for checksum
mismatch, `TLE-PAIR-001` for orphan lines, `TLE-COL-001` for wrong length,
etc.) so a defect surfaces under one identifier across the per-file summary,
`report.md`, and `.broken.txt`. The header counters are independent (issue
#5): `paired_records` is the count of true 2-line TLEs (here 8,412,066),
`orphan_entries` is the count of unpaired single lines surfaced as findings
(here 1, also visible under `TLE-PAIR-001`), and `input_lines_seen` is every
physical line read from the file. `clean + quarantined == paired + orphan`
(the invariant), so the percentages stay coherent.

`reconstructed-checksum` is reported as its own line item, separate from content-preserving
fixes: those records are format-conformant but their checksums are computed, not verified
(Section 6.2).

### 9.4 Run report — `report.md`

A `clean` run writes a Markdown report to the `--out-dir` root, aggregating every processed
file: corpus totals (records, percentage cleaned, percentage quarantined), the corpus-wide fix
counts, the defect-rule breakdown, a per-file table, and a per-NORAD breakdown table
listing each satellite whose records were quarantined with its per-rule counts and the
files it appeared in (sorted descending by quarantined-record count, capped at top-N rows with
a "...and N more" footer pointing at `broken-noradids.ndjson` for the long tail). It is the
human-readable companion to the per-file `.broken.txt` sidecars and the minimal
`broken-noradids.ndjson` feed — a single at-a-glance picture of what the run did.

## 10. Error handling

Built so that a 20-minute run does not die on one bad byte:

- Non-ASCII / undecodable bytes are treated as a *data defect* — that record is quarantined, never
  raised as an exception.
- Any unexpected per-record exception is caught; that record is quarantined with reason
  `internal-error: …` and processing continues.
- File-level errors (unreadable, permissions) are reported per file; other files continue.
- The `.broken.txt` reject file is written **byte-faithfully**: quarantined lines are copied as
  raw bytes, so a record quarantined for a non-ASCII/undecodable byte appears verbatim and the
  file may not be valid UTF-8. The header and per-record reason lines are ASCII; consumers of
  `.broken.txt` must treat the quarantined-line payloads as opaque bytes.
- Output is written to temp files and atomically renamed on success, so an interrupted run never
  leaves a half-written `.cleaned.txt` that looks complete.
- `clean` checks free space on `--out-dir` before starting (cleaned + broken output ≈ input size,
  plus transient headroom for the temp file), so a 20-minute run does not fail late on a full disk.
  Free below the 2× input-size floor aborts with exit `2`; free in the 2× to 2.5× borderline
  band prints a warning to stderr and proceeds; above 2.5× is silent.
- Exit codes: `0` = clean (no defects, or — for `clean` — every defect was repaired and every
  emitted record is valid); `1` = **unrepairable** records exist (`validate`: at least one record
  *would* be quarantined; `clean`: records were routed to `.broken.txt`); `2` = operational error
  (unreadable file, bad arguments). Repairable defects alone — including the near-universal
  trailing `\` — do **not** raise the exit code above 0; otherwise the code would carry no signal,
  since almost every raw file contains them.

## 11. Testing

Test-driven, dev dependencies `pytest` and `sgp4` (optionally `tletools`).

- **`tle.py` unit tests:** checksum computation; every validation rule; Alpha-5 acceptance; edge
  cases (blank international designator, negative derivatives, exponential fields).
- **Oracle cross-check:** a set of known-good real TLEs validated by our code *and* by
  `sgp4`/`tletools`. The check is *asymmetric* — it confirms that genuinely valid TLEs are
  accepted by both. It is **not** used to confirm rejections: `sgp4` is permissive and parses
  many malformed TLEs, so a disagreement on a bad input is expected, not a bug. The oracle is a
  dev-time test fixture only; it is never part of the runtime validator.
- **`repair.py` unit tests:** each fix class (6.1–6.3); the reconstructed-checksum repair — a
  69-byte `\`-terminated line and a plain 68-byte line are repaired only when columns 1–68 pass
  the layout and semantic checks, and quarantined otherwise (interior character missing); and the
  speculative-reject path — a `\`-terminated line whose columns 1–69 still fail the checksum must
  be quarantined, not "fixed."
- **`report.py` structural invariants:** `RejectSink.add` honours the per-rule cap by
  construction — verified under adversarial input order, a deterministic randomized sequence,
  and a finalize-then-add `RuntimeError` lock; `FileSample.from_bounded` raises on over-cap
  input; context-manager exit without `finalize` discards `.broken.txt` partials. These tests
  lock the cap as a structural property of the sink rather than a convention enforced by a
  single caller (issue #19).
- **Golden / integration tests:** a fixture file in `tests/fixtures/` (one per defect class) fed
  through `pipeline`; assert the exact bytes of `.cleaned.txt` and `.broken.txt`.
- **Idempotence test:** `clean(clean(x)) == clean(x)`; `validate` of a cleaned file reports perfect.

## 12. Build sequence

1. `pyproject.toml` (uv project, console script, dev deps) + package skeleton.
2. `tle.py` — the correctness oracle — built first, test-driven, including the `sgp4` cross-check.
3. `repair.py` — conservative fixes (Sections 6.1–6.3, including the reconstructed-checksum
   repair), test-driven against `tle.py`.
4. `pipeline.py` — streaming reader, pairing state machine, routing.
5. `report.py` and `cli.py` — sidecar/summary rendering, argument parsing, parallelism.
6. **First real milestone:** run `validate` across all 29 files to surface the *actual* full
   defect catalog (the trailing `\` and the missing-checksum defects are known; the validator is
   the discovery tool for the rest). Confirm the repair rules cover every safe-to-fix defect found.
7. Run `clean` across the corpus; review `.broken.txt` sidecars; report findings to space-track.

## 13. Resumable runs

**Status:** ❌ Considered and rejected — issue #12 closed wontfix (2026-05-27).
This `manifest.json` cross-run skip cache is **not** being built; the section is
retained for its reasoning and as the contrast case for its replacement. A skip is an
un-validated trust that nothing changed, against the correctness-over-recovery
principle (§4.1); the `lintle_version` guard (§13.2) is defeated by the project's "no
per-merge version bumps" workflow; and a skip-run would silently under-fill
`report.jsonl`, breaking `diff` (#10). **Superseded by single-run resume**
(`--resume`) — a durable checkpoint scoped to *finishing one interrupted run*, deleted
on success, validated refuse-on-change — see issue #56. The original design follows
unchanged.

A `clean` run reprocesses every input file from scratch. For the 30 GB corpus, the dominant
multiplier on iteration time is "redo work already done." A `manifest.json` written to
`--out-dir` captures enough per-file state to **skip** a file whose inputs and current code
are unchanged from the previous run, reusing that file's cached `cleaned/` and `broken/`
outputs along with its `FileStats` summary. Typical case (one file changed in a 29-file
corpus): the next run touches ~3.5% of the I/O, not 100%.

The mechanism is opt-in by *presence*. A fresh `--out-dir` has no manifest, so nothing is
skipped; once a run completes, subsequent runs against that same `--out-dir` consult the
manifest it wrote.

### 13.1 Manifest format — `manifest.json`

Written to `<out-dir>/manifest.json`, alongside `report.md` and `broken-noradids.ndjson`:

```json
{
  "lintle_version": "0.3.0",
  "schema_version": 1,
  "generated": "2026-05-24T14:03:00Z",
  "entries": {
    "data/source/tle2022.txt": {
      "size": 3221225472,
      "mtime": 1700000000.0,
      "head_sha256": "<sha256 of first 65536 bytes>",
      "tail_sha256": "<sha256 of last 65536 bytes>",
      "stats": { "...": "JSON-serialised FileStats snapshot for this file" }
    }
  }
}
```

Entry keys are the input paths as passed to `discover_paths()` — not their realpaths — so a
manifest generated against a symlinked source tree (as used in the worktree workflow) keeps
matching while the symlink stays valid. Each entry's `stats` is the JSON-serialisable subset
of the `FileStats` (§9) for that file — every counter, the fix and reject tallies, and the
per-NORAD breakdown — sufficient for the run report (§9.4) to include reused files in corpus
totals without re-reading them. The `reject_sample` field (a `FileSample` of `RejectEntry`
objects carrying raw bytes, §11) is **not** serialised: it feeds only `validate`'s grouped
exemplar output, and `validate` does not consult the manifest. On reuse, the file's
`reject_sample` reconstructs as `FileSample.empty(...)`.

### 13.2 Skip predicate

A file is skipped **iff every one** of the following holds:

1. The manifest's `lintle_version` equals the current `__version__`.
2. The manifest's `schema_version` equals the current code's schema version.
3. The file's current `os.stat().st_size` equals the recorded `size`.
4. The file's current `os.stat().st_mtime` equals the recorded `mtime`.
5. SHA-256 of the file's first 65,536 bytes equals `head_sha256`.
6. SHA-256 of the file's last 65,536 bytes equals `tail_sha256`.

Failure on *any* check reprocesses the whole file; there are no partial reuses.

The head+tail hash is **probabilistic identity, not a content hash.** A modification that
preserves size, mtime, and both 64 KB windows but changes the interior would be silently
skipped. This is acceptable for the corpus's actual usage — TLE files from space-track are
replaced wholesale or appended to, not edited in place — and avoids the I/O of hashing a 3 GB
file, which would defeat the purpose of skipping. The 65,536-byte window is small enough to
read in one disk seek and large enough that any append (the tail changes) or any truncation
(size changes) is caught.

The `lintle_version` check is **load-bearing.** The dominant `clean` use case for this
project is iterating on the cleaner itself — every release of `repair.py` or `tle.py` can
change a file's `cleaned/` or `broken/` outputs, and reusing the previous version's outputs
would silently ship stale data downstream. Any version mismatch therefore invalidates the
*entire* manifest, not just per-file: proving that a given patch release "could not have
changed output" is harder than redoing the work, so the invariant is pessimistic by design.
This is the same principle as §4.1 (validated transformation), turned inward: a skip is
provisionally valid *only against the exact code that produced it*.

`schema_version` is a separate integer that increments when `manifest.json` itself changes
shape (added fields, removed fields, renamed keys), independent of `lintle_version`. A
consumer reading a manifest with an unknown `schema_version` treats it as a full invalidation.

### 13.3 Atomicity

A manifest entry MUST only exist for a file whose `cleaned/` and `broken/` outputs are fully
committed to disk. The relevant hooks in the current pipeline (post issue #19 refactor):

- **Per-file commit window** — `pipeline._run` lines 263-266: `os.replace(cleaned_tmp,
  cleaned_path)` publishes the cleaned file, then `sink.finalize(...)` stitches the
  `.broken.txt` sidecar via its owned `BrokenFileWriter`. Both calls run inside the active
  `with sink:` block; both must succeed before the file's outputs are durably published. The
  manifest entry must be assembled only after this window completes, not within it.
- **Per-file aggregation** — `cli.py:489`: the parent receives the worker's `FileStats` from
  `future.result()`. This is where the manifest entry is *assembled* by the parent process,
  not where the manifest is *written*.
- **End-of-run manifest write** — the parent writes the full `manifest.json` once, after
  every worker has reported back, by writing `manifest.json.partial` and atomically renaming
  it. Workers never touch the manifest, side-stepping cross-process locking on a shared JSON
  file.

Interruption semantics fall out cleanly:

- A worker crashes mid-file (before `os.replace`) → the `finally` block at
  `pipeline._run:247-256` discards the cleaned-file partial; the sink's `__exit__` (fired
  when the `with sink:` block unwinds) discards the `.broken.txt` partials. No `FileStats`
  returns, no manifest entry is assembled. Next run reprocesses naturally.
- A worker crashes between `os.replace` and `sink.finalize` → the cleaned file is committed
  but the `.broken.txt` is not. The sink's `__exit__` discards the broken-file partials. The
  worker's exception propagates, no `FileStats` returns, no manifest entry is assembled.
  Next run reprocesses and harmlessly overwrites the orphaned cleaned file. This window is
  narrow (one syscall plus a stitched-rename) but a new partial-outcome surface introduced
  when the refactor moved `finalize` inside `with sink:` — the manifest's all-or-nothing
  worker-return discipline papers over it cleanly.
- `Ctrl-C` mid-run → workers that finished contributed `FileStats`, but the parent's
  manifest write happens only at end-of-run. An interrupted run writes *no* manifest. Next
  run reprocesses everything. This is the safe default: redoing work is cheaper than
  committing partial state.

The non-incremental write means a 29-file run that completes writes one manifest at the end;
a run that completes 28 files and dies on file 29 writes no manifest and the next run redoes
all 29. A checkpointed manifest written every N files completed is out of scope for v1 but
compatible with this design.

### 13.4 CLI

```
lintle clean [paths...] --no-skip      # ignore the manifest entirely; reprocess every file
lintle clean [paths...] --force        # synonym for --no-skip
```

`validate` is read-only and writes no outputs, so it has no manifest concept; a caller
wanting "what changed since last `clean`" reads the manifest directly.

The run summary distinguishes reused from processed:

```
processing 29 file(s) with 8 worker(s): 1 to process, 28 reused from manifest
...
tle2022.txt    8,412,066 records  8,412,064 clean  3 quarantined  (1 orphan, 16,824,135 lines)
(reused) tle2021.txt   8,398,712 records  8,398,710 clean  2 quarantined
```

`report.md` (§9.4) includes every file — reused or processed — and flags reused rows so the
operator can see at a glance what this run actually did versus what was carried over.

### 13.5 Tests

- **Round-trip skip:** a fixture run writes a manifest; a second run on the same inputs
  skips every file and the corpus totals match the first run's exactly.
- **Per-field invalidation:** between runs, one of `mtime`, `lintle_version`, or
  `schema_version` is mutated; the appropriate set of files is reprocessed (one, all, all
  respectively).
- **Crash-mid-run safety:** an injected exception between `os.replace` and the parent's
  `FileStats` collection leaves no manifest entry for that file and no partial output.
- **Documented limit:** a stealth interior modification (preserving size, mtime, head, tail)
  IS silently skipped. The test asserts the skip — it is the contract, not a bug.

### 13.6 Risks and limits

- **Stealth interior modification → silent skip** (§13.2). Acceptable for the intended
  corpus (append-only space-track exports), documented for users with different workflows.
- **Manifest write is non-incremental.** Interrupted runs write no manifest; next run redoes
  everything. The worst case is "redo work," never "skip something that should have been
  redone."
- **Per-`--out-dir` scope.** Each `--out-dir` has its own manifest; the parallel worktree
  workflow (CLAUDE.md) intentionally does not share skip state across out-dirs — different
  output trees ARE different runs.
- **Operator-edited manifest.** A user who hand-edits the manifest to skip the version check
  bypasses the version-pin invariant and bears the consequences.

## 14. Open considerations

- If the `validate` discovery pass surfaces a defect type that is genuinely safe and unambiguous
  to repair (not yet anticipated), it is added to the appropriate fix class (Sections 6.1–6.3) in
  `repair.py` — still governed by the validated-transformation principle. Anything ambiguous stays
  quarantined.
- Aggressive reconstruction of missing *data* characters remains explicitly out of scope
  (Section 2); reconstructing a missing *checksum* digit from intact columns 1–68 is in scope and
  specified in Section 6.2.

## 15. Single-run resume (`--resume`)

**Status:** implemented (issue #56). Supersedes the rejected §13 manifest.

A `clean` run over the full corpus can take hours; an interruption — Ctrl-C, a closed laptop,
a crash — should not force redoing the files already finished. `clean --resume` continues an
interrupted run in the same `--out-dir`, processing only the files not yet completed. Unlike
the rejected §13 manifest this is **not** a cross-run cache: the checkpoint exists only while a
run is incomplete and is deleted on success, so a finished run leaves no state behind and no
run ever skips re-validating a record it emits (§4.1).

### 15.1 Checkpoint — `<out-dir>/.clean-state.json`

Checkpointing is **always on**, independent of `--resume`. At run start the parent computes an
identity fingerprint for every discovered input; after each file's outputs commit (`os.replace`
of `cleaned/`, the stitched `broken/` sidecar, and the findings shard) the parent atomically
rewrites the checkpoint (`.partial` + `os.replace`). On full success it is deleted, so its
presence marks an interrupted run.

```json
{
  "schema_version": 1,
  "lintle_version": "0.2.0",
  "inputs":    { "<discover path>": {"size": 0, "mtime_ns": 0, "head_sha256": "", "tail_sha256": ""} },
  "completed": { "<discover path>": "<report.summary_dict() snapshot>" }
}
```

`inputs` carries every file in the intended set; `completed` carries the finished subset with
each file's serialised `FileStats` (§9), so the final report covers reused files without
re-reading them. The bytes-bearing `reject_sample` is not stored (as in §13.1); it reconstructs
empty via `report.stats_from_summary`. Identity uses integer `mtime_ns` (not the float
`st_mtime`) to avoid JSON precision loss and cross-filesystem granularity skew, plus the SHA-256
of the first and last 64 KB — constant-memory, never reading the interior.

### 15.2 Resume validation — refuse-on-change

`--resume` loads the checkpoint and refuses (exit 2, with a specific message) unless **all**
hold: the `schema_version` is known, `lintle_version` equals the current `__version__`, the
discovered file set equals the checkpoint's `inputs` keys, and every input's current identity
matches. Any drift means re-running a clean full pass, never silently mixing outputs produced by
two different code or input states. This is the §13.2 version-pin reasoning applied at the right
granularity — a one-time gate on resume, not a per-run skip cache. A missing or corrupt
checkpoint is treated as no checkpoint. The known limit of head+tail hashing (a stealth interior
edit preserving size, `mtime_ns`, and both windows) is inherited and accepted, the same
trade-off §13.2 documented.

### 15.3 Report assembly and lifecycle

Reused files contribute their reconstructed `FileStats` to the run, so `report.md`,
`--report json`, and `broken-noradids.ndjson` cover the whole corpus. `report.jsonl` stays
complete because completed files' findings shards survive the interruption on disk and the
end-of-run concat reads them. `.shards/` and `.clean-state.json` are both in-progress run
state and are torn down **together, only on a fully successful run**: `concat_findings_shards`
only *reads* the shards, and the success-only cleanup removes both. An interrupted run (exit
130, returns before the cleanup) or a failed-file run (exit 2, keeps the checkpoint) therefore
keeps *both*, so a later `--resume` re-reads the surviving shards and rebuilds a complete
`report.jsonl`. A fresh run (no `--resume`) clears any stale checkpoint and scrubs `.shards/`
at start as before. The Ctrl-C handler is otherwise unchanged; the on-disk checkpoint already
reflects the files completed before the interruption.

### 15.4 Durability

Every committed file — the checkpoint, the per-file `cleaned/` output, the `.broken.txt`
sidecar, each findings shard, and the end-of-run `report.jsonl` / `report.md` /
`broken-noradids.ndjson` — is committed through one helper, `fsutil.durable_replace`
(issue #58). It `fsync`s the temp file's data, `os.replace`s it onto the destination, then
`fsync`s the containing directory so the rename itself is durable. `os.replace` alone gives
*atomicity* (a reader sees the old name or the new one, never a half-written file); the added
`fsync`s give *durability* — the committed bytes survive a hard power loss or kernel panic,
not just the ordinary Ctrl-C / sleep / crash that the page cache already covers.

**Platform barrier.** On macOS `os.fsync` flushes to the drive but not the drive's own write
cache, so it is *not* a true power-loss barrier; `fcntl(fd, F_FULLFSYNC)` is. On Linux and
other platforms `os.fsync` is the real barrier. `fsutil` selects the correct one per platform.

**`--resume` ordering invariant.** Because a worker routes all of its outputs through
`durable_replace`, those bytes are durable before the worker returns its stats — and only
*then* does the parent record the file `completed` in the checkpoint (also via
`durable_replace`). So the checkpoint can never name a file whose data is not yet on disk,
which is what `--resume`'s "trust a committed output without reprocessing" guarantee requires
(Critical Rule #2).

**Always-on, no flag.** Measured on the 30 GB corpus's APFS/SSD: a full run commits ~120
files (≈4 per input + 3 final reports — bounded by *file* count, not record count), and the
`F_FULLFSYNC` barrier costs ~9 ms/call (~1 s/run total); the data-flush portion is dominated
by writes that must reach disk anyway and overlaps the CPU-bound parsing. The overhead is far
under 1 % of a multi-minute run, so durability is unconditional — a flag would only add a
foot-gun (disable it and `--resume` silently loses its guarantee) for no measurable benefit.

**Out of scope:** cross-filesystem / network-FS durability beyond what `fsync` + directory
`fsync` provide on local disks.
