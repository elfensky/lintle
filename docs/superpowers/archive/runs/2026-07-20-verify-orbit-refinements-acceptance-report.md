# lintle verify --orbit — #163 refinements acceptance run

- Generated: 2026-07-20
- Tool: lintle 0.9.0 (the release-candidate `develop` tree after #163's five
  refinements landed via PRs #167–#171)
- Corpus: `~/Downloads/tle/output/cleaned` (29 files, 232,281,894 records)
- Command: `lintle verify <out> --no-source-diff --orbit` (default `--sample 3000`,
  default `--sensitivity sensitive`)

One-time, pre-release acceptance — **not** CI. Confirms the five `verify --orbit`
refinements behave as designed at full 232 M-record cardinality and that none of
them broke a hard invariant. Config deliberately matches the #144-core baseline
(`verify-orbit-2026-07-15`) so the refinements are the only variable.

## Result: ACCEPT (exit 1 = the 5 known #158 clashes, unchanged)

The exit-1 verdict is the expected pass condition: all 5 hard suspects are the
sample-independent `VRFY-EPOCH-CONFLICT`s on catalogs 11027, 23319, 27378, 33108,
39612 — byte-identical to the #144 baseline and the #152 full-corpus run. Zero
orbit findings hard-convicted (soft-stays-soft held).

## Comparison vs the #144-core baseline (2026-07-15)

| Metric | Baseline | This run (#163) | Reading |
|---|---:|---:|---|
| records | 232,281,894 | 232,281,894 | — |
| orbit_population | 65,300 | 65,300 | — |
| orbit_sampled | 3,000 | 3,000 | — |
| orbit_pairs_measured | 9,873,231 | 12,867,314 | +30.3% — #4 regime-aware GEO 7-day gate admits pairs the flat 3-day gate dropped |
| VRFY-ORBIT-OUTLIER (soft) | 88,035 | 41,348 | fewer, better-attributed — #5 local-median + #1 leave-one-out, over #2's dup-epoch-biased sample (not apples-to-apples: different 3,000 sats) |
| VRFY-ORBIT-ERROR (hard) | 1 | 0 | sample-dependent; the errored sat is not in the new sample (not a regression) |
| VRFY-EPOCH-CONFLICT (hard) | 5 | 5 | identical catalogs |
| hard / exit_code | 6 / 1 | 5 / 1 | exit driven only by the 5 known clashes |
| epoch_reissues | 369,700 | 369,700 | unchanged — #2 left the re-issue counter untouched, as designed |

## Invariants confirmed at scale

- **Soft stays soft** — no orbit finding became hard; the only hard suspects are the
  5 known `VRFY-EPOCH-CONFLICT`s (sample-independent, over all 232 M records).
- **Constant memory (Rule #3)** — the streaming worker held a flat ~121–131 MB RSS
  across the whole run (same class as the #152 baseline's ~135 MB), spilling ~34 GB
  to on-disk external-sort runs; no whole-file loads.
- **#2 independence** — `epoch_reissues` unchanged at 369,700 confirms the new
  dup-epoch collection did not perturb the existing re-issue counter.
- **Per-refinement fingerprints** all present: +30% pairs (#4), fewer/cleaner
  outliers (#5, #1), a different dup-epoch-biased sample (#2). `--sensitivity` (#3)
  ran at its default `sensitive` tier, byte-identical to the prior release.

Archived summary: `2026-07-20-verify-orbit-refinements-acceptance-summary.json`.
