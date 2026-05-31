# `--max-quarantined` Percentage Threshold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `--max-quarantined` accept a trailing `%` so the CI/pipeline exit-code gate can be expressed as a scale-invariant rate (fraction of routed records quarantined), while fully preserving the existing absolute-count contract.

**Architecture:** A single CLI value is parsed by a new pure module-level helper `parse_quarantine_threshold(raw)` in `cli.py` into `(mode, threshold)` where `mode in {"count", "pct"}`. `main()` calls the helper once after the `--jobs` check and stores the pair; the exit-code decision at the end of `main()` branches on `mode`. No other module is touched.

**Tech Stack:** Python 3.11, stdlib only at runtime; `pytest` and `ruff` for dev. All work happens in the existing worktree at `.worktrees/feature-max-quarantined-percentage` on branch `feature/max-quarantined-percentage`.

**Spec:** `docs/superpowers/specs/2026-05-27-max-quarantined-percentage-design.md` (committed at `73aa680`). Read it before starting — section §3 (Behavior contract) and §4 (Parsing & validation) are normative.

---

## Pre-flight

The worktree already exists with the spec committed. Confirm a green baseline before any change.

- [ ] **Step 1: Install dev dependencies in the worktree.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv sync
```

Expected: completes without error; `.venv/` materialises in the worktree.

- [ ] **Step 2: Confirm baseline tests pass.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run pytest -q
```

Expected: all tests pass. Record the total ("`NNN passed`"); the same number must still pass after Task 1.

- [ ] **Step 3: Confirm baseline lint/format are clean.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run ruff check . && uv run ruff format --check .
```

Expected: both report clean (no findings, no formatting changes needed).

---

## Task 1: Add `parse_quarantine_threshold` helper (unit-tested, not yet wired)

**Files:**
- Modify: `tests/test_cli.py` — add `import pytest` to the imports block, and append a new `TestParseQuarantineThreshold` class at the end of the file.
- Modify: `src/lintle/cli.py` — add the pure helper right after `discover_paths` (it ends at the `return result` of the function spanning lines 42–66 in the pre-change file; the new helper is inserted as the next module-level function, before `_detect_basename_collisions`).

This task introduces the helper plus full unit-test coverage. No CLI wiring yet — every existing test still passes because nothing calls the new function.

- [ ] **Step 1: Add `import pytest` to `tests/test_cli.py`.**

The current import block is:

```python
import json
import os
import queue
import signal
import time

from lintle import cli, pipeline, report, resume
```

Insert `import pytest` in alphabetical order (between `os` and `queue`):

```python
import json
import os
import pytest
import queue
import signal
import time

from lintle import cli, pipeline, report, resume
```

- [ ] **Step 2: Append the failing test class to the bottom of `tests/test_cli.py`.**

```python
class TestParseQuarantineThreshold:
    """The ``--max-quarantined`` value parser. A bare integer is an absolute
    count; a trailing ``%`` switches to a percentage of routed records. The
    two modes are mutually exclusive by construction (a single value is one
    or the other, never both).
    """

    def test_bare_integer_is_count_mode(self):
        assert cli.parse_quarantine_threshold("100") == ("count", 100)

    def test_zero_is_count_zero(self):
        assert cli.parse_quarantine_threshold("0") == ("count", 0)

    def test_trailing_percent_is_pct_mode(self):
        assert cli.parse_quarantine_threshold("1%") == ("pct", 1.0)

    def test_zero_percent_is_valid(self):
        assert cli.parse_quarantine_threshold("0%") == ("pct", 0.0)

    def test_hundred_percent_is_valid(self):
        assert cli.parse_quarantine_threshold("100%") == ("pct", 100.0)

    def test_fractional_percent(self):
        assert cli.parse_quarantine_threshold("1.5%") == ("pct", 1.5)

    def test_surrounding_whitespace_tolerated(self):
        assert cli.parse_quarantine_threshold("  100  ") == ("count", 100)
        assert cli.parse_quarantine_threshold("  1%  ") == ("pct", 1.0)

    def test_negative_count_rejected_with_legacy_message(self):
        # Preserves the issue-#13 substring required by the existing
        # negative-value integration test in TestMaxQuarantinedThreshold.
        with pytest.raises(ValueError, match=r"--max-quarantined must be >= 0"):
            cli.parse_quarantine_threshold("-1")

    def test_non_integer_count_rejected(self):
        # Counts are whole records; "1.5" with no `%` is not a count.
        with pytest.raises(ValueError, match="invalid value"):
            cli.parse_quarantine_threshold("1.5")

    def test_non_numeric_rejected(self):
        with pytest.raises(ValueError, match="invalid value"):
            cli.parse_quarantine_threshold("abc")

    def test_bare_percent_rejected(self):
        with pytest.raises(ValueError, match="invalid percentage"):
            cli.parse_quarantine_threshold("%")

    def test_pct_over_one_hundred_rejected(self):
        with pytest.raises(ValueError, match=r"percentage must be in 0\.\.100"):
            cli.parse_quarantine_threshold("150%")

    def test_pct_negative_rejected(self):
        with pytest.raises(ValueError, match=r"percentage must be in 0\.\.100"):
            cli.parse_quarantine_threshold("-1%")

    def test_pct_malformed_rejected(self):
        with pytest.raises(ValueError, match="invalid percentage"):
            cli.parse_quarantine_threshold("1.2.3%")
