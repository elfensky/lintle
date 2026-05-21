# TLE Corpus Validator & Cleaner — Design

- **Date:** 2026-05-21
- **Status:** Approved (design); pending implementation plan
- **Topic:** A tool to validate and clean a multi-gigabyte corpus of Two-Line Element (TLE) files exported from space-track.org

## 1. Problem statement

The working directory holds 22 `.txt` files of TLE data (years 2004–2025), totalling roughly 40 GB,
plus a 12 GB `TLEs.zip`. The files contain systematic, era-specific defects:

- **2004-era files** — *every* Line 1 carries an extra trailing `\` byte (70 bytes before the
  newline instead of 69). A systematic export artifact, not sporadic corruption.
- **2025 file** — mostly clean 69-character lines, but a small fraction (~0.1% in the sampled
  region) are 68 characters: a character is *missing*.

These are only the directly observed defects. The full defect catalog is unknown until the corpus
is scanned.

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
- **No aggressive reconstruction** of corrupt records (e.g. brute-forcing a missing character).
  A mod-10 checksum has a 1-in-10 false-accept rate; reconstruction risks silently producing
  plausible-but-wrong data. Corrupt records are quarantined for space-track to fix.
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
  trusted implementation. The runtime stays pure-stdlib.

## 4. Architecture (Approach B)

A single `uv`-managed Python project, **pure standard library at runtime**. The two user-facing
asks become **one validator** used in two modes: the validator defines "perfect"; the cleaner
reuses it and emits only records that pass it.

### 4.1 The validated-transformation principle

The cleaner never applies a fix and hopes. It treats the validator as an oracle:

1. Apply a candidate transformation (strip the trailing `\`, normalize line endings, …).
2. Re-run *full* validation on the result — column layout **and** the mod-10 checksum.
3. Commit the fix **only if the result now passes.** Otherwise the record is quarantined.

Consequences: the cleaner provably cannot turn a bad record into a wrong-but-valid-looking one,
and every line in the cleaned file is valid *by construction* because it was validated on the way
out.

### 4.2 Project layout

```
TLEs/
├── pyproject.toml          # uv project; console script "tle-clean"; dev deps: pytest, sgp4
├── src/tlekit/
│   ├── __init__.py
│   ├── tle.py              # CORE: defines a "perfect" TLE record (pure, no I/O)
│   ├── repair.py           # candidate fixes — speculative, validated by tle.py (pure, no I/O)
│   ├── pipeline.py         # streaming I/O: read → pair into records → route
│   ├── report.py           # writes .broken.txt sidecar + per-file summary
│   ├── cli.py              # argparse: `validate` and `clean` subcommands
│   └── __main__.py         # `python -m tlekit`
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
| `report.py` | Renders the `.broken.txt` reject file and the run summary. | nothing |
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

- **Line level:** length is exactly 69; correct line-number prefix; required separator columns
  contain spaces; literal decimal points are in position (epoch fraction, first derivative, mean
  motion); variable fields contain only their permitted character set; checksum matches.
  The checksum is the primary integrity check; column-layout checks are the backstop against a
  checksum collision.
- **Record level:** a Line 1 immediately followed by a Line 2, with **matching satellite catalog
  numbers** (columns 3–7).

Edge cases the validator must tolerate as valid: a blank international designator on older objects;
optional leading sign or space in numeric fields; Alpha-5 alphanumeric catalog numbers.

## 6. Defect catalog and fix policy

Three fix classes:

| Class | Defect | Action |
|-------|--------|--------|
| **Cosmetic — auto-fix** (strip, re-validate, commit only if the result passes) | Trailing `\` on Line 1 (the 2004 artifact) | strip the byte |
| | `\r\n` or lone `\r` line endings | normalize to `\n` |
| | Trailing spaces / tabs after column 69 | trim |
| | Leading whitespace / UTF-8 BOM | trim |
| **Structural — safe drop** | Blank / empty line between records | drop |
| **Corrupt — quarantine** (→ `.broken.txt`) | 68-char or other wrong-length line (missing/extra char, not a known artifact) | quarantine record |
| | Checksum mismatch on a full 69-char line | quarantine record |
| | Line does not start with `1 ` / `2 ` | quarantine |
| | Orphan Line 1 or Line 2 (no valid pair) | quarantine |
| | Catalog-number mismatch between paired lines | quarantine both |
| | Non-ASCII / control character inside a record body | quarantine |
| | Column-layout violation (e.g. non-digit in a digit-only column) | quarantine |

Every cosmetic fix is **speculative**: applied, then re-validated. A 70-byte line ending in `\`
whose bytes 1–69 still fail the checksum (meaning the `\` was not the only problem) is **not**
committed — that record is quarantined. The fix counts are reported even for cosmetic fixes, so a
run reports e.g. "stripped 8,412,064 trailing backslashes."

### Definition of a "perfect" cleaned file

Pairs of records — Line 1 then Line 2 — each exactly 69 ASCII characters, `\n`-terminated, no
blank lines, matching catalog numbers, both checksums valid. This uniform shape *is* the
easily-parsable architecture downstream applications ingest.

## 7. CLI

A console script `tle-clean` with two subcommands matching the two asks:

```
tle-clean validate [paths…]    # ask #1 — read-only audit, mutates nothing
tle-clean clean    [paths…]    # ask #2 — produces cleaned + broken files
```

Options:

- `paths` — files or a directory. A directory is globbed for `tle*.txt`. Defaults to `.`.
- `--out-dir DIR` — destination for cleaned/broken files. Default `./cleaned/`, keeping the
  original inputs pristine.
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

1. `pipeline.read_records(path)` opens the file in **binary** mode (to observe `\r`, `\`, and
   encoding precisely) and iterates lines. A small state machine consumes raw lines, drops blank
   lines, and yields record candidates: `(raw_line1, raw_line2, source_line_numbers)`. Lines that
   cannot be paired (orphans) are yielded as orphan candidates.
2. `repair.process(candidate)` decodes, applies cosmetic strips speculatively, runs
   `tle.validate_record`, and returns either `Accepted(line1, line2, fixes_applied)` or
   `Rejected(raw, reason, source_lines)`.
3. `pipeline` routes Accepted records to `<name>.cleaned.txt` and Rejected records to
   `<name>.broken.txt` (via `report`), accumulating statistics.
4. After each file, a summary is emitted.

The pairing state machine holds at most two lines, so a 40 GB file streams in constant memory.
Each of the 22 files is independent, so `cli.py` runs them through a
`concurrent.futures.ProcessPoolExecutor`.

**Idempotence:** running `clean` on an already-clean file yields byte-identical output with zero
fixes and zero rejects; running `validate` on a `.cleaned.txt` reports it perfect.

## 9. Output formats

### 9.1 Cleaned file — `<name>.cleaned.txt`

Standard 2-line TLE text, every record guaranteed valid (Section 6).

### 9.2 Reject file — `<name>.broken.txt`

Per source file, formatted to be detailed enough to file a report with space-track.org:

```
# tle2022.broken.txt — quarantined records
# source: tle2022.txt | generated: 2026-05-21T14:03:00Z | tlekit 0.1.0
# 3 records quarantined of 8,412,067 total

