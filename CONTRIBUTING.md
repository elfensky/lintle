# Contributing to lintle

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — Python package and project manager

## Setup

```bash
git clone <repo-url>
cd lintle
uv sync
```

`uv sync` installs Python 3.11 if needed, creates a `.venv/`, and installs the project
plus all dev dependencies (`pytest`, `pytest-cov`, `sgp4`, `ruff`) from `uv.lock`.

### Managing dependencies

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock` (committed to git).

```bash
uv add --group dev <pkg>   # Add a dev-only dependency
uv sync                    # Reinstall from the lock file (after a pull)
```

The runtime's one third-party dependency is **`rich`** (`>=13,<14`, terminal rendering for
`clean`). Further additions are governed by a *relaxed* policy (2026-05-31): a popular,
actively-maintained library that genuinely reduces the code we'd otherwise own may be adopted
where it makes sense — gated only by the hard correctness invariants (one validator,
constant-memory streaming, byte-deterministic *unstyled* structured/stdout output, the
atomic-durable commit + host-aware lock, `sgp4`-never-at-runtime). The canonical rule and
considered/deferred table live in [`ARCHITECTURE.md` §7](ARCHITECTURE.md#7-runtime-dependency-policy).
`sgp4` is a dev-only test oracle and must never be imported at runtime.

## Running

```bash
uv run lintle validate          # Read-only audit of data/source/
uv run lintle clean             # Write cleaned output to data/output/
```

`uv run` executes a command inside the project virtual environment — no manual
activation needed.

## Testing

```bash
uv run pytest                      # Run all tests
uv run pytest -x                   # Stop on first failure
uv run pytest -k "checksum"        # Run tests matching an expression
uv run pytest tests/test_tle.py    # Run one file
uv run pytest tests/test_tle.py::TestComputeChecksum   # Run one class
```

### Coverage

```bash
uv run pytest --cov=lintle --cov-report=term-missing --cov-branch
```

This reports line and branch coverage, listing uncovered lines in the `Missing` column.

### Test layout

Tests are grouped into `Test*` classes, one per unit or behaviour under test.

| File | What it covers |
|------|----------------|
| `test_tle.py` | The validator: checksum, column layout, semantic ranges, record pairing |
| `test_diagnostics.py` | `RuleID` registry, `Diagnostic` dataclass, `RULES` metadata, the `diagnostic()` constructor |
| `test_categories.py` | `FixClass`/`FixSpec`/`FIXES` repair-tag registry and its import-time coverage guard |
| `test_repair.py` | Speculative line/record repair and the rule IDs each repair tier emits |
| `test_pipeline.py` | Streaming I/O, line pairing, per-file processing, progress, temp-file safety |
| `test_report.py` | `FileStats`, the `.broken.txt` sidecar, summaries, the run report, per-NORAD breakdown |
| `test_cli.py` | Argument parsing, path discovery, exit codes, elapsed-time formatting |
| `test_diff.py` | `lintle diff` — per-rule delta between two runs' `report.jsonl` |
| `test_explain.py` | `lintle explain` — rule/fix docs, examples validated against the live validator, coverage + disjointness guards |
| `test_integration.py` | End-to-end: golden output, idempotence, re-validation |
| `test_oracle.py` | Cross-checks a known-good TLE against the trusted `sgp4` parser |
| `test_pipeline_throughput.py` | Opt-in records/sec regression guard, gated by `pytest -m slow` (excluded by default) |

`conftest.py` holds the shared `line1` / `line2` fixtures — a canonical, known-good TLE.

## Linting & Formatting

[Ruff](https://docs.astral.sh/ruff/) handles both linting and formatting. Its
configuration lives in `pyproject.toml` under `[tool.ruff]` (rule sets `E`, `F`, `I`,
`UP`, `B`, `SIM`; 88-column lines).

```bash
uv run ruff check .                # Lint
uv run ruff check . --fix          # Lint with auto-fix
uv run ruff format .               # Format
uv run ruff format --check .       # Check formatting (no writes)
```

Run both before committing:

```bash
uv run ruff check . && uv run ruff format --check .
```

## Verification

Before reporting any change as done, run — and report the actual output of:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Never claim success without the output. If a check fails, report the failure.

## Git Workflow

Two branches, two roles:

- **`develop`** is the long-running trunk. All non-release history lives here.
  Two paths in, by scope:
  - **Direct commits** for chores and bugfixes (`chore:`, `fix:`, `docs:`,
    `test:`, `style:`) — commit on `develop`, push. No branch, no PR.
  - **Branch + PR** for features and multi-file refactors
    (`feature/<desc>` or `refactor/<desc>`) — land via **rebase-and-merge**
    so `develop` stays linear (no merge bubbles).
- **`main`** is the release branch. Each release is a single merge commit on
  `main` whose tree is develop's release-point tree and whose second parent is
  develop's release-point commit. The second parent gives graph visualizers a
  "branched-from" edge from each release on `main` back to its origin on
  `develop`. Use `git log --first-parent main` to see only the releases.
  Releases are annotated tags on `main`. There is no separate release branch.
  **Never commit directly to `main`** — release commits only, hand-built with
  `git commit-tree` (see § Versioning § Release flow).

- Branch names (when branching): `feature/<desc>`, `refactor/<desc>` —
  lowercase, hyphens. The release-prep branch is the documented exception:
  `chore/release-X.Y.Z` (see § Versioning § Release flow) carries the version
  bump + dated `CHANGELOG.md` section through a review PR.
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `test:`, `refactor:`, `style:`, `chore:`. Direct commits to
  `develop` use these as the commit prefix; branched work uses them on
  individual commits inside the branch.
- Run the verification commands above before pushing any commit to `develop`
  (direct or via PR merge).
- Land PRs to `develop` via **"Rebase and merge"** in the GitHub UI (or
  `gh pr merge --rebase --delete-branch` locally). Do not use "Create a merge
  commit" — merge bubbles fragment the visualizer into apparent multiple
  develop lanes. Do not use "Squash and merge" either — keep the individual
  commits readable in `git log develop`.

### Parallel development with git worktrees

Worktrees let one clone host several branches simultaneously, each in its own
directory with its own `.venv/`. Use one for any non-trivial feature; iterate in
one worktree while a slow test run finishes in another.

```bash
# 1. Create the worktree from develop
git worktree add .worktrees/<branch-dir> -b feature/<desc> develop

