# Contributing to lintle

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — Python package and project manager

## Setup

```bash
git clone <repo-url>
cd TLEs
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

The **runtime has no third-party dependencies** — `lintle` is pure standard library.
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
| `test_repair.py` | Speculative line/record repair and the rejection categories |
| `test_pipeline.py` | Streaming I/O, line pairing, per-file processing, progress, temp-file safety |
| `test_report.py` | `FileStats`, the `.broken.txt` sidecar, summaries, the run report |
| `test_cli.py` | Argument parsing, path discovery, exit codes, elapsed-time formatting |
| `test_integration.py` | End-to-end: golden output, idempotence, re-validation |
| `test_oracle.py` | Cross-checks a known-good TLE against the trusted `sgp4` parser |

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

This repo uses a Git Flow-inspired model with two long-lived branches:

- **`main`** — release log. Each commit on `main` is one shipped release,
  squash-merged from a `develop` → `main` PR and tagged `vX.Y.Z`. `main` has no
  other history; never commit to it directly.
- **`develop`** — integration branch and the full project history. Default branch
  for every PR.

Short-lived branches all branch from `develop` and PR back into `develop`:

| Prefix         | Purpose                                  |
|----------------|------------------------------------------|
| `feature/<x>`  | New functionality                        |
| `bugfix/<x>`   | Fix a bug                                |
| `chore/<x>`    | Tooling, deps, refactors, doc-only edits |

Names are lowercase with hyphens. Use [Conventional Commits](https://www.conventionalcommits.org/)
on `develop`: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `style:`, `chore:`.
Release PR titles to `main` use the form `Release vX.Y.Z`.

**Merge policy** — the two merge styles serve opposite purposes, so don't mix them up:

- PRs into `develop`: **never squash.** Use `--no-ff` (or the "Create a merge
  commit" option in the GitHub UI) so the detailed branch history is preserved
  on `develop`.
- Release PRs into `main`: **always squash.** `main` is a release log, not a
  history — `develop` already holds the full history. Squash collapses the
  release PR into a single commit, which is then tagged `vX.Y.Z`.

Run the verification commands above before merging.

### Parallel development with git worktrees

Worktrees let one clone host several branches simultaneously, each in its own directory
with its own `.venv/`. Use one for any non-trivial feature; iterate in one worktree
while a slow test run finishes in another.

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

# 5. Merge back (from the main checkout)
cd ../.. && git checkout develop && git merge --no-ff feature/<desc>

# 6. Clean up
git worktree remove .worktrees/<branch-dir>
git branch -d feature/<desc>
```

Worktree directory names mirror the branch with slashes replaced by hyphens —
`feature/repair-tier-2` → `.worktrees/feature-repair-tier-2`. The whole `.worktrees/`
tree is git-ignored.

When running `lintle clean` from multiple worktrees in parallel, pass
`--out-dir <local-dir>` to each — the default `data/output/` is shared through the
symlink and concurrent runs will collide.

See `CLAUDE.md` § Worktree Workflow for the Claude-facing version of these rules.

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

1. On `develop`, bump `version` in `pyproject.toml`.
2. Add a new `## [X.Y.Z] - YYYY-MM-DD` section at the top of `CHANGELOG.md` with
   `### Added` / `### Changed` / `### Fixed` subsections (see Keep a Changelog).
3. Run the verification commands (`uv run pytest`, `uv run ruff check .`,
   `uv run ruff format --check .`) and report the actual output.
4. Commit on `develop` and push.
5. Open a PR from `develop` → `main` titled `Release vX.Y.Z` (the PR body should
   restate the changelog entry):
   `gh pr create --base main --head develop --title "Release vX.Y.Z" --body-file -`.
6. Once CI is green, **squash-merge** the PR: `gh pr merge --squash --delete-branch=false`
   (the GitHub UI's "Squash and merge" button does the same). `main` now has one
   new commit representing the release; `develop` is unchanged.
7. Tag the squash commit on `main`: `git fetch origin main && git tag -a vX.Y.Z
   -m "Release X.Y.Z" origin/main && git push origin vX.Y.Z`. Trigger the
   `Publish` workflow.

Hotfixes follow the same shape: fix on a `bugfix/<x>` branch off `develop`, merge
to `develop` with `--no-ff`, bump the patch version, then ship via the same
develop → main release PR.

Nothing else needs to change — `lintle --version`, the `report.py` headers, and any
downstream `from lintle import __version__` import all pick the new value up from
`pyproject.toml` automatically.
