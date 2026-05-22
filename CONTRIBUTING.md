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

- **Never commit directly to `main`.** Branch for every change.
- Branch names: `feature/<desc>`, `bugfix/<desc>`, `chore/<desc>` — lowercase, hyphens.
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
  `docs:`, `test:`, `refactor:`, `style:`, `chore:`.
- Open a pull request to `main`; run the verification commands above before merging.

## Versioning

Semantic versioning (`MAJOR.MINOR.PATCH`). The version is tracked in two places that must
stay in sync: `pyproject.toml` (`version`) and `src/lintle/__init__.py` (`__version__`).
Record every release in `CHANGELOG.md`.
