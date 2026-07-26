# v0.5 Modernization: validator test-hardening + humanize the human display — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the `tle.py` validator oracle with boundary + property-based tests and adopt `humanize` for the human display, landing into the pending v0.5.0 release.

**Architecture:** Two independent task groups. Group A (Tasks 1–6) is dev/test only — explicit boundary tests for every `_check_semantics` range, `hypothesis` property tests for checksum / ranges / repair contract, `repair.py` multi-line combo tests, and `pytest-xdist`. Group B (Tasks 7–9) adopts `humanize` (a second runtime dep) confined to the human display leaves (`summary.py`, `cli_progress.py`); machine outputs stay raw. Task 10 syncs docs/policy.

**Tech Stack:** Python 3.14 · uv · pytest · hypothesis · pytest-xdist (dev) · humanize, rich (runtime) · ruff.

**Spec:** `docs/superpowers/archive/specs/2026-06-07-test-hardening-and-humanize-design.md`

**Execution setup (do once, before Task 1):**
- Create an isolated worktree off `develop` (via `superpowers:using-git-worktrees`):
  `git worktree add .worktrees/feature-v05-modernization -b feature/v05-modernization develop`,
  then `cd .worktrees/feature-v05-modernization`, `uv sync`, `ln -s ../../data data`.
- **Commit signing:** commits are signed. If the 1Password SSH agent is unavailable, sign with the local key:
  `git -c gpg.ssh.program=ssh-keygen -c user.signingkey=$HOME/.ssh/id_ed25519.pub commit …`. Never use `--no-gpg-sign`.
- End every commit message body with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- After each task: `uv run pytest && uv run ruff check . && uv run ruff format --check .` must pass.

**Verified facts used below (from the live tree):**
- `tle.validate_body(body, lineno) -> list[str]`; `tle.validate_line`, `tle.validate_record`, `tle.compute_checksum`; private `tle._check_semantics(body, lineno)`. Fixtures `line1`/`line2` (conftest, the canonical NORAD-5 pair).
- `_check_semantics` ranges + error substrings (exact): epoch day-of-year `0.0 < day < 367.0` "epoch day-of-year"; inclination `0.0 <= inc <= 180.0` "inclination"; RAAN `0.0 <= raan < 360.0` "RAAN"; eccentricity `0.0 <= ecc < 1.0` "eccentricity"; argument-of-perigee `0.0 <= argp < 360.0` "argument of perigee"; mean-anomaly `0.0 <= mean_anom < 360.0` "mean anomaly"; mean-motion `mean_motion > 0.0` "mean motion"; parse failure → "a numeric field could not be parsed for semantic checks".
- Body slices (0-indexed): inclination `[8:16]` (w8), RAAN `[17:25]` (w8), eccentricity `[26:33]` (w7, `int(...)/1e7`), argp `[34:42]` (w8), mean-anom `[43:51]` (w8), mean-motion `[52:63]` (w11); line-1 epoch `[20:32]` (w12, form `DDD.FFFFFFFF`).
- `repair.repair_line(raw: bytes, lineno, source_line_no) -> (str, list[FixClass], None) | (None, list[FixClass], Diagnostic)`; `repair.repair_record(raw1, src1, raw2, src2) -> Accepted | Quarantined`. `Accepted(line1, line2, fixes)`, `Quarantined(raw_lines, source_lines, primary, related=())`. Both-fail → `primary=diag1, related=(diag2,)`; one-fail → `related=()`.
- humanize 4.15.0; pins: `humanize>=4,<5`, `hypothesis>=6,<7`, `pytest-xdist>=3,<4`.

---

### Task 1: `tle.py` semantic-range boundary coverage

**Files:**
- Test: `tests/test_tle.py` (add a new `class TestSemanticBoundaries:` after the existing `TestValidateBody`)

- [ ] **Step 1: Write the failing boundary tests**

Append to `tests/test_tle.py`:

```python
class TestSemanticBoundaries:
    """Explicit boundary-value tests for every _check_semantics range.

    Inclusive edges are accepted; exclusive edges are rejected. These
    document the intended bounds and anchor the hypothesis property tests.
    """

    # epoch day-of-year (line 1): 0.0 < day < 367.0 (both exclusive)
    def test_epoch_day_lower_exclusive_rejected(self, line1):
        body = line1[:20] + "000.00000000" + line1[32:68]  # day = 0.0
        assert any("epoch day-of-year" in e for e in tle.validate_body(body, 1))

    def test_epoch_day_just_above_zero_accepted(self, line1):
        body = line1[:20] + "000.00100000" + line1[32:68]  # day = 0.001
        assert not any("epoch day-of-year" in e for e in tle.validate_body(body, 1))

    def test_epoch_day_upper_exclusive_rejected(self, line1):
        body = line1[:20] + "367.00000000" + line1[32:68]  # day = 367.0
        assert any("epoch day-of-year" in e for e in tle.validate_body(body, 1))

    def test_epoch_day_just_below_upper_accepted(self, line1):
        body = line1[:20] + "366.99900000" + line1[32:68]  # day = 366.999
        assert not any("epoch day-of-year" in e for e in tle.validate_body(body, 1))

    # inclination (line 2): 0.0 <= inc <= 180.0 (both inclusive)
    def test_inclination_lower_inclusive_accepted(self, line2):
        body = line2[:8] + "000.0000" + line2[16:68]  # inc = 0.0
        assert not any("inclination" in e for e in tle.validate_body(body, 2))

    def test_inclination_upper_inclusive_accepted(self, line2):
        body = line2[:8] + "180.0000" + line2[16:68]  # inc = 180.0
        assert not any("inclination" in e for e in tle.validate_body(body, 2))

    def test_inclination_just_above_upper_rejected(self, line2):
        body = line2[:8] + "180.0001" + line2[16:68]  # inc = 180.0001
        assert any("inclination" in e for e in tle.validate_body(body, 2))

    # RAAN (line 2): 0.0 <= raan < 360.0 (inclusive lower, exclusive upper)
    def test_raan_upper_exclusive_rejected(self, line2):
        body = line2[:17] + "360.0000" + line2[25:68]  # raan = 360.0
        assert any("RAAN" in e for e in tle.validate_body(body, 2))

    def test_raan_just_below_upper_accepted(self, line2):
        body = line2[:17] + "359.9999" + line2[25:68]  # raan = 359.9999
        assert not any("RAAN" in e for e in tle.validate_body(body, 2))

    # eccentricity (line 2): 0.0 <= ecc < 1.0; field = int(body[26:33]) / 1e7
    def test_eccentricity_zero_accepted(self, line2):
        body = line2[:26] + "0000000" + line2[33:68]  # ecc = 0.0
        assert not any("eccentricity" in e for e in tle.validate_body(body, 2))

    def test_eccentricity_max_field_accepted(self, line2):
        # 9999999 -> 0.9999999, the largest value a 7-digit field can encode;
        # the < 1.0 upper bound is therefore structurally unreachable via
        # column data (documented: the rejection branch is defensive only).
        body = line2[:26] + "9999999" + line2[33:68]
        assert not any("eccentricity" in e for e in tle.validate_body(body, 2))

    # argument of perigee (line 2): 0.0 <= argp < 360.0
    def test_argp_upper_exclusive_rejected(self, line2):
        body = line2[:34] + "360.0000" + line2[42:68]  # argp = 360.0
        assert any("argument of perigee" in e for e in tle.validate_body(body, 2))

    def test_argp_just_below_upper_accepted(self, line2):
        body = line2[:34] + "359.9999" + line2[42:68]  # argp = 359.9999
        assert not any("argument of perigee" in e for e in tle.validate_body(body, 2))

    # mean anomaly (line 2): 0.0 <= mean_anom < 360.0
    def test_mean_anomaly_upper_exclusive_rejected(self, line2):
        body = line2[:43] + "360.0000" + line2[51:68]  # mean_anom = 360.0
        assert any("mean anomaly" in e for e in tle.validate_body(body, 2))

    def test_mean_anomaly_just_below_upper_accepted(self, line2):
        body = line2[:43] + "359.9999" + line2[51:68]  # mean_anom = 359.9999
        assert not any("mean anomaly" in e for e in tle.validate_body(body, 2))

    # mean motion (line 2): mean_motion > 0.0 (strictly positive)
    def test_mean_motion_small_positive_accepted(self, line2):
        body = line2[:52] + "00.00010000" + line2[63:68]  # 0.0001 rev/day
        assert not any("mean motion" in e for e in tle.validate_body(body, 2))

    # numeric-parse-failure path (call _check_semantics directly: it assumes
    # columns already passed, so a parse-breaking field reaches the except branch)
    def test_unparseable_numeric_field_reports_parse_failure(self, line2):
        body = line2[:26] + "       " + line2[33:68]  # eccentricity = 7 spaces
        errs = tle._check_semantics(body, 2)
        assert any("could not be parsed" in e for e in errs)
```

