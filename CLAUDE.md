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

Python 3.14 · uv · lean runtime (**`rich`** — the one third-party dep) · `sgp4` (dev-only
test oracle) · `pytest` · `pytest-cov` · `ruff`

**Runtime dependencies** are governed by a *relaxed* policy (revised 2026-05-31): a popular,
actively-maintained library that genuinely reduces the code we'd otherwise own should be
adopted where it makes sense. The old four-MUST gate and its "aim is a veto" clause are
retired — those four (popular · maintained · reduces-our-burden · sensible shape) are now
*favourable signals*, not necessary conditions. The **only vetoes are the hard correctness
invariants**: one validator definition (Critical Rule #4), constant-memory streaming (#3),
`sgp4`-never-at-runtime, byte-deterministic *unstyled* structured/stdout output (#1/#2 —
`report.*`, NDJSON, sidecar, `--report json`, checkpoint, `cleaned/*`), and the atomic-durable
commit + host-aware lock. The canonical rule and the considered/deferred table live in the
authoritative spec §3.1 (`docs/superpowers/specs/2026-05-21-tle-corpus-cleaner-design.md`);
rationale in `2026-05-28-runtime-dependency-policy-design.md`. **Current runtime deps:
`rich>=13,<14`** (terminal rendering for `clean`) — a relaxed-bar audit re-evaluated every
candidate and still adopted none, since each trips a hard invariant or removes ~0 code. `sgp4`
and `pytest` are dev-only; `sgp4` is a test oracle and must never be imported at runtime.

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

- Python 3.14. Concise one-paragraph docstrings on every public module, function, and
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
├── report.py      # FileStats + dataclasses, the validate summaries, the run report
├── report_writers.py # structured-file writers: .broken.txt sidecar, report.jsonl findings, broken-noradids.ndjson, shard concat
├── resume.py      # single-run checkpoint for `clean --resume` (issue #56)
├── fsutil.py      # durable_replace — the one atomic+fsync commit path (issue #58)
├── term.py        # shared stderr Console + error/warning/note/prompt helpers (rich)
├── diff.py        # read-only: per-rule delta between two runs' report.jsonl (lintle diff)
├── explain.py     # read-only: renders rule/fix documentation (lintle explain)
├── tle.py         # the validator — column layout, checksum, semantic ranges, pairing
├── diagnostics.py # stable RuleID registry + structured Diagnostic dataclass (pure data)
├── categories.py  # FixClass enum + FixSpec registry — the repair taxonomy (pure data)
└── explain_examples.py # validator-verified examples + citations backing explain (pure data)
```

Module dependencies flow one way: `cli.py → pipeline.py → repair.py → tle.py`,
with the read-only `cli.py → diff.py` and `cli.py → explain.py → explain_examples.py`
consumers and the `cli.py → resume.py` single-run checkpoint (`resume.py` depends only
on `__version__`) alongside. `diagnostics.py` and `categories.py` are pure-data leaves
depended on by `repair`, `pipeline`, `report`, and `explain`; `explain_examples.py`
is also pure data, composing those two leaves into documented examples.
`report_writers.py` is the structured-file writers leaf (the `.broken.txt`
sidecar, the `report.jsonl` findings shards, the corpus `broken-noradids.ndjson`,
and the shard concat) depended on by `pipeline` and `cli`; it imports the
dataclasses and the shared `_format_diagnostic` renderer from `report.py` —
one-way, never the reverse, so no cycle. `fsutil.py`
is a stdlib-only I/O leaf (the durable-commit helper) depended on by `pipeline`,
`report`, `report_writers`, and `resume`. `term.py` is a rich-only stderr-output leaf (the shared
Console plus the `error`/`warning`/`note`/`prompt` emitters) depended on by `cli`
and `diff` — so the styled `error:`/`warning:` prefix lives in one place without a
`diff → cli` cycle. `tle.py` and the data modules carry no I/O, so cycles are
structurally impossible.

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

Trunk is `develop`; `main` carries one merge commit per release and never
receives direct commits. Two paths into `develop`:

- **Direct commits** for chores and bugfixes (`chore:`, `fix:`, `docs:`,
  `test:`, `style:`) — commit on `develop`, push. No branch, no PR.
- **Branch + PR** for features and multi-file refactors (`feature/<desc>` or
  `refactor/<desc>`) — land via **rebase-and-merge** so `develop` stays linear
  (see `CONTRIBUTING.md` § Git Workflow).

**Worktrees are the parallel-development mechanism for branched work** — they
let multiple branches share one clone without contention, so you can keep a
long-running test run in one worktree while editing in another.

**When to use a worktree:** any `feature/<desc>` or `refactor/<desc>` branch.
Default for any non-trivial change you'd raise a PR for.

**When to skip the worktree:** chores and bugfixes — single-line fixes, doc
edits, dependency bumps, `ruff format` passes. Commit directly on `develop` in
the main checkout. No branch, no PR.

**Feature workflow (worktree):**

1. From the main checkout, create the worktree off `develop`:
   `git worktree add .worktrees/<branch-dir> -b <branch-name> develop`
2. `cd .worktrees/<branch-dir>`
3. Install dev deps in the worktree: `uv sync`
4. **Symlink the corpus into the worktree** (the ~30 GB `data/` tree lives only
   in the main checkout; the symlink keeps a single copy on disk and lets the
   CLI work transparently): `ln -s ../../data data`
5. Do the work in the worktree directory — small, logical commits as you go
   (tests first, then implementation), not one giant commit at the end
6. Verify in the worktree: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
7. Land via PR. Push the branch (`git push -u origin <branch-name>`), open a
   PR against `develop`, then use the GitHub UI's **"Rebase and merge"**
   button (or `gh pr merge --rebase --delete-branch`). Do not use "Create a
   merge commit" or "Squash and merge".
8. Clean up: `git worktree remove .worktrees/<branch-dir>` then
   `git branch -D <branch-name>` (use `-D`, not `-d`: rebase-and-merge
   rewrites the SHAs on `develop`, so the local branch won't look "merged"
   to git even though its content has landed).

**No per-merge version bumps.** Feature merges to `develop` do not touch
`pyproject.toml`'s version. Version bumps and the dated `CHANGELOG.md` section
land together on a `chore/release-X.Y.Z` branch — see `CONTRIBUTING.md`
§ Versioning § Release flow. Add CHANGELOG-worthy notes alongside the code in
your feature branch; they'll be collected under the next dated version when the
release is cut.

**Chore/bugfix workflow (direct on develop):** stay on `develop` in the main
checkout, edit, run the same verification chain, commit with the right
conventional-commit prefix (`chore:`, `fix:`, `docs:`, `test:`, `style:`),
push. No branch, no worktree, no PR.

**Worktree directory:** `.worktrees/` in project root (git-ignored). Directory
names mirror the branch with slashes replaced by hyphens —
`feature/repair-tier-2` → `.worktrees/feature-repair-tier-2`.

**Parallel worktrees:** multiple `.worktrees/*` directories can coexist. Each has
its own `.venv/` (created by `uv sync`); the symlinked `data/` is shared, so
don't write through it — `clean` writes to `data/output/` and concurrent
worktrees writing there will collide. Pass `--out-dir <worktree-local-dir>` to
`lintle clean` when iterating in parallel so each worktree writes to its own
output tree.

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
- Git: `develop` is the trunk; `main` carries one merge commit per release and
  never receives direct commits. On `develop`: chores and bugfixes (`chore:`,
  `fix:`, `docs:`, `test:`, `style:`) commit directly; features and multi-file
  refactors go on a `feature/<desc>` or `refactor/<desc>` branch and land via
  **rebase-and-merge** so `develop` stays linear. Releases are hand-assembled
  merge commits on `main` (tree = develop's release-point tree; second parent =
  develop's release-point) — see CONTRIBUTING.md § Versioning for the
  `git commit-tree` recipe. Tagged on `main`. Use `git log --first-parent main`
  for the release-only view. Use conventional commits (`feat:`, `fix:`,
  `docs:`, `test:`, `refactor:`, `style:`, `chore:`).
- Versioning: `pyproject.toml`'s `[project] version` is the single source of truth;
  `src/lintle/__init__.py` resolves `__version__` from it at runtime via
  `importlib.metadata`. Bump it once, add a `CHANGELOG.md` entry — see CONTRIBUTING.md
  for the release flow.
