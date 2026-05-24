# Grouped Reject Exemplars Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace lintle's flat `reject_exemplars` buffer with per-category bounded sampling so the `validate` summary surfaces up to 5 exemplars *per* `RejectCategory`, sorted by descending occurrence count. No noisy defect class can drown out rarer ones.

**Architecture:** `FileStats.reject_exemplars` flips from `list[RejectEntry]` to `dict[RejectCategory, list[RejectEntry]]`. The pipeline's `_record_reject` uses a get-or-create insertion pattern capped at `_PER_CATEGORY_EXEMPLAR_BOUND = 5`. `format_reject_lines` walks categories sorted by total count (alphabetic tiebreak), emitting heading + exemplars + per-category remainder. `write_broken_file` (test-only helper) flattens the dict and sorts by `source_lines[0]` so its sidecar mirrors production encounter order. The on-disk `.broken.txt` streaming path is untouched — the full byte-faithful catalog still reaches disk.

**Tech Stack:** Python 3.11 · uv · pure stdlib runtime · pytest · ruff · `sgp4` (test oracle, dev-only).

**Authoritative spec:** `docs/superpowers/specs/2026-05-24-grouped-reject-exemplars-design.md` (commit `570c87f`). Read it before starting Task 2.

**Related parent spec:** `docs/superpowers/specs/2026-05-21-tle-corpus-cleaner-design.md` — the cleaner's overall design, including the critical-rule constant-memory invariant this change preserves.

**Project conventions:**
- All tests live in `tests/`, grouped into `Test*` classes by behavior.
- Conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, etc.).
- Trunk is `develop`; this work is a multi-file refactor and ships as a `feature/grouped-reject-exemplars` branch landed via **rebase-and-merge** (not squash, not merge commit) — see `CONTRIBUTING.md` § Git Workflow.
- After every code change, the full verification chain is `uv run pytest && uv run ruff check . && uv run ruff format --check .` — all three must pass before commit.

---

## Task 1: Set up isolated worktree

**Files:** none (worktree-creation only).

This change touches `src/lintle/pipeline.py`, `src/lintle/report.py`, `tests/test_pipeline.py`, `tests/test_report.py`, and `tests/test_cli.py` — a multi-file refactor. Per CLAUDE.md § Worktree Workflow, that means a feature branch in an isolated worktree.

- [ ] **Step 1: Confirm starting point**

Run from the main lintle checkout:

```bash
git status
git log --oneline -3
```

Expected: clean working tree on `develop`; HEAD is `570c87f docs: spec revisions from multi-AI adversarial review (issue #21)` or a later commit on `develop`.

- [ ] **Step 2: Create the worktree**

Run:

```bash
git worktree add .worktrees/feature-grouped-reject-exemplars -b feature/grouped-reject-exemplars develop
```

Expected: `Preparing worktree (new branch 'feature/grouped-reject-exemplars')` and a new directory `.worktrees/feature-grouped-reject-exemplars/`.

- [ ] **Step 3: Enter the worktree and install dev deps**

```bash
cd .worktrees/feature-grouped-reject-exemplars
uv sync
```

Expected: `uv sync` installs `pytest`, `ruff`, and `sgp4`. (All subsequent tasks run inside this directory unless told otherwise.)

- [ ] **Step 4: Symlink the corpus so the CLI works in the worktree**

```bash
ln -s ../../data data
```