- [ ] **Step 2: Run to verify they pass against current code**

Run: `uv run pytest tests/test_tle.py::TestSemanticBoundaries -v`
Expected: all PASS. (These exercise existing branches; if any *fails*, a body slice width is wrong — fix the replacement string width to match the slice, e.g. RAAN replacement must be exactly 8 chars.)

- [ ] **Step 3: Confirm they cover the previously-uncovered branches**

Run: `uv run pytest tests/test_tle.py --cov=lintle.tle --cov-report=term-missing --cov-branch`
Expected: the `_check_semantics` lines (epoch/RAAN/ecc/argp/mean-anom/parse-failure) no longer appear under "Missing".

- [ ] **Step 4: Lint + format**

Run: `uv run ruff check tests/test_tle.py && uv run ruff format tests/test_tle.py`
Expected: clean (format may rewrite; re-run `ruff format --check .`).

- [ ] **Step 5: Commit**

```bash
git add tests/test_tle.py
git commit -m "test(tle): explicit boundary-value coverage for every semantic range

Anchors the inclusive/exclusive edges of epoch-day, inclination, RAAN,
eccentricity, argument-of-perigee, mean-anomaly, mean-motion, plus the
numeric-parse-failure path. Documents that the eccentricity < 1.0 upper
bound is structurally unreachable via 7-digit column data.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Add `hypothesis`; property-test the checksum

**Files:**
- Modify: `pyproject.toml` (`[dependency-groups].dev`), `uv.lock`
- Test: `tests/test_tle.py` (new `class TestChecksumProperties:`)

- [ ] **Step 1: Add the dev dependency**

Run: `uv add --group dev 'hypothesis>=6,<7'`
Then: `uv sync`
Expected: `pyproject.toml` `dev` list now includes `"hypothesis>=6,<7"`; `uv.lock` updated.

- [ ] **Step 2: Write the failing property test**

Add to `tests/test_tle.py` (and add `from hypothesis import given, strategies as st` to the imports at the top):

```python
class TestChecksumProperties:
    """Property-based invariants for the mod-10 checksum."""

    @given(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=68, max_size=68))
    def test_checksum_is_a_single_digit(self, body):
        assert tle.compute_checksum(body) in range(10)

    @given(st.text(alphabet="0123456789 .-+", min_size=68, max_size=68))
    def test_appended_checksum_satisfies_checksum_error(self, body):
        line = body + str(tle.compute_checksum(body))
        assert tle.checksum_error(line) is None

    @given(
        st.text(alphabet="0123456789 .-+", min_size=68, max_size=68),
        st.integers(min_value=1, max_value=9),
    )
    def test_wrong_checksum_digit_is_rejected(self, body, offset):
        correct = tle.compute_checksum(body)
        wrong = (correct + offset) % 10
        assert tle.checksum_error(body + str(wrong)) is not None
```

- [ ] **Step 3: Run to verify it passes**

Run: `uv run pytest tests/test_tle.py::TestChecksumProperties -v`
Expected: PASS (hypothesis runs ~100 examples per property). If `test_wrong_checksum_digit_is_rejected` flakes, it means a generated body+wrong digit still validated — investigate before weakening (it should not happen: `wrong != correct` by construction).

- [ ] **Step 4: Lint + format + full suite**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: all PASS/clean.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock tests/test_tle.py
git commit -m "test(tle): add hypothesis; property-test the mod-10 checksum

hypothesis is dev-only (never imported at runtime). Properties: checksum
is always a single digit; appending it satisfies checksum_error; any wrong
digit is rejected.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Property-test the semantic ranges

**Files:**
- Test: `tests/test_tle.py` (new `class TestSemanticRangeProperties:`)

- [ ] **Step 1: Write the failing property test**

Add to `tests/test_tle.py`:

```python
class TestSemanticRangeProperties:
    """Fuzz inclination around its [0, 180] bound on a valid line-2 body."""

    @given(st.floats(min_value=0.0, max_value=270.0, allow_nan=False, allow_infinity=False))
    @settings(max_examples=300)
    def test_inclination_accepted_iff_in_range(self, line2, inc):
        # Non-negative only: a leading '-' would fail column layout (a column
        # error, not an inclination error), desyncing the oracle. 0..270 still
        # spans in-range and above-range. Width is always 8 (e.g. "270.0000").
        field = f"{inc:08.4f}"
        body = line2[:8] + field + line2[16:68]
        in_range = 0.0 <= float(field) <= 180.0  # value as the column encodes it
        has_error = any("inclination" in e for e in tle.validate_body(body, 2))
        assert has_error != in_range
