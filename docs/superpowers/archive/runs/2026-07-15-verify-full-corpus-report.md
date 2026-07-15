# lintle verify — full-corpus acceptance run (#152)

- Generated: 2026-07-15T18:48:30Z
- Tool: lintle 0.7.0 (post-#158 validator, post-#164 dedup)
- Corpus: `~/Downloads/tle/output/cleaned` (29 files, 232,281,894 records; source
  byte-diff against `~/Downloads/tle/original`)
- Command: `/usr/bin/time -l python -m lintle verify <out> --source <src>`
  (sgp4-free exhaustive pass; **no** `--orbit` — the sampled physics pass does not
  exercise the full-cardinality external sort this run validates)

One-time, pre-merge acceptance — **not** CI. Confirms the three scale claims that
only surface at full 232 M-record cardinality; sample runs cannot test them.

## Result: PASS

Every #152 acceptance criterion held. The run's own exit code is **1**, which is
*expected and correct* — it reports the 5 genuine `VRFY-EPOCH-CONFLICT` clashes
that #158 legitimately keeps (one shared element-set naming two orbits). That is a
finding about the data, not a failure of the acceptance run.

| Criterion | Target | Measured | Verdict |
|-----------|--------|----------|:------:|
| External merge sort spills to disk & completes at real cardinality (Critical Rule #3) | constant memory | peak RSS **135 MB** flat over 232 M records + 30 GB source diff | ✅ |
| `ORIGIN_MISSING` against the real quarantine-gap distribution (gates #143's 4096-line resync `ponytail:` marker) | ~0 | **0** | ✅ |
| Records audited | 232,281,894 | 232,281,894 | ✅ |
| `missing_source_files` (byte-diff coverage) | 0 | 0 | ✅ |
| Wall-clock (one-time, off-CI) | acceptable | 6,204.6 s (1 h 43 m) | ✅ |

The 135 MB peak is the proof of the spill: holding 232 M parsed records in RAM
would need tens of GB, so the `ExternalSorter` necessarily spilled chunks to disk
and k-way-merged them — while resident memory stayed flat. `ORIGIN_MISSING = 0`
confirms the 4096-line sliding resync window is never exhausted by the real
0.0444 % (103,228-record) quarantine gap distribution; the `ponytail:` window
marker holds on real data.

## verify suspect census

| Rule | Count | Severity | Note |
|------|------:|----------|------|
| `VRFY-EPOCH-CONFLICT` | 5 | hard | genuine same-element-set clashes (#158 keeps these) |
| `VRFY-INTERIOR-MUT` | 0 | hard | no cleaned line is an unsanctioned edit of its source (goal 1) |
| `VRFY-ORIGIN-MISSING` | 0 | soft | every cleaned line's source origin found within the window |
| epoch re-issue census | 369,700 | — | benign successive solutions (counted, not flagged) |

- Peak RSS (maximum resident set size): 141,377,536 B (134.8 MB); peak memory
  footprint 141,673,072 B (135.1 MB)
- Wall-clock: 6,204.59 s real / 5,678.23 s user / 73.62 s sys

## dedup add-on (#164 confirmation, not part of #152)

Run on the same warm corpus to re-measure the #164 fix at scale. Pre-#164 this
corpus produced 364,149 `conflict:true` "genuine contradictions" (exit 1); the
fix aligned dedup's predicate with verify's #158 element-set clash rule.

| Metric | Value |
|--------|------:|
| Exit code | **0** (was 1 pre-#164) |
| Genuine conflicts | **0** (was 364,149 pre-#164) |
| `import.txt` cards written | 214,711,734 |
| Re-issue duplicates collapsed | 17,570,155 |
| Peak RSS | 147,472,384 B (140.6 MB) |
| Wall-clock | 1,902.27 s (31 m 42 s) |

The 5 verify-hard suspects were excluded before dedup's own conflict scan (dedup
reads verify's `suspects.jsonl` first), so dedup's residual genuine-conflict count
is 0 and it exits clean. #164 confirmed on real data.

## Notes

- Ran against the current post-#158/#164 code. The prior on-disk verify dirs were
  stale for this purpose: `verify-152-baseline` (pre-#154/#158, 3.24 M bogus
  raw-byte conflicts + the 1,805 `INTERIOR-MUT` since triaged) and `verify-pre-r4`
  (pre-#158, 369,705 conflicts before the re-issue census split). The prior
  current-code `verify` run skipped the source diff and is preserved as
  `verify-orbit-2026-07-15` (it carries the sampled orbit numbers).
- Reproducibility: `summary.json` beside this report is the verifier's own emitted
  summary, verbatim. Peak RSS and wall-clock are from `/usr/bin/time -l` (the
  verifier does not self-instrument memory; a one-time external measurement is
  sufficient and adds no runtime code).
