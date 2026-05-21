# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**Design phase — no source code exists yet.** The only substantive artifact is the design doc
at `docs/superpowers/specs/2026-05-21-tle-corpus-cleaner-design.md`. It is the **authoritative
specification** — read it before implementing anything. The planned package (`pyproject.toml`,
`src/tlekit/`, `tests/`) has not been created.

## What this project is

`tlekit` — console script `tle-clean` — validates and cleans a ~30 GB corpus of Two-Line Element
(TLE) satellite-tracking files exported from space-track.org. Two modes:

- `tle-clean validate` — read-only audit; reports defects by type and source location.
- `tle-clean clean` — emits a corrected file per input, quarantining records it cannot safely fix.

## The corpus (`data/`, git-ignored)

- `data/source/` — 29 raw `tle*.txt` files (~30 GB) plus `TLEs.zip` (~12 GB). **Inputs.**
- `data/output/` — where the cleaner writes `<name>.cleaned.txt` / `<name>.broken.txt`. **Outputs.**
- The whole `data/` tree is git-ignored — ~42 GB, must never be staged or committed.
- **Never read a corpus file whole** — the largest is 3.2 GB. Sample with `head`, `awk`, `sed -n`.
- Measured defect distribution (spec §1.1): ~67% of records carry a trailing `\` export artifact,
  ~15% were exported without their checksum digit, ~17% are already clean, <0.01% are corrupt.

## Architecture (planned — see spec §4)

A `uv`-managed Python project. **Runtime is pure standard library**; `sgp4` and `pytest` are
dev-only dependencies — `sgp4` is a test oracle and must never be imported at runtime.

One validator, used two ways: it defines what a "perfect" TLE record is, and both `validate` and
`clean` reuse that single definition. Module dependencies point one way only:

`cli.py → pipeline.py → repair.py → tle.py`

- `tle.py` — defines validity (column layout, mod-10 checksum, semantic ranges, record pairing).
  Pure, no I/O. The single source of truth for "perfect."
- `repair.py` — candidate fixes, each applied speculatively and confirmed by `tle.py`.
- `pipeline.py` — streams a file in binary, pairs `1 `/`2 ` lines into records, routes them.
- `report.py` — renders the `.broken.txt` quarantine sidecar and the run summary.
- `cli.py` — argparse; globs paths; drives per-file `ProcessPoolExecutor` parallelism.

## Principles that must not be violated

These are the reason the design exists; an implementation that breaks them is wrong:

1. **Validated-transformation (§4.1).** Never apply a fix and trust it. Apply a candidate fix,
   re-run *full* validation, and commit the result **only if it now passes** — otherwise quarantine.
2. **Correctness over recovery.** Never emit a wrong-but-valid-looking record; when in doubt,
   quarantine. No reconstruction of missing *data* characters. The one sanctioned reconstruction
   is a missing *checksum* digit, which is deterministically recomputable (spec §6.2) — and even
   that is a distinct, weaker repair tier with its own reporting.
3. **Constant memory.** Files stream; the pairing state machine holds at most two lines. A 3.2 GB
   file must never be loaded whole.
4. **One validator definition.** "Perfect" is defined once, in `tle.py`. Never add a second,
   divergent validation path.

## Commands (once the project is scaffolded)

The package does not exist yet. Per the design doc, once `pyproject.toml` is created:

```
uv sync                                          # install, incl. dev deps (sgp4, pytest)
uv run pytest                                    # full test suite
uv run pytest tests/test_tle.py::test_name       # a single test
uv run tle-clean validate                        # audit data/source/ (read-only)
uv run tle-clean clean                           # clean data/source/ -> data/output/
```

Build order (spec §12): `pyproject.toml` → `tle.py` (test-first, it is the correctness oracle)
→ `repair.py` → `pipeline.py` → `report.py` / `cli.py`.

## Conventions

- Design docs live in `docs/superpowers/specs/`, named `YYYY-MM-DD-topic.md`.
- The design doc carries a revision log in its header — keep it current when the design changes.