```

- [ ] **Step 3: Run the new tests; verify they all fail with the same root cause.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run pytest tests/test_cli.py::TestParseQuarantineThreshold -v
```

Expected: every test fails with `AttributeError: module 'lintle.cli' has no attribute 'parse_quarantine_threshold'` (or equivalent). All failures are the same — the helper does not exist yet.

- [ ] **Step 4: Implement the helper in `src/lintle/cli.py`.**

Insert the following function as a new module-level definition immediately after the `discover_paths` function ends (after its `return result` line) and before `def _detect_basename_collisions(files):`:

```python
def parse_quarantine_threshold(raw):
    """Parse a ``--max-quarantined`` value into ``(mode, threshold)``.

    A bare integer (e.g. ``"100"``) is an absolute record count and returns
    ``("count", int)``. A value with a trailing ``%`` (e.g. ``"1%"`` or
    ``"1.5%"``) is a percentage of routed records and returns
    ``("pct", float)``; the percentage must lie in ``0..100``. Surrounding
    whitespace is tolerated. Raises :class:`ValueError` on malformed input
    or out-of-range values; the message for a negative count preserves the
    exact substring asserted by the legacy issue-#13 integration test.
    """
    raw = raw.strip()
    if raw.endswith("%"):
        body = raw[:-1].strip()
        try:
            pct = float(body)
        except ValueError:
            raise ValueError(
                f"--max-quarantined: invalid percentage {raw!r}"
            ) from None
        if not (0.0 <= pct <= 100.0):
            raise ValueError(
                f"--max-quarantined percentage must be in 0..100 (got {raw!r})"
            )
        return ("pct", pct)
    try:
        count = int(raw)
    except ValueError:
        raise ValueError(f"--max-quarantined: invalid value {raw!r}") from None
    if count < 0:
        raise ValueError(f"--max-quarantined must be >= 0 (got {count})")
    return ("count", count)
```

- [ ] **Step 5: Run the new tests; verify all pass.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run pytest tests/test_cli.py::TestParseQuarantineThreshold -v
```

Expected: every test in `TestParseQuarantineThreshold` passes.

- [ ] **Step 6: Run the full suite; confirm no regressions.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run pytest -q
```

Expected: the same total as the pre-flight baseline, plus the 14 new tests, all passing.

