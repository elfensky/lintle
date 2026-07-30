# Epoch Single Definition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the three disagreeing definitions of "a record's moment in time" into one stdlib-only `src/lintle/epoch.py` leaf, fix the year-boundary key/instant divergence and its downstream artifacts (dedup grouping, manifest spans, verify histogram, gap suppression), and kill the n=3 gap dead zone. Spec: `docs/superpowers/specs/2026-07-30-epoch-single-definition-design.md`. Issue: [#199](https://github.com/elfensky/lintle/issues/199).

**Architecture:** Add `lintle/epoch.py` — four wrappers (`parse_epoch`/`epoch_key`/`epoch_dt`/`iso`) over one private `_normalize`, normalizing on the decimal string (Task 1). Repoint every `verify.epoch` importer and delete `verify/epoch.py`; `epoch_dt`/`iso` move out of `history.py`, which re-exports them (Task 2). Bump both schema versions (Task 3). Collapse the histogram's inline copy onto `epoch_dt` (Task 4). Fix the gap threshold with `median_low` + `MIN_GAP_RECORDS`, add the `gap_silent_satellites` tally (Task 5). Docs + CHANGELOG (Task 6). Land via PR (Task 7). Tasks are strictly ordered.

**Tech Stack:** Python 3.14 · uv · `pytest` (`-n auto`) · `hypothesis` (already a dev dep) · `ruff` · stdlib only (`calendar`, `datetime`, `statistics`) — no new deps, `sgp4` stays walled out.

## Global Constraints