# 2. Enter and install
cd .worktrees/<branch-dir>
uv sync

# 3. Symlink the corpus so the CLI sees data/ — keeps a single ~30 GB copy on disk
ln -s ../../data data

# 4. Work, commit incrementally, then verify
uv run pytest && uv run ruff check . && uv run ruff format --check .

# 5. Push and open a PR; land via "Rebase and merge"
git push -u origin feature/<desc>
gh pr create --base develop --title "<title>" --body "<body>"
gh pr merge --rebase --delete-branch

# 6. Clean up (use -D, not -d: rebase rewrites SHAs so the local branch
#    won't appear "merged" to git even after origin landed it)
git worktree remove .worktrees/<branch-dir>
git branch -D feature/<desc>
```

Worktree directory names mirror the branch with slashes replaced by hyphens —
`feature/repair-tier-2` → `.worktrees/feature-repair-tier-2`. The whole
`.worktrees/` tree is git-ignored.

When running `lintle clean` from multiple worktrees in parallel, pass
`--out-dir <local-dir>` to each — the default `data/output/` is shared through
the symlink and concurrent runs will collide.

## Versioning

Semantic versioning (`MAJOR.MINOR.PATCH`). The version lives in **one place** —
`pyproject.toml`'s `[project] version` field — and is resolved at runtime from the
installed distribution metadata by `src/lintle/__init__.py`:

```python
from importlib.metadata import PackageNotFoundError, version as _dist_version

try:
    __version__ = _dist_version("lintle")
except PackageNotFoundError:  # source checkout that was never installed
    __version__ = "0.0.0+local"
```

Because the lookup needs the project to be installed (even editable), keep `uv sync`
current — every dev workflow in this repo already does.

Release flow:

1. On a `chore/release-X.Y.Z` branch off `develop`, bump `version` in
   `pyproject.toml`.
2. Add a new `## [X.Y.Z] - YYYY-MM-DD` section at the top of `CHANGELOG.md` with
   `### Added` / `### Changed` / `### Fixed` subsections (see Keep a Changelog).
3. Run the verification commands (`uv run pytest`, `uv run ruff check .`,
   `uv run ruff format --check .`) and report the actual output.
4. Open a PR to `develop`, land via **"Rebase and merge"** once it's green.
5. Build the release commit on `main` with `git commit-tree`. The tree comes
   from `develop`'s release-point; the parents are `main`'s current tip and
   `develop`'s release-point. This is what gives the graph a visible
   "branched-from" edge from `main` to `develop` at each release while keeping
   the release tree byte-identical to what gets published:
   ```bash
   git fetch origin
   TREE=$(git rev-parse origin/develop^{tree})
   COMMIT=$(git commit-tree "$TREE" \
              -p origin/main \
              -p origin/develop \
              -m "Release vX.Y.Z")
   git update-ref refs/heads/main "$COMMIT"
   git checkout main
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push origin main vX.Y.Z
   ```
   To see only the release commits on `main` (skipping the develop history
   reachable via second parents), use `git log --first-parent main`.
6. Create the GitHub release:
   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-from-tag --latest
   ```
7. Trigger the `Publish` workflow.

Nothing else needs to change — `lintle --version`, the `report.py` headers, and
any downstream `from lintle import __version__` import all pick the new value up
from `pyproject.toml` automatically.