- [ ] **Step 7: Lint and format check.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run ruff check . && uv run ruff format --check .
```

Expected: clean.

- [ ] **Step 8: Commit.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && git add src/lintle/cli.py tests/test_cli.py && git commit -m "$(cat <<'EOF'
feat(cli): add parse_quarantine_threshold helper

Pure module-level parser that turns the string value of --max-quarantined
into ("count", int) or ("pct", float). Mutually exclusive by construction;
preserves the exact "--max-quarantined must be >= 0" substring on a negative
count so the existing issue-#13 integration test stays green. Not yet wired
into main() — that lands in the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: a single commit on `feature/max-quarantined-percentage` adding the helper and its tests. The commit must be signed by the configured key — do not pass `--no-gpg-sign`. If signing fails because 1Password is locked, unlock it and re-run the `git commit` command exactly as above (the `git add` is already done).

---

## Task 2: Wire the helper into `main()`, add the rate-mode exit branch, update docs and CHANGELOG

**Files:**
- Modify: `src/lintle/cli.py` — argparse arg definition, the validation block in `main()`, the exit-code branch in `main()`, the "Exit codes" epilog, and the `main()` docstring.
- Modify: `tests/test_cli.py` — add one driving integration test for rate-mode passing under threshold (`test_pct_under_threshold_passes`), and a shared fixture helper `_write_n_good_and_one_bad` on `TestMaxQuarantinedThreshold`.
- Modify: `CHANGELOG.md` — add a new `### Added` subsection under `## [Unreleased]`, before the existing `### Changed`.

After this task the feature is complete end-to-end: rate-mode invocations work, count-mode behaviour is byte-for-byte preserved, the help text and exit-codes documentation describe both forms, and the CHANGELOG records the change.

- [ ] **Step 1: Add the shared fixture helper to `TestMaxQuarantinedThreshold`.**

Inside the existing `class TestMaxQuarantinedThreshold:` in `tests/test_cli.py`, immediately after the existing `_write_one_bad_record` method (and before `test_max_quarantined_one_allows_single_quarantined_record`), insert a second helper:

```python
    def _write_n_good_and_one_bad(self, tmp_path, line1, line2, n_good):
        # n_good copies of a valid 2-line record + one wrong-checksum pair.
        # The bad pair is quarantined under TLE-CHK-001; the n_good pairs
        # route to clean. Total routed = n_good + 1; quarantined = 1; rate
        # = 1 / (n_good + 1).
        src = tmp_path / "src"
        src.mkdir()
        bad_line1 = line1[:68] + "9"
        body = (line1 + "\n" + line2 + "\n") * n_good
        body += bad_line1 + "\n" + line2 + "\n"
        (src / "tle2099.txt").write_bytes(body.encode("ascii"))
        return src
```

- [ ] **Step 2: Write the failing driving test.**

Append a new test method to `TestMaxQuarantinedThreshold`, at the end of the class:

```python
    def test_pct_under_threshold_passes(self, tmp_path, line1, line2):
        # 1 bad of 100 routed records = 1.0%. `--max-quarantined 5%` is
        # well above that, so the run exits 0.
        src = self._write_n_good_and_one_bad(tmp_path, line1, line2, n_good=99)
        out = tmp_path / "out"

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out),
                "--jobs",
                "1",
                "--max-quarantined",
                "5%",
            ]
        )

        assert rc == 0
```

- [ ] **Step 3: Run the new test; verify it fails.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run pytest tests/test_cli.py::TestMaxQuarantinedThreshold::test_pct_under_threshold_passes -v
```

Expected: fails. With the current `type=int` argparse, `"5%"` cannot be parsed as an int — argparse will exit with status 2 and an error like `argument --max-quarantined: invalid int value: '5%'`. The test expects `rc == 0` and gets a different failure mode (likely `SystemExit`). Either way it's red.

- [ ] **Step 4: Update the argparse argument definition.**

In `src/lintle/cli.py`, locate the existing block that defines `--max-quarantined` (search for `"--max-quarantined"` — it appears once in `build_parser`):

```python
        sub.add_argument(
            "--max-quarantined",
            type=int,
            default=0,
            metavar="N",
            help=(
                "exit non-zero only if MORE than N records were quarantined "
                "(default: 0 — any quarantine fails)"
            ),
        )
```

Replace it with:

```python
        sub.add_argument(
            "--max-quarantined",
            default="0",
            metavar="N[%]",
            help=(
                "exit non-zero only if MORE than N records were quarantined; "
                "or, with a trailing `%%`, more than N%% of routed records "
                "(default: 0 — any quarantine fails)"
            ),
        )
