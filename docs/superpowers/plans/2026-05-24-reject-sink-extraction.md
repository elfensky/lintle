# Reject Sink Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

> **2026-05-24 post-review note:** This plan was revised after a multi-AI
> adversarial debate (Claude Opus 4.7 + Sonnet 4.6 + Codex + Gemini). Two
> reviewers independently caught a `sink.finalize()` placement bug in the
> original Task 4 Step 3 pseudocode — fixed below (`finalize()` must be
> *inside* the `with` block, before the writer's `__exit__` deletes the
> body partial). Task 4's ordering was also restructured per Sonnet's
> bisectability concern: the new `reject_sample` field lives alongside the
> legacy `reject_exemplars` through Tasks 4–5, and `reject_exemplars` is
> deleted in Task 6 — keeps `uv run pytest` green at every commit.
> Test-line-change estimate revised from "~30" to "~50–80" per a grep
> audit. Task 6 Step 4 smoke-test step simplified (the original referenced
> a recipe that doesn't exist). Task 3 Step 2's "golden output" language
> clarified to mean dynamic `_render_entry` comparison, matching the
> existing `TestStreamingRejects` pattern.

**Goal:** Extract a `RejectSink` class and a `FileSample` immutable value
object so the 5-per-rule exemplar cap is structurally enforced rather than
respected by convention in exactly one caller. `pipeline.process_file`'s
three-site juggling of `broken_writer` plus the `reject_exemplars` dict
collapses to one `sink.add(entry)` call.

**Architecture:** `RejectSink` is file-scoped, owns `BrokenFileWriter`, and
exposes `add(entry)` as the single mutation entry point that enforces the
cap. On `finalize` it returns an immutable `FileSample` (frozen dataclass
holding `buckets: Mapping[RuleID, tuple[RejectEntry, ...]]` and `cap: int`),
which becomes `FileStats.reject_sample`. Renderers (`format_reject_lines`,
`write_broken_file`) read from `FileSample`. Tests of renderers construct
`FileSample.from_bounded(cap=5, {...})` directly; tests of write semantics
drive the sink. No user-visible byte format changes.

**Tech Stack:** Python 3.11 · uv · pure stdlib runtime · pytest · ruff ·
`sgp4` (test oracle, dev-only).

**Authoritative spec:** `docs/superpowers/specs/2026-05-24-reject-sink-extraction-design.md`.
Read it before starting Task 2.

**Related parent spec:** `docs/superpowers/specs/2026-05-21-tle-corpus-cleaner-design.md`
— the cleaner's overall design, including the constant-memory critical rule
this change strengthens.

**Project conventions:**

- All tests live in `tests/`, grouped into `Test*` classes by behavior.
- Conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, etc.).
- Trunk is `develop`; this is a multi-file refactor and ships as a
  `refactor/reject-sink-extraction` branch landed via **rebase-and-merge**
  — see `CONTRIBUTING.md` § Git Workflow.
- After every code change, the full verification chain is
  `uv run pytest && uv run ruff check . && uv run ruff format --check .`
  — all three must pass before commit.

---

## Task 1: Set up isolated worktree

**Files:** none (worktree-creation only).

This change touches `src/lintle/report.py`, `src/lintle/pipeline.py`,
`tests/test_pipeline.py`, `tests/test_report.py` — multi-file refactor.
Per `CLAUDE.md` § Worktree Workflow, that means a feature branch in an
isolated worktree.

- [ ] **Step 1: Confirm starting point.** Run from the main lintle checkout:

  ```bash
  git status
  git log --oneline -3
  ```

  Expected: clean working tree on `develop`; HEAD includes the
  `docs:` commit that landed this plan (and the matching spec commit).

- [ ] **Step 2: Create the worktree.**

  ```bash
  git worktree add .worktrees/refactor-reject-sink-extraction \
    -b refactor/reject-sink-extraction develop
  ```

- [ ] **Step 3: Enter worktree, install dev deps, symlink the corpus.**

  ```bash
  cd .worktrees/refactor-reject-sink-extraction
  uv sync
  ln -s ../../data data
  ```

- [ ] **Step 4: Sanity-check the baseline.**

  ```bash
  uv run pytest
  uv run ruff check .
  uv run ruff format --check .
  ```

  All three must pass before any code change.

---

