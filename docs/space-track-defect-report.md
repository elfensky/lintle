# Space-Track Historical TLE Export — Systematic Defect Report

| | |
|-----------------|--------------------------------------------------------------|
| **Prepared**    | 2026-07-06 |
| **Corpus**      | space-track.org bulk historical TLE export, 29 files `tle2004`–`tle2025` (~30 GB) |
| **Records**     | 232,378,271 two-line element sets (464.7 M lines) |
| **Analysed by** | [`lintle`](../README.md) 0.6.0 — a correctness-first TLE validator/cleaner (full-corpus run 2026-07-05) |
| **Purpose**     | Document the systematic defects in the historical export and request a corrected re-export |

---

## Executive summary

A full validation pass over the 232.4 M-record historical export found **six distinct,
systematic defect classes**. They are not random corruption — each is concentrated in
specific export years or specific catalog objects, which points at generation/export
tooling rather than transmission damage.

Four of the six are **mechanical formatting defects** that a re-export can eliminate
outright. After applying the deterministic, independently-verified repairs for these,
**99.959 % of records (232,281,894) are recovered as spec-valid TLEs**.

The remaining **103,228 records (0.044 %) cannot be safely repaired** because doing so
would require inventing or guessing data. These fall into two data-integrity defects plus
a small structural tail, and are the ones that genuinely need action on space-track's side.

| # | Defect | Records affected | Class | Re-export can fix? |
|---|--------|-----------------:|-------|:------------------:|
| 1 | Spurious trailing `\` on line 1 | 187,881,771 (80.9 %) | formatting | ✅ Yes |
| 2 | Missing column-69 checksum digit | 71,252,858 lines | formatting | ✅ Yes |
| 3 | CRLF (Windows) line endings | 1,805 | formatting | ✅ Yes |
| 4 | **Per-object checksum-generator error** | 47,465 (67 objects) | data integrity | ⚠️ Needs investigation |
| 5 | **Malformed B\* drag term** | 48,483 (~1,892 objects) | data integrity | ⚠️ Needs investigation |
| 6 | Structural (orphans, mislabels, length, NORAD mismatch) | 7,280 | structural | ⚠️ Partial |

**The ask (details in [§6](#6-what-we-are-requesting)):** a re-export that (a) uses Unix `LF`
endings with no trailing `\`, (b) includes a correct column-69 checksum on every line, and
(c) is accompanied by an investigation of the 67 catalog objects in
[Appendix B](#appendix-b--the-67-catalog-objects-with-a-checksum-generator-error) and the
malformed-B\* objects, whose *data* appears intact but whose exported bytes are wrong.

---

## 1. Scope and method

The corpus is the complete set of annual bulk TLE files exported from space-track.org,
`tle2004.txt` through `tle2025.txt` (2004 is split into eight parts), totalling
232,378,271 paired records.

Every record was checked against the standardized TLE specification: fixed 69-column
layout, per-field column typing, semantic field ranges, the two-line pairing rules, and
the modulo-10 column-69 checksum. Candidate repairs were applied only under a
**validated-transformation** rule — a fix is committed only if the fully re-validated
result passes; otherwise the record is quarantined rather than guessed. No orbital-data
character is ever reconstructed. This report's figures are byte-reproducible; see
[§7](#7-reproducing-this-report).

Counts of *fixes applied* count individual lines (either line of a pair can carry a
defect); counts of *records* count two-line sets.

---

## 2. Formatting defects (a re-export removes these at source)

These three are purely lexical. `lintle` repairs all of them deterministically and
re-validates the result, but every one is an artefact of the export process and should not
appear in a clean export at all.

### 2.1 Spurious trailing backslash on line 1 — 187,881,771 records (80.9 %)

The single most prevalent defect. Line 1 of the overwhelming majority of records carries an
extra `\` (0x5C) byte appended before the line terminator, making the line 70 bytes instead
of 69:

```
1 11015U 78083A   04151.70524109  .00568677 -21183-6  17439-2 0  8494\
                                                                     └─ spurious 0x5C (byte 70)
```

Removing the trailing `\` yields a byte-perfect, checksum-valid 69-column line. **This
defect is era-specific** — it is absent from the 2019, 2023, and 2025 exports (0
occurrences) but present in ~99.9 % of line-1s everywhere else (see
[Appendix A](#appendix-a--defect-prevalence-by-export-year)), which strongly implies a
specific export path or wrapper appends it.

### 2.2 Missing column-69 checksum digit — 71,252,858 lines

Many lines were exported as 68 columns — the column-69 checksum digit is simply absent:

```
1   553U 63004A   77069.11186343 -.00000050 +00000-0 +00000-0 0 0009
                                                                    └─ column 69 (checksum) absent
