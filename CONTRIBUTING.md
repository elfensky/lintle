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

Single trunk on `main`. Branch off it for every change, work, PR back to `main`,
merge with `--no-ff` so branch history is preserved. Releases are annotated tags
on `main` — there is no separate release branch.

- Branch names: `feature/<desc>`, `bugfix/<desc>`, `chore/<desc>` — lowercase,
  hyphens.
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `test:`, `refactor:`, `style:`, `chore:`.
- Never commit directly to `main`. Open a PR; run the verification commands
  above before merging.
- Never squash PRs to `main` — use `--no-ff` (or "Create a merge commit" in the
  GitHub UI) so branch history survives.

### Parallel development with git worktrees

Worktrees let one clone host several branches simultaneously, each in its own
directory with its own `.venv/`. Use one for any non-trivial feature; iterate in
one worktree while a slow test run finishes in another.

```bash
# 1. Create the worktree from main
git worktree add .worktrees/<branch-dir> -b feature/<desc> main

# 2. Enter and install
cd .worktrees/<branch-dir>
uv sync

# 3. Symlink the corpus so the CLI sees data/ — keeps a single ~30 GB copy on disk
ln -s ../../data data

# 4. Work, commit incrementally, then verify
uv run pytest && uv run ruff check . && uv run ruff format --check .

# 5. Merge back (from the main checkout)
cd ../.. && git checkout main && git merge --no-ff feature/<desc>

# 6. Clean up
git worktree remove .worktrees/<branch-dir>
git branch -d feature/<desc>
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

1. On a `chore/release-X.Y.Z` branch off `main`, bump `version` in
   `pyproject.toml`.
2. Add a new `## [X.Y.Z] - YYYY-MM-DD` section at the top of `CHANGELOG.md` with
   `### Added` / `### Changed` / `### Fixed` subsections (see Keep a Changelog).
3. Run the verification commands (`uv run pytest`, `uv run ruff check .`,
   `uv run ruff format --check .`) and report the actual output.
4. Open a PR to `main`, merge with `--no-ff` once it's green.
5. Tag the merge commit on `main` and push the tag:
   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "Release X.Y.Z"
   git push origin vX.Y.Z
   ```
6. Trigger the `Publish` workflow.

Nothing else needs to change — `lintle --version`, the `report.py` headers, and
any downstream `from lintle import __version__` import all pick the new value up
from `pyproject.toml` automatically.