[1] source lines 14820-14821 — reason: Line 2 checksum mismatch (col 69 is '3', computed '7')
1 43210U 18014A   22045.12345678  .00001234  00000-0  12345-4 0  9991
2 43210  53.0123 211.4567 0001234  90.1234 270.9876 15.12345678123453

[2] source line 99102 — reason: orphan Line 1 (no following Line 2)
1 51234U 21001A   22045.12345678  .00001234  00000-0  12345-4 0  9991

[3] source line 250011 — reason: line length 68, expected 69 (missing character)
1 27497U 01055E   0415 .01279831  .00005767  00000-0  41216-3 0 7230
```

Each entry: index, source filename + line number(s), human-readable reason, then the raw line(s)
verbatim. The header carries totals, an ISO-8601 timestamp, and the tool version.

### 9.3 Run summary

Printed to stdout (and as JSON with `--report json`):

```
tle2022.txt   8,412,067 records   8,412,064 clean   3 quarantined
  fixes:   trailing-backslash 8,412,064 | crlf 0 | trailing-ws 0
  rejects: checksum-mismatch 1 | orphan-line 1 | wrong-length 1
```

## 10. Error handling

Built so that a 20-minute run does not die on one bad byte:

- Non-ASCII / undecodable bytes are treated as a *data defect* — that record is quarantined, never
  raised as an exception.
- Any unexpected per-record exception is caught; that record is quarantined with reason
  `internal-error: …` and processing continues.
- File-level errors (unreadable, permissions) are reported per file; other files continue.
- Output is written to temp files and atomically renamed on success, so an interrupted run never
  leaves a half-written `.cleaned.txt` that looks complete.
- Exit codes: `0` = clean / all-perfect; `1` = defects found (`validate`) or records quarantined
  (`clean`); `2` = operational error (unreadable file, bad arguments).

## 11. Testing

Test-driven, dev dependencies `pytest` and `sgp4` (optionally `tletools`).

- **`tle.py` unit tests:** checksum computation; every validation rule; Alpha-5 acceptance; edge
  cases (blank international designator, negative derivatives, exponential fields).
- **Oracle cross-check:** a set of known-good real TLEs validated by our code *and* by
  `sgp4`/`tletools`; the results must agree.
- **`repair.py` unit tests:** each cosmetic fix; and the speculative-reject path — a `\`-terminated
  line whose bytes 1–69 still fail the checksum must be quarantined, not "fixed."
- **Golden / integration tests:** a fixture file in `tests/fixtures/` (one per defect class) fed
  through `pipeline`; assert the exact bytes of `.cleaned.txt` and `.broken.txt`.
- **Idempotence test:** `clean(clean(x)) == clean(x)`; `validate` of a cleaned file reports perfect.

## 12. Build sequence

1. `pyproject.toml` (uv project, console script, dev deps) + package skeleton.
2. `tle.py` — the correctness oracle — built first, test-driven, including the `sgp4` cross-check.
3. `repair.py` — conservative fixes, test-driven against `tle.py`.
4. `pipeline.py` — streaming reader, pairing state machine, routing.
5. `report.py` and `cli.py` — sidecar/summary rendering, argument parsing, parallelism.
6. **First real milestone:** run `validate` across all 22 files to surface the *actual* full
   defect catalog (only 2 defect types have been directly observed; the validator is the
   discovery tool). Confirm the repair rules cover every safe-to-fix defect found.
7. Run `clean` across the corpus; review `.broken.txt` sidecars; report findings to space-track.

## 13. Open considerations

- If the `validate` discovery pass surfaces a defect type that is genuinely safe and unambiguous
  to repair (not yet anticipated), it is added to the cosmetic-fix class in `repair.py` — still
  governed by the validated-transformation principle. Anything ambiguous stays quarantined.
- Aggressive reconstruction of missing characters remains explicitly out of scope (Section 2).