```

Notes:
- `type=int` is removed; the value flows through as a string and is parsed by `parse_quarantine_threshold`.
- `default=0` becomes `default="0"` so the unset case takes the same parse path as an explicit `--max-quarantined 0` (and still maps to `("count", 0)`).
- `metavar="N"` becomes `metavar="N[%]"`.
- The `%%` in the help string is the argparse escape for a literal `%` — argparse uses `%`-interpolation against the parser namespace when rendering help, so a raw `%` would crash help generation. Verify by running `uv run lintle clean --help` after the change (Step 11) and confirming the help text shows `more than N% of routed records`.

- [ ] **Step 5: Replace the validation block in `main()`.**

In `src/lintle/cli.py`, locate the current validation block (in `main()`, immediately after the `--jobs` validation):

```python
    if args.max_quarantined < 0:
        print(
            f"error: --max-quarantined must be >= 0 (got {args.max_quarantined})",
            file=sys.stderr,
        )
        return 2
```

Replace it with:

```python
    try:
        quarantine_mode, quarantine_threshold = parse_quarantine_threshold(
            args.max_quarantined
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
```

The helper's error message for a negative count is `--max-quarantined must be >= 0 (got -1)`; main prepends `error: ` and prints to stderr, so the resulting line contains the substring `--max-quarantined must be >= 0` that the existing `test_max_quarantined_rejects_negative_value` asserts.

- [ ] **Step 6: Replace the exit-decision block at the end of `main()`.**

Locate the existing block (the final two lines of `main()` before the implicit return):

```python
    total_quarantined = sum(s.quarantined_count for s in all_stats)
    return 1 if total_quarantined > args.max_quarantined else 0
```

Replace it with:

```python
    total_quarantined = sum(s.quarantined_count for s in all_stats)
    if quarantine_mode == "count":
        return 1 if total_quarantined > quarantine_threshold else 0
    # Rate mode: cross-multiplied (`100*q > p*r`) to avoid divide-by-zero on
    # an empty corpus and float drift at the boundary. See design §3.
    total_routed = sum(s.clean_count + s.quarantined_count for s in all_stats)
    if 100 * total_quarantined > quarantine_threshold * total_routed:
        return 1
    return 0
```

- [ ] **Step 7: Update the "Exit codes" epilog.**

Locate the `_EPILOG = """\` block near the top of `cli.py` (it begins right after the `_DEFAULT_OUTPUT` constant). The current `Exit codes:` section is:

```
Exit codes:
  0    no records quarantined — every defect repaired (or under --max-quarantined)
  1    more than --max-quarantined records were quarantined (default threshold: 0)
  2    operational error (missing input, disk shortfall, file failure)
  130  interrupted (Ctrl-C)
```

Replace those four lines with:

```
Exit codes:
  0    quarantine count (or rate) is at or below --max-quarantined
  1    quarantine count (or rate) exceeded --max-quarantined (default: 0 — any quarantine fails)
  2    operational error (missing input, disk shortfall, file failure)
  130  interrupted (Ctrl-C)
```

- [ ] **Step 8: Update the `main()` docstring.**

The current docstring inside `def main(argv=None):` reads:

```python
    """Entry point for the ``lintle`` console script.

    Returns the process exit code: ``0`` = total quarantined is at or below
    ``--max-quarantined`` (default 0); ``1`` = more than ``--max-quarantined``
    records quarantined; ``2`` = operational error (no input files, disk
    shortfall, or a file that failed to process); ``130`` = interrupted with
    Ctrl-C.
    """
```

Replace it with:

```python
    """Entry point for the ``lintle`` console script.

    Returns the process exit code: ``0`` = quarantine count (or rate) is at
    or below ``--max-quarantined``; ``1`` = it exceeded the threshold
    (default ``0`` — any quarantine fails); ``2`` = operational error (no
    input files, disk shortfall, or a file that failed to process); ``130``
    = interrupted with Ctrl-C. The threshold accepts either an integer
    record count (``--max-quarantined 100``) or a percentage of routed
    records (``--max-quarantined 1%``); see :func:`parse_quarantine_threshold`.
    """
```

- [ ] **Step 9: Add the CHANGELOG entry.**

In `CHANGELOG.md`, the `## [Unreleased]` section currently starts with a `### Changed` subsection (the fsync durability entry). Insert a new `### Added` subsection between the `## [Unreleased]` heading and `### Changed`:

```markdown
## [Unreleased]

### Added

- `--max-quarantined` (on both `validate` and `clean`) now accepts a trailing
  `%` to express the exit-code threshold as a **rate** rather than an absolute
  count. `--max-quarantined 1%` exits non-zero if more than 1% of routed
  records (`clean_count + quarantined_count`) were quarantined; the integer
  form (`--max-quarantined 100`) is unchanged and the default `0` still means
  "any quarantine fails". The two modes are mutually exclusive by construction
  — a single value is either a count or a rate, never both — which sidesteps
  the combination semantics that a separate `--max-quarantined-pct` flag would
  have forced. Comparison is strictly greater (`100*q > p*r`,
  cross-multiplied to avoid divide-by-zero on an empty corpus and float drift
  at the boundary); `0%` ≡ `0` and `100%` effectively never trips. Design at
  `docs/superpowers/specs/2026-05-27-max-quarantined-percentage-design.md`.

### Changed
```

(Leave the existing `### Changed` heading and its content untouched.)

- [ ] **Step 10: Run the driving test; verify it now passes.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run pytest tests/test_cli.py::TestMaxQuarantinedThreshold::test_pct_under_threshold_passes -v
```

Expected: passes.

- [ ] **Step 11: Run the full suite; confirm no regressions.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run pytest -q
```

Expected: all tests pass (the baseline + 14 from Task 1 + 1 from this task). In particular, every test in `TestMaxQuarantinedThreshold` (including `test_max_quarantined_rejects_negative_value` and `test_max_quarantined_default_is_zero_legacy_behavior`) must still pass unmodified.

- [ ] **Step 12: Smoke-test the help text and rate-mode CLI.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run lintle clean --help | grep -A1 -- "--max-quarantined"
```

Expected: the help text mentions `N[%]` and `more than N% of routed records` — a single literal `%`, not `%%`. If you see `%%`, the argparse escape failed; revisit Step 4.

- [ ] **Step 13: Lint and format check.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run ruff check . && uv run ruff format --check .
```

Expected: clean.

- [ ] **Step 14: Commit.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && git add src/lintle/cli.py tests/test_cli.py CHANGELOG.md && git commit -m "$(cat <<'EOF'
feat(cli): support trailing % in --max-quarantined for rate threshold

--max-quarantined now accepts either an integer record count
(--max-quarantined 100, unchanged) or a percentage of routed records
(--max-quarantined 1%). The two modes are mutually exclusive by
construction; default "0" preserves the existing "any quarantine fails"
contract. Comparison is strictly greater, cross-multiplied (100*q > p*r)
to avoid divide-by-zero on an empty corpus and float drift at the
boundary. Validation stays in main() returning exit 2, preserving the
issue-#13 negative-value contract. Help text, epilog "Exit codes" block,
main() docstring, and CHANGELOG updated. Design committed at 73aa680.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: a single commit. Signing must succeed — do not pass `--no-gpg-sign`.

---

## Task 3: Round out rate-mode integration tests

**Files:**
- Modify: `tests/test_cli.py` — append five more tests to `TestMaxQuarantinedThreshold` covering the remaining rate-mode behaviours from spec §3 and §4.

The wiring is in place after Task 2; this task strengthens coverage with the edge cases the spec calls out as normative: the strictly-greater boundary, the over-threshold fail, `validate` parity, malformed input, and out-of-range percentages.

- [ ] **Step 1: Append the five tests to `TestMaxQuarantinedThreshold`.**

Add at the end of the class, after `test_pct_under_threshold_passes`:

```python
    def test_pct_over_threshold_fails(self, tmp_path, line1, line2):
        # 1 bad of 100 routed = 1.0%. `--max-quarantined 0.5%` is below
        # that, so the run exits 1.
        src = self._write_n_good_and_one_bad(tmp_path, line1, line2, n_good=99)
        out = tmp_path / "out"

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out),
                "--jobs",
                "1",
                "--max-quarantined",
                "0.5%",
            ]
        )

        assert rc == 1

    def test_pct_at_exact_boundary_passes(self, tmp_path, line1, line2):
        # 1 bad of 100 routed = exactly 1.0%. Strictly-greater semantics
        # (matching count mode) mean exactly-at-boundary passes.
        src = self._write_n_good_and_one_bad(tmp_path, line1, line2, n_good=99)
        out = tmp_path / "out"

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out),
                "--jobs",
                "1",
                "--max-quarantined",
                "1%",
            ]
        )

        assert rc == 0

    def test_pct_hundred_percent_never_fails(self, tmp_path, line1, line2):
        # 100% is the upper bound. The cross-multiplied comparison
        # `100*q > 100*r` reduces to `q > r`, which is structurally
        # impossible (quarantined <= routed). Even an all-bad input
        # passes a 100% gate.
        src = self._write_one_bad_record(tmp_path, line1, line2)
        out = tmp_path / "out"

        rc = cli.main(
            [
                "clean",
                str(src),
                "--out-dir",
                str(out),
                "--jobs",
                "1",
                "--max-quarantined",
                "100%",
            ]
        )

        assert rc == 0

    def test_pct_applies_to_validate(self, tmp_path, line1, line2):
        src = self._write_n_good_and_one_bad(tmp_path, line1, line2, n_good=99)

        rc = cli.main(
            ["validate", str(src), "--jobs", "1", "--max-quarantined", "5%"]
        )

        assert rc == 0

    def test_pct_malformed_returns_2(self, tmp_path, line1, line2, capsys):
        src = self._write_one_bad_record(tmp_path, line1, line2)

        rc = cli.main(
            ["validate", str(src), "--jobs", "1", "--max-quarantined", "1.2.3%"]
        )

        assert rc == 2
        assert "invalid percentage" in capsys.readouterr().err

    def test_pct_out_of_range_returns_2(self, tmp_path, line1, line2, capsys):
        src = self._write_one_bad_record(tmp_path, line1, line2)

        rc = cli.main(
            ["validate", str(src), "--jobs", "1", "--max-quarantined", "150%"]
        )

        assert rc == 2
        assert "percentage must be in 0..100" in capsys.readouterr().err
```

- [ ] **Step 2: Run the six new tests; verify all pass.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run pytest tests/test_cli.py::TestMaxQuarantinedThreshold -v
```

Expected: every test in the class passes — the six new ones plus the four pre-existing count-mode tests plus `test_pct_under_threshold_passes` from Task 2.

- [ ] **Step 3: Run the full suite.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Lint and format check.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run ruff check . && uv run ruff format --check .
```

Expected: clean.

- [ ] **Step 5: Commit.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && git add tests/test_cli.py && git commit -m "$(cat <<'EOF'
test(cli): cover --max-quarantined rate-mode edge cases

Strictly-greater boundary (1.0% rate vs 1% threshold passes), over-threshold
fail (0.5% threshold on a 1.0% corpus), 100% upper-bound (never trips,
even on an all-bad input), validate parity, malformed value (1.2.3%)
returns 2, and out-of-range percentage (150%) returns 2. All six
exercise the wiring landed in the previous commit; no production code
changes here.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: a single test-only commit.

---

## Final verification

After Task 3's commit, do a clean end-to-end check of the worktree before moving to PR.

- [ ] **Step 1: Full suite + lint + format, one more time, clean run.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 2: Review the branch's commit log.**

```bash
cd /Users/andrei/Developer/lintle/.worktrees/feature-max-quarantined-percentage && git log --oneline develop..HEAD
```

Expected: five commits in order (most recent first) — `test(cli): cover --max-quarantined rate-mode edge cases`, `feat(cli): support trailing % in --max-quarantined for rate threshold`, `feat(cli): add parse_quarantine_threshold helper`, `docs(cli): add implementation plan for --max-quarantined percentage` (the plan itself), and `docs(cli): design --max-quarantined percentage threshold` (the spec).

The integration tests in Task 3 are the smoke test — they exercise the full CLI through `cli.main()` with the same fixtures the suite uses. No additional manual smoke is required before opening the PR.

---

## Out of plan (already deferred in the spec, do NOT do)

- Do not add a `--max-quarantined-pct` flag.
- Do not surface the threshold or mode in `--report json`.
- Do not add a `--fail-on-regression` mode to `lintle diff`.
- Do not change `pipeline.py`, `repair.py`, `tle.py`, or `report.py` — the feature lives entirely in `cli.py` and its tests.

These are listed in §6 (Out of scope) of the spec.