## Task 2: `FileSample` value object (test-first)

**Files:**

- Add: `tests/test_report.py` — new class `TestFileSample`
- Modify: `src/lintle/report.py` — add `FileSample` dataclass

`FileSample` is a tiny, leaf value object — the safest place to start. No
production consumers depend on it until Task 4.

- [ ] **Step 1: Write `TestFileSample` failing tests.**

  - `test_from_bounded_clones_into_tuples` — pass a list; assert
    `sample.buckets[rule]` is a `tuple`, not a `list`.
  - `test_from_bounded_rejects_over_cap` — `FileSample.from_bounded(cap=5,
    {RuleID.CHECKSUM_MISMATCH: [6 RejectEntry stubs]})` raises `ValueError`
    whose message names the rule and counts.
  - `test_from_bounded_accepts_exactly_cap` — 5 entries at `cap=5` does not
    raise.
  - `test_empty_default_has_zero_buckets` — `FileSample.empty(cap=5).buckets
    == {}` and `.cap == 5`.
  - `test_frozen` — `sample.cap = 99` raises
    `dataclasses.FrozenInstanceError`.

  Run `uv run pytest tests/test_report.py::TestFileSample` — all fail
  (`AttributeError: module 'lintle.report' has no attribute 'FileSample'`).

- [ ] **Step 2: Implement `FileSample` in `src/lintle/report.py`.**

  Add the frozen dataclass per spec §4.4 with `from_bounded` and `empty`
  classmethods. Module-level docstring on the class only — keep it concise,
  matching surrounding style.

- [ ] **Step 3: Verify.**

  ```bash
  uv run pytest tests/test_report.py::TestFileSample
  uv run ruff check src/lintle/report.py tests/test_report.py
  uv run ruff format --check src/lintle/report.py tests/test_report.py
  ```

  All green.

- [ ] **Step 4: Commit.**

  ```
  refactor(report): add FileSample value object (issue #19)
  ```

  No production consumers yet; this is a pure addition.

---

## Task 3: `RejectSink` class (test-first)

**Files:**

- Add: `tests/test_report.py` — new class `TestRejectSink`
- Modify: `src/lintle/report.py` — add `RejectSink` class, move
  `_PER_RULE_EXEMPLAR_BOUND` constant here from `pipeline.py` (still re-imported
  there until Task 4)

- [ ] **Step 1: Move the cap constant.**

  In `src/lintle/report.py`, add at module level (near `BrokenFileWriter`):

  ```python
  # The per-rule cap on retained exemplars. Source of truth for the
  # bounded-sample invariant; consumed by ``RejectSink`` and surfaced on
  # ``FileSample.cap`` so renderers can show truncation. Five is the value
  # issue #21 established as sufficient evidence for space-track defect
  # reports without diluting rarer rules.
  _PER_RULE_EXEMPLAR_BOUND = 5
  ```

  In `src/lintle/pipeline.py`, change `_PER_RULE_EXEMPLAR_BOUND = 5` to
  `from lintle.report import _PER_RULE_EXEMPLAR_BOUND` (the rest of `_record_reject`
  still uses the constant — full retirement happens in Task 4).

  Verify with `uv run pytest` — still green; only the constant moved.

- [ ] **Step 2: Write `TestRejectSink` failing tests.**

  - `test_add_under_cap_accepts` — single rule, 3 entries, all show up.
  - `test_add_over_cap_silently_drops` — 6 entries of one rule, finalized
    sample has exactly 5; `sink.add` does not raise.
  - `test_cap_holds_under_skew` — 1000 `CHECKSUM_MISMATCH` then 1
    `BAD_PREFIX`; finalized sample has exactly 5 checksum and 1 bad-prefix.
  - `test_cap_holds_under_random_input` — `random.Random(42)` driving a
    1000-element `(rule, entry)` stream over all `RuleID` values; assert
    every bucket in the finalized sample is `<= cap`.
  - `test_finalize_returns_filesample_with_matching_cap` — sink built with
    `cap=5`; `sample.cap == 5`.
  - `test_validate_mode_skips_writer` — `RejectSink()` without `broken_path`
    accepts entries and finalizes without touching disk; no temp file
    leakage in a `tmp_path` parent dir.
  - `test_clean_mode_writes_byte_faithful_sidecar` — `RejectSink(broken_path=tmp_path/"x.broken.txt", src_name="x")`,
    add a fixed set of entries, finalize, read the file back, assert bytes
    equal the result of calling `_render_entry(idx, entry)` for each entry
    in turn (plus the `_render_header` preamble). This follows the
    dynamic-render comparison pattern from the existing
    `TestStreamingRejects` — there is no stored golden file in this repo.
  - `test_exit_without_finalize_cleans_partials` — open sink with
    `broken_path`, add an entry, raise inside the `with`-block, assert no
    `*.partial` files remain in the directory.

  Run `uv run pytest tests/test_report.py::TestRejectSink` — all fail
  (`AttributeError: module 'lintle.report' has no attribute 'RejectSink'`).

