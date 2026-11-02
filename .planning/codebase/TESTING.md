---
last_mapped_commit: bf834f78234eaa5b6f879c0bff96587181b4d723
last_mapped_at: 2026-11-02
---
# Testing Patterns

**Analysis Date:** 2026-11-02

## Test Framework

**Runner:**
- `pytest` `>=9.0,<10` (dev group in `pyproject.toml`).
- Parallel execution: `pytest-xdist` `>=3,<4`. The default `addopts` in `pyproject.toml` is `-m 'not slow' -n auto`.
- Config: `[tool.pytest.ini_options]` in `pyproject.toml`. `testpaths = ["tests"]`.
- Markers: `slow` is registered in `pyproject.toml` and opt-in. Throughput tests in `tests/test_pipeline_throughput.py` use it.

**Assertion Library:**
- Plain `assert` statements (pytest rewriting). No assertion library beyond pytest.
- Exception checks use `pytest.raises(ExcType, match="...")`. Example: `tests/test_diagnostics.py:182`.

**Property-based testing:**
- `hypothesis` `>=6,<7` (dev group). Used in three files: `tests/test_tle.py`, `tests/test_repair.py`, and `tests/test_epoch.py`. Pattern: `@given(...)` with `@settings(suppress_health_check=[HealthCheck.too_slow], ...)` when generation is expensive. Examples: `tests/test_tle.py:174`, `tests/test_tle.py:192`, `tests/test_repair.py:277`.

**Coverage:**
- `pytest-cov` `>=7.1,<8`.

**Lint of tests:**
- Tests are linted and formatted by the same `ruff` config as source. Test files are not excluded.

**Run Commands:**
```bash
uv run pytest                                              # Full suite (parallel, excludes slow)
uv run pytest tests/test_tle.py                            # One file
uv run pytest tests/test_tle.py::TestComputeChecksum       # One class
uv run pytest -k "checksum"                                # Expression match
uv run pytest -x                                           # Stop on first failure
uv run pytest -m slow                                      # Opt-in throughput tests
uv run pytest -n0 --pdb tests/test_tle.py                  # Debug (pdb needs no xdist)
uv run pytest --cov=lintle --cov-report=term-missing --cov-branch   # Coverage
```

Note: `--pdb` is incompatible with `pytest-xdist`. Pass `-n0` to debug.

## Test File Organization

**Location:**
- All tests live in a single top-level directory, `tests/`. Tests are not co-located with source.
- Sub-directory exists only for data: `tests/fixtures/`.

**Naming:**
- `tests/test_<module>.py` mirrors `src/lintle/<module>.py`. Examples: `tests/test_tle.py` → `src/lintle/tle.py`; `tests/test_cli_progress.py` → `src/lintle/cli_progress.py`.
- Cross-cutting suites have descriptive names: `tests/test_integration.py` (end-to-end), `tests/test_freeze.py` (byte-freeze), `tests/test_oracle.py` (sgp4 cross-check), `tests/test_pipeline_throughput.py` (slow), `tests/test_verify_stream.py`.

**Structure:**
```
tests/
├── __init__.py                 # empty, makes tests a package
├── conftest.py                 # shared line1 / line2 fixtures
├── fixtures/
│   ├── freeze-e2e.golden.json  # sha256 map for test_freeze.py
│   ├── report-envelope-v2.golden.json
│   └── report-envelope-v3.golden.json
└── test_<module>.py           # one per src module, plus cross-cutting suites
```

## Test Structure

**Suite Organization:**
Tests group into `Test*` classes, one per unit or behaviour. Methods are `test_<behaviour>`. Docstrings and comments explain the invariant under test.

From `tests/test_tle.py`:
```python
class TestComputeChecksum:
    def test_checksum_of_canonical_line1(self, line1):
        # NORAD 00005 line 1 checksum digit (column 69) is 3.
        assert tle.compute_checksum(line1) == 3
        assert tle.compute_checksum(line1) == int(line1[68])

    def test_minus_sign_counts_as_one(self):
        # compute_checksum sums only the first 68 characters.
        assert tle.compute_checksum("-" * 10 + " " * 58) == 0  # 10 % 10
```

Counts: about 1074 `def test_` functions and 239 `Test*` classes across `tests/`.

**Module docstring:** Every test file opens with a docstring naming the module under test, e.g. `"""Tests for lintle.tle — the TLE validator."""`. Longer suites explain the guarantee they pin (`tests/test_term.py`, `tests/test_freeze.py`).