- **Worktree**: branch `refactor/epoch-single-definition`, worktree `.worktrees/refactor-epoch-single-definition` (`git worktree add .worktrees/refactor-epoch-single-definition -b refactor/epoch-single-definition develop`, then `uv sync` and `ln -s ../../data data`). Land via rebase-and-merge.
- **Byte-determinism (Critical Rules #1/#2 territory)**: `epoch_key` is serialized via `repr()` in `grouping.py:37` and `verify/report.py:223` — normalization must produce keys via the re-formed decimal string so equal instants give `repr()`-identical floats, and no-roll records give keys bit-identical to the v1 formula.
- **Raise-on-garbage**: `parse_epoch`/`epoch_key`/`epoch_dt` raise `ValueError` on non-numeric columns — dedup's `DEDUP-UNUSABLE-RECORD` seam (`dedup.py:378-385`) and verify's revalidate depend on it. Never a silent zero.
- **The walls stand**: `lintle.epoch` is stdlib-only (new import-graph test leg); the clean-path closure test (`tests/test_verify.py::TestImportGuard`) must stay green; `tle.py` gets a comment, never an import of anything new.
- **`tle.py`'s `(0, 367)` bound is untouched** (Critical Rule #4).
- Concise one-paragraph docstrings; `@dataclass(slots=True, frozen=True)`; `Counter.update()` for tallies, `dict()` at output boundaries.
- Verify after every task: `uv run pytest && uv run ruff check . && uv run ruff format --check .`. One conventional commit per task, tests included, suite green at every commit.

---

### Task 1: `lintle/epoch.py` — the single definition (pure addition, no consumers yet)

**Files:**
- Create: `src/lintle/epoch.py`
- Test: `tests/test_epoch.py` (new)

**Interfaces:**
- `_normalize(line1: str) -> tuple[int, float]` (private): raw `yy` → four-digit year (pivot 57), raw day; roll `day < 1.0` back using the *prior* year's length, `day ≥ len(year)+1` forward; re-form the day as `float(f"{ndd:03d}.{line1[24:32]}")` — fractional digits verbatim, whole-day arithmetic only, `calendar.isleap` for year lengths.
- `parse_epoch(line1) -> tuple[int, float]` — the **normalized** pair (docstring says so explicitly).
- `epoch_key(line1) -> float` — `year*1000.0 + day` on normalized values; no `datetime` construction (hot sort path).
- `epoch_dt(line1) -> datetime` — `datetime(year, 1, 1, tzinfo=UTC) + timedelta(days=day - 1)` on normalized values (moves from `history.py:17`).
- `iso(dt) -> str` — moves verbatim from `history.py:24`.

- [ ] **Step 1: Write the failing tests** — `tests/test_epoch.py`:
  - `TestNormalization`: non-leap `366.x` rolls forward (`19/366.5` → `(2020, 1.5)`); leap `366.x` stays (`00/366.5` → `(2000, 366.5)` — year 2000 pins the ÷400 leap rule); `0.x` rolls back using the *prior* year's length (`21/000.5` → `(2020, 366.5)`); in-range records unchanged.
  - `TestSameInstantSameKey`: `epoch_key("...20 000.50000000...") == epoch_key("...19 365.50000000...")` — equal via `==` *and* `repr()`-identical.
  - `TestKeyIsMonotoneInInstant` (hypothesis): generate line-1 epochs from digit strings (`yy` 00–99, whole-day 000–366, 8 fractional digits; filter `0.0 < day < 367.0`); assert `epoch_key(a) < epoch_key(b) ⇔ epoch_dt(a) < epoch_dt(b)`.
  - `TestEpochDtUnchanged` (hypothesis, same strategy): `epoch_dt(line1) == datetime(raw_year, 1, 1, tzinfo=UTC) + timedelta(days=raw_day - 1)` — normalization moves **no instant** (invariant 4).
  - `TestYearPivot`: `57` → 1957, `56` → 2056; the pivot-edge collision: `epoch_key` of `57/000.5` (back-rolls to `1956366.5`) must differ from a literal `56/366.5` (`2056366.5`). (The brief's forward-roll-from-56 case is unreachable — 2056 is leap — this is the reachable mirror.)
  - `TestKeyBackCompat`: for a sample of in-range epochs, `epoch_key(line1)` is bit-identical (`==` and `repr()`-equal) to the v1 formula computed inline: `(2000 + yy if yy < 57 else 1900 + yy) * 1000.0 + float(line1[20:23] + "." + line1[24:32])`.
  - `TestExceptionContract`: alpha garbage in year/day/fraction columns raises `ValueError` from all of `parse_epoch`/`epoch_key`/`epoch_dt`.
- [ ] **Step 2: Implement `src/lintle/epoch.py`** until green. Module docstring carries the "one definition" charter and the column map (moved from `verify/epoch.py`'s header).
- [ ] **Step 3: Verify + commit** — `feat(epoch): lintle.epoch — one normalized definition of a record's moment in time (#199)`.

### Task 2: Repoint the world, delete `verify/epoch.py`

**Files:**
- Delete: `src/lintle/verify/epoch.py`
- Modify: `src/lintle/verify/records.py:13` (`from lintle import epoch`), `src/lintle/verify/__init__.py:22` (`from lintle.epoch import parse_epoch` — call site untouched until Task 4), `src/lintle/history.py` (drop `epoch_dt`/`iso` bodies and the `verify.epoch` import; `from lintle.epoch import epoch_dt, iso` re-export so `extract.py:18-20` is untouched), `src/lintle/tle.py` (comment at the `(0, 367)` check pointing at `lintle.epoch` as normalization's owner)
- Test: `tests/test_dedup.py:10`, `tests/test_orbit.py:12` (repoint imports); `tests/test_verify.py::TestImportGuard` — drop `epoch` from the enumerated verify submodule set, add the leg walking `lintle.epoch`'s closure and asserting **stdlib-only**

- [ ] **Step 1: Repoint + delete**; `git grep -n "verify.epoch\|verify import epoch"` must come back empty.
- [ ] **Step 2: Import-guard updates** as above; confirm the clean-path closure test still passes untouched (nothing new imports into `{cli, pipeline, repair, tle}`).
- [ ] **Step 3: Verify + commit** — note in the message that normalization goes **live** here for verify/dedup (sort order and keys change at year boundaries). `refactor(epoch): route verify, history, dedup through lintle.epoch; drop verify/epoch.py (#199)`.

### Task 3: Schema bumps

**Files:**
- Modify: `src/lintle/dedup.py:51`, `src/lintle/verify/report.py:27` — `SCHEMA_VERSION = "2"`
- Test: `tests/test_dedup.py` / `tests/test_verify.py` reference the constants dynamically (no literal `"1"` fixtures — verified 2026-07-30), so expect no fixture edits; re-grep to confirm.

- [ ] **Step 1: Bump both.** Same shape, changed value semantics — the version field is the in-band comparability signal (spec §Schema versions). **Do NOT touch the clean path's `report.jsonl` schema**: the literal `schema_version: "1"` asserts in `tests/test_report_writers.py` / `tests/test_output_artifacts.py` belong to a third, unrelated schema (`report_writers.py` / `output_artifacts.py`) that stays `"1"`.
- [ ] **Step 2: Verify + commit** — `chore(schema): dedup + verify artifacts to schema_version 2 for normalized epoch keys (#199)`.

### Task 4: Histogram — delete the third copy at its call site

**Files:**
- Modify: `src/lintle/verify/__init__.py:103-107` — replace the inline `datetime(year,1,1) + timedelta(...)` dance with `dt = epoch_dt(rec.line1)`; bucket on `f"{dt.year}-{dt.month:02d}"`; drop the now-unused `parse_epoch` import if nothing else uses it
- Test: `tests/test_verify.py` — `TestEpochHistogram`: `19/366.5` buckets into `2020-01` (not `2019-01`); an in-range record's bucket unchanged

- [ ] **Step 1: Failing test, then the call-site collapse.**
- [ ] **Step 2: Verify + commit** — `fix(verify): histogram year+month from the normalized instant (#199)`.

### Task 5: Gap math — `median_low` threshold, `MIN_GAP_RECORDS`, `gap_silent_satellites`

**Files:**
- Modify: `src/lintle/history.py` — `MIN_GAP_RECORDS = 3`; median guard becomes `len(deltas) >= MIN_GAP_RECORDS - 1`; **report** `statistics.median(deltas)` unchanged, **threshold** on `statistics.median_low(deltas)`
- Modify: `src/lintle/dedup.py` — `gap_silent_satellites` tally (manifest rows with `count < MIN_GAP_RECORDS`) in `summary.json`, byte-deterministic key order
- Test: `tests/test_history.py` — `TestAnalyzeEpochs`: n=3 dead zone dies (`_days([0, 1, 1001])` → `gap_count == 1`); deltas never negative once fed sorted-normalized epochs; `median_spacing_days` still the plain median (byte-stable). `tests/test_dedup.py` — `TestManifest`: year-boundary records give non-negative `span_days`, `first_epoch <= last_epoch`; `TestCollapse`: same instant across the boundary (`20/000.5` vs `19/365.5`) collapses to **one** group; summary carries `gap_silent_satellites`. `tests/test_extract.py` — `TestSidecarV2`: sidecar `span_days` / `mean_records_per_day` non-negative across a year boundary (extract *code* untouched; the fix arrives via the import stream).

- [ ] **Step 1: Failing tests across the three files, then the two-line history fix + the dedup tally.**
- [ ] **Step 2: Verify + commit** — `fix(history): gap threshold on median_low, named MIN_GAP_RECORDS; dedup tallies gap-silent satellites (#199)`.

### Task 6: Docs + CHANGELOG

**Files:**
- Modify: `ARCHITECTURE.md` (module map gains `epoch.py`; verify submodule list drops `epoch`; `history.py` entry; the `extract → history → verify.epoch` prose; the day-of-year note now points at `lintle.epoch`), `CLAUDE.md` (project-layout tree), `CHANGELOG.md` (unreleased note: normalized epoch keys, schema 2, ripple summary from the spec)

- [ ] **Step 1: Update all three; re-read spec §Ripple to make sure the CHANGELOG names every changed artifact.**
- [ ] **Step 2: Verify + commit** — `docs: lintle.epoch in the module maps; changelog for schema 2 (#199)`.

### Task 7: Land

- [ ] Full chain in the worktree: `uv run pytest && uv run ruff check . && uv run ruff format --check .` — report actual output.
- [ ] Push, open PR against `develop` (body links #199, notes #200 as follow-up), **rebase-and-merge** (`gh pr merge --rebase --delete-branch`).
- [ ] **Close #199 by hand** (develop-targeted PRs don't auto-close — main is default).
- [ ] Cleanup: `git worktree remove .worktrees/refactor-epoch-single-definition`, `git branch -D refactor/epoch-single-definition`.
- [ ] Post-merge, *not this branch*: first corpus re-run of `verify`/`dedup` re-baselines the orbit census — that observation belongs to [#200](https://github.com/elfensky/lintle/issues/200).
