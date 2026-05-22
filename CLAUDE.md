# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

`tlekit` — console script `tle-clean` — validates and cleans a ~30 GB corpus of Two-Line
Element (TLE) satellite-tracking files exported from space-track.org.

## Authoritative spec

The design doc at `docs/superpowers/specs/2026-05-21-tle-corpus-cleaner-design.md` is the
authoritative specification — read it before changing validation, repair, or pipeline
behaviour. It carries a revision log in its header; keep that current when the design
changes.

## Tech Stack

Python 3.11 · uv · standard library only at runtime · `sgp4` (dev-only test oracle) ·
`pytest` · `pytest-cov` · `ruff`

The runtime is **pure standard library**. `sgp4` and `pytest` are dev-only dependencies;
`sgp4` is a test oracle and must never be imported at runtime.

## Critical Rules — principles that must not be violated

These are the reason the design exists; an implementation that breaks them is wrong.

1. **Validated transformation.** Never apply a fix and trust it. Apply a candidate fix,
   re-run *full* validation, and commit the result only if it now passes — otherwise
   quarantine.
2. **Correctness over recovery.** Never emit a wrong-but-valid-looking record; when in
   doubt, quarantine. No reconstruction of missing *data* characters. The one sanctioned
   reconstruction is a missing *checksum* digit, which is deterministically recomputable —
   and even that is a distinct, weaker repair tier with its own reporting.
3. **Constant memory.** Files stream; the pairing state machine holds at most two lines.
   A 3.2 GB file must never be loaded whole.
4. **One validator definition.** "Perfect" is defined once, in `tle.py`. Never add a
   second, divergent validation path.

**Report outcomes faithfully.** If tests fail, say so with the output. If a verification
step was skipped, say that rather than implying it ran. Never claim "all tests pass" when
output shows failures.

## The corpus (`data/`, git-ignored)

- `data/source/` — 29 raw `tle*.txt` files (~30 GB) plus `TLEs.zip` (~12 GB). Inputs.
- `data/output/` — where `clean` writes `cleaned/`, `broken/`, and `report.md`. Outputs.
- The whole `data/` tree is git-ignored — ~42 GB — and must never be staged or committed.
- **Never read a corpus file whole** — the largest is 3.2 GB. Sample with `head`, `awk`,
  or `sed -n`.

## Code Style

- Python 3.11. Concise one-paragraph docstrings on every public module, function, and
  class — match that established style; do not expand to Args/Returns/Raises blocks.
- `ruff` for linting and formatting, configured in `pyproject.toml` (rule sets `E`, `F`,
  `I`, `UP`, `B`, `SIM`; 88-column lines).
- `src/` layout — all package code lives under `src/tlekit/`.
- Run `uv run ruff check .` and `uv run ruff format --check .` before committing.

## Project Layout

```
src/tlekit/
├── __main__.py    # python -m tlekit entry point
├── __init__.py    # __version__, stem() filename helper
├── cli.py         # argparse, globbing, parallel workers, live progress, Ctrl-C handling
├── pipeline.py    # streams a file in binary, pairs 1/2 lines into records, routes them
├── repair.py      # speculative fixes, each confirmed by tle.py before commit
├── report.py      # FileStats, the .broken.txt sidecar writer, the run report
└── tle.py         # the validator — column layout, checksum, semantic ranges, pairing
```

Module dependencies point one way only: `cli.py → pipeline.py → repair.py → tle.py`.

→ See [`README.md`](README.md) for the architecture, usage, and data flow.
→ See [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup, testing, and the git workflow.

## Commands

```bash
uv sync                            # Install, including dev deps (sgp4, pytest, ruff)
uv run pytest                      # Full test suite
uv run pytest tests/test_tle.py::TestComputeChecksum   # A single test class
uv run pytest --cov=tlekit --cov-report=term-missing --cov-branch  # Tests + coverage
uv run ruff check .                # Lint
uv run ruff format --check .       # Format check
uv run tle-clean validate          # Audit data/source/ (read-only)
uv run tle-clean clean             # Clean data/source/ -> data/output/
```

## Working Style

- **Use agents** for codebase exploration and multi-step research tasks.
- **Always verify** after a change: run `uv run pytest`, `uv run ruff check .`, and
  `uv run ruff format --check .`, and report the actual output.
- Build order, if rebuilding from the spec (§12): `pyproject.toml` → `tle.py` (test-first,
  it is the correctness oracle) → `repair.py` → `pipeline.py` → `report.py` / `cli.py`.

## Verification

After completing edits, run these before reporting success:

```bash
uv run pytest                      # Must pass
uv run ruff check .                # Must pass
uv run ruff format --check .       # Must pass
```

If any fail, report the actual output — do not suppress or simplify failures.

## File Guidelines

- Never read a corpus file whole — sample with `head`, `awk`, or `sed -n`.
- When renaming a function or variable, search for direct calls, string literals
  containing the name, re-exports, and test references.
- Prefer files with one clear responsibility; keep functions focused and readable.

## Conventions

- Design docs live in `docs/superpowers/specs/`, named `YYYY-MM-DD-topic.md`. The design
  doc carries a revision log in its header — keep it current when the design changes.
- Tests are grouped into `Test*` classes, one per unit or behaviour under test.
- Git: never commit to `main` directly; branch (`feature/`, `bugfix/`, `chore/`); use
  conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `style:`, `chore:`).
