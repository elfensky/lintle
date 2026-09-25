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

The runtime's one third-party dependency is **`rich`** (`>=15,<16`, terminal rendering for
`clean`). Further additions are governed by a *relaxed* policy (2026-05-31): a popular,
actively-maintained library that genuinely reduces the code we'd otherwise own may be adopted
where it makes sense — gated only by the hard correctness invariants (one validator,
constant-memory streaming, byte-deterministic *unstyled* structured/stdout output, the
atomic-durable commit + advisory-flock out-dir lock, `sgp4`-never-at-runtime). The canonical rule and
considered/deferred table live in [`ARCHITECTURE.md` §7](ARCHITECTURE.md#7-runtime-dependency-policy).
`sgp4` is a dev-only test oracle and must never be imported at runtime.

**Pin every dependency `>=current_major,<next_major`** (runtime and dev alike). Minor and patch
releases resolve automatically; **major upgrades are manual, one at a time**, with the verification
chain green + a changelog note (for `0.x` deps the leftmost non-zero component is the major —
`ruff>=0.15,<0.16`). Commit the updated `uv.lock` with the bump. See `ARCHITECTURE.md` §7.

## Running

```bash
uv run lintle clean             # Write cleaned output to data/output/
uv run lintle report            # Re-render the last run's summary from report.json
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

This is a Python 3.14 codebase and uses modern idioms. The `UP`/`SIM` rule sets
auto-enforce most of them (f-strings, `X | None` unions, builtin generics,
`contextlib.suppress`, PEP 758 `except A, B:`). Three conventions Ruff does *not*
enforce — please apply them by hand so new code matches the existing style:

- **`match`** for 3-or-more-way type/shape dispatch (not `isinstance`/`elif` chains).
- **`@dataclasses.dataclass(slots=True)`** on every dataclass (`frozen=True` when immutable).
- **`collections.Counter`** for tally/accumulate loops (not `d[k] = d.get(k, 0) + 1`);
  convert back with `dict()` at byte-deterministic output boundaries to preserve key order.

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
  One path in: every change — feature, refactor, chore or one-line fix — goes
  on its own branch in its own worktree and lands by PR via
  **rebase-and-merge**, so `develop` stays linear (no merge bubbles).
- **`main`** is the release branch. Each release is a single merge commit on
  `main` whose tree is develop's release-point tree and whose second parent is
  develop's release-point commit. The second parent gives graph visualizers a
  "branched-from" edge from each release on `main` back to its origin on
  `develop`. Use `git log --first-parent main` to see only the releases.
  Releases are annotated tags on `main`. There is no separate release branch.
  **Never commit directly to `main`** — a release lands by a release PR from
  `develop`, merged with a merge commit (see § Versioning § Release flow). A
  ruleset makes `main` PR-only and merge-only.

- Branch names: `feature/<desc>`, `refactor/<desc>`, `fix/<desc>`,
  `chore/<desc>` — lowercase, hyphens. The release-prep branch is
  `chore/release-X.Y.Z` (see § Versioning § Release flow); it carries the
  version bump + dated `CHANGELOG.md` section.
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `test:`, `refactor:`, `style:`, `chore:`, on every individual
  commit inside the branch.
- Run the verification commands above before opening or merging a PR.
- Land PRs to `develop` via **"Rebase and merge"** in the GitHub UI (or
  `gh pr merge --rebase --delete-branch` locally). Do not use "Create a merge
  commit" — merge bubbles fragment the visualizer into apparent multiple
  develop lanes. Do not use "Squash and merge" either — keep the individual
  commits readable in `git log develop`. The one merge commit is the release PR
  into `main` (§ Versioning § Release flow).

### Worktrees — one per session

Every session works in its own worktree under `.worktrees/`, created `--lock` from
`origin/develop`; the main checkout stays on `develop` and only pulls. The commands are in
[AGENTS.md § Worktrees](AGENTS.md#worktrees--one-lane-always). lintle specifics:

```bash
uv sync                  # a new worktree has its own .venv/
ln -s ../../data data    # the ~30 GB corpus stays in the main checkout; one copy on disk
```

When running `lintle clean` from multiple worktrees in parallel, pass
`--out-dir <local-dir>` to each — the default `data/output/` is shared through
the symlink and concurrent runs will collide.

If `gh pr merge --rebase --delete-branch` errors on its local step inside a worktree
("'develop' is already used by worktree at ..."), the merge has still landed. Running it from
outside the repo avoids the error: `(cd /tmp && gh pr merge <N> --rebase --delete-branch --repo elfensky/lintle)`.

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

1. On a `chore/release-X.Y.Z` branch in its own worktree off `origin/develop`,
   bump `version` in `pyproject.toml`.
2. Add a new `## [X.Y.Z] - YYYY-MM-DD` section at the top of `CHANGELOG.md` with
   `### Added` / `### Changed` / `### Fixed` subsections (see Keep a Changelog).
3. Run the verification commands (`uv run pytest`, `uv run ruff check .`,
   `uv run ruff format --check .`) and report the actual output.
4. Open a PR to `develop`, land via **"Rebase and merge"** once it's green.
5. Open the release PR from `develop` to `main` and merge it with a **merge
   commit** — the one place this repo uses one. GitHub builds the same commit the
   old hand-built `git commit-tree` recipe did: parents are `main`'s tip and
   `develop`'s release-point, and the tree is `develop`'s release-point tree
   (because `main`'s tree always equals the previous release-point's tree, the
   merge has nothing to combine). That keeps the visible "branched-from" edge
   from `main` to `develop` at each release and the release tree byte-identical
   to what gets published — and CI plus the version gate now run on the release
   before it lands:
   ```bash
   gh pr create --base main --head develop --title "Release vX.Y.Z" --body "Release vX.Y.Z"
   gh pr checks --watch --required
   gh pr merge --merge --subject "Release vX.Y.Z"   # never --delete-branch: the head is develop
   git fetch origin
   git tag -a vX.Y.Z origin/main -m "Release vX.Y.Z"
   git push origin vX.Y.Z
   ```
   Merge it before anything else lands on `develop`, or the release takes that
   too. To see only the release commits on `main` (skipping the develop history
   reachable via second parents), use `git log --first-parent main`.

   **The merge to `main` auto-publishes to TestPyPI.** `publish.yml` fires on every
   push to `main` (main only ever takes release merges, so this is per-release,
   not per-merge) and uploads the built sdist + wheel to **TestPyPI** via
   **Trusted Publishing (OIDC)** — no API tokens are stored or needed; GitHub's
   signed OIDC identity is the credential. Watch the run under *Actions → Publish*
   and confirm it's green before continuing.
6. Create the GitHub release:
   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-from-tag --latest
   ```
7. **Publish to production PyPI — a deliberate manual step.** A push to `main`
   *never* touches prod PyPI; only an explicit run does. Once you've validated the
   TestPyPI artifact (e.g. `pip install -i https://test.pypi.org/simple/ lintle==X.Y.Z`
   and a smoke run), trigger the `Publish` workflow via **workflow_dispatch** with
   `target: pypi` (Actions → Publish → *Run workflow*). It re-runs the full
   verification, rebuilds, and uploads to PyPI over the same Trusted-Publishing
   (OIDC) path. PyPI uploads are permanent, so this stays a human action.

Nothing else needs to change — `lintle --version`, the `report.py` headers, and
any downstream `from lintle import __version__` import all pick the new value up
from `pyproject.toml` automatically.
