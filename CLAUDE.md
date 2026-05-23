# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

`lintle` validates and cleans a ~30 GB corpus of Two-Line Element (TLE)
satellite-tracking files exported from space-track.org.

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
- `src/` layout — all package code lives under `src/lintle/`.
- Run `uv run ruff check .` and `uv run ruff format --check .` before committing.

## Project Layout

```
src/lintle/
├── __main__.py    # python -m lintle entry point
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
uv run pytest --cov=lintle --cov-report=term-missing --cov-branch  # Tests + coverage
uv run ruff check .                # Lint
uv run ruff format --check .       # Format check
uv run lintle validate          # Audit data/source/ (read-only)
uv run lintle clean             # Clean data/source/ -> data/output/
```

## Working Style

- **Use agents** for codebase exploration and multi-step research tasks.
- **Always verify** after a change: run `uv run pytest`, `uv run ruff check .`, and
  `uv run ruff format --check .`, and report the actual output.
- Build order, if rebuilding from the spec (§12): `pyproject.toml` → `tle.py` (test-first,
  it is the correctness oracle) → `repair.py` → `pipeline.py` → `report.py` / `cli.py`.

## Worktree Workflow

Lintle's branching model: `main` is a squash-merged release log (one commit per
tagged release), `develop` is the integration branch and holds the full history,
and every change goes through a short-lived branch + PR off `develop` (see
`CONTRIBUTING.md` § Git Workflow). **Worktrees are the parallel-development
mechanism** — they let multiple branches share one clone without contention, so
you can keep a long-running test run in one worktree while editing in another.

**When to use a worktree (features):** new modules, multi-file refactors, anything you'd
raise a PR for, anything large enough to want isolation while iterating. Default for any
non-trivial change.

**When a worktree is overkill (small chores):** single-file doc edits, a one-line bugfix,
a `ruff format` pass. Still branch (`feature/`, `bugfix/`, `chore/`), but check it out in
the main directory — no worktree needed. Use judgment; if unsure, default to a worktree.

**Feature workflow (worktree):**

1. From the main checkout, create the worktree off `develop`:
   `git worktree add .worktrees/<branch-dir> -b <branch-name> develop`
2. `cd .worktrees/<branch-dir>`
3. Install dev deps in the worktree: `uv sync`
4. **Symlink the corpus into the worktree** (the ~30 GB `data/` tree lives only in the
   main checkout; the symlink keeps a single copy on disk and lets the CLI work
   transparently): `ln -s ../../data data`
5. Do the work in the worktree directory — small, logical commits as you go (tests
   first, then implementation), not one giant commit at the end
6. Verify in the worktree: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
7. Merge back: from the main checkout, `git checkout develop && git merge --no-ff <branch-name>`
   (or open a PR to `develop` — see § Conventions). PRs into `develop` are **never
   squashed** so branch history is preserved. Release PRs (`develop` → `main`) are
   the exception — those are always squash-merged so `main` stays a one-commit-per-release
   log.
8. If the change is user-visible, bump `pyproject.toml`'s `[project] version` and add a
   `CHANGELOG.md` entry in the same merge — see `CONTRIBUTING.md` § Versioning
9. Clean up: `git worktree remove .worktrees/<branch-dir>` then
   `git branch -d <branch-name>`

**Small-chore workflow (branch in main checkout):** from `develop`, branch
(`git checkout develop && git checkout -b <branch-name>`), edit, run the same
verification chain, commit, PR to `develop`. Skip steps 1, 2, 4, 9 above.

**Worktree directory:** `.worktrees/` in project root (git-ignored). Directory names
mirror the branch with slashes replaced by hyphens — `feature/repair-tier-2` →
`.worktrees/feature-repair-tier-2`.

**Parallel worktrees:** multiple `.worktrees/*` directories can coexist. Each has its own
`.venv/` (created by `uv sync`); the symlinked `data/` is shared, so don't write through
it — `clean` writes to `data/output/` and concurrent worktrees writing there will
collide. Pass `--out-dir <worktree-local-dir>` to `lintle clean` when iterating in
parallel so each worktree writes to its own output tree.

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
- Git: `main` is a release log (squash-merged from `develop` → `main` PRs, one
  commit per tagged release; never commit directly). `develop` is the integration
  branch and the base for every short-lived `feature/`, `bugfix/`, `chore/` branch.
  Merge style is opposite at each line: `--no-ff` into `develop` (preserve branch
  history), squash into `main` (keep main lean). Use conventional commits on
  `develop` (`feat:`, `fix:`, `docs:`, `test:`, `style:`, `chore:`); release PR
  titles to `main` are `Release vX.Y.Z`.
- Versioning: `pyproject.toml`'s `[project] version` is the single source of truth;
  `src/lintle/__init__.py` resolves `__version__` from it at runtime via
  `importlib.metadata`. Bump it once, add a `CHANGELOG.md` entry — see CONTRIBUTING.md
  for the release flow.
