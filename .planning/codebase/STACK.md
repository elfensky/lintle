---
last_mapped_commit: bf834f78234eaa5b6f879c0bff96587181b4d723
last_mapped_at: 2026-11-02
---
# Technology Stack

**Analysis Date:** 2026-11-02

## Languages

**Primary:**
- Python 3.14 (`requires-python = ">=3.14"` in `pyproject.toml`; pinned in `.python-version`) - all package code under `src/lintle/` and tests under `tests/`

**Secondary:**
- YAML - CI and publish workflows in `.github/workflows/ci.yml`, `.github/workflows/publish.yml`, `.github/workflows/version.yml`, `.github/dependabot.yml`
- TOML - build, lint, and test config in `pyproject.toml`

## Runtime

**Environment:**
- CPython 3.14 (`.python-version` = `3.14`; CI matrix in `.github/workflows/ci.yml` = `["3.14"]`)
- Multi-process worker pool (`concurrent.futures` / `multiprocessing`) in `src/lintle/worker_pool.py`
- Platform code: `fcntl` (flock, `F_FULLFSYNC` on macOS) in `src/lintle/fsutil.py`. POSIX-only paths; no Windows branch detected.

**Package Manager:**
- uv (no version pinned in the repo; CI uses `astral-sh/setup-uv@v9.0.0`)
- Lockfile: present (`uv.lock`, lock format `version = 1`). CI runs `uv sync --locked`.
- Build backend: hatchling (`[build-system]` in `pyproject.toml`; wheel packages = `src/lintle`)

## Frameworks

**Core:**
- None. The CLI uses stdlib `argparse` (`src/lintle/cli.py`). No web or service framework.
- `rich` 15.0.0 - terminal rendering: live tables, progress, panels, prompts. Used by `src/lintle/term.py`, `src/lintle/cli_progress.py`, `src/lintle/summary.py`, `src/lintle/wizard.py`.

**Testing:**
- `pytest` 9.1.1 - test runner (`testpaths = ["tests"]` in `pyproject.toml`)
- `pytest-xdist` 3.8.0 - parallel runs (`addopts = "-m 'not slow' -n auto"`)
- `pytest-cov` 7.1.0 - coverage (`uv run pytest --cov=lintle`)
- `hypothesis` 6.164.0 - property-based tests
- `sgp4` 2.27 is also used as a test oracle (`tests/test_oracle.py`)

**Build/Dev:**
- `ruff` 0.16.1 (dev group, `>=0.15,<0.17`) - lint (`E`, `F`, `I`, `UP`, `B`, `SIM`) and format; `line-length = 88`, `target-version = "py314"`; `extend-exclude = ["docs/"]`
- `hatchling` - build backend for sdist/wheel (`uv build` in `.github/workflows/publish.yml`)
- `uv` - environment, lock, build, and publish tool

## Key Dependencies

**Critical (runtime, declared in `pyproject.toml`):**
- `sgp4` 2.27 (`>=2.25,<3`) - SGP4 orbit propagation. Imported only by `src/lintle/verify/orbit.py` (`from sgp4.api import SGP4_ERRORS, Satrec`), lazily, when `lintle verify --orbit` runs. The clean/validate/repair path must not import it (enforced by an import-graph test).
- `rich` 15.0.0 (`>=15,<16`) - terminal UI for every command that writes to a TTY.
- `humanize` 4.16.0 (`>=4,<5`) - human-readable durations and sizes. Confined to `src/lintle/summary.py`; never used for structured output.

**Dev-only (dependency group `dev`):**
- `pytest`, `pytest-cov`, `pytest-xdist`, `hypothesis`, `ruff` (see above)

**Transitive (from `uv.lock`):**
- `markdown-it-py` 4.2.0, `mdurl` 0.1.2, `pygments` 2.20.0 (via rich)
- `colorama` 0.4.6, `coverage` 7.14.1, `execnet` 2.1.2, `iniconfig` 2.3.0, `packaging` 26.2, `pluggy` 1.6.0, `sortedcontainers` 2.4.0

**Stdlib modules in use (no install needed):**
- `json`, `hashlib`, `heapq`, `statistics`, `fractions`, `datetime`, `calendar`, `tempfile`, `shutil`, `fcntl`, `signal`, `socket` (hostname only, `src/lintle/fsutil.py`), `multiprocessing`, `concurrent.futures`, `threading`, `queue`, `importlib.metadata`

Policy: runtime dependencies follow the relaxed rule in `ARCHITECTURE.md` section 7. Only the three runtime packages above are accepted.

## Configuration

**Environment variables read by the code:**
- `LINTLE_DEBUG` - when set, emits full tracebacks to stderr (`src/lintle/cli.py`, `src/lintle/worker_pool.py`)
- `CI` and `NONINTERACTIVE` - when set, disables interactive prompts (`src/lintle/term.py`, `is_interactive`)
- No required env vars. No `.env` loading detected.

**Project config files:**
- `./.lintle.json` - optional, per-user, git-ignored (`.gitignore`). Remembers source and output dirs. Read by `src/lintle/config.py` with stdlib `json`.

**Build and tooling:**
- `pyproject.toml` - project metadata, dependencies, dependency groups, ruff, pytest, and the `testpypi` index (`explicit = true`)
- `uv.lock` - locked dependency graph
- `.python-version` - `3.14`
- `.github/dependabot.yml` - weekly updates for the `uv` and `github-actions` ecosystems

## CI/CD Toolchain

- `.github/workflows/ci.yml` - on push and PR to `develop` and `main`: `uv sync --locked`, `ruff check`, `ruff format --check`, then `pytest`. Runs on `ubuntu-latest`, Python 3.14.
- `.github/workflows/publish.yml` - on push to `main`: publish to TestPyPI. On manual dispatch: publish to TestPyPI or PyPI. Uses `uv publish --trusted-publishing always`.
- `.github/workflows/version.yml` - on PR to `main` touching `src/**` or `pyproject.toml`: fails if the version is unchanged.
- Actions versions: `actions/checkout@v7`, `astral-sh/setup-uv@v9.0.0`.

## Platform Requirements

**Development:**
- Python 3.14 and `uv`. Run `uv sync`, then `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`.
- Memory: constant-memory streaming is a design rule (`CLAUDE.md`). Corpus input is about 30 GB under `data/` (git-ignored).
- Disk: output is roughly the input size. The CLI refuses to run without free space (`src/lintle/run_planning.py`).
- Lock support: `fcntl.flock` for the out-dir lock. Use a POSIX system.

**Production:**
- Distributed as a PyPI package named `lintle` (console script `lintle = "lintle.cli:main"`).
- Published by CI through PyPI Trusted Publishing (OIDC). No API tokens are stored.
- Used as a local CLI. No server deployment.

## Notes for Maintainers

- `CONTRIBUTING.md` line 5 states "Python 3.11+". `pyproject.toml` and `README.md` require 3.14. Treat 3.14 as authoritative.

---

*Stack analysis: 2026-11-02*
