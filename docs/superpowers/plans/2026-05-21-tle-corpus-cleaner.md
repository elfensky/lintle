# TLE Corpus Validator & Cleaner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `uv`-managed Python CLI that validates TLE corpus files against the TLE specification and emits cleaned, guaranteed-valid output, quarantining unfixable records to a reportable sidecar.

**Architecture:** One validator core (`tle.py`) defines "a perfect TLE record"; the cleaner (`repair.py`) applies conservative, speculative fixes and only commits them when the validator confirms the result. The `pipeline.py` streams multi-gigabyte files in constant memory, pairing lines into records; `report.py` renders the sidecar and summaries; `cli.py` drives `validate`/`clean` subcommands with per-file parallelism.

**Tech Stack:** Python 3.11+, standard library only at runtime. `uv` for project/venv management. Dev dependencies: `pytest` (test runner) and `sgp4` (test oracle only — never imported at runtime).

**Reference spec:** `docs/superpowers/specs/2026-05-21-tle-corpus-cleaner-design.md`. Section numbers below (§N) refer to it.

**Commit convention:** End every commit message with the trailer `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`. The commands below show short messages for brevity; append that trailer.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `pyproject.toml` | `uv` project metadata, console script `tle-clean`, dev deps. |
| `src/tlekit/__init__.py` | `__version__` and the `stem()` filename helper. |
| `src/tlekit/tle.py` | Core validation: checksum, column layout, semantic ranges, record pairing. Pure, no I/O. |
| `src/tlekit/repair.py` | Speculative fixes; `repair_line` and `process_record`. Pure, no I/O. |
| `src/tlekit/report.py` | `FileStats`/`RejectEntry` data, `.broken.txt` writer, summary formatting. |
| `src/tlekit/pipeline.py` | Binary streaming reader, prefix-driven pairing state machine, per-file routing. |
| `src/tlekit/cli.py` | Argument parsing, file discovery, `ProcessPoolExecutor` fan-out, exit codes. |
| `src/tlekit/__main__.py` | `python -m tlekit` entry point. |
| `tests/conftest.py` | Shared `line1`/`line2` fixtures (a canonical known-good TLE). |
| `tests/test_*.py` | One test module per source module, plus `test_integration.py`. |

---

## Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `src/tlekit/__init__.py`
- Create: `tests/conftest.py`
- Move: existing `tle*.txt` + `TLEs.zip` from repo root into `data/source/`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "tlekit"
version = "0.1.0"
description = "Validator and cleaner for Two-Line Element (TLE) corpus files"
requires-python = ">=3.11"
dependencies = []

[project.scripts]
tle-clean = "tlekit.cli:main"