```

Add `settings` to the hypothesis import: `from hypothesis import given, settings, strategies as st`.

- [ ] **Step 2: Run to verify it passes**

Run: `uv run pytest tests/test_tle.py::TestSemanticRangeProperties -v`
Expected: PASS. Note: the test compares against `float(field)` (the value as the 8-char column actually encodes it), so width-truncation can't desync the oracle from the assertion.

- [ ] **Step 3: Full suite + lint/format**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: PASS/clean.

- [ ] **Step 4: Commit**

```bash
git add tests/test_tle.py
git commit -m "test(tle): property-test the inclination [0,180] boundary

hypothesis fuzzes inclination across and around its bounds on an otherwise
valid line-2 body; the validator must reject iff out of range.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Property-test the repair → revalidate contract

**Files:**
- Test: `tests/test_repair.py` (new `class TestRepairContractProperties:`)

- [ ] **Step 1: Write the failing property test**

Add to `tests/test_repair.py` (imports: `from hypothesis import given, settings, strategies as st`; ensure `from lintle import repair, tle` and `from lintle.categories import FixClass` are present):

```python
class TestRepairContractProperties:
    """The repair contract: a committed line is always tle-valid; a quarantine
    never claims success. Fuzz benign normalizations around the canonical line."""

    @given(
        prefix_ws=st.text(alphabet=" ", max_size=3),
        suffix=st.sampled_from(["", "\n", "\r\n", " ", "  ", "\\"]),
        drop_checksum=st.booleans(),
    )
    @settings(max_examples=200)
    def test_repaired_line_is_always_valid_or_quarantined(
        self, line1, prefix_ws, suffix, drop_checksum
    ):
        base = line1[:68] if drop_checksum else line1
        raw = (prefix_ws + base + suffix).encode("ascii")
        clean, fixes, diag = repair.repair_line(raw, 1, source_line_no=7)
        if diag is None:
            # committed: must pass full validation (validated-transformation)
            assert clean is not None
            assert tle.validate_line(clean, 1) == []
        else:
            # quarantined: no committed line, and provenance is recorded
            assert clean is None
            assert diag.source_line_nos == (7,)

    def test_record_fixes_reflect_a_single_lines_checksum_reconstruct(
        self, line1, line2
    ):
        # Drop line-2's checksum so its repair reaches RECONSTRUCTED_CHECKSUM;
        # the record-level fixes include it even though line-1 needed none.
        result = repair.repair_record(
            line1.encode("ascii"), 1, line2[:68].encode("ascii"), 2
        )
        assert isinstance(result, repair.Accepted)
        assert FixClass.RECONSTRUCTED_CHECKSUM in result.fixes
```

- [ ] **Step 2: Run to verify it passes**