**Patterns:**
- **Setup:** Pytest fixtures and `tmp_path`. Helper methods on the test class build inputs (`self._write_one_bad_record(tmp_path, line1, line2)` in `tests/test_thresholds.py`; `self._make_src(tmp_path, line1, line2)` in `tests/test_run_planning.py`).
- **Teardown:** Handled by `tmp_path` and context managers. No manual teardown is common.
- **Assertions:** Plain `assert`. Compare full strings where the exact output is the contract:
  ```python
  assert out.err == "error: disk full\n"
  assert out.out == ""  # never leaks to stdout
  ```
- **Negative cases:** Test the rule ID or message substring, not the whole message, unless the message is a contract.

## Shared Fixtures

**`tests/conftest.py`** provides two canonical, known-good TLE lines:
```python
CANONICAL_LINE1 = "1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753"
CANONICAL_LINE2 = "2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667"

assert len(CANONICAL_LINE1) == 69, len(CANONICAL_LINE1)   # fail at collection time
assert len(CANONICAL_LINE2) == 69, len(CANONICAL_LINE2)

@pytest.fixture
def line1():
    """A valid 69-character TLE line 1."""
    return CANONICAL_LINE1
```

Use `line1` and `line2` in any test that needs a valid record. Build bad records by slicing them, e.g. `bad_line1 = line1[:68] + "9"` (`tests/test_integration.py`).

**Fixture location:**
- Shared fixtures: `tests/conftest.py`. Per-file fixtures stay in the test module.
- Golden data: `tests/fixtures/*.golden.json`. Regenerated only on purpose (see Freeze tests).

## Mocking

**Framework:** `pytest`'s `monkeypatch` fixture. The `unittest.mock` module is not used in the suite.

**Patterns:**
```python
# tests/test_cli.py
monkeypatch.setattr(cli.os, "access", lambda _p, _m: False)
monkeypatch.setattr(cli.shutil, "disk_usage", lambda _path: _Usage())
monkeypatch.setattr(worker_pool.concurrent.futures, "as_completed", _interrupt)
monkeypatch.setattr(sys, "stdout", buf)
```
- Patch the module attribute the code under test calls, not the source. Call sites use `module.function`, so `monkeypatch.setattr(cli.shutil, ...)` works.
- Replace a boom with a function that raises, to test the error path: `monkeypatch.setattr(worker_pool.resume, "write_checkpoint", _boom)` in `tests/test_cli.py:639`.
- Small fakes are local classes, e.g. `_Usage` with the attributes the code reads.

**What to Mock:**
- Platform and OS calls that cannot be reproduced: `shutil.disk_usage`, `os.access`, signal delivery.
- Interruption: replace `concurrent.futures.as_completed` with a function that raises `KeyboardInterrupt`, to test the shutdown path.
- Output streams: `capsys` for stderr and stdout, `monkeypatch.setattr(sys, "stdout", ...)` where a real buffer is needed.

**What NOT to Mock:**
- The validator. `tle.py` is pure and fast. Tests call it directly with real records.
- The filesystem. Tests write real files under `tmp_path`. `test_fsutil.py` and `test_chunking.py` depend on real atomic rename and fsync behaviour.
- The `sgp4` oracle. `tests/test_oracle.py` cross-checks against the real library.
- Output bytes. Golden and freeze tests compare real bytes.

## Fixtures and Factories

**Test Data:**
Records are built from the canonical lines and slices. Multi-record files are written to `tmp_path` with `write_bytes`:
```python
src = tmp_path / "tle2099.txt"
src.write_bytes((line1 + "\n" + line2 + "\n" + ...).encode("ascii"))
```

Helper builders are private methods or module functions. Examples:
- `write_import_tree(tmp_path, recs((100, 1.0)), 10)` in `tests/test_extract.py`.
- `build_tree(tmp_path, pairs)` in `tests/test_dedup.py`.
- `self._two_file_src(tmp_path, line1, line2)` in `tests/test_run_planning.py`.

**Golden and freeze data:**
- `tests/fixtures/freeze-e2e.golden.json`: sha256 per artifact after a full `clean -> verify -> dedup -> extract` run. Canonicalises wall clock, timings, versions, and absolute tmp paths. Regenerate deliberately:
  ```bash
  LINTLE_FREEZE_UPDATE=1 uv run pytest tests/test_freeze.py -n0
  ```
  The diff on the golden file is the reviewable record of what changed.
- `tests/fixtures/report-envelope-v2.golden.json` and `v3`: schema pins for the `report.json` envelope.

**Location:**
- `tests/fixtures/`. Keep fixtures small and committed. Corpus files never go here. The `data/` tree is git-ignored and must not be read by tests.

## Coverage

**Requirements:** None enforced. No `fail_under` is set in `pyproject.toml`. CI runs `uv run pytest` without coverage.