- [ ] **Step 3: Implement `RejectSink` in `src/lintle/report.py`.**

  Per spec §4.5. The class owns:
  - An internal `dict[RuleID, list[RejectEntry]]` for sample accumulation.
  - An optional `BrokenFileWriter` when `broken_path` is set.
  - The cap (defaults to `_PER_RULE_EXEMPLAR_BOUND`).

  `add(entry)`:
  - Look up `entry.primary.rule_id` bucket; create if missing.
  - If `len(bucket) < self._cap`, append; else drop silently (matches today's
    pipeline behavior — silent drop is correct because the count is in
    `reject_counts` regardless).
  - If `self._writer is not None`, call `self._writer.write_entry(entry)`.

  `finalize(*, entries)`:
  - If `self._writer is not None`, call `self._writer.finalize(entries)`.
  - Return `FileSample.from_bounded(self._cap, self._buckets)`.

  `__enter__` / `__exit__`:
  - `__enter__` calls `self._writer.__enter__()` if writer is set; returns
    `self`.
  - `__exit__` delegates to `self._writer.__exit__(...)` if writer is set
    and writer hasn't been finalized. Returns `False` (don't swallow
    exceptions).

- [ ] **Step 4: Verify.**

  ```bash
  uv run pytest tests/test_report.py::TestRejectSink
  uv run ruff check src/lintle/report.py tests/test_report.py
  uv run ruff format --check src/lintle/report.py tests/test_report.py
  ```

  All green.

- [ ] **Step 5: Commit.**

  ```
  refactor(report): introduce RejectSink with cap enforced by construction (issue #19)
  ```

---

## Task 4: Wire `RejectSink` into `pipeline.process_file`

**Files:**

- Modify: `src/lintle/pipeline.py` — replace local `broken_writer` + the dict-write
  in `_record_reject` with a `RejectSink` constructed by `process_file`.
- Modify: `src/lintle/report.py` — change `FileStats.reject_exemplars: dict`
  to `FileStats.reject_sample: FileSample` with the `empty(cap)` default.
- Modify: `tests/test_pipeline.py` — update shape assertions that read
  `stats.reject_exemplars[rule]` to `stats.reject_sample.buckets[rule]`.