```

The checksum is a deterministic modulo-10 function of columns 1–68, so a *missing* one is
recomputable without ambiguity (unlike a missing data character, which is not). `lintle`
recomputes it only under an explicit opt-in and re-validates. **This defect is bimodal**:
heavy in 2004 (16.5 M lines), absent 2005–2013, then reappearing from 2014 onward — again
suggesting two distinct export pipelines over the corpus's history. In the 2004 files it
frequently co-occurs with the trailing backslash (§2.1) on the same line.

### 2.3 CRLF (Windows) line endings — 1,805 lines

1,805 lines in the 2019 export terminate with `\r\n` instead of `\n`. Trivial, but it
confirms at least one export batch passed through a Windows toolchain.

---

## 3. Data-integrity defects (these need space-track's attention)

Unlike §2, these cannot be repaired downstream without risking a *wrong-but-valid-looking*
record, so they are quarantined. They are, however, highly systematic — which is what makes
them worth reporting rather than writing off as noise.

### 3.1 Per-object checksum-generator error — 47,465 records across 67 objects ⭐

This is the most striking finding. 47,465 records have a column-69 checksum that does **not**
match their own columns 1–68 — yet **every one of these lines is otherwise fully valid**
(correct layout, correct field types, in-range semantics). Only the checksum byte is wrong.

Crucially, these 47,465 records come from just **67 distinct catalog objects**, and for
**65 of them (99.9 % of the records) the checksum is wrong by a fixed per-object constant**.
Example — NORAD 01528 (1963-038F), whose checksum is consistently the correct value **+1**:

```
As exported:  1 01528U 63038F   65242.19247746  .00000012 +14982-3 +00000-0 0 00010
Correct:      1 01528U 63038F   65242.19247746  .00000012 +14982-3 +00000-0 0 00019
                                                                                    └ col 69: exported 0, should be 9
```

Every record of NORAD 5432 (1968-097CY) is off by +4; every record of 20307 by +1; of
13914 by +3; and so on — each object has its own constant offset, stable across records
spanning decades of epochs. A per-object constant offset is the signature of a **checksum
computation bug in the generator** that produced these specific objects' TLEs: the orbital
data is intact, but the stored checksum was mis-derived.

We deliberately do **not** auto-correct these, because from the bytes alone a "wrong
checksum over right data" is indistinguishable from a "right checksum over one consistently
wrong identity-field character." Confirming that the data (not the checksum) is the intact
part requires space-track's authoritative catalog. The full list of 67 objects, their
offsets, record counts, and epoch spans is in
[Appendix B](#appendix-b--the-67-catalog-objects-with-a-checksum-generator-error).

### 3.2 Malformed B\* drag term — 48,483 records across ~1,892 objects

The B\* drag term occupies columns 54–61 in the format `SNNNNNSN` (sign, 5-digit mantissa,
exponent sign, exponent). In 48,483 records this field contains a **digit where a sign
column belongs** — 97 % of the time at column 60 (the exponent sign):

```
1   898U 64049E   04127.46445435  .99999999  00000-0 973196+1 0  1716
                                                     └──────┘ B* (cols 54-61): "973196+1"
                                                     col 54 should be a sign (' ','+','-'); got '9'