Expected: `ls data/source/` shows `tle*.txt` and `TLEs.zip` (the symlink resolves into the main checkout's corpus).

- [ ] **Step 5: Sanity-check the baseline**

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Expected: all three pass. If any fails, stop and report — the worktree's starting state is broken and must be fixed before implementation begins.

---

## Task 2: Migrate the data structure (atomic refactor)

**Files:**
- Modify: `src/lintle/report.py` — `FileStats.reject_exemplars` default (lines 53–54 of the current file) and its docstring
- Modify: `src/lintle/pipeline.py` — `_EXEMPLAR_BOUND` constant (line 15) and `_record_reject` insertion (lines 262–276)
- Modify: `src/lintle/report.py` — `format_reject_lines` (lines 204–221) and `write_broken_file` (lines 152–163)
- Modify: `tests/test_pipeline.py` — `TestStreamingRejects` (lines 269–309)
- Modify: `tests/test_report.py` — `TestWriteBrokenFile` (lines 46–102) and `TestFormatRejectLines` (lines 139–162)

The data structure, its consumers, and its tests are mutually dependent — changing one in isolation leaves the codebase broken. This task is a single logical refactor across all four files plus their tests, committed atomically.

- [ ] **Step 1: Update `FileStats.reject_exemplars` default**

Open `src/lintle/report.py`. Find the `FileStats` dataclass (around line 27). Update the `reject_exemplars` field and its containing docstring:

```python
@dataclasses.dataclass
class FileStats:
    """Accumulated results for one processed source file.

    Three independent counters disambiguate what was previously a single
    ``total_records`` tally (issue #5): ``paired_records`` counts proper
    2-line entries; ``orphan_entries`` counts unpaired single lines surfaced
    as findings; ``input_lines_seen`` counts every physical line read,
    including blanks the pairing loop drops. The invariant
    ``paired_records + orphan_entries == clean_count + quarantined_count``
    holds — orphans still flow through ``_record_reject`` so they are tallied
    in ``quarantined_count`` and ``reject_categories['orphan-line']``.

    ``reject_exemplars`` is a *per-category bounded* sample of quarantined
    records keyed by ``RejectCategory``, used only by the human-facing
    ``validate`` summary; each per-category list is capped at
    ``pipeline._PER_CATEGORY_EXEMPLAR_BOUND`` so a single noisy category
    cannot crowd out rarer ones (issue #21). The byte-faithful full catalog
    is streamed to ``.broken.txt`` during processing. The cap is enforced
    by the pipeline, not by this dataclass, so tests can populate it freely
    via ``stats.reject_exemplars.setdefault(category, []).append(entry)``.
    """

    src_name: str
    paired_records: int = 0
    orphan_entries: int = 0
    input_lines_seen: int = 0
    clean_count: int = 0
    quarantined_count: int = 0
    fix_counts: dict = dataclasses.field(default_factory=dict)
    reject_categories: dict = dataclasses.field(default_factory=dict)
    reject_exemplars: dict = dataclasses.field(default_factory=dict)
    # NORAD IDs of records quarantined in this file, decoded once at
    # reject time from line-1 columns 3-7. Bounded by the satellite
    # catalog (~tens of thousands of IDs corpus-wide), so the in-memory
    # set is independent of reject count and keeps memory constant.
    quarantined_norad_ids: set = dataclasses.field(default_factory=set)
```

- [ ] **Step 2: Rename the pipeline constant and rewrite the insertion**

Open `src/lintle/pipeline.py`. Replace the constant definition (line 15) and its docstring comment:

```python
# How many quarantined records to retain in memory as exemplars per
# ``RejectCategory`` for the ``validate`` summary. The full byte-faithful
# catalog goes straight to the ``.broken.txt`` sidecar via
# ``BrokenFileWriter`` — this bound only caps the per-category in-memory
# display sample, so peak memory stays constant even on files where every
# record is corrupt. Total ceiling per file is bounded by
# ``|RejectCategory| × _PER_CATEGORY_EXEMPLAR_BOUND``.
_PER_CATEGORY_EXEMPLAR_BOUND = 5
```

Then in `_record_reject` (around line 262), replace the old insertion block:

```python
def _record_reject(stats, broken_writer, category, reason, raw_lines, source_lines):
    """Tally one quarantined record; stream its bytes to the broken sidecar.

    The in-memory ``reject_exemplars`` dict holds up to
    ``_PER_CATEGORY_EXEMPLAR_BOUND`` entries per ``RejectCategory`` so the
    ``validate`` summary surfaces every observed defect class (issue #21).
    The full byte-faithful catalog streams to the sidecar via
    ``BrokenFileWriter`` when one is open (``clean`` mode).
    """
    stats.quarantined_count += 1
    stats.reject_categories[category] = stats.reject_categories.get(category, 0) + 1
    entry = report.RejectEntry(raw_lines, source_lines, reason)
    # Get-or-create avoids the per-call empty-list allocation that
    # ``setdefault(category, [])`` would incur on the hot path.
    bucket = stats.reject_exemplars.get(category)
    if bucket is None:
        bucket = []
        stats.reject_exemplars[category] = bucket
    if len(bucket) < _PER_CATEGORY_EXEMPLAR_BOUND:
        bucket.append(entry)
    if broken_writer is not None:
        broken_writer.write_entry(entry)
    # Recover a NORAD ID from line 1 when one is readable; orphan-line-2
    # and bad-prefix rejects expose no line-1 catalog field and are
    # silently skipped per the issue contract (line 1 unreadable -> omit).
    norad_id = tle.extract_norad_id(raw_lines[0])
    if norad_id is not None:
        stats.quarantined_norad_ids.add(norad_id)
```

- [ ] **Step 3: Rewrite `format_reject_lines`**

Back in `src/lintle/report.py`, replace the existing `format_reject_lines` function (around line 204) with the grouped version:

```python
def format_reject_lines(stats):
    """Render grouped reject exemplars for the ``validate`` summary.

    Walks categories in descending order of total occurrences from
    ``stats.reject_categories`` and emits up to N exemplars per category
    from ``stats.reject_exemplars``, followed by a trailing
    ``...and X more`` when the bucket is shorter than the category total.
    A single noisy category cannot hide rarer defects (issue #21).
    """
    blocks = []
    for category, total in sorted(
        stats.reject_categories.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        bucket = stats.reject_exemplars.get(category, [])
        lines = [f"  {category} ({total:,}):"]
        for entry in bucket:
            if len(entry.source_lines) == 2:
                location = f"{entry.source_lines[0]}-{entry.source_lines[1]}"
            else:
                location = str(entry.source_lines[0])
            lines.append(f"    line {location}: {entry.reason}")
        remaining = total - len(bucket)
        if remaining > 0:
            lines.append(f"    ...and {remaining:,} more")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
```

Note: the `limit` kwarg is removed. There is no caller in the codebase that passes a non-default value; the per-category cap is the only meaningful ceiling.

- [ ] **Step 4: Update `write_broken_file` to flatten + sort the dict**

In the same file (`src/lintle/report.py`), replace the existing `write_broken_file` function (around line 152) with the dict-flattening, source-line-sorting version:

```python
def write_broken_file(path, src_name, stats):
    """Write the ``.broken.txt`` sidecar from a populated ``FileStats``.

    Test-only helper that flattens ``stats.reject_exemplars`` (a per-category
    dict) and sorts by ``source_lines[0]`` so the rendered sidecar matches
    production encounter order. Production cleaning streams entries through
    ``BrokenFileWriter`` directly so memory stays bounded; this wrapper is
    only used by tests and small-corpus paths where the sampled set fits
    in memory.
    """
    with BrokenFileWriter(path, src_name) as writer:
        flattened = [
            entry
            for bucket in stats.reject_exemplars.values()
            for entry in bucket
        ]
        flattened.sort(key=lambda e: e.source_lines[0])
        for entry in flattened:
            writer.write_entry(entry)
        writer.finalize(stats.paired_records + stats.orphan_entries)
```

- [ ] **Step 5: Adapt `TestStreamingRejects` to the per-category bucket**

Open `tests/test_pipeline.py`. Replace the entire `TestStreamingRejects` class (lines 269–309) with:

```python
class TestStreamingRejects:
    """The constant-memory invariant: each ``RejectCategory`` bucket in
    ``reject_exemplars`` stays bounded even on reject-heavy files, while the
    on-disk ``.broken.txt`` catalog is complete.
    """

    def test_exemplars_bucketed_per_category_with_complete_broken_catalog(
        self, tmp_path
    ):
        # Far more bad-prefix orphans than the per-category exemplar bound —
        # the full catalog must reach disk; only the in-memory bucket caps.
        n = pipeline._PER_CATEGORY_EXEMPLAR_BOUND + 1500
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"\n".join(f"junk {i:08d}".encode("ascii") for i in range(n)))
        out = tmp_path / "out"

        stats = pipeline.process_file(str(src), str(out), "clean")

        # Full counters reflect every reject…
        assert stats.quarantined_count == n
        assert stats.reject_categories.get(RejectCategory.BAD_PREFIX) == n
        # …but the in-memory bucket for that category is capped at the bound.
        assert (
            len(stats.reject_exemplars[RejectCategory.BAD_PREFIX])
            == pipeline._PER_CATEGORY_EXEMPLAR_BOUND
        )
        # The on-disk catalog header and trailing entry both reflect every
        # quarantined record — none were dropped due to the in-memory cap.
        broken = (out / "broken" / "tle2099.broken.txt").read_bytes()
        assert f"# {n} quarantined of {n} entries".encode("ascii") in broken
        last = f"junk {n - 1:08d}".encode("ascii")
        assert last in broken

    def test_validate_mode_bucket_caps_per_category(self, tmp_path):
        # In validate mode no sidecar is written, but each per-category
        # bucket still caps so peak memory does not grow with reject count.
        n = pipeline._PER_CATEGORY_EXEMPLAR_BOUND + 500
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"\n".join(f"junk {i:08d}".encode("ascii") for i in range(n)))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "validate")

        assert stats.quarantined_count == n
        assert (
            len(stats.reject_exemplars[RejectCategory.BAD_PREFIX])
            == pipeline._PER_CATEGORY_EXEMPLAR_BOUND
        )
```

- [ ] **Step 6: Update `tests/test_report.py::TestWriteBrokenFile` to use the dict pattern**

Open `tests/test_report.py`. Update the three existing test methods in `TestWriteBrokenFile` (lines 47–102) so they populate the dict via `setdefault(...).append(...)`. The methods are `test_write_broken_file`, `test_broken_file_is_byte_faithful`, and `test_two_line_record_location`.

For each, replace the `stats.reject_exemplars.append(...)` call with `stats.reject_exemplars.setdefault(<category>, []).append(...)`. Choose the category that matches the existing `reason`:

```python
class TestWriteBrokenFile:
    def test_write_broken_file(self, tmp_path):
        stats = report.FileStats(src_name="tle2099.txt")
        stats.paired_records = 5
        stats.quarantined_count = 1
        stats.reject_exemplars.setdefault(RejectCategory.BAD_PREFIX, []).append(
            report.RejectEntry(
                raw_lines=[b"1 garbage"],
                source_lines=[42],
                reason="bad-prefix: line does not start with '1 ' or '2 '",
            )
        )
        out = tmp_path / "tle2099.broken.txt"

        report.write_broken_file(str(out), "tle2099.txt", stats)

        text = out.read_bytes()
        assert b"# source: tle2099.txt" in text
        assert b"1 quarantined of 5 entries" in text
        assert b"source line 42" in text
        assert b"1 garbage" in text

    def test_broken_file_is_byte_faithful(self, tmp_path):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.reject_exemplars.setdefault(RejectCategory.NON_ASCII, []).append(
            report.RejectEntry(
                raw_lines=[b"1 \xff\xfe non-ascii"],
                source_lines=[7],
                reason="non-ascii",
            )
        )
        out = tmp_path / "x.broken.txt"

        report.write_broken_file(str(out), "x.txt", stats)

        assert b"\xff\xfe" in out.read_bytes()

    def test_two_line_record_location(self, tmp_path):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.reject_exemplars.setdefault(
            RejectCategory.CHECKSUM_MISMATCH, []
        ).append(
            report.RejectEntry(
                raw_lines=[b"1 aaa", b"2 bbb"],
                source_lines=[14820, 14821],
                reason="line 2: checksum mismatch",
            )
        )
        out = tmp_path / "x.broken.txt"

        report.write_broken_file(str(out), "x.txt", stats)

        assert b"source lines 14820-14821" in out.read_bytes()
```

- [ ] **Step 7: Update `TestFormatRejectLines` for the new shape and grouped output**

In `tests/test_report.py`, replace the entire `TestFormatRejectLines` class (around lines 139–162) with the new grouped-output tests:

```python
class TestFormatRejectLines:
    def test_format_reject_lines_groups_by_category(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 4
        stats.reject_categories = {
            RejectCategory.CHECKSUM_MISMATCH: 2,
            RejectCategory.BAD_PREFIX: 2,
        }
        stats.reject_exemplars.setdefault(
            RejectCategory.CHECKSUM_MISMATCH, []
        ).append(
            report.RejectEntry(
                raw_lines=[b"1 a", b"2 b"],
                source_lines=[10, 11],
                reason="line 2: checksum mismatch",
            )
        )
        stats.reject_exemplars.setdefault(RejectCategory.BAD_PREFIX, []).append(
            report.RejectEntry(
                raw_lines=[b"x"], source_lines=[20], reason="bad-prefix"
            )
        )

        out = report.format_reject_lines(stats)

        # Two category-heading blocks appear, each with their count.
        assert "checksum-mismatch (2):" in out
        assert "bad-prefix (2):" in out
        # Exemplars appear under their headings.
        assert "line 10-11: line 2: checksum mismatch" in out
        assert "line 20: bad-prefix" in out
```

(The remaining `TestFormatRejectLines` methods are added in Tasks 5 and 6.)

- [ ] **Step 8: Run the full test suite**

```bash
uv run pytest
```

Expected: all tests pass. If any test fails, read the failure carefully — the most likely cause is a missed `.append(...)` on `stats.reject_exemplars` elsewhere in the test suite. Find it with:

```bash
grep -rn "reject_exemplars\.append\|reject_exemplars\[" tests/
```

Each match should already have been updated by the steps above. Any leftover hit is a miss — update it to use the `setdefault(<category>, []).append(...)` pattern with the appropriate category.

- [ ] **Step 9: Run ruff**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: both pass. If `ruff format --check` reports diffs, run `uv run ruff format .` to apply them.

- [ ] **Step 10: Commit the migration**

```bash
git add src/lintle/pipeline.py src/lintle/report.py tests/test_pipeline.py tests/test_report.py
git commit -m "$(cat <<'EOF'
refactor: per-category bounded sampling for reject exemplars (issue #21)

`FileStats.reject_exemplars` becomes `dict[RejectCategory, list[RejectEntry]]`
with `_PER_CATEGORY_EXEMPLAR_BOUND = 5`. `_record_reject` uses a
get-or-create insertion pattern (no per-call empty-list allocation).
`format_reject_lines` walks categories sorted by descending count and
emits a category heading plus up to N exemplars and a per-category
"...and X more" remainder. `write_broken_file` flattens the dict and
sorts by `source_lines[0]` so the test helper matches production
encounter order.

The on-disk `.broken.txt` streaming path is untouched: the full
byte-faithful catalog still reaches disk. Memory ceiling per file
drops from 1000 entries to |RejectCategory| × 5 = 45 entries.

EOF
)"
```

Expected: commit succeeds; `git log --oneline -1` shows the new commit on `feature/grouped-reject-exemplars`.

---

## Task 3: Add the rare-categories-preserved-under-skew test

**Files:**
- Modify: `tests/test_pipeline.py` — add a method to `TestStreamingRejects`

This test is the regression guard for the old design's failure mode: a noisy category filling the buffer before rarer ones arrive.

- [ ] **Step 1: Write the failing test**

Append a new method to `TestStreamingRejects` in `tests/test_pipeline.py`:

```python
    def test_rare_categories_preserved_under_skew(self, tmp_path):
        # Feed 1000 bad-prefix rejects then a smaller batch of a different
        # category. Under the old flat 1000-entry buffer, the rare category
        # would never enter the sample. With per-category buckets, both
        # appear in stats.reject_exemplars.
        many = 1000
        few = 3
        lines = [f"junk {i:08d}".encode("ascii") for i in range(many)]
        # Append a few orphan line-1 records (no following line-2): these
        # land in RejectCategory.ORPHAN_LINE, distinct from BAD_PREFIX.
        lines.extend(
            f"1 {i:05d}U 24001A   24001.00000000  .00000000  00000-0  00000-0 0  0001".encode(
                "ascii"
            )
            for i in range(few)
        )
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"\n".join(lines))

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "validate")

        # Both categories appear in the sample dict — the failure mode of
        # the old flat buffer is gone.
        assert RejectCategory.BAD_PREFIX in stats.reject_exemplars
        assert RejectCategory.ORPHAN_LINE in stats.reject_exemplars
        # The rare category has all its occurrences (well under the cap).
        assert len(stats.reject_exemplars[RejectCategory.ORPHAN_LINE]) == few
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest tests/test_pipeline.py::TestStreamingRejects::test_rare_categories_preserved_under_skew -v
```

Expected: PASS. The implementation from Task 2 already supports this — this test locks in the new invariant.

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_pipeline.py
git commit -m "test: rare-category exemplars preserved under skew (issue #21)"
```

---

## Task 4: Add the internal-error category bucketed test

**Files:**
- Modify: `tests/test_pipeline.py` — add a method to `TestStreamingRejects`

`RejectCategory.INTERNAL_ERROR` is the catch-all in `pipeline._run` (line 208) for unexpected per-record exceptions. The new per-category sampling must treat it like any other category.

- [ ] **Step 1: Write the test**

Append to `TestStreamingRejects` in `tests/test_pipeline.py`:

```python
    def test_internal_error_category_bucketed_like_data_defects(
        self, tmp_path, monkeypatch
    ):
        # Force ``repair.process_record`` to raise so every paired record
        # lands in RejectCategory.INTERNAL_ERROR. With many more rejects
        # than the cap, the bucket caps just like a data-defect category.
        n = pipeline._PER_CATEGORY_EXEMPLAR_BOUND + 5
        lines = []
        for i in range(n):
            lines.append(
                f"1 {i:05d}U 24001A   24001.00000000  .00000000  00000-0  00000-0 0  0001".encode(
                    "ascii"
                )
            )
            lines.append(
                f"2 {i:05d}  51.6000 000.0000 0001000   0.0000   0.0000 15.50000000000001".encode(
                    "ascii"
                )
            )
        src = tmp_path / "tle2099.txt"
        src.write_bytes(b"\n".join(lines))

        from lintle import repair

        def _boom(*_args, **_kwargs):
            raise RuntimeError("synthetic per-record failure")

        monkeypatch.setattr(repair, "process_record", _boom)

        stats = pipeline.process_file(str(src), str(tmp_path / "out"), "validate")

        # Every paired record raised, so all n become INTERNAL_ERROR rejects;
        # the bucket caps at the per-category bound.
        assert stats.reject_categories.get(RejectCategory.INTERNAL_ERROR) == n
        assert (
            len(stats.reject_exemplars[RejectCategory.INTERNAL_ERROR])
            == pipeline._PER_CATEGORY_EXEMPLAR_BOUND
        )
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest tests/test_pipeline.py::TestStreamingRejects::test_internal_error_category_bucketed_like_data_defects -v
```

Expected: PASS.

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_pipeline.py
git commit -m "test: INTERNAL_ERROR rejects bucket like data defects (issue #21)"
```

---

## Task 5: Add format_reject_lines sort and tiebreak tests

**Files:**
- Modify: `tests/test_report.py` — append methods to `TestFormatRejectLines`

These two tests lock the category ordering contract: descending by count, alphabetic on ties.

- [ ] **Step 1: Write both tests**

Append to `TestFormatRejectLines` in `tests/test_report.py`:

```python
    def test_format_reject_lines_sorts_by_descending_count(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 115
        stats.reject_categories = {
            RejectCategory.NON_ASCII: 5,
            RejectCategory.CHECKSUM_MISMATCH: 100,
            RejectCategory.BAD_PREFIX: 10,
        }
        for cat in (
            RejectCategory.NON_ASCII,
            RejectCategory.CHECKSUM_MISMATCH,
            RejectCategory.BAD_PREFIX,
        ):
            stats.reject_exemplars.setdefault(cat, []).append(
                report.RejectEntry(
                    raw_lines=[b"x"], source_lines=[1], reason="r"
                )
            )

        out = report.format_reject_lines(stats)

        # The category with count 100 must appear before count 10, and 10
        # before 5 — independent of dict insertion order.
        pos_checksum = out.index("checksum-mismatch")
        pos_bad = out.index("bad-prefix")
        pos_non_ascii = out.index("non-ascii")
        assert pos_checksum < pos_bad < pos_non_ascii

    def test_format_reject_lines_ties_break_alphabetically(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 14
        # Same count for both categories — alphabetic tiebreak applies.
        stats.reject_categories = {
            RejectCategory.WRONG_LENGTH: 7,
            RejectCategory.BAD_PREFIX: 7,
        }
        for cat in (RejectCategory.WRONG_LENGTH, RejectCategory.BAD_PREFIX):
            stats.reject_exemplars.setdefault(cat, []).append(
                report.RejectEntry(
                    raw_lines=[b"x"], source_lines=[1], reason="r"
                )
            )

        out = report.format_reject_lines(stats)

        # "bad-prefix" sorts before "wrong-length" alphabetically.
        assert out.index("bad-prefix") < out.index("wrong-length")
```

- [ ] **Step 2: Run the tests**

```bash
uv run pytest tests/test_report.py::TestFormatRejectLines -v
```

Expected: PASS for all the new methods (and any earlier `TestFormatRejectLines` test still passes too).

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_report.py
git commit -m "test: format_reject_lines sorts by count desc, alphabetic on ties (issue #21)"
```

---

## Task 6: Add format_reject_lines remainder, empty, and indent tests

**Files:**
- Modify: `tests/test_report.py` — append three methods to `TestFormatRejectLines`

These three lock the rest of the display contract: trailing remainder, empty-rejects case, and indentation.

- [ ] **Step 1: Write all three tests**

Append to `TestFormatRejectLines` in `tests/test_report.py`:

```python
    def test_format_reject_lines_emits_per_category_remainder(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1003
        stats.reject_categories = {
            RejectCategory.CHECKSUM_MISMATCH: 1000,
            RejectCategory.BAD_PREFIX: 3,
        }
        # Full bucket of 5 for the noisy category.
        for i in range(5):
            stats.reject_exemplars.setdefault(
                RejectCategory.CHECKSUM_MISMATCH, []
            ).append(
                report.RejectEntry(
                    raw_lines=[b"x"],
                    source_lines=[i],
                    reason="line 2: checksum mismatch",
                )
            )
        # Bucket equal to the category's total count — no remainder.
        for i in range(3):
            stats.reject_exemplars.setdefault(RejectCategory.BAD_PREFIX, []).append(
                report.RejectEntry(
                    raw_lines=[b"x"],
                    source_lines=[100 + i],
                    reason="bad-prefix",
                )
            )

        out = report.format_reject_lines(stats)

        # Noisy category shows the trailing remainder (1000 - 5 = 995).
        assert "...and 995 more" in out
        # The complete-bucket category does NOT show a remainder — and
        # since it is the only one with a remainder, the literal "...and"
        # appears exactly once in the whole output.
        assert out.count("...and") == 1

    def test_format_reject_lines_empty_when_no_rejects(self):
        stats = report.FileStats(src_name="x.txt")
        # reject_categories is empty by default; reject_exemplars is {}.
        assert report.format_reject_lines(stats) == ""

    def test_format_reject_lines_indentation_contract(self):
        stats = report.FileStats(src_name="x.txt")
        stats.quarantined_count = 1
        stats.reject_categories = {RejectCategory.CHECKSUM_MISMATCH: 1}
        stats.reject_exemplars.setdefault(
            RejectCategory.CHECKSUM_MISMATCH, []
        ).append(
            report.RejectEntry(
                raw_lines=[b"x"],
                source_lines=[10, 11],
                reason="line 2: checksum mismatch",
            )
        )

        out = report.format_reject_lines(stats)
        lines = out.splitlines()

        # Category heading is 2-space indented.
        assert lines[0].startswith("  checksum-mismatch")
        assert not lines[0].startswith("   ")
        # Exemplar line is 4-space indented (one nest deeper).
        assert lines[1].startswith("    line ")
        assert not lines[1].startswith("     ")
```

- [ ] **Step 2: Run the tests**

```bash
uv run pytest tests/test_report.py::TestFormatRejectLines -v
```

Expected: PASS for all methods (existing and new).

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_report.py
git commit -m "test: format_reject_lines per-category remainder, empty, indent contract (issue #21)"
```

---

## Task 7: Add write_broken_file multi-category and source-line-sort tests

**Files:**
- Modify: `tests/test_report.py` — append two methods to `TestWriteBrokenFile`

These guard the §4.4 flattening + sort logic.

- [ ] **Step 1: Write both tests**

Append to `TestWriteBrokenFile` in `tests/test_report.py`:

```python
    def test_write_broken_file_flattens_multiple_categories(self, tmp_path):
        stats = report.FileStats(src_name="x.txt")
        stats.paired_records = 6
        stats.quarantined_count = 6
        # Three categories, two entries each, source lines interleaved
        # (10/40, 20/50, 30/60) so a correct sort by source_lines[0] yields
        # the order 10, 20, 30, 40, 50, 60.
        for cat, srcs in (
            (RejectCategory.CHECKSUM_MISMATCH, [10, 40]),
            (RejectCategory.BAD_PREFIX, [20, 50]),
            (RejectCategory.NON_ASCII, [30, 60]),
        ):
            for s in srcs:
                stats.reject_exemplars.setdefault(cat, []).append(
                    report.RejectEntry(
                        raw_lines=[f"row-{s}".encode("ascii")],
                        source_lines=[s],
                        reason=str(cat),
                    )
                )

        out = tmp_path / "x.broken.txt"
        report.write_broken_file(str(out), "x.txt", stats)

        text = out.read_bytes()
        # All six entries reach the sidecar — no bucket was skipped.
        for s in (10, 20, 30, 40, 50, 60):
            assert f"row-{s}".encode("ascii") in text
        # Index numbering covers 1..6, one per entry.
        for i in range(1, 7):
            assert f"[{i}] source line".encode("ascii") in text

    def test_write_broken_file_orders_by_source_line(self, tmp_path):
        stats = report.FileStats(src_name="x.txt")
        stats.paired_records = 6
        stats.quarantined_count = 6
        for cat, srcs in (
            (RejectCategory.CHECKSUM_MISMATCH, [10, 40]),
            (RejectCategory.BAD_PREFIX, [20, 50]),
            (RejectCategory.NON_ASCII, [30, 60]),
        ):
            for s in srcs:
                stats.reject_exemplars.setdefault(cat, []).append(
                    report.RejectEntry(
                        raw_lines=[f"row-{s}".encode("ascii")],
                        source_lines=[s],
                        reason=str(cat),
                    )
                )

        out = tmp_path / "x.broken.txt"
        report.write_broken_file(str(out), "x.txt", stats)
        text = out.read_text("ascii")

        # Order of appearance must follow source_lines, not dict insertion
        # order or category grouping.
        positions = [text.index(f"row-{s}") for s in (10, 20, 30, 40, 50, 60)]
        assert positions == sorted(positions)
```

- [ ] **Step 2: Run the tests**

```bash
uv run pytest tests/test_report.py::TestWriteBrokenFile -v
```

Expected: PASS for all methods.

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_report.py
git commit -m "test: write_broken_file flattens multiple categories in source-line order (issue #21)"
```

---

## Task 8: Add the CLI shape-lock test

**Files:**
- Modify: `tests/test_cli.py` — append a new test method to the `TestCli` class (or wherever `test_main_validate_lists_reject_locations` lives)

This integration test locks the rendered shape at the CLI boundary so future regressions in the display contract get caught even when unit tests pass.

- [ ] **Step 1: Locate the right class**

```bash
grep -n "test_main_validate_lists_reject_locations" tests/test_cli.py
```

Expected: a single match showing the line number and the enclosing context. The test class is the one that wraps `cli.main([...])` calls and uses `capsys` for stdout capture. Add the new test method to the same class.

- [ ] **Step 2: Write the test**

Append the new method to that class (typically near `test_main_validate_lists_reject_locations`). The fixture pattern (`line1`, `line2`, `capsys`) is the same the existing test uses, and a `bad_line1` with a wrong checksum produces a `RejectCategory.CHECKSUM_MISMATCH` reject:

```python
    def test_main_validate_renders_grouped_exemplars(
        self, tmp_path, line1, line2, capsys
    ):
        # Two distinct defect categories in one file: a checksum mismatch
        # (CHECKSUM_MISMATCH) and a stray line that isn't a TLE (BAD_PREFIX).
        src = tmp_path / "src"
        src.mkdir()
        bad_line1 = line1[:68] + "9"  # wrong checksum — quarantined
        (src / "tle2099.txt").write_bytes(
            (bad_line1 + "\n" + line2 + "\n" + "garbage\n").encode("ascii")
        )

        rc = cli.main(["validate", str(src), "--jobs", "1"])

        out = capsys.readouterr().out
        assert rc == 1
        # The grouped category heading (2-space indent, count parenthesized).
        assert "  checksum-mismatch (" in out
        # The 4-space-indented exemplar line under it.
        assert "    line " in out
        # The other defect class is grouped under its own heading.
        assert "  bad-prefix (" in out
```

- [ ] **Step 3: Run the test**

```bash
uv run pytest tests/test_cli.py -v -k test_main_validate_renders_grouped_exemplars
```

Expected: PASS.

- [ ] **Step 4: Run the full suite**

```bash
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cli.py
git commit -m "test: CLI validate output groups exemplars per category (issue #21)"
```

---

## Task 9: End-to-end smoke check on a real source file

**Files:** none (read-only).

Before opening the PR, drive the CLI against a small corpus file and eyeball the output to make sure the new format reads correctly. This is the "test the change in production-shaped conditions" step from CLAUDE.md.

- [ ] **Step 1: Pick a known-defective file**

The 2017 file is reject-rich (~3,200 mis-prefixed lines, per the parent spec's measured distribution table). From the worktree:

```bash
ls -lh data/source/tle2017.txt
```

Expected: a multi-hundred-megabyte file is present.

- [ ] **Step 2: Run `validate` and capture the output**

```bash
uv run lintle validate data/source/tle2017.txt --jobs 1 2>&1 | tee /tmp/lintle-task9.log
```

Expected: the run finishes with exit code 1 (rejects exist) and the captured log contains:
- the per-file summary line (`tle2017.txt   N records   N clean   N quarantined ...`)
- the `fixes:` line (the existing format, unchanged)
- the `rejects:` line (the existing count-only summary, unchanged)
- the **new** grouped block: one or more 2-space-indented category headings like `  checksum-mismatch (N):`, each followed by up to 5 4-space-indented exemplar lines (`    line X-Y: ...`) and a per-category `    ...and X more` if the category exceeds the cap

- [ ] **Step 3: Eyeball the categories**

```bash
grep '^  [a-z-][a-z-]*\s\+([0-9]' /tmp/lintle-task9.log
```

Expected: each line is a category heading. Categories should appear in descending count order (the biggest defect class first). If a rarer category is absent from the heading list but its count is non-zero in the `rejects:` line, that's a bug — investigate.

If the output reads sensibly, the new format works. If not, stop and report — there is a discrepancy between what the tests cover and what `validate` actually emits.

---

## Task 10: Open the pull request

**Files:** none.

This change lands on `develop` via **rebase-and-merge** so trunk stays linear (CLAUDE.md § Worktree Workflow).

- [ ] **Step 1: Push the branch**

From the worktree:

```bash
git push -u origin feature/grouped-reject-exemplars
```

Expected: `* [new branch]      feature/grouped-reject-exemplars -> feature/grouped-reject-exemplars`.

- [ ] **Step 2: Open the PR**

```bash
gh pr create --base develop --title "feat: grouped reject exemplars (first N per category)" --body "$(cat <<'EOF'
## Summary

Closes #21. `validate` now surfaces up to 5 exemplars per `RejectCategory`,
sorted by descending occurrence count (alphabetic tiebreak). A noisy defect
class can no longer drown out rarer ones, making the output immediately
usable for filing space-track defect reports.

Design: `docs/superpowers/specs/2026-05-24-grouped-reject-exemplars-design.md`.

`FileStats.reject_exemplars` becomes `dict[RejectCategory, list[RejectEntry]]`
with `_PER_CATEGORY_EXEMPLAR_BOUND = 5`. The pipeline's get-or-create
insertion is allocation-quiet on the hot path. `format_reject_lines` walks
categories sorted by descending count and emits heading + N exemplars +
per-category remainder. `write_broken_file` (test-only helper) flattens
the dict and sorts by `source_lines[0]` so its sidecar mirrors production
encounter order. The on-disk `.broken.txt` streaming path is untouched.

## Test plan

- [x] `uv run pytest`
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] End-to-end `uv run lintle validate data/source/tle2017.txt` — output groups by category with 2/4-space indent and per-category remainders

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: a PR URL printed. Open it in a browser and skim once.

- [ ] **Step 3: Merge the PR**

Once CI is green:

```bash
gh pr merge --rebase --delete-branch
```

Expected: `Pull request #N successfully merged`. **Note (from auto-memory):** `--delete-branch` may silently leave the remote branch alive when run from `.worktrees/*` — verify with `git ls-remote origin feature/grouped-reject-exemplars` and clean up manually if the remote ref is still present.

- [ ] **Step 4: Clean up the worktree**

From the **main** lintle checkout (not the worktree):

```bash
cd <main-checkout>
git fetch origin
git checkout develop
git pull --rebase origin develop
git worktree remove .worktrees/feature-grouped-reject-exemplars
git branch -D feature/grouped-reject-exemplars
```

The `-D` (capital) is intentional — rebase-and-merge rewrites SHAs on `develop`, so the local feature branch will not look "merged" to git even though its content has landed (CLAUDE.md § Worktree Workflow).

Expected: `develop` is at the new merge tip; the worktree directory is gone; the local feature branch is deleted.

---

## Self-review check (for the engineer executing this plan)

After finishing Task 10, verify against the spec's §1–§7 quickly:

| Spec section | Plan task |
|---|---|
| §4.1 dict default | Task 2, Step 1 |
| §4.2 get-or-create insertion + renamed constant | Task 2, Step 2 |
| §4.3 grouped renderer + sort | Task 2, Step 3 + Tasks 5–6 lock the contract |
| §4.4 flatten + sort by source_lines | Task 2, Step 4 + Task 7 locks the contract |
| §4.5 CLI unchanged | (no implementation step needed; verified by Task 9 smoke test) |
| §6 error-handling edge cases | Tasks 5, 6, 7 (remainder, empty, bucketing under skew, internal-error) |
| §7.1 pipeline tests | Tasks 2, 3, 4 |
| §7.2 report tests | Tasks 2, 5, 6, 7 |
| §7.3 CLI shape lock | Task 8 |

If any spec section is uncovered by a task, stop and report.