[dependency-groups]
dev = ["pytest>=8.0", "sgp4>=2.23"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/tlekit"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Create `src/tlekit/__init__.py`**

```python
"""tlekit — validator and cleaner for Two-Line Element (TLE) corpus files."""

__version__ = "0.1.0"


def stem(filename):
    """Return a filename without its trailing ``.txt`` extension.

    ``"tle2022.txt"`` -> ``"tle2022"``; other names are returned unchanged.
    """
    return filename[:-4] if filename.endswith(".txt") else filename
```

- [ ] **Step 3: Create `tests/conftest.py`**

The canonical record is NORAD 00005 (Vanguard 1) from the official SGP4 test suite — both lines are exactly 69 characters and both checksums are valid.

```python
"""Shared pytest fixtures: a canonical, known-good TLE record."""

import pytest

CANONICAL_LINE1 = "1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753"
CANONICAL_LINE2 = "2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667"

# Fail loudly at collection time if either constant was mistranscribed.
assert len(CANONICAL_LINE1) == 69, len(CANONICAL_LINE1)
assert len(CANONICAL_LINE2) == 69, len(CANONICAL_LINE2)


@pytest.fixture
def line1():
    """A valid 69-character TLE line 1."""
    return CANONICAL_LINE1


@pytest.fixture
def line2():
    """A valid 69-character TLE line 2."""
    return CANONICAL_LINE2
```

- [ ] **Step 4: Move the corpus into `data/source/`**

The spec (§4.2) expects raw inputs under `data/source/`. The files currently sit at the repo root; `mv` within the same volume is instant. Run from the repo root:

```bash
mkdir -p data/source data/output
mv tle*.txt TLEs.zip data/source/ 2>/dev/null || true
ls data/source/ | head
```
Expected: the `tle*.txt` files (and `TLEs.zip`) are listed under `data/source/`. `data/` is already git-ignored.

- [ ] **Step 5: Verify the project builds and imports**

Run: `uv run python -c "import tlekit; print(tlekit.__version__, tlekit.stem('tle2022.txt'))"`
Expected: `0.1.0 tle2022`

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/tlekit/__init__.py tests/conftest.py
git commit -m "chore: scaffold tlekit uv project"
```

---

## Task 2: `tle.py` — checksum

**Files:**
- Create: `src/tlekit/tle.py`
- Test: `tests/test_tle.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tle.py`:

```python
from tlekit import tle


def test_checksum_of_canonical_line1(line1):
    # NORAD 00005 line 1 checksum digit (column 69) is 3.
    assert tle.compute_checksum(line1) == 3
    assert tle.compute_checksum(line1) == int(line1[68])


def test_checksum_of_canonical_line2(line2):
    assert tle.compute_checksum(line2) == int(line2[68])


def test_minus_sign_counts_as_one():
    # compute_checksum sums only the first 68 characters.
    assert tle.compute_checksum("-" * 10 + " " * 58) == 0   # 10 % 10
    assert tle.compute_checksum("-" * 7 + " " * 61) == 7


def test_non_digit_non_minus_counts_as_zero():
    assert tle.compute_checksum("ABCDE.+ " * 8 + "    ") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tle.py -v`
Expected: FAIL — `AttributeError: module 'tlekit.tle' has no attribute 'compute_checksum'` (or `ModuleNotFoundError` if the file does not exist yet).

- [ ] **Step 3: Write minimal implementation**

Create `src/tlekit/tle.py`:

```python
"""Core TLE validation: the single definition of a "perfect" record.

Pure functions only — no I/O. Column references use 1-indexed TLE column
numbers in prose; Python slices below are 0-indexed.
"""

LINE_LENGTH = 69


def compute_checksum(line):
    """Return the mod-10 TLE checksum of the first 68 characters of ``line``.

    Each digit adds its value, each ``-`` adds 1, every other character
    (letters, spaces, ``.``, ``+``) adds 0. The result is ``sum % 10``.
    """
    total = 0
    for ch in line[:68]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tle.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tlekit/tle.py tests/test_tle.py
git commit -m "feat: add TLE mod-10 checksum"
```

---

## Task 3: `tle.py` — column-layout validation

**Files:**
- Modify: `src/tlekit/tle.py`
- Test: `tests/test_tle.py`

This task adds `_check_columns(body, lineno)`, which validates the 68 data columns (columns 1–68) of a TLE line against fixed-position rules.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tle.py`:

```python
def test_valid_line1_passes_column_checks(line1):
    assert tle._check_columns(line1[:68], 1) == []


def test_valid_line2_passes_column_checks(line2):
    assert tle._check_columns(line2[:68], 2) == []


def test_wrong_body_length_reported():
    errs = tle._check_columns("1 00005U", 1)
    assert errs and "length" in errs[0]


def test_bad_line_number_prefix(line1):
    body = "9" + line1[1:68]
    assert any("line number" in e for e in tle._check_columns(body, 1))


def test_missing_separator_space(line2):
    # Column 9 must be a space on line 2.
    body = line2[:8] + "X" + line2[9:68]
    assert tle._check_columns(body, 2)


def test_letter_in_digit_only_field_rejected(line1):
    # Epoch year (columns 19-20) must be digits.
    body = line1[:18] + "X" + line1[19:68]
    assert tle._check_columns(body, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tle.py -k column -v`
Expected: FAIL — `AttributeError: module 'tlekit.tle' has no attribute '_check_columns'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/tlekit/tle.py`:

```python
# --- Column-layout rules -------------------------------------------------
# Slices below are 0-indexed half-open ranges into the 68-character body.

_DIGIT = "0123456789"
_DIGIT_SPACE = "0123456789 "
_SIGN = " +-"
_EXP_SIGN = "+-"
_ALNUM_SPACE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "

# Single-character positions: (index, allowed_chars, description).
_LINE1_CHARS = [
    (0, "1", "line number"),
    (1, " ", "column 2 separator"),
    (7, "UCS", "classification"),
    (8, " ", "column 9 separator"),
    (17, " ", "column 18 separator"),
    (23, ".", "epoch decimal point"),
    (32, " ", "column 33 separator"),
    (33, _SIGN, "first-derivative sign"),
    (34, ".", "first-derivative decimal point"),
    (43, " ", "column 44 separator"),
    (44, _SIGN, "second-derivative mantissa sign"),
    (50, _EXP_SIGN, "second-derivative exponent sign"),
    (52, " ", "column 53 separator"),
    (53, _SIGN, "B* mantissa sign"),
    (59, _EXP_SIGN, "B* exponent sign"),
    (61, " ", "column 62 separator"),
    (63, " ", "column 64 separator"),
]
# Multi-character fields: (start, end, allowed_chars, description).
_LINE1_FIELDS = [
    (2, 7, _ALNUM_SPACE, "satellite catalog number"),
    (9, 17, _ALNUM_SPACE, "international designator"),
    (18, 20, _DIGIT, "epoch year"),
    (20, 23, _DIGIT_SPACE, "epoch day-of-year"),
    (24, 32, _DIGIT, "epoch fraction"),
    (35, 43, _DIGIT, "first-derivative digits"),
    (45, 50, _DIGIT, "second-derivative mantissa"),
    (51, 52, _DIGIT, "second-derivative exponent"),
    (54, 59, _DIGIT, "B* mantissa"),
    (60, 61, _DIGIT, "B* exponent"),
    (62, 63, _DIGIT, "ephemeris type"),
    (64, 68, _DIGIT_SPACE, "element set number"),
]
_LINE2_CHARS = [
    (0, "2", "line number"),
    (1, " ", "column 2 separator"),
    (7, " ", "column 8 separator"),
    (11, ".", "inclination decimal point"),
    (16, " ", "column 17 separator"),
    (20, ".", "RAAN decimal point"),
    (25, " ", "column 26 separator"),
    (33, " ", "column 34 separator"),
    (37, ".", "argument-of-perigee decimal point"),
    (42, " ", "column 43 separator"),
    (46, ".", "mean-anomaly decimal point"),
    (51, " ", "column 52 separator"),
    (54, ".", "mean-motion decimal point"),
]
_LINE2_FIELDS = [
    (2, 7, _ALNUM_SPACE, "satellite catalog number"),
    (8, 11, _DIGIT_SPACE, "inclination integer part"),
    (12, 16, _DIGIT, "inclination fraction"),
    (17, 20, _DIGIT_SPACE, "RAAN integer part"),
    (21, 25, _DIGIT, "RAAN fraction"),
    (26, 33, _DIGIT, "eccentricity"),
    (34, 37, _DIGIT_SPACE, "argument-of-perigee integer part"),
    (38, 42, _DIGIT, "argument-of-perigee fraction"),
    (43, 46, _DIGIT_SPACE, "mean-anomaly integer part"),
    (47, 51, _DIGIT, "mean-anomaly fraction"),
    (52, 54, _DIGIT_SPACE, "mean-motion integer part"),
    (55, 63, _DIGIT, "mean-motion fraction"),
    (63, 68, _DIGIT_SPACE, "revolution number"),
]
_LINE_SPEC = {1: (_LINE1_CHARS, _LINE1_FIELDS), 2: (_LINE2_CHARS, _LINE2_FIELDS)}


def _check_columns(body, lineno):
    """Validate the fixed-position column layout of a 68-character ``body``.

    ``lineno`` is 1 or 2. Returns a list of human-readable error strings;
    an empty list means the column layout is valid.
    """
    if len(body) != 68:
        return [f"body length {len(body)}, expected 68 columns"]
    chars, fields = _LINE_SPEC[lineno]
    errors = []
    for idx, allowed, desc in chars:
        if body[idx] not in allowed:
            errors.append(
                f"column {idx + 1} ({desc}): got {body[idx]!r}, "
                f"expected one of {allowed!r}"
            )
    for start, end, allowed, desc in fields:
        if any(c not in allowed for c in body[start:end]):
            errors.append(
                f"columns {start + 1}-{end} ({desc}): "
                f"contains a character outside {allowed!r}"
            )
    return errors
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tle.py -v`
Expected: PASS (all tests, including Task 2's).

- [ ] **Step 5: Commit**

```bash
git add src/tlekit/tle.py tests/test_tle.py
git commit -m "feat: add TLE column-layout validation"
```

---

## Task 4: `tle.py` — semantic ranges and `validate_body`

**Files:**
- Modify: `src/tlekit/tle.py`
- Test: `tests/test_tle.py`

This task adds `_check_semantics` (physical range checks) and the public `validate_body`, which composes the column and semantic levels (§5.4).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tle.py`:

```python
def test_validate_body_accepts_canonical(line1, line2):
    assert tle.validate_body(line1[:68], 1) == []
    assert tle.validate_body(line2[:68], 2) == []


def test_inclination_out_of_range_rejected(line2):
    # Replace columns 9-16 with an inclination of 999.2682 degrees.
    body = line2[:8] + "999.2682" + line2[16:68]
    assert any("inclination" in e for e in tle.validate_body(body, 2))


def test_mean_motion_must_be_positive(line2):
    body = line2[:52] + "00.00000000" + line2[63:68]
    assert any("mean motion" in e for e in tle.validate_body(body, 2))


def test_column_failure_short_circuits_semantics(line1):
    # A bad prefix is a column error; semantics are not even attempted.
    errs = tle.validate_body("9" + line1[1:68], 1)
    assert errs and all("column" in e or "length" in e for e in errs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tle.py -k "validate_body or semantics or mean_motion or inclination" -v`
Expected: FAIL — `AttributeError: module 'tlekit.tle' has no attribute 'validate_body'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/tlekit/tle.py`:

```python
def _check_semantics(body, lineno):
    """Validate that numeric fields fall in their physically valid ranges.

    Assumes ``body`` already passed ``_check_columns`` for ``lineno``.
    Returns a list of error strings; empty means valid.
    """
    errors = []
    try:
        if lineno == 1:
            day = float(body[20:23] + "." + body[24:32])
            if not 0.0 < day < 367.0:
                errors.append(f"epoch day-of-year {day} outside (0, 367)")
        else:
            inc = float(body[8:16])
            if not 0.0 <= inc <= 180.0:
                errors.append(f"inclination {inc} outside [0, 180]")
            raan = float(body[17:25])
            if not 0.0 <= raan < 360.0:
                errors.append(f"RAAN {raan} outside [0, 360)")
            ecc = int(body[26:33]) / 1e7
            if not 0.0 <= ecc < 1.0:
                errors.append(f"eccentricity {ecc} outside [0, 1)")
            argp = float(body[34:42])
            if not 0.0 <= argp < 360.0:
                errors.append(f"argument of perigee {argp} outside [0, 360)")
            mean_anom = float(body[43:51])
            if not 0.0 <= mean_anom < 360.0:
                errors.append(f"mean anomaly {mean_anom} outside [0, 360)")
            mean_motion = float(body[52:63])
            if mean_motion <= 0.0:
                errors.append(f"mean motion {mean_motion} is not strictly positive")
    except ValueError:
        errors.append("a numeric field could not be parsed for semantic checks")
    return errors


def validate_body(body, lineno):
    """Validate columns 1-68 of a TLE line: column layout then semantics.

    ``lineno`` is 1 or 2. Returns a list of error strings (empty = valid).
    The checksum (column 69) is intentionally NOT checked here — see
    ``validate_line``. Semantics are only checked if the column layout is
    sound, so callers get the more fundamental error first.
    """
    errors = _check_columns(body, lineno)
    if errors:
        return errors
    return _check_semantics(body, lineno)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tle.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add src/tlekit/tle.py tests/test_tle.py
git commit -m "feat: add TLE semantic range validation"
```

---

## Task 5: `tle.py` — line and record validation

**Files:**
- Modify: `src/tlekit/tle.py`
- Test: `tests/test_tle.py`

Adds `checksum_error`, `validate_line` (full 69-char line), and `validate_record` (paired lines + catalog match).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tle.py`:

```python
def test_validate_line_accepts_canonical(line1, line2):
    assert tle.validate_line(line1, 1) == []
    assert tle.validate_line(line2, 2) == []


def test_validate_line_rejects_wrong_length(line1):
    assert tle.validate_line(line1[:68], 1)  # 68 chars -> error


def test_checksum_mismatch_detected(line1):
    bad = line1[:68] + "9"  # canonical checksum is 3
    assert any("checksum" in e for e in tle.validate_line(bad, 1))


def test_checksum_error_returns_none_when_valid(line1):
    assert tle.checksum_error(line1) is None


def test_validate_record_accepts_canonical(line1, line2):
    assert tle.validate_record(line1, line2) == []


def test_validate_record_detects_catalog_mismatch(line1, line2):
    other_body = "2 09999" + line2[7:68]
    other = other_body + str(tle.compute_checksum(other_body))
    assert any("catalog" in e for e in tle.validate_record(line1, other))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tle.py -k "validate_line or validate_record or checksum_error" -v`
Expected: FAIL — `AttributeError: module 'tlekit.tle' has no attribute 'checksum_error'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/tlekit/tle.py`:

```python
def checksum_error(line):
    """Return an error string if the column-69 checksum of a 69-char
    ``line`` is wrong or non-numeric, else ``None``.
    """
    actual = line[68]
    if not actual.isdigit():
        return f"checksum column 69 is {actual!r}, not a digit"
    expected = compute_checksum(line)
    if int(actual) != expected:
        return f"checksum mismatch: column 69 is {actual!r}, computed {expected}"
    return None


def validate_line(line, lineno):
    """Fully validate a single 69-character TLE line.

    ``lineno`` is 1 or 2. Returns a list of error strings (empty = valid):
    length, column layout, semantic ranges, and the column-69 checksum.
    """
    if len(line) != LINE_LENGTH:
        return [f"line length {len(line)}, expected {LINE_LENGTH}"]
    errors = validate_body(line[:68], lineno)
    if errors:
        return errors
    err = checksum_error(line)
    return [err] if err else []


def validate_record(line1, line2):
    """Validate a paired TLE record: each line valid, and the satellite
    catalog numbers (columns 3-7) match. Returns a list of error strings.
    """
    errors = []
    for label, line, lineno in (("line 1", line1, 1), ("line 2", line2, 2)):
        for err in validate_line(line, lineno):
            errors.append(f"{label}: {err}")
    if not errors and line1[2:7] != line2[2:7]:
        errors.append(
            f"catalog number mismatch: line 1 {line1[2:7]!r} "
            f"vs line 2 {line2[2:7]!r}"
        )
    return errors
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tle.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add src/tlekit/tle.py tests/test_tle.py
git commit -m "feat: add TLE line and record validation"
```

---

## Task 6: `tle.py` — sgp4 oracle cross-check (test only)

**Files:**
- Create: `tests/test_oracle.py`

The spec (§11) requires an asymmetric cross-check: a genuinely valid TLE must be accepted both by our validator and by the trusted `sgp4` parser. This is a dev-time fixture only — `sgp4` is never imported by runtime code.

- [ ] **Step 1: Write the test**

Create `tests/test_oracle.py`:

```python
"""Asymmetric oracle check: a known-good TLE is accepted by both our
validator and the trusted `sgp4` parser. Disagreement on a *bad* TLE is
expected (sgp4 is permissive), so only acceptance is cross-checked.
"""

from sgp4.api import Satrec

from tlekit import tle


def test_canonical_tle_accepted_by_both(line1, line2):
    assert tle.validate_record(line1, line2) == []

    sat = Satrec.twoline2rv(line1, line2)
    assert sat.error == 0  # sgp4 reports no parse/initialisation error
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_oracle.py -v`
Expected: PASS. (`uv` auto-installs the `dev` dependency group, including `sgp4`, on first `uv run`.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_oracle.py
git commit -m "test: cross-check validator against sgp4 oracle"
```

---

## Task 7: `repair.py` — `repair_line`

**Files:**
- Create: `src/tlekit/repair.py`
- Test: `tests/test_repair.py`

`repair_line` decodes one raw line, applies the speculative fixes in the fixed order from §6.6 (line-ending → leading-trim → trailing-trim → backslash-strip → checksum reconstruction), then runs full validation once. It returns `(clean_line, fixes, error, category)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_repair.py`:

```python
from tlekit import repair, tle


def test_strip_trailing_backslash(line1):
    raw = (line1 + "\\").encode("ascii")  # 70 bytes: 69 columns + '\'
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert err is None and clean == line1
    assert "trailing-backslash" in fixes


def test_reconstruct_missing_checksum(line1):
    raw = line1[:68].encode("ascii")  # 68 columns, checksum absent
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert err is None and clean == line1
    assert "reconstructed-checksum" in fixes


def test_reconstruct_with_backslash_artifact(line1):
    raw = (line1[:68] + "\\").encode("ascii")  # 69 bytes: 68 columns + '\'
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert err is None and clean == line1
    assert "trailing-backslash" in fixes and "reconstructed-checksum" in fixes


def test_crlf_normalised(line1):
    clean, fixes, err, cat = repair.repair_line((line1 + "\r").encode("ascii"), 1)
    assert err is None and clean == line1 and "crlf" in fixes


def test_checksum_mismatch_rejected(line1):
    raw = (line1[:68] + "9").encode("ascii")  # 69 chars, wrong checksum
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert clean is None and cat == "checksum-mismatch"


def test_non_ascii_byte_rejected(line1):
    clean, fixes, err, cat = repair.repair_line(line1.encode("ascii") + b"\xff", 1)
    assert clean is None and cat == "non-ascii"


def test_interior_character_missing_rejected(line1):
    # Delete an interior digit: 68 chars whose columns 1-68 fail layout.
    raw = (line1[:30] + line1[31:]).encode("ascii")
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert clean is None and cat == "interior-char-missing"


def test_wrong_length_rejected(line1):
    raw = (line1 + "XX").encode("ascii")  # 71 chars, not a known shape
    clean, fixes, err, cat = repair.repair_line(raw, 1)
    assert clean is None and cat == "wrong-length"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_repair.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tlekit.repair'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/tlekit/repair.py`:

```python
"""Speculative, validated repair of raw TLE lines and records.

Every fix is applied and then confirmed by ``tle`` validation; a fix is
committed only if the result passes. Pure functions — no I/O.
"""

from tlekit import tle

RECONSTRUCTED_CHECKSUM = "reconstructed-checksum"


def repair_line(raw, lineno):
    """Attempt to repair one raw line into a valid 69-character TLE line.

    ``raw`` is the bytes of a single line WITHOUT its ``\\n`` terminator
    (a trailing ``\\r`` may remain). ``lineno`` is 1 or 2.

    Returns ``(clean_line, fixes, error, category)``:
      * success -> ``(str, list[str], None, None)``
      * failure -> ``(None, list[str], str, str)`` where ``category`` is a
        short tag for summary aggregation.
    """
    fixes = []

    try:
        line = raw.decode("ascii")
    except UnicodeDecodeError:
        return None, fixes, "line contains a non-ASCII byte", "non-ascii"

    # Fix order is fixed (spec §6.6).
    if line.endswith("\r"):
        line = line[:-1]
        fixes.append("crlf")
    lstripped = line.lstrip(" \t")
    if lstripped != line:
        line = lstripped
        fixes.append("leading-trim")
    rstripped = line.rstrip(" \t")
    if rstripped != line:
        line = rstripped
        fixes.append("trailing-ws")
    if line.endswith("\\"):
        line = line[:-1]
        fixes.append("trailing-backslash")

    # Build a 69-character candidate.
    if len(line) == tle.LINE_LENGTH:
        candidate = line
    elif len(line) == 68:
        body_errors = tle.validate_body(line, lineno)
        if body_errors:
            return (
                None,
                fixes,
                "68-char line; columns 1-68 fail layout/semantic checks "
                "(interior character missing): " + "; ".join(body_errors),
                "interior-char-missing",
            )
        candidate = line + str(tle.compute_checksum(line))
        fixes.append(RECONSTRUCTED_CHECKSUM)
    else:
        return (
            None,
            fixes,
            f"line length {len(line)} after normalization, expected 68 or 69",
            "wrong-length",
        )

    # Single full re-validation of the final candidate (spec §4.1, §6.6).
    errors = tle.validate_line(candidate, lineno)
    if errors:
        category = "checksum-mismatch" if any(
            "checksum" in e for e in errors
        ) else "invalid-columns"
        return None, fixes, "; ".join(errors), category

    return candidate, fixes, None, None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_repair.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tlekit/repair.py tests/test_repair.py
git commit -m "feat: add speculative single-line repair"
```

---

## Task 8: `repair.py` — `process_record`

**Files:**
- Modify: `src/tlekit/repair.py`
- Test: `tests/test_repair.py`

Adds the `Accepted`/`Rejected` result types and `process_record`, which repairs both lines of a candidate and validates them as a pair.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_repair.py`:

```python
def test_process_accepts_clean_record(line1, line2):
    result = repair.process_record(line1.encode("ascii"), 10,
                                   line2.encode("ascii"), 11)
    assert isinstance(result, repair.Accepted)
    assert result.line1 == line1 and result.line2 == line2
    assert result.fixes == []


def test_process_repairs_backslash_and_checksum(line1, line2):
    raw1 = (line1[:68] + "\\").encode("ascii")  # checksumless + backslash
    raw2 = line2[:68].encode("ascii")           # checksumless
    result = repair.process_record(raw1, 4, raw2, 5)
    assert isinstance(result, repair.Accepted)
    assert result.line1 == line1 and result.line2 == line2
    assert "reconstructed-checksum" in result.fixes


def test_process_rejects_bad_line(line1, line2):
    raw1 = (line1[:68] + "9").encode("ascii")  # bad checksum
    result = repair.process_record(raw1, 4, line2.encode("ascii"), 5)
    assert isinstance(result, repair.Rejected)
    assert result.category == "checksum-mismatch"
    assert result.source_lines == [4, 5]
    assert result.raw_lines == [raw1, line2.encode("ascii")]


def test_process_rejects_catalog_mismatch(line1, line2):
    other_body = "2 09999" + line2[7:68]
    other = other_body + str(tle.compute_checksum(other_body))
    result = repair.process_record(line1.encode("ascii"), 1,
                                   other.encode("ascii"), 2)
    assert isinstance(result, repair.Rejected)
    assert result.category == "catalog-mismatch"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_repair.py -k process -v`
Expected: FAIL — `AttributeError: module 'tlekit.repair' has no attribute 'process_record'`.

- [ ] **Step 3: Write minimal implementation**

Add the import and append the rest to `src/tlekit/repair.py`. Change the existing import line at the top of the file to:

```python
import dataclasses

from tlekit import tle
```

Then append:

```python
@dataclasses.dataclass
class Accepted:
    """A record that is valid after repair. ``fixes`` lists the fix-class
    names applied across both lines (e.g. ``"trailing-backslash"``).
    """

    line1: str
    line2: str
    fixes: list


@dataclasses.dataclass
class Rejected:
    """A record that could not be safely repaired. ``raw_lines`` holds the
    original bytes for byte-faithful quarantine; ``category`` is a short
    tag for summary aggregation; ``reason`` is the human-readable detail.
    """

    raw_lines: list
    source_lines: list
    category: str
    reason: str


def process_record(raw_line1, src1, raw_line2, src2):
    """Repair and validate a paired record.

    ``raw_line1``/``raw_line2`` are line bytes (no ``\\n``); ``src1``/``src2``
    are their 1-indexed source line numbers. Returns ``Accepted`` or
    ``Rejected``.
    """
    line1, fixes1, err1, cat1 = repair_line(raw_line1, 1)
    line2, fixes2, err2, cat2 = repair_line(raw_line2, 2)

    if err1 or err2:
        parts = []
        if err1:
            parts.append(f"line 1: {err1}")
        if err2:
            parts.append(f"line 2: {err2}")
        return Rejected(
            [raw_line1, raw_line2], [src1, src2],
            cat1 or cat2, "; ".join(parts),
        )

    record_errors = tle.validate_record(line1, line2)
    if record_errors:
        return Rejected(
            [raw_line1, raw_line2], [src1, src2],
            "catalog-mismatch", "; ".join(record_errors),
        )

    return Accepted(line1, line2, fixes1 + fixes2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_repair.py -v`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tlekit/repair.py tests/test_repair.py
git commit -m "feat: add record-level repair and result types"
```

---

## Task 9: `report.py` — stats types and `.broken.txt` writer

**Files:**
- Create: `src/tlekit/report.py`
- Test: `tests/test_report.py`

Defines `FileStats` (per-file accumulator), `RejectEntry` (one quarantined record for the sidecar), and `write_broken_file`, which writes the byte-faithful `.broken.txt` (§9.2, §10).

- [ ] **Step 1: Write the failing test**

Create `tests/test_report.py`:

```python
from tlekit import report


def test_write_broken_file(tmp_path):
    stats = report.FileStats(src_name="tle2099.txt")
    stats.total_records = 5
    stats.quarantined_count = 1
    stats.rejects.append(report.RejectEntry(
        raw_lines=[b"1 garbage"], source_lines=[42],
        reason="bad-prefix: line does not start with '1 ' or '2 '"))
    out = tmp_path / "tle2099.broken.txt"

    report.write_broken_file(str(out), "tle2099.txt", stats)

    text = out.read_bytes()
    assert b"# source: tle2099.txt" in text
    assert b"1 records quarantined of 5 total" in text
    assert b"source line 42" in text
    assert b"1 garbage" in text


def test_broken_file_is_byte_faithful(tmp_path):
    # A line quarantined for a non-ASCII byte must appear verbatim.
    stats = report.FileStats(src_name="x.txt")
    stats.quarantined_count = 1
    stats.rejects.append(report.RejectEntry(
        raw_lines=[b"1 \xff\xfe non-ascii"], source_lines=[7],
        reason="non-ascii"))
    out = tmp_path / "x.broken.txt"

    report.write_broken_file(str(out), "x.txt", stats)

    assert b"\xff\xfe" in out.read_bytes()


def test_two_line_record_location(tmp_path):
    stats = report.FileStats(src_name="x.txt")
    stats.quarantined_count = 1
    stats.rejects.append(report.RejectEntry(
        raw_lines=[b"1 aaa", b"2 bbb"], source_lines=[14820, 14821],
        reason="line 2: checksum mismatch"))
    out = tmp_path / "x.broken.txt"

    report.write_broken_file(str(out), "x.txt", stats)

    assert b"source lines 14820-14821" in out.read_bytes()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_report.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tlekit.report'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/tlekit/report.py`:

```python
"""Per-file statistics, the quarantine sidecar writer, and summaries."""

import dataclasses
import datetime

from tlekit import __version__, stem


@dataclasses.dataclass
class RejectEntry:
    """One quarantined record, rendered into ``.broken.txt``.

    ``raw_lines`` are original bytes (1 line for an orphan, 2 for a record)
    and are written verbatim so the sidecar is byte-faithful.
    """

    raw_lines: list
    source_lines: list
    reason: str


@dataclasses.dataclass
class FileStats:
    """Accumulated results for one processed source file."""

    src_name: str
    total_records: int = 0
    clean_count: int = 0
    quarantined_count: int = 0
    fix_counts: dict = dataclasses.field(default_factory=dict)
    reject_categories: dict = dataclasses.field(default_factory=dict)
    rejects: list = dataclasses.field(default_factory=list)


def write_broken_file(path, src_name, stats):
    """Write the byte-faithful ``.broken.txt`` quarantine sidecar.

    The header and per-record reason lines are ASCII; the quarantined-line
    payloads are copied as raw bytes, so the file may not be valid UTF-8.
    """
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    header = (
        f"# {stem(src_name)}.broken.txt - quarantined records\n"
        f"# source: {src_name} | generated: {timestamp} | tlekit {__version__}\n"
        f"# {stats.quarantined_count} records quarantined "
        f"of {stats.total_records} total\n\n"
    )
    with open(path, "wb") as handle:
        handle.write(header.encode("ascii"))
        for index, entry in enumerate(stats.rejects, start=1):
            if len(entry.source_lines) == 2:
                location = (
                    f"source lines {entry.source_lines[0]}-"
                    f"{entry.source_lines[1]}"
                )
            else:
                location = f"source line {entry.source_lines[0]}"
            handle.write(
                f"[{index}] {location} - reason: {entry.reason}\n".encode(
                    "ascii", errors="replace"
                )
            )
            for raw in entry.raw_lines:
                handle.write(raw)
                handle.write(b"\n")
            handle.write(b"\n")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_report.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tlekit/report.py tests/test_report.py
git commit -m "feat: add file stats and quarantine sidecar writer"
```

---

## Task 10: `report.py` — summary formatting

**Files:**
- Modify: `src/tlekit/report.py`
- Test: `tests/test_report.py`

Adds `format_summary` (the human one-liner block, §9.3), `summary_dict` (the `--report json` shape), and `format_reject_lines` (the per-defect line listing for `validate` mode, §7).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_report.py`:

```python
def _stats_with_counts():
    stats = report.FileStats(src_name="tle2022.txt")
    stats.total_records = 100
    stats.clean_count = 98
    stats.quarantined_count = 2
    stats.fix_counts = {"trailing-backslash": 50, "reconstructed-checksum": 7}
    stats.reject_categories = {"checksum-mismatch": 2}
    return stats


def test_format_summary_shows_counts():
    out = report.format_summary(_stats_with_counts())
    assert "tle2022.txt" in out
    assert "98" in out
    assert "trailing-backslash 50" in out
    assert "reconstructed-checksum 7" in out
    assert "checksum-mismatch 2" in out


def test_summary_dict_is_json_friendly():
    data = report.summary_dict(_stats_with_counts())
    assert data["src_name"] == "tle2022.txt"
    assert data["total_records"] == 100
    assert data["fix_counts"]["trailing-backslash"] == 50
    assert data["reject_categories"]["checksum-mismatch"] == 2


def test_format_reject_lines_lists_locations():
    stats = report.FileStats(src_name="x.txt")
    stats.rejects.append(report.RejectEntry(
        raw_lines=[b"1 a", b"2 b"], source_lines=[10, 11],
        reason="line 2: checksum mismatch"))
    out = report.format_reject_lines(stats)
    assert "10-11" in out and "checksum mismatch" in out


def test_format_reject_lines_caps_long_lists():
    stats = report.FileStats(src_name="x.txt")
    for i in range(250):
        stats.rejects.append(report.RejectEntry(
            raw_lines=[b"1 a"], source_lines=[i], reason="bad-prefix"))
    out = report.format_reject_lines(stats, limit=100)
    assert "150 more" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_report.py -k "summary or reject_lines" -v`
Expected: FAIL — `AttributeError: module 'tlekit.report' has no attribute 'format_summary'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/tlekit/report.py`:

```python
def _join_counts(counts):
    """Render a count dict as ``"key value | key value"``, sorted by key."""
    return " | ".join(f"{key} {value:,}" for key, value in sorted(counts.items()))


def format_summary(stats):
    """Return the human-readable multi-line summary block for one file."""
    lines = [
        f"{stats.src_name}   {stats.total_records:,} records   "
        f"{stats.clean_count:,} clean   {stats.quarantined_count:,} quarantined"
    ]
    if stats.fix_counts:
        lines.append(f"  fixes:   {_join_counts(stats.fix_counts)}")
    if stats.reject_categories:
        lines.append(f"  rejects: {_join_counts(stats.reject_categories)}")
    return "\n".join(lines)


def summary_dict(stats):
    """Return a JSON-serialisable summary of one file's stats."""
    return {
        "src_name": stats.src_name,
        "total_records": stats.total_records,
        "clean_count": stats.clean_count,
        "quarantined_count": stats.quarantined_count,
        "fix_counts": dict(stats.fix_counts),
        "reject_categories": dict(stats.reject_categories),
    }


def format_reject_lines(stats, limit=100):
    """Return a listing of quarantined records' source locations.

    Used by ``validate`` mode. At most ``limit`` entries are shown; the
    remainder are summarised as a trailing count.
    """
    lines = []
    for entry in stats.rejects[:limit]:
        if len(entry.source_lines) == 2:
            location = f"{entry.source_lines[0]}-{entry.source_lines[1]}"
        else:
            location = str(entry.source_lines[0])
        lines.append(f"  line {location}: {entry.reason}")
    remaining = len(stats.rejects) - limit
    if remaining > 0:
        lines.append(f"  ...and {remaining} more")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_report.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tlekit/report.py tests/test_report.py
git commit -m "feat: add run summary formatting"
```

---

## Task 11: `pipeline.py` — streaming reader and pairing

**Files:**
- Create: `src/tlekit/pipeline.py`
- Test: `tests/test_pipeline.py`

`iter_records` streams a file in binary, drops blank/CR-only lines, and runs the prefix-driven pairing state machine (§8), yielding `RecordCandidate` or `Orphan`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pipeline.py`:

```python
from tlekit import pipeline


def test_pairs_simple_records(tmp_path, line1, line2):
    src = tmp_path / "in.txt"
    src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
    records = list(pipeline.iter_records(str(src)))
    assert len(records) == 1
    assert isinstance(records[0], pipeline.RecordCandidate)
    assert records[0].src1 == 1 and records[0].src2 == 2


def test_blank_and_cr_only_lines_dropped(tmp_path, line1, line2):
    src = tmp_path / "in.txt"
    src.write_bytes((line1 + "\n\n" + "\r\n" + line2 + "\n").encode("ascii"))
    records = list(pipeline.iter_records(str(src)))
    assert len(records) == 1
    assert isinstance(records[0], pipeline.RecordCandidate)


def test_two_line1s_orphan_the_first(tmp_path, line1):
    src = tmp_path / "in.txt"
    src.write_bytes((line1 + "\n" + line1 + "\n").encode("ascii"))
    records = list(pipeline.iter_records(str(src)))
    assert all(isinstance(r, pipeline.Orphan) for r in records)
    assert records[0].category == "orphan-line"


def test_orphan_line2(tmp_path, line2):
    src = tmp_path / "in.txt"
    src.write_bytes((line2 + "\n").encode("ascii"))
    records = list(pipeline.iter_records(str(src)))
    assert len(records) == 1 and isinstance(records[0], pipeline.Orphan)


def test_bad_prefix_line(tmp_path, line1, line2):
    src = tmp_path / "in.txt"
    src.write_bytes(("garbage\n" + line1 + "\n" + line2 + "\n").encode("ascii"))
    records = list(pipeline.iter_records(str(src)))
    orphans = [r for r in records if isinstance(r, pipeline.Orphan)]
    assert any(o.category == "bad-prefix" for o in orphans)
    # The valid record after the garbage line still pairs.
    assert any(isinstance(r, pipeline.RecordCandidate) for r in records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tlekit.pipeline'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/tlekit/pipeline.py`:

```python
"""Streaming I/O: read a file, pair lines into records, route them."""

import dataclasses


@dataclasses.dataclass
class RecordCandidate:
    """A line-1 / line-2 pair, with their 1-indexed source line numbers."""

    raw_line1: bytes
    raw_line2: bytes
    src1: int
    src2: int


@dataclasses.dataclass
class Orphan:
    """A line that could not be paired into a record."""

    raw_line: bytes
    src: int
    category: str
    reason: str


def iter_records(path):
    """Yield ``RecordCandidate`` / ``Orphan`` items streamed from ``path``.

    The file is read in binary so ``\\r`` and stray bytes are observed
    exactly. Blank and CR-only lines are dropped. Pairing is prefix-driven
    and resynchronises on every ``1 `` line, so one missing line cannot
    cascade into a run of mispaired records.
    """
    held = None  # (raw_bytes, line_number) of a line-1 awaiting its line-2

    with open(path, "rb") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.rstrip(b"\n")
            if line.rstrip(b"\r") == b"":
                continue  # blank or CR-only line — dropped

            prefix = line[:2]
            if prefix == b"1 ":
                if held is not None:
                    yield Orphan(
                        held[0], held[1], "orphan-line",
                        "orphan line 1: followed by another line 1",
                    )
                held = (line, lineno)
            elif prefix == b"2 ":
                if held is not None:
                    yield RecordCandidate(held[0], line, held[1], lineno)
                    held = None
                else:
                    yield Orphan(
                        line, lineno, "orphan-line",
                        "orphan line 2: no preceding line 1",
                    )
            else:
                if held is not None:
                    yield Orphan(
                        held[0], held[1], "orphan-line",
                        "orphan line 1: followed by a non-TLE line",
                    )
                    held = None
                yield Orphan(
                    line, lineno, "bad-prefix",
                    "line does not start with '1 ' or '2 '",
                )

    if held is not None:
        yield Orphan(
            held[0], held[1], "orphan-line", "orphan line 1 at end of file"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tlekit/pipeline.py tests/test_pipeline.py
git commit -m "feat: add streaming reader and prefix-driven pairing"
```

---

## Task 12: `pipeline.py` — `process_file`

**Files:**
- Modify: `src/tlekit/pipeline.py`
- Test: `tests/test_pipeline.py`

`process_file` streams one source file, routes every candidate through `repair`, tallies a `FileStats`, and — in `clean` mode — writes `<name>.cleaned.txt` (atomically) and `<name>.broken.txt`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pipeline.py`:

```python
def test_process_file_clean_mode(tmp_path, line1, line2):
    src = tmp_path / "tle2099.txt"
    # One clean record, then one checksumless record (both repairable).
    src.write_bytes((
        line1 + "\n" + line2 + "\n" + line1[:68] + "\n" + line2[:68] + "\n"
    ).encode("ascii"))
    out = tmp_path / "out"

    stats = pipeline.process_file(str(src), str(out), "clean")

    assert stats.total_records == 2
    assert stats.clean_count == 2
    assert stats.quarantined_count == 0
    cleaned = (out / "tle2099.cleaned.txt").read_text()
    assert cleaned == line1 + "\n" + line2 + "\n" + line1 + "\n" + line2 + "\n"
    assert (out / "tle2099.broken.txt").exists()


def test_process_file_quarantines_bad_record(tmp_path, line1, line2):
    src = tmp_path / "tle2099.txt"
    bad_line1 = line1[:68] + "9"  # 69 chars, wrong checksum
    src.write_bytes((bad_line1 + "\n" + line2 + "\n").encode("ascii"))
    out = tmp_path / "out"

    stats = pipeline.process_file(str(src), str(out), "clean")

    assert stats.quarantined_count == 1
    assert stats.reject_categories.get("checksum-mismatch") == 1
    assert b"checksum" in (out / "tle2099.broken.txt").read_bytes()


def test_validate_mode_writes_nothing(tmp_path, line1, line2):
    src = tmp_path / "tle2099.txt"
    src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
    out = tmp_path / "out"

    stats = pipeline.process_file(str(src), str(out), "validate")

    assert stats.clean_count == 1
    assert not out.exists()  # validate mode never creates the output dir


def test_internal_error_is_quarantined_not_raised(tmp_path, line1, line2,
                                                  monkeypatch):
    src = tmp_path / "tle2099.txt"
    src.write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(pipeline.repair, "process_record", boom)
    stats = pipeline.process_file(str(src), str(tmp_path / "out"), "clean")

    assert stats.quarantined_count == 1
    assert stats.reject_categories.get("internal-error") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline.py -k process_file -v`
Expected: FAIL — `AttributeError: module 'tlekit.pipeline' has no attribute 'process_file'`.

- [ ] **Step 3: Write minimal implementation**

Add the imports and append the function. Change the top of `src/tlekit/pipeline.py` so the imports read:

```python
"""Streaming I/O: read a file, pair lines into records, route them."""

import dataclasses
import os
import tempfile

from tlekit import repair, report, stem
```

Then append:

```python
def process_file(src_path, out_dir, mode):
    """Process one source file and return its ``report.FileStats``.

    ``mode`` is ``"validate"`` (audit only — writes nothing) or ``"clean"``
    (also writes ``<name>.cleaned.txt`` and ``<name>.broken.txt`` to
    ``out_dir``). The cleaned file is written to a temp file and atomically
    renamed, so an interrupted run never leaves a half-written output.
    """
    src_name = os.path.basename(src_path)
    stats = report.FileStats(src_name=src_name)

    cleaned_handle = None
    cleaned_tmp = None
    cleaned_path = None
    if mode == "clean":
        os.makedirs(out_dir, exist_ok=True)
        cleaned_path = os.path.join(out_dir, stem(src_name) + ".cleaned.txt")
        fd, cleaned_tmp = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
        cleaned_handle = os.fdopen(fd, "w", encoding="ascii", newline="\n")

    completed = False
    try:
        for candidate in iter_records(src_path):
            stats.total_records += 1

            if isinstance(candidate, Orphan):
                _record_reject(
                    stats, candidate.category, candidate.reason,
                    [candidate.raw_line], [candidate.src],
                )
                continue

            try:
                result = repair.process_record(
                    candidate.raw_line1, candidate.src1,
                    candidate.raw_line2, candidate.src2,
                )
            except Exception as exc:  # one bad record must not kill the run
                _record_reject(
                    stats, "internal-error", f"internal-error: {exc!r}",
                    [candidate.raw_line1, candidate.raw_line2],
                    [candidate.src1, candidate.src2],
                )
                continue

            if isinstance(result, repair.Accepted):
                stats.clean_count += 1
                for fix in result.fixes:
                    stats.fix_counts[fix] = stats.fix_counts.get(fix, 0) + 1
                if cleaned_handle is not None:
                    cleaned_handle.write(result.line1 + "\n")
                    cleaned_handle.write(result.line2 + "\n")
            else:
                _record_reject(
                    stats, result.category, result.reason,
                    result.raw_lines, result.source_lines,
                )
        completed = True
    finally:
        if cleaned_handle is not None:
            cleaned_handle.close()
        # On any failure, discard the partial temp file — never publish a
        # half-written .cleaned.txt and never leak the .tmp behind.
        if cleaned_tmp is not None and not completed:
            try:
                os.unlink(cleaned_tmp)
            except OSError:
                pass

    if mode == "clean":
        os.replace(cleaned_tmp, cleaned_path)
        broken_path = os.path.join(out_dir, stem(src_name) + ".broken.txt")
        report.write_broken_file(broken_path, src_name, stats)

    return stats


def _record_reject(stats, category, reason, raw_lines, source_lines):
    """Tally one quarantined record into ``stats``."""
    stats.quarantined_count += 1
    stats.reject_categories[category] = (
        stats.reject_categories.get(category, 0) + 1
    )
    stats.rejects.append(report.RejectEntry(raw_lines, source_lines, reason))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tlekit/pipeline.py tests/test_pipeline.py
git commit -m "feat: add per-file processing and output routing"
```

---

## Task 13: `cli.py` — file discovery and argument parsing

**Files:**
- Create: `src/tlekit/cli.py`
- Test: `tests/test_cli.py`

Adds `discover_paths` (expand directories to `tle*.txt`, excluding tool output) and `build_parser` (the `validate`/`clean` argument parser).

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli.py`:

```python
import os

from tlekit import cli


def test_discover_expands_directory(tmp_path):
    (tmp_path / "tle2001.txt").write_text("x")
    (tmp_path / "tle2002.txt").write_text("x")
    (tmp_path / "tle2001.cleaned.txt").write_text("x")  # tool output — excluded
    (tmp_path / "tle2001.broken.txt").write_text("x")   # tool output — excluded
    (tmp_path / "notes.md").write_text("x")             # not a TLE file

    found = cli.discover_paths([str(tmp_path)])

    names = sorted(os.path.basename(p) for p in found)
    assert names == ["tle2001.txt", "tle2002.txt"]


def test_discover_passes_through_explicit_files(tmp_path):
    explicit = tmp_path / "tle2001.txt"
    explicit.write_text("x")
    assert cli.discover_paths([str(explicit)]) == [str(explicit)]


def test_parser_defaults():
    args = cli.build_parser().parse_args(["validate"])
    assert args.command == "validate"
    assert args.paths == ["data/source"]
    assert args.out_dir == "data/output"
    assert args.report == "text"


def test_parser_accepts_jobs_and_paths():
    args = cli.build_parser().parse_args(
        ["clean", "a.txt", "b.txt", "--jobs", "4", "--report", "json"]
    )
    assert args.command == "clean"
    assert args.paths == ["a.txt", "b.txt"]
    assert args.jobs == 4
    assert args.report == "json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tlekit.cli'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/tlekit/cli.py`:

```python
"""Command-line interface: ``tle-clean validate`` and ``tle-clean clean``."""

import argparse
import concurrent.futures
import json
import os
import shutil
import sys

from tlekit import pipeline, report

_DEFAULT_SOURCE = "data/source"
_DEFAULT_OUTPUT = "data/output"


def discover_paths(paths):
    """Expand each entry in ``paths``: a directory becomes its sorted
    ``tle*.txt`` files (excluding ``*.cleaned.txt`` / ``*.broken.txt`` tool
    output); a file is passed through unchanged.
    """
    result = []
    for path in paths:
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if (
                    name.startswith("tle")
                    and name.endswith(".txt")
                    and not name.endswith(".cleaned.txt")
                    and not name.endswith(".broken.txt")
                ):
                    result.append(os.path.join(path, name))
        else:
            result.append(path)
    return result


def build_parser():
    """Build the ``tle-clean`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="tle-clean",
        description="Validate and clean Two-Line Element (TLE) corpus files.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("validate", "audit files and report defects (writes nothing)"),
        ("clean", "write cleaned files and quarantine sidecars"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "paths", nargs="*", default=[_DEFAULT_SOURCE],
            help=f"files or directories to process (default: {_DEFAULT_SOURCE})",
        )
        sub.add_argument(
            "--out-dir", default=_DEFAULT_OUTPUT,
            help=f"destination for cleaned/broken files (default: {_DEFAULT_OUTPUT})",
        )
        sub.add_argument(
            "--jobs", type=int, default=os.cpu_count() or 1,
            help="number of files to process in parallel",
        )
        sub.add_argument(
            "--report", choices=["text", "json"], default="text",
            help="summary output format",
        )
    return parser
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tlekit/cli.py tests/test_cli.py
git commit -m "feat: add CLI file discovery and argument parser"
```

---

## Task 14: `cli.py` — `main` and the module entry point

**Files:**
- Modify: `src/tlekit/cli.py`
- Create: `src/tlekit/__main__.py`
- Test: `tests/test_cli.py`

`main` discovers files, checks disk space before a `clean` run (§10), fans the files out across a `ProcessPoolExecutor`, prints the summary, and returns the exit code (`0` clean / `1` records quarantined / `2` operational error).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_main_clean_returns_zero_on_clean_corpus(tmp_path, line1, line2):
    src = tmp_path / "src"
    src.mkdir()
    (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))
    out = tmp_path / "out"

    rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

    assert rc == 0
    assert (out / "tle2099.cleaned.txt").exists()


def test_main_returns_one_when_records_quarantined(tmp_path, line1, line2):
    src = tmp_path / "src"
    src.mkdir()
    bad_line1 = line1[:68] + "9"
    (src / "tle2099.txt").write_bytes(
        (bad_line1 + "\n" + line2 + "\n").encode("ascii")
    )
    out = tmp_path / "out"

    rc = cli.main(["clean", str(src), "--out-dir", str(out), "--jobs", "1"])

    assert rc == 1


def test_main_returns_two_when_no_input_files(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = cli.main(["validate", str(empty)])
    assert rc == 2


def test_main_validate_prints_summary(tmp_path, line1, line2, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "tle2099.txt").write_bytes((line1 + "\n" + line2 + "\n").encode("ascii"))

    rc = cli.main(["validate", str(src), "--jobs", "1"])

    assert rc == 0
    assert "tle2099.txt" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -k main -v`
Expected: FAIL — `AttributeError: module 'tlekit.cli' has no attribute 'main'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/tlekit/cli.py`:

```python
def _check_disk_space(out_dir, files):
    """Return an error string if ``out_dir`` lacks room for cleaned +
    broken output (roughly twice the total input size), else ``None``.
    """
    needed = sum(os.path.getsize(f) for f in files) * 2
    free = shutil.disk_usage(out_dir).free
    if free < needed:
        return (
            f"insufficient disk space in {out_dir}: "
            f"need ~{needed:,} bytes, have {free:,}"
        )
    return None


def main(argv=None):
    """Entry point for the ``tle-clean`` console script.

    Returns the process exit code: ``0`` = no records quarantined;
    ``1`` = at least one record quarantined; ``2`` = operational error.
    """
    args = build_parser().parse_args(argv)
    files = discover_paths(args.paths)
    if not files:
        print("no input files found", file=sys.stderr)
        return 2

    if args.command == "clean":
        os.makedirs(args.out_dir, exist_ok=True)
        disk_error = _check_disk_space(args.out_dir, files)
        if disk_error:
            print(disk_error, file=sys.stderr)
            return 2

    all_stats = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(
                pipeline.process_file, path, args.out_dir, args.command
            ): path
            for path in files
        }
        for future in concurrent.futures.as_completed(futures):
            path = futures[future]
            try:
                all_stats.append(future.result())
            except Exception as exc:
                print(f"error processing {path}: {exc!r}", file=sys.stderr)

    all_stats.sort(key=lambda stats: stats.src_name)

    if args.report == "json":
        print(json.dumps([report.summary_dict(s) for s in all_stats], indent=2))
    else:
        for stats in all_stats:
            print(report.format_summary(stats))
            if args.command == "validate" and stats.rejects:
                print(report.format_reject_lines(stats))

    total_quarantined = sum(s.quarantined_count for s in all_stats)
    return 1 if total_quarantined else 0
```

- [ ] **Step 4: Create `src/tlekit/__main__.py`**

```python
"""Allow ``python -m tlekit`` to run the CLI."""

import sys

from tlekit.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS (8 tests).

- [ ] **Step 6: Verify the console script works end to end**

```bash
uv run tle-clean --help
uv run python -m tlekit validate --help
```
Expected: both print usage text showing the `validate` and `clean` subcommands.

- [ ] **Step 7: Commit**

```bash
git add src/tlekit/cli.py src/tlekit/__main__.py tests/test_cli.py
git commit -m "feat: add CLI run loop, parallelism, and exit codes"
```

---

## Task 15: Integration — golden output and idempotence

**Files:**
- Create: `tests/test_integration.py`

End-to-end tests over a multi-record file mixing every defect class, plus the idempotence guarantee (§8) and the "cleaned output re-validates as perfect" guarantee.

- [ ] **Step 1: Write the failing test**

Create `tests/test_integration.py`:

```python
"""End-to-end pipeline tests: golden output, idempotence, re-validation."""

from tlekit import pipeline, tle


def test_golden_mixed_file(tmp_path, line1, line2):
    # Record A: clean. Record B: checksumless line 1 + backslash, checksumless
    # line 2 (both repairable). Record C: line 1 with a wrong checksum (bad).
    bad_line1 = line1[:68] + "9"
    src = tmp_path / "tle2099.txt"
    src.write_bytes((
        line1 + "\n" + line2 + "\n"
        + line1[:68] + "\\\n" + line2[:68] + "\n"
        + bad_line1 + "\n" + line2 + "\n"
    ).encode("ascii"))
    out = tmp_path / "out"

    stats = pipeline.process_file(str(src), str(out), "clean")

    assert stats.total_records == 3
    assert stats.clean_count == 2
    assert stats.quarantined_count == 1
    assert stats.fix_counts.get("reconstructed-checksum") == 2
    assert stats.fix_counts.get("trailing-backslash") == 1

    cleaned = (out / "tle2099.cleaned.txt").read_text()
    assert cleaned == (
        line1 + "\n" + line2 + "\n" + line1 + "\n" + line2 + "\n"
    )
    broken = (out / "tle2099.broken.txt").read_bytes()
    assert b"checksum" in broken
    assert bad_line1.encode("ascii") in broken


def test_clean_is_idempotent(tmp_path, line1, line2):
    src = tmp_path / "tle2099.txt"
    src.write_bytes((line1[:68] + "\\\n" + line2[:68] + "\n").encode("ascii"))

    out1 = tmp_path / "out1"
    pipeline.process_file(str(src), str(out1), "clean")
    cleaned1 = out1 / "tle2099.cleaned.txt"

    # Re-clean the cleaned output. stem("tle2099.cleaned.txt") == "tle2099.cleaned".
    out2 = tmp_path / "out2"
    pipeline.process_file(str(cleaned1), str(out2), "clean")
    cleaned2 = out2 / "tle2099.cleaned.cleaned.txt"

    assert cleaned1.read_bytes() == cleaned2.read_bytes()


def test_cleaned_output_revalidates_as_perfect(tmp_path, line1, line2):
    src = tmp_path / "tle2099.txt"
    src.write_bytes((line1[:68] + "\\\n" + line2[:68] + "\n").encode("ascii"))
    out = tmp_path / "out"
    pipeline.process_file(str(src), str(out), "clean")

    stats = pipeline.process_file(
        str(out / "tle2099.cleaned.txt"), str(tmp_path / "verify"), "validate"
    )
    assert stats.clean_count == 1
    assert stats.quarantined_count == 0


def test_every_cleaned_line_passes_validate_line(tmp_path, line1, line2):
    src = tmp_path / "tle2099.txt"
    src.write_bytes((line1[:68] + "\n" + line2[:68] + "\n").encode("ascii"))
    out = tmp_path / "out"
    pipeline.process_file(str(src), str(out), "clean")

    lines = (out / "tle2099.cleaned.txt").read_text().splitlines()
    assert tle.validate_line(lines[0], 1) == []
    assert tle.validate_line(lines[1], 2) == []
```

- [ ] **Step 2: Run test to verify it fails (then passes)**

Run: `uv run pytest tests/test_integration.py -v`
Expected: with Tasks 1–14 complete these PASS immediately. If any fail, the failure is a real defect in an earlier task — fix it there, do not weaken the test.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS — every test across all modules.

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add golden-output and idempotence integration tests"
```

---

## Task 16: First corpus run (operational milestone — spec §12)

**Files:** none — this task runs the finished tool against the real corpus.

No code changes. This is the discovery milestone: the `validate` pass is the authoritative defect catalogue (only the trailing `\` and missing-checksum defects were measured up front).

- [ ] **Step 1: Validate the whole corpus**

Run: `uv run tle-clean validate data/source --report text`
Expected: a per-file summary block for each of the 29 files. Review the `rejects:` categories.

- [ ] **Step 2: Assess the defect catalogue**

If `validate` surfaces a reject category that is genuinely safe and unambiguous to repair (not anticipated by §6.1–6.3), stop and add it to `repair.py` as a new fix under the validated-transformation principle (spec §13) — with its own task, test-first. Anything ambiguous stays quarantined; do not weaken validation to make rejects disappear.

- [ ] **Step 3: Clean the corpus**

Run: `uv run tle-clean clean data/source --out-dir data/output --report json > data/output/run-summary.json`
Expected: `data/output/` contains a `<name>.cleaned.txt` and `<name>.broken.txt` per source file. If a single slow disk causes I/O contention, re-run with `--jobs 1`.

- [ ] **Step 4: Spot-check the results**

```bash
uv run tle-clean validate data/output/tle2025.cleaned.txt
head -5 data/output/tle2017.broken.txt
```
Expected: the cleaned file re-validates with zero quarantined records; the `.broken.txt` header and entries are well-formed and suitable for a space-track report.

- [ ] **Step 5: Commit the run summary**

`data/` is git-ignored, so copy the summary into the tracked tree first:

```bash
mkdir -p docs/superpowers/runs
cp data/output/run-summary.json docs/superpowers/runs/2026-05-21-corpus-run-summary.json
git add docs/superpowers/runs/2026-05-21-corpus-run-summary.json
git commit -m "docs: record first full corpus clean run summary"
```

---

## Self-Review

**Spec coverage** — every spec section maps to a task:

- §1, §1.1 defect model — encoded in the validator (Tasks 2–5) and exercised by the corpus run (Task 16).
- §4.1 validated-transformation principle — `repair_line`'s single final re-validation (Task 7).
- §4.2 layout — Task 1 scaffold and the `data/source`+`data/output` move (Task 1).
- §4.3 modules — one task group per module (`tle` 2–5, `repair` 7–8, `report` 9–10, `pipeline` 11–12, `cli` 13–14).
- §5.1–5.3 column layout & checksum — Tasks 2, 3.
- §5.4 three validation levels — `_check_columns`, `_check_semantics`, `validate_line` (Tasks 3–5).
- §6.1 content-preserving fixes / §6.2 reconstructed checksum / §6.3 leading-trim / §6.6 fix order — `repair_line` (Task 7).
- §6.4 blank-line drop — `iter_records` (Task 11). §6.5 quarantine categories — Tasks 7–8, 11.
- §7 CLI — Tasks 13–14. §8 streaming & prefix-driven pairing — Tasks 11–12.
- §9 output formats — `.cleaned.txt`/`.broken.txt` (Task 12), summaries (Task 10).
- §10 error handling — internal-error catch & atomic write (Task 12), disk check & exit codes (Task 14), byte-faithful sidecar (Task 9).
- §11 testing & oracle — Task 6 and the per-module test files. §12 build sequence — task order. §13 — Task 16 Step 2.

**Placeholder scan:** none — every code and test step contains complete content.

**Type consistency:** `repair_line` returns the 4-tuple `(clean, fixes, error, category)` consistently in Tasks 7–8; `Accepted(line1, line2, fixes)` and `Rejected(raw_lines, source_lines, category, reason)` match their constructor calls in `process_record` and `pipeline._record_reject`; `FileStats`/`RejectEntry` field names are used identically in `report`, `pipeline`, and the tests; `stem()` is defined once (Task 1) and imported by `report` and `pipeline`.