- [ ] **Step 1: Add `reject_sample` field ALONGSIDE `reject_exemplars`.**

  Per the post-review note at the top of this plan, both fields stay alive
  through Tasks 4–5 so every commit is green and bisectable. The legacy
  field is deleted in Task 6.

  In `src/lintle/report.py`, add to `FileStats`:

  ```python
  reject_sample: FileSample = dataclasses.field(
      default_factory=lambda: FileSample.empty(_PER_RULE_EXEMPLAR_BOUND)
  )
  ```

  (Keep `reject_exemplars: dict = dataclasses.field(default_factory=dict)`
  for now. Update its docstring to note the transition: "Legacy mirror;
  removed in the final cleanup of this refactor — new code should read
  `reject_sample.buckets` instead.")

  Add a one-sentence pointer to `FileSample` and `RejectSink` in the
  `FileStats` class docstring.

  Tests should pass after this step — no behavior change yet.

- [ ] **Step 2: Rewrite `pipeline._record_reject`.**

  Replace the current signature
  `_record_reject(stats, broken_writer, primary, related, raw_lines, source_lines)`
  with `_record_reject(stats, sink, primary, related, raw_lines, source_lines)`.

  Body:
  ```python
  stats.quarantined_count += 1
  stats.reject_counts[primary.rule_id] = (
      stats.reject_counts.get(primary.rule_id, 0) + 1
  )
  entry = report.RejectEntry(raw_lines, source_lines, primary, related)
  sink.add(entry)  # one call: cap-checked, streamed if writer is open
  # TRANSITION: keep the legacy dict populated so renderers (which still
  # read reject_exemplars until Task 5) see the same data. Removed in
  # Task 6 Step 1 once renderers migrate to reject_sample.
  bucket = stats.reject_exemplars.get(primary.rule_id)
  if bucket is None:
      bucket = []
      stats.reject_exemplars[primary.rule_id] = bucket
  if len(bucket) < _PER_RULE_EXEMPLAR_BOUND:
      bucket.append(entry)
  norad_id = tle.extract_norad_id(raw_lines[0])
  if norad_id is not None:
      per_rule = stats.quarantined_norad_ids.setdefault(norad_id, {})
      per_rule[primary.rule_id] = per_rule.get(primary.rule_id, 0) + 1
  ```

  The legacy bookkeeping is retained temporarily to keep tests green
  during the migration; Task 6 Step 1 removes it.

- [ ] **Step 3: Rewrite `pipeline.process_file`.**

  Replace the local `broken_writer = None / broken_writer = BrokenFileWriter(...) / broken_writer.__enter__()`
  block (lines 169–176 of the current file) with:

  ```python
  broken_path = None
  if mode == "clean":
      broken_dir = os.path.join(out_dir, "broken")
      os.makedirs(broken_dir, exist_ok=True)
      broken_path = os.path.join(broken_dir, stem(src_name) + ".broken.txt")

  sink = report.RejectSink(broken_path=broken_path, src_name=src_name)
  ```

  Replace the `try/finally` that juggles `broken_writer.__exit__` with the
  `with sink:` idiom. **CRITICAL: `sink.finalize()` MUST be called inside
  the `with` block, before `__exit__` fires.** If finalize is outside the
  `with`, the sink's `__exit__` runs first, sees `_completed = False`,
  deletes the body partial, and then `finalize` tries to stitch a file
  that no longer exists. This bug was caught independently by two
  adversarial-review voices.

  ```python
  with sink:
      # ... existing record loop, calling _record_reject(stats, sink, ...) ...
      completed = True
      if mode == "clean":
          os.replace(cleaned_tmp, cleaned_path)
      sample = sink.finalize(
          entries=stats.paired_records + stats.orphan_entries
      )

  stats.reject_sample = sample
  ```

  Inside the `with` block, `sink.finalize` stitches the sidecar and marks
  the sink as completed; when `__exit__` then fires on context exit, it
  sees `_completed = True` and the partial cleanup path is a no-op. The
  current code's manual `__enter__`/`__exit__` pair at `pipeline.py:175-176,
  268-269` collapses to one `with` block.

  **Edge case:** if `process_file` raises mid-loop, the `with sink:` exits
  without `finalize` being called; the writer's `__exit__` discards
  partials and `stats.reject_sample` keeps the default `empty(...)`.
  `reject_counts` and `quarantined_count` will already have been
  incremented before the exception — this counter/sample divergence is
  observable on abnormal exit, matches today's behavior (the legacy dict
  also gets partial entries), and is not a regression. Add a brief
  comment in `_record_reject` noting why the two can diverge on
  exception. Existing test `TestStreamingRejects::test_interrupt_leaves_no_debris`
  exercises this shape.

- [ ] **Step 4: Update affected pipeline tests.**

  In `tests/test_pipeline.py` find every `stats.reject_exemplars[RuleID.X]`
  and replace with `stats.reject_sample.buckets[RuleID.X]`. Find every
  `RuleID.X in stats.reject_exemplars` and replace with `RuleID.X in
  stats.reject_sample.buckets`. Roughly 8 sites per the spec inventory.

  The test `TestStreamingRejects::test_high_reject_density_stays_bounded`
  becomes redundant with the new
  `TestRejectSink::test_cap_holds_under_skew` — keep the pipeline-level
  version since it asserts the bound *through* `process_file`, which the
  unit-level version does not.

- [ ] **Step 5: Verify.**

  ```bash
  uv run pytest tests/test_pipeline.py tests/test_report.py
  uv run ruff check .
  uv run ruff format --check .
  ```

  Should be green. If any test outside these two files fails, list them
  before proceeding — likely indicates a renderer or CLI site that hasn't
  been migrated yet.

- [ ] **Step 6: Commit.**

  ```
  refactor(pipeline): drive rejects through RejectSink (issue #19)
  ```

---

## Task 5: Migrate renderers to `FileSample`

**Files:**

- Modify: `src/lintle/report.py` — `format_reject_lines` and
  `write_broken_file`, both currently reading `stats.reject_exemplars`.
- Modify: `tests/test_report.py` — every test that constructs reject
  exemplars (14 sites).

- [ ] **Step 1: Update `format_reject_lines`.**

  Current body reads `stats.reject_exemplars.get(rule_id, [])`. Change to
  `stats.reject_sample.buckets.get(rule_id, ())`. Remainder calculation
  (`total - len(bucket)`) is unchanged. The `cap` field on `FileSample` is
  not needed here — the per-rule total comes from `reject_counts`.

- [ ] **Step 2: Update `write_broken_file`.**

  Current body flattens `stats.reject_exemplars.values()`. Change to
  `stats.reject_sample.buckets.values()`. The sort by `source_lines[0]` is
  unchanged. The function remains a test-only helper; production cleaning
  still streams through `RejectSink` end-to-end.

- [ ] **Step 3: Migrate `tests/test_report.py` exemplar setup.**

  Every occurrence of
  `stats.reject_exemplars.setdefault(RuleID.X, []).append(entry)` becomes
  a `FileSample.from_bounded(cap=5, {RuleID.X: [entry, ...]})` call
  assigned to `stats.reject_sample`. Where a test populates multiple rules,
  build the bucket dict once and pass it whole.

  Pattern:

  ```python
  # Before
  stats = FileStats("x.txt")
  stats.reject_exemplars.setdefault(RuleID.CHECKSUM_MISMATCH, []).append(entry_a)
  stats.reject_exemplars.setdefault(RuleID.CHECKSUM_MISMATCH, []).append(entry_b)
  stats.reject_exemplars.setdefault(RuleID.BAD_PREFIX, []).append(entry_c)

  # After
  stats = FileStats("x.txt")
  stats.reject_sample = FileSample.from_bounded(
      cap=5,
      entries_by_rule={
          RuleID.CHECKSUM_MISMATCH: [entry_a, entry_b],
          RuleID.BAD_PREFIX: [entry_c],
      },
  )
  ```

  Where a test currently appends *more* than 5 entries deliberately (to
  exercise truncation in `format_reject_lines`), the `from_bounded` call
  must use `cap=N` matching the entry count — `cap=5` would raise. Adjust
  per test intent; the original tests were using the unbounded dict to
  emulate scenarios the cap would normally prevent.

- [ ] **Step 4: Verify.**

  ```bash
  uv run pytest
  uv run ruff check .
  uv run ruff format --check .
  ```

  All green. If any CLI or integration test fails, it's because a site
  outside `test_report.py`/`test_pipeline.py` still references
  `reject_exemplars` — grep `git diff` against `reject_exemplars` to find
  it.

- [ ] **Step 5: Commit.**

  ```
  refactor(report): read renderers from FileSample (issue #19)
  ```

---

## Task 6: Retire `FileStats.reject_exemplars` (the rename's final mile)

Renderers and pipeline now read `reject_sample`; the legacy
`reject_exemplars` field and its parallel-population code in
`_record_reject` are dead weight. This task removes them.

- [ ] **Step 1: Remove the transitional dict-write from `_record_reject`.**

  In `src/lintle/pipeline.py`, delete the legacy `bucket = stats.reject_exemplars.get(...)` block
  added in Task 4 Step 2 (the comment block marked `# TRANSITION`). Only
  `sink.add(entry)` populates exemplars now.

  Run `uv run pytest tests/test_pipeline.py` — should still pass
  (`reject_exemplars` is no longer read by renderers as of Task 5).

- [ ] **Step 2: Delete the `reject_exemplars` field from `FileStats`.**

  In `src/lintle/report.py`, remove
  `reject_exemplars: dict = dataclasses.field(default_factory=dict)`
  and any docstring lines referencing it. Update the `FileStats` docstring
  to describe `reject_sample` as the canonical sample carrier.

  Also remove the import of `_PER_RULE_EXEMPLAR_BOUND` in `pipeline.py`
  if no other reference remains (the constant lives only on the sink
  default now). Verify with `grep -n _PER_RULE_EXEMPLAR_BOUND src/lintle/pipeline.py`.

- [ ] **Step 3: Grep the tree.**

  ```bash
  grep -rn "reject_exemplars" src/ tests/
  ```

  Expected: **zero matches**. If anything turns up, fix it before continuing.

- [ ] **Step 4: Update the CHANGELOG `[Unreleased]` section.**

  ```bash
  grep -n "Unreleased" CHANGELOG.md
  ```

  Add under `### Changed`:

  ```markdown
  - Internal: extract `RejectSink` and `FileSample` from `FileStats` so the
    5-per-rule exemplar cap is enforced by construction rather than by
    convention in a single caller. `pipeline.process_file` no longer juggles
    a separate `broken_writer` and exemplar dict — `RejectSink` owns both.
    No user-visible byte format changes. Closes #19.
  ```

- [ ] **Step 5: Final verification.**

  ```bash
  uv run pytest
  uv run ruff check .
  uv run ruff format --check .
  ```

  All green. The byte-format-lock tests in
  `tests/test_report.py::TestStreamingRejects` and
  `tests/test_cli.py` are the authoritative check that
  `.broken.txt` output is byte-identical to the pre-refactor baseline —
  no separate CLI smoke-test step is needed.

- [ ] **Step 6: Commit the CHANGELOG.**

  ```
  docs(changelog): document RejectSink extraction (issue #19)
  ```

---

## Task 7: Open the pull request

- [ ] **Step 1: Push the branch.**

  ```bash
  git push -u origin refactor/reject-sink-extraction
  ```

- [ ] **Step 2: Open the PR via `gh pr create`** with the title
  `refactor: extract RejectSink to enforce exemplar cap by construction (#19)`
  and a body containing:

  - Summary (3 bullets): what changed, why, what it doesn't change.
  - Test plan: link to the new structural-invariant tests; note that the
    `.broken.txt` and JSON outputs are byte-identical.
  - Risk: small. No user-visible change; full test suite + format-lock
    coverage.

- [ ] **Step 3: After CI passes and review approves**, land via
  **Rebase and merge** (not squash, not merge commit). Delete the remote
  branch — note from memory: `gh pr merge --delete-branch` from inside a
  worktree silently leaves the remote branch; run it from the main
  checkout or use the GitHub UI button.

- [ ] **Step 4: Clean up the worktree** from the main checkout:

  ```bash
  git worktree remove .worktrees/refactor-reject-sink-extraction
  git branch -D refactor/reject-sink-extraction
  ```

---

## Summary

7 tasks, ~25 file touches, no user-visible change. The diff prize is
collapsing `pipeline.process_file`'s three-site bookkeeping into one
`sink.add(entry)` call. The invariant prize is the structural cap
enforcement — future writers cannot blow the bound.

## Test plan

- All existing tests pass with mechanical rewrite of `reject_exemplars` →
  `reject_sample.buckets` and the test setup migration to
  `FileSample.from_bounded`.
- New `TestFileSample` class: from_bounded validation, frozen-ness, empty
  sentinel.
- New `TestRejectSink` class: cap enforcement (under-cap, over-cap silent
  drop, adversarial skew, deterministic random), finalize handoff,
  validate-mode no-writer path, clean-mode byte-faithful sidecar,
  context-manager cleanup on abnormal exit.
- Smoke test against a real source file confirms `.broken.txt` bytes are
  identical pre/post refactor.

## Self-review check (for the engineer executing this plan)

Before opening the PR:

- [ ] `grep -rn "reject_exemplars" src/ tests/` returns nothing.
- [ ] `_PER_RULE_EXEMPLAR_BOUND` is defined exactly once (in `report.py`).
- [ ] `pipeline.process_file` no longer has a local `broken_writer`
  variable.
- [ ] `_record_reject`'s third argument is `sink`, not `broken_writer`.
- [ ] No test reaches `FileStats.reject_sample.buckets` to *mutate* — only
  to read. (`FileSample` is frozen; the buckets are tuples — mutation is
  impossible, but verify the test pattern is clean reads.)
- [ ] `CHANGELOG.md` has a `[Unreleased]` `### Changed` entry citing #19.
- [ ] Smoke-test artifacts are byte-identical to a pre-refactor baseline.