```

Failing-column distribution: col 60 (47,108), col 51 (725), col 24 (356), col 54 (154). The
field is packed in a non-standard notation that is not parseable under the TLE spec. Unlike
the checksum digit, the B\* value is *not* recomputable — it is a fitted physical parameter —
so a downstream tool cannot recover the intended value without guessing. These are
concentrated in the 2004–2005 exports.

---

## 4. Structural defects — 7,280 records/lines

The small remaining tail, genuinely broken at the record level:

| Defect | Count | Description |
|--------|------:|-------------|
| Orphaned line | 4,568 + 6,851 orphan entries | A line 1 or line 2 with no valid partner line |
| Non-TLE / mislabeled line | 2,283 | A line not beginning with `1 ` or `2 ` where a TLE line was expected |
| Wrong line length | 428 | Length ≠ 69 after normalization, not attributable to a missing checksum |
| Intra-pair NORAD mismatch | 1 | Paired line 1 and line 2 carry different catalog IDs |

---

## 5. Where the defects concentrate

The per-year breakdown ([Appendix A](#appendix-a--defect-prevalence-by-export-year)) shows
the defects are **not uniform** — they cluster by export batch:

- **Trailing `\`**: ~99.9 % of line-1s in every year *except* 2019, 2023, 2025 (zero).
- **Missing checksums**: heavy in 2004, none 2005–2013, then returning 2014→present.
- **Quarantined records**: 91 % of all 103,228 fall in the 2004 + 2005 files.
- **CRLF**: exclusively 2019.

The strong batch-correlation is the central evidence that these are **export-tooling
artefacts**, not data corruption — and therefore that a corrected re-export is feasible.

---

## 6. What we are requesting

A re-export of the historical corpus that resolves the defects at source:

1. **Line format** — emit each line as exactly 69 columns terminated by a single `\n`, with
   **no trailing `\`** (§2.1) and **no CRLF** (§2.3).
2. **Checksums present** — include the column-69 checksum digit on **every** line; no
   68-column lines (§2.2).
3. **Checksums correct** — investigate and re-derive the column-69 checksum for the **67
   catalog objects in [Appendix B](#appendix-b--the-67-catalog-objects-with-a-checksum-generator-error)**.
   Their orbital data validates; only the stored checksum is wrong, by a fixed per-object
   offset — consistent with a generator bug scoped to those objects.
4. **B\* formatting** — correct the B\* drag-term encoding for the ~1,892 objects whose
   field carries a digit in a sign column (§3.2), so column 54–61 conforms to the spec.
5. **Structural** — resolve the orphaned, mislabeled, wrong-length, and NORAD-mismatched
   records (§4) where the source records can be located.

Items 1–2 are the highest-impact and simplest: together they account for **259.1 M defect
occurrences** and would move the corpus from "requires a cleaning pass" to "spec-clean as
delivered." Items 3–4 are smaller in volume but are genuine data-quality issues in the
archive that only space-track can authoritatively correct.

---

## 7. Reproducing this report

Every figure here is reproducible from the public export with the open-source `lintle` tool:

```bash
# produces cleaned/, broken/ sidecars, and report.{md,json,jsonl}
lintle clean <corpus-dir> --reconstruct-checksum --out-dir <out>
```

- Per-file and per-rule counts: `report.json` (`--report json` envelope, schema 3).
- Every quarantined record, byte-faithful with its failure reason and source line numbers:
  the per-file `broken/<file>.broken.txt` sidecars.
- The checksum-offset and B\*-field analyses in §3 were derived directly from those sidecars;
  see [`ARCHITECTURE.md`](../ARCHITECTURE.md) for the validator and rule definitions.

---

## Appendix A — Defect prevalence by export year

| Year | Records | Trailing `\` | Missing checksum | CRLF | Quarantined |
|------|--------:|-------------:|-----------------:|-----:|------------:|
| 2004 | 40,275,817 | 40,219,583 | 16,486,074 | 0 | 56,234 |
| 2005 | 15,627,022 | 15,589,046 | 0 | 0 | 37,976 |
| 2006 | 4,011,286 | 4,011,282 | 0 | 0 | 4 |
| 2007 | 4,529,497 | 4,529,487 | 0 | 0 | 10 |
| 2008 | 4,526,795 | 4,526,790 | 0 | 0 | 5 |
| 2009 | 5,598,810 | 5,598,793 | 0 | 0 | 17 |
| 2010 | 5,271,610 | 5,271,601 | 0 | 0 | 9 |
| 2011 | 5,517,712 | 5,517,698 | 0 | 0 | 14 |
| 2012 | 5,193,623 | 5,193,604 | 0 | 0 | 19 |
| 2013 | 2,962,309 | 2,962,279 | 0 | 0 | 30 |
| 2014 | 3,180,242 | 3,180,234 | 62 | 0 | 8 |
| 2015 | 4,521,946 | 4,521,933 | 2,594,738 | 0 | 13 |
| 2016 | 6,607,130 | 6,607,112 | 6,543,432 | 0 | 18 |
| 2017 | 7,672,880 | 7,672,879 | 11,445,916 | 0 | 3 |
| 2018 | 8,821,289 | 8,821,286 | 11,306,068 | 0 | 9 |
| 2019 | 7,885,065 | 0 | 8,378,342 | 1,805 | 1,416 |
| 2020 | 11,637,437 | 11,637,423 | 13,862,942 | 0 | 6,857 |
| 2021 | 15,170,011 | 15,170,009 | 200,070 | 0 | 2 |
| 2022 | 17,173,210 | 17,172,917 | 158,042 | 0 | 293 |
| 2023 | 15,737,159 | 0 | 118,092 | 0 | 260 |
| 2024 | 19,677,820 | 19,677,815 | 87,528 | 0 | 5 |
| 2025 | 20,779,601 | 0 | 71,552 | 0 | 26 |
| **Total** | **232,378,271** | **187,881,771** | **71,252,858** | **1,805** | **103,228** |

## Appendix B — The 67 catalog objects with a checksum-generator error

Column-69 checksum is wrong by a fixed per-object offset; the orbital data validates.
"Offset" is `(exported − correct) mod 10`. 65 of the 67 have a perfectly constant offset;
two (marked *varies*) show a dominant offset with a few exceptions.

| NORAD | Int'l designator | Records | Checksum offset | Epoch span |
|------:|------------------|--------:|:---------------:|:----------:|
| 5432 | 68097CY | 7,733 | +4 | 1969–1999 |
| 17652 | 87022E | 5,303 | +4 | 1971–2000 |
| 20307 | 89080F | 5,129 | +1 | 1989–2002 |
| 13914 | 79015F | 4,681 | +3 | 1980–1991 |
| 17651 | 87022D | 4,214 | +4 | 1969–2000 |
| 18971 | 79087E | 2,407 | +3 | 1988–2000 |
| 13913 | 79015E | 2,270 | +3 | 1976–1988 |
| 13939 | 73023C | 1,902 | +9 | 1976–1990 |
| 5279 | 68097CQ | 1,644 | +4 | 1969–1981 |
| 13915 | 78116C | 1,569 | +2 | 1980–1987 |
| 4587 | 63038J | 1,328 | +7 | 1970–1981 |
| 7259 | 63038K | 1,231 | +9 | 1974–1982 |
| 1528 | 63038F | 1,144 | +1 | 1965–1979 |
| 11687 | 80010A | 903 | +1 | 1980–1982 |
| 11262 | 79010A | 836 | +1 | 1979–1981 |
| 19062 | 63038M | 806 | +3 | 1985–1991 |
| 11077 | 78073E | 702 | +2 | 1977–1983 |
| 11263 | 79010B | 467 | +1 | 1979–1980 |
| 46141 | 20057AA | 395 | +6 | 2022–2023 |
| 18434 | 86067U | 378 | +1 | 1987–1988 |
| 25423 | 96051L | 303 | +6 | 1998–1999 |
| 39768 | 14029C | 244 | +4 | 2019 |
| 43682 | 18084L | 224 | +9 | 2019 |
| 33901 | 93036DE | 150 | +9 | 2022–2023 |
| 18528 | 79035F | 147 | +4 | 1987–1988 |
| 34023 | 93036HJ | 137 | +9 | 2019 |
| 34489 | 97051GE | 114 | +8 | 2019 |
| 33881 | 97051BA | 108 | +8 | 2019 |
| 8745 | 76022B | 98 | +3 | 2005 |
| 30871 | 99025AXH | 72 | +5 | 2019 |
| 38775 | 12050B | 72 | +2 | 2019 |
| 17240 | 86100B | 67 | +5 | 2019 |
| 44226 | 19026B | 64 | +2 | 2019 |
| 16007 | 85076G | 62 | +4 | 2019 |
| 10629 | 77065EY | 62 | +5 | 2019 |
| 19237 | 86019UP | 57 | +9 | 1988 |
| 5648 | 71105D | 53 | +4 | 1971–1972 |
| 11688 | 80010B | 51 | +1 | 1980 |
| 15339 | 61015LD | 50 | +1 | 1984 |
| 5309 | 65108H | 45 | +6 *(varies)* | 1972–1973 |
| 40815 | 10042Y | 41 | +3 | 2019 |
| 22338 | 92093AB | 41 | +7 | 2019 |
| 28421 | 04037C | 41 | +6 | 2019 |
| 23344 | 94074C | 23 | +6 | 2019 |
| 14563 | 77065FP | 23 | +5 | 2019 |
| 19665 | 84015F | 14 | +1 | 1988 |
| 25938 | 99056B | 13 | +1 | 2019 |
| 45787 | 20038BK | 11 | +2 | 2020 |
| 23080 | 89089CB | 5 | +6 | 1997 |
| 4849 | 71003A | 4 | +9 | 2005 |
| 32273 | 07051A | 3 | +9 | 2007 |
| 44620 | 18084R | 3 | +9 | 2019 |
| 11689 | 80010C | 2 | +1 | 1980 |
| 12041 | 80084E | 2 | +5 | 1997 |
| 26413 | 92072H | 2 | +2 | 2000 |
| 44476 | 19049B | 2 | +9 | 2019 |
| 41745 | 16052B | 2 | +5 *(varies)* | 2019–2025 |
| 41744 | 16052A | 2 | +6 | 2022–2025 |
| 12125 | 80074D | 1 | +5 | 1980 |
| 25017 | 97064A | 1 | +7 | 1997 |
| 25591 | 91076L | 1 | +8 | 1998 |
| 25797 | 89001J | 1 | +1 | 1999 |
| 54246 | 20029D | 1 | +7 | 2022 |
| 40099 | 14043A | 1 | +8 | 2022 |
| 40100 | 14043B | 1 | +8 | 2022 |
| 43339 | 18036A | 1 | +2 | 2025 |
| 55263 | 23008A | 1 | +7 | 2025 |

**67 objects · 47,465 records · 65 with a perfectly constant offset.**