**View Coverage:**
```bash
uv run pytest --cov=lintle --cov-report=term-missing --cov-branch
```
This reports line and branch coverage. Uncovered lines appear in the `Missing` column.

## Test Types

**Unit Tests:**
- Scope: one function or class. Examples: `TestComputeChecksum` (`tests/test_tle.py`), `TestEmit` (`tests/test_term.py`), `TestNormalizeEdges` (`tests/test_repair.py`).
- Pure validators need no fixtures beyond the canonical lines.

**Property Tests:**
- `hypothesis` checks invariants over generated input. Example: `test_content_free_agrees_with_full_reduction` in `tests/test_repair.py` asserts that `is_content_free` and `normalize_edges` never disagree.
- Use `@settings(suppress_health_check=[HealthCheck.too_slow], ...)` for expensive strategies.

**Integration Tests:**
- Scope: the pipeline and CLI on real files. `tests/test_integration.py` runs a golden mixed-file case and idempotence checks. `tests/test_pipeline.py` covers per-file processing. `tests/test_cli.py` drives `cli.main` with argv lists.
- Tests call `cli.main([...])` and check the exit code and output files.

**Byte-freeze and Golden Tests:**
- `tests/test_freeze.py` and the `report-envelope-*.golden.json` files. A failure means output bytes moved. Decide whether the change was intended, then regenerate.

**Cross-check (Oracle) Tests:**
- `tests/test_oracle.py` checks a known-good TLE against the trusted `sgp4` parser. This is the only test that touches `sgp4` outside `verify`.

**Throughput Tests (slow):**
- `tests/test_pipeline_throughput.py`. Marked `@pytest.mark.slow`. Excluded by default. Run with `uv run pytest -m slow`.

**E2E Tests:**
- Not a separate framework. The byte-freeze test is the end-to-end check.

## Common Patterns

**Exception testing:**
```python
with pytest.raises(ValueError, match="observed exceeds"):
    ...
```
Use `match=` with a stable substring. Do not match full messages unless they are a contract.

**Output testing:**
```python
def test_error_prefixes_message_and_adds_newline(self, capsys):
    term.error("disk full")
    out = capsys.readouterr()
    assert out.err == "error: disk full\n"
    assert out.out == ""
```
Use `capsys.readouterr()` once per check, and assert both `.out` and `.err` where stream separation is the contract.

**Filesystem testing:**
```python
def test_...(self, tmp_path):
    out = tmp_path / "out"
    stats = pipeline.process_file(str(src), str(out), "clean")
```
Pass `str(...)` to functions that take string paths. Use `tmp_path` always. Never write to `data/`.

**Lock testing:**
```python
with fsutil.out_dir_lock(str(tmp_path)):
    ...
```
Lock tests use a real advisory flock in `tmp_path`.

**Async testing:**
- Not used. The codebase is synchronous and uses `concurrent.futures` process pools. Pool behaviour is tested by monkeypatching `as_completed` and `write_checkpoint`.

**Parametrize:**
- `@pytest.mark.parametrize` is rare (three uses). Prefer one assertion per named case inside a `Test*` class, or a loop over a tuple when the cases share one assertion:
  ```python
  for line in ("", " ", "\r", "\\", "\\\\", " \\ ", "\r\\", "\t\\\r "):
      assert repair.is_content_free(line), line
  ```
  Pass the input as the assertion message so a failure names the case.

## Adding New Tests

- New module `src/lintle/foo.py`: add `tests/test_foo.py` with a module docstring and one `Test*` class per unit.
- New validation or repair rule: add the case to `tests/test_tle.py` or `tests/test_repair.py`, and pin the rule ID in `tests/test_diagnostics.py`. Add an example to `src/lintle/explain_examples.py`. `tests/test_explain.py` validates every example against the live validator.
- New output file or byte change: expect `tests/test_freeze.py` to fail. Regenerate the golden file only after you confirm the change is intended.
- New non-trivial logic (a branch, loop, or parser): include at least one test. Trivial changes need none.
- Long TLE literal: use `# noqa: E501 — a TLE line is a fixed 69-column record`, as in `tests/conftest.py`.

## Verification Chain

Run all three before reporting success, and report the actual output:
```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`, then `pytest` on Python 3.14, using `uv sync --locked`.

## Documentation Drift

- `CONTRIBUTING.md` states Python 3.11+ and lists `sgp4` as dev-only. `pyproject.toml` requires `>=3.14` and lists `sgp4` as a runtime dependency. Treat `pyproject.toml` as the source of truth.
- `CONTRIBUTING.md` lists `test_` files in a table that omits many current suites (for example `test_verify.py`, `test_extract.py`, `test_dedup.py`).

---

*Testing analysis: 2026-11-02*