Run: `uv run pytest tests/test_repair.py::TestRepairContractProperties -v`
Expected: PASS. If `test_repaired_line_is_always_valid_or_quarantined` fails, that is a *real bug* (a committed line that doesn't validate) — stop and report it; do not weaken the test.

- [ ] **Step 3: Full suite + lint/format**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: PASS/clean.

- [ ] **Step 4: Commit**

```bash
git add tests/test_repair.py
git commit -m "test(repair): property-test the apply->revalidate->commit contract

Every committed line must pass tle.validate_line; a quarantine never returns
a committed line and records source provenance. Confirms the record tier
reflects the strongest per-line repair.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `repair.py` multi-line combo tests

**Files:**
- Test: `tests/test_repair.py` (new `class TestRepairRecordComboCases:`)

- [ ] **Step 1: Write the failing combo tests**

Add to `tests/test_repair.py` (ensure `from lintle.diagnostics import RuleID` is imported):

```python
class TestRepairRecordComboCases:
    """Multi-line failure orchestration: primary/related selection + tiers."""

    def _bad_line(self, lineno):
        # 69 chars that pass length but fail column layout (all 'Z').
        return ("Z" * 69).encode("ascii")

    def test_both_lines_fail_primary_is_line1_related_is_line2(self):
        result = repair.repair_record(self._bad_line(1), 1, self._bad_line(2), 2)
        assert isinstance(result, repair.Quarantined)
        assert result.primary.source_line_nos == (1,)
        assert len(result.related) == 1
        assert result.related[0].source_line_nos == (2,)

    def test_only_line2_fails_related_is_empty(self, line1):
        result = repair.repair_record(line1.encode("ascii"), 5, self._bad_line(2), 6)
        assert isinstance(result, repair.Quarantined)
        assert result.primary.source_line_nos == (6,)
        assert result.related == ()

    def test_catalog_mismatch_after_both_repair(self, line1, line2):
        other_body = "2 09999" + line2[7:68]
        other = other_body + str(tle.compute_checksum(other_body))
        result = repair.repair_record(
            line1.encode("ascii"), 1, other.encode("ascii"), 2
        )
        assert isinstance(result, repair.Quarantined)
        assert result.primary.rule_id == RuleID.CATALOG_MISMATCH
        assert result.primary.source_line_nos == (1, 2)

    def test_both_clean_lines_accepted_with_no_fixes(self, line1, line2):
        result = repair.repair_record(
            line1.encode("ascii"), 1, line2.encode("ascii"), 2
        )
        assert isinstance(result, repair.Accepted)
        assert result.fixes == []
```

- [ ] **Step 2: Run to verify it passes**

Run: `uv run pytest tests/test_repair.py::TestRepairRecordComboCases -v`
Expected: PASS. (If `_bad_line` is unexpectedly *repaired*, swap the payload for one that fails column layout but passes length — verify with `repair.repair_line(("Z"*69).encode("ascii"), 1, 1)[2] is not None`.)

- [ ] **Step 3: Full suite + lint/format**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: PASS/clean.

- [ ] **Step 4: Commit**

```bash
git add tests/test_repair.py
git commit -m "test(repair): multi-line combo cases (primary/related, catalog mismatch)

Pins the orchestration: both-fail -> primary=line1, related=(line2,);
one-fail -> related empty; catalog mismatch after both repair carries the
paired source lines.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Add `pytest-xdist`; run the suite in parallel

**Files:**
- Modify: `pyproject.toml` (`[dependency-groups].dev` and `[tool.pytest.ini_options].addopts`), `uv.lock`

- [ ] **Step 1: Add the dev dependency**

Run: `uv add --group dev 'pytest-xdist>=3,<4'`
Then: `uv sync`

- [ ] **Step 2: Enable parallel by default**

Edit `pyproject.toml` `[tool.pytest.ini_options]`:

```toml
addopts = "-m 'not slow' -n auto"
```

(Current value is `addopts = "-m 'not slow'"`.)

- [ ] **Step 3: Run the full suite to verify isolation holds in parallel**

Run: `uv run pytest`
Expected: same pass count as before, completed across multiple workers (header shows `N workers`). If any test fails only under xdist, it has hidden shared state (fixed path/port/global) — fix the test's isolation (prefer `tmp_path`); do not disable xdist.

- [ ] **Step 4: Lint + format**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "test: run the suite in parallel with pytest-xdist (-n auto)

Dev-only; tests already use isolated tmp_path fixtures, so no test changes
needed. Faster local + CI feedback.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Add `humanize`; humanize the summary-panel duration

**Files:**
- Modify: `pyproject.toml` (`[project].dependencies`), `uv.lock`
- Modify: `src/lintle/summary.py:20-29` (replace `_humanize_duration`), `:86` (call site unchanged in shape)
- Test: `tests/test_summary.py:56-59` (update assertions)

- [ ] **Step 1: Add the runtime dependency**

Run: `uv add 'humanize>=4,<5'`
Then: `uv sync`
Expected: `[project].dependencies` now `["rich>=15,<16", "humanize>=4,<5"]`; `uv.lock` updated.

- [ ] **Step 2: Update the failing test first (TDD: assertion changes to new format)**

Replace `tests/test_summary.py:56-59` (`TestHelpers.test_humanize_duration`) with:

```python
    def test_humanize_duration(self):
        assert summary._humanize_duration(45.2) == "45 seconds"
        assert summary._humanize_duration(124.0) == "2 minutes and 4 seconds"
        assert summary._humanize_duration(3661.0) == "1 hour, 1 minute and 1 second"
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_summary.py::TestHelpers::test_humanize_duration -v`
Expected: FAIL (current code returns `"45.2s"` / `"2m 04s"` / `"1h 01m 01s"`).

- [ ] **Step 4: Reimplement `_humanize_duration` with humanize**

In `src/lintle/summary.py`, add `import humanize` to the imports, and replace lines 20-29:

```python
def _humanize_duration(seconds):
    """Return a human-readable duration string for ``seconds`` (e.g.
    ``"2 minutes and 4 seconds"``), via humanize for the operator panel."""
    return humanize.precisedelta(seconds, minimum_unit="seconds", format="%d")
```

(The call site at line 86, `("elapsed", _humanize_duration(run["elapsed_seconds"]), "")`, is unchanged.)

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_summary.py -v`
Expected: PASS.

- [ ] **Step 6: Full suite + lint/format**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: PASS/clean.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/lintle/summary.py tests/test_summary.py
git commit -m "feat(summary): humanize the panel duration via precisedelta

Adds humanize (2nd runtime dep, human display only). The summary 'elapsed'
row now reads e.g. '2 minutes and 4 seconds'. Machine outputs unchanged.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Humanize the roster sizes (`naturalsize`, gnu) + fix the unit-label bug

**Files:**
- Modify: `src/lintle/cli_progress.py:214-222` (replace `_format_size`), `:239`/`:241` (call sites unchanged in shape)
- Test: `tests/test_cli_progress.py:15-25` (`TestFormatSize`), `:43-46` (`TestRenderRoster`)

- [ ] **Step 1: Update the failing tests first**

Replace `tests/test_cli_progress.py:15-25` (`TestFormatSize`) with:

```python
    def test_bytes_below_one_kib(self):
        assert cli_progress._format_size(0) == "0B"
        assert cli_progress._format_size(512) == "512B"

    def test_kilobytes(self):
        assert cli_progress._format_size(1024) == "1.0K"
        assert cli_progress._format_size(1536) == "1.5K"

    def test_gigabytes(self):
        assert cli_progress._format_size(1024**3) == "1.0G"
        assert cli_progress._format_size(3 * 1024**3) == "3.0G"
```

And in `tests/test_cli_progress.py:43-46` (`TestRenderRoster`) update the size assertions:

```python
        assert "1.5K" in out
        assert "512B" in out
        assert "total" in out
        assert "2.0K" in out  # 1536 + 512 = 2048 bytes
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cli_progress.py::TestFormatSize tests/test_cli_progress.py::TestRenderRoster -v`
Expected: FAIL (current returns `"512 B"` / `"1.5 KB"` / `"3.0 GB"`).

- [ ] **Step 3: Reimplement `_format_size` with humanize**

In `src/lintle/cli_progress.py`, add `import humanize` to the imports, and replace lines 214-222:

```python
def _format_size(n_bytes):
    """Render a byte count compactly for the roster (e.g. ``"3.0G"``), via
    humanize's gnu units — fixes the prior binary-math/decimal-label mismatch."""
    return humanize.naturalsize(n_bytes, gnu=True)
```

(Call sites at lines 239/241, `_format_size(size)` / `_format_size(total)`, are unchanged.)

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_cli_progress.py -v`
Expected: PASS. (`_format_elapsed` and its test `TestFormatElapsed` are untouched — `2:04` stays.)

- [ ] **Step 5: Full suite + lint/format**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: PASS/clean.

- [ ] **Step 6: Commit**

```bash
git add src/lintle/cli_progress.py tests/test_cli_progress.py
git commit -m "feat(cli_progress): humanize roster sizes via naturalsize(gnu)

Roster file sizes now render as e.g. '3.0G' (was '3.0 GB' with binary math
on a decimal label — a unit-label bug). The live progress clock stays custom.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Guard test — machine outputs stay raw numbers

**Files:**
- Test: `tests/test_report.py` (add `class TestEnvelopeRawNumbers:`)

- [ ] **Step 1: Write the guard test**

Add to `tests/test_report.py` (a bare `FileStats(src_name=...)` is valid — every other field defaults):

```python
class TestEnvelopeRawNumbers:
    """The machine envelope carries raw numbers, never humanized strings —
    guards against a humanize leak into byte-deterministic output."""

    def test_elapsed_and_counts_are_numbers_not_strings(self):
        env = report.build_run_envelope(
            [report.FileStats(src_name="tle.txt", elapsed_seconds=12.0)],
            command="clean",
            started_at="2026-06-07T00:00:00Z",
            elapsed_seconds=124.0,
        )
        assert isinstance(env["run"]["elapsed_seconds"], float)
        assert isinstance(env["summary"]["files_processed"], int)
        assert isinstance(env["files"][0]["elapsed_seconds"], float)
```

- [ ] **Step 2: Run to verify it passes**

Run: `uv run pytest tests/test_report.py::TestEnvelopeRawNumbers -v`
Expected: PASS (the envelope already stores raw numbers; this locks it in).

- [ ] **Step 3: Full suite + lint/format**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: PASS/clean.

- [ ] **Step 4: Commit**

```bash
git add tests/test_report.py
git commit -m "test(report): guard that the run envelope stays raw numbers

Locks in that elapsed_seconds / counts are numeric in the machine envelope,
so a future humanize use can't leak formatted strings into report.json.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Docs & policy sync

**Files:**
- Modify: `ARCHITECTURE.md` (§7 runtime-dependency policy + Considered/deferred table; module-dependency notes)
- Modify: `CLAUDE.md` (Tech Stack runtime-deps line; module-dependency prose)
- Modify: `CHANGELOG.md` (`[Unreleased]`)

> **Note:** `CLAUDE.md` edits are "self-modification" — the push of this branch will need explicit user authorization at PR time. That's expected; do not skip the `CLAUDE.md` update.

- [ ] **Step 1: Update `ARCHITECTURE.md` §7**

- In the runtime-deps prose, change the single-dep statement to two: `rich>=15,<16` and `humanize>=4,<5` (human-display formatting; pure-Python, zero transitive deps; confined to the stderr/stdout panel, never structured output).
- In the "Considered & deferred" table, change the `humanize` disposition (add a row if absent): **Adopted** — "durations via `precisedelta`, roster sizes via `naturalsize(gnu)` on the human display only; never structured output."
- Add a one-line note that a 2026-06-07 re-audit re-confirmed every other candidate as rejected/deferred for the tabled reasons.

- [ ] **Step 2: Update `CLAUDE.md`**

- Tech Stack line: runtime deps are now **`rich>=15,<16`** + **`humanize>=4,<5`**.
- Module-dependency prose: note `summary.py` and `cli_progress.py` import `humanize` (human display only).

- [ ] **Step 3: Update `CHANGELOG.md` `[Unreleased]`**

Add under the existing `[Unreleased]` section:
- **Added:** `humanize` runtime dependency for human-readable durations/sizes on the `clean` panel/roster; `hypothesis` + `pytest-xdist` dev dependencies (property-based validator tests + parallel suite).
- **Changed:** the `clean` summary panel duration now reads e.g. `"2 minutes and 4 seconds"` and roster sizes e.g. `"3.0G"` (humanize) — a display-format change.

- [ ] **Step 4: Verify nothing else broke + lint/format**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: PASS/clean.

- [ ] **Step 5: Commit**

```bash
git add ARCHITECTURE.md CLAUDE.md CHANGELOG.md
git commit -m "docs: record humanize adoption + test-hardening in §7 / CLAUDE / CHANGELOG

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Done criteria

- `uv run pytest` green (new boundary + property + combo + guard tests pass), running in parallel.
- `uv run ruff check .` and `uv run ruff format --check .` clean.
- `humanize` is a runtime dep used only in `summary.py`/`cli_progress.py`; `report.*`, the sidecar, the checkpoint, and `cleaned/*` are byte-unchanged.
- Docs/policy reflect the second runtime dep and the test-infra additions.
- Open one PR against `develop` (rebase-and-merge); it folds into the pending v0.5.0 release.
