# Flat Numbered Output Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the `data/` grouping for a flat, pipeline-ordered out-dir (`01-cleaned` … `06-extract`), give every dir a README, and default `extract` into `06-extract/`.

**Architecture:** Directory-level rename driven entirely through the naming constants in `lintle/__init__.py` (the one authority); chunk formats, stems, and suffixes untouched. Spec: `docs/superpowers/specs/2026-07-23-flat-numbered-output-layout-design.md` — read it first; it is the requirements authority for every task.

**Tech Stack:** stdlib only; pytest; existing `fsutil.durable_write_text` for READMEs.

## Global Constraints

- Worktree branch `feature/flat-numbered-layout` off `develop`; conventional commits; land via rebase-and-merge PR.
- Constants (exact values): `CLEANED_DIRNAME="01-cleaned"` · `BROKEN_DIRNAME="02-broken"` · `REPORT_DIRNAME="03-report"` · `VERIFY_DIRNAME="04-verify"` · `DEDUP_DIRNAME="05-dedup"` · `EXTRACT_DIRNAME="06-extract"`; `DATA_DIRNAME` deleted. All defined in `src/lintle/__init__.py`; `verify/report.py` and `dedup.py` import (not redefine) `VERIFY_DIRNAME`/`DEDUP_DIRNAME` from `lintle` and may re-export for existing importers.
- Byte-deterministic structured outputs unchanged; READMEs are static deterministic text via `durable_write_text`, no timestamps/counts.
- Resume checkpoint `schema_version` bumps `3` → `"4"` (match existing type — check `resume.py`); a schema-3 checkpoint must classify STALE.
- Legacy scrub: fresh runs scrub the new six dirs AND the 0.10.1-era `data/` tree AND the ≤0.10.0 root layout (keep existing legacy logic, add the `data/` tree).
- Verify before each commit: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .` — report actual numbers.

---

### Task 1: The rename wave — constants + every path consumer + scrub + resume bump

**Files:**
- Modify: `src/lintle/__init__.py` (constants), `pipeline.py` (`_clean_output_paths`), `report_writers.py`, `output_artifacts.py`, `summary.py`, `diff.py`, `resume.py` (`_OUTPUT_DIRS`, `output_sizes`, `SCHEMA_VERSION`), `run_planning.py` (scrub), `cli.py` (any `DATA_DIRNAME` use — grep), `verify/__init__.py`, `verify/records.py`, `verify/report.py` (import `VERIFY_DIRNAME` from `lintle`), `dedup.py` (import `DEDUP_DIRNAME` from `lintle`)
- Test: every failing test across the suite (mechanical path updates in fixtures/assertions), plus new `tests/test_run_planning.py` cases below.

**Interfaces:**
- Consumes: current constants. Produces: the six new constants importable from `lintle`; all steps read/write the new dirs; suite green.

- [ ] **Step 1: Red first.** Change ONLY `src/lintle/__init__.py`: set the six constants per Global Constraints (add `VERIFY_DIRNAME`, `DEDUP_DIRNAME`, `EXTRACT_DIRNAME`; delete `DATA_DIRNAME`). Run `uv run pytest -qn0 2>&1 | tail -5` — expect a large failure wave (ImportError on `DATA_DIRNAME` consumers). This is the work list.
- [ ] **Step 2: Fix every consumer.** `grep -rn "DATA_DIRNAME" src/ tests/` and remove the `data/` path segment everywhere (e.g. `Path(out_dir) / DATA_DIRNAME / CLEANED_DIRNAME` → `Path(out_dir) / CLEANED_DIRNAME`). Point `verify/report.py`'s `VERIFY_DIRNAME` and `dedup.py`'s `DEDUP_DIRNAME` at the `lintle` constants (`from lintle import VERIFY_DIRNAME` / `DEDUP_DIRNAME`) keeping their names importable from those modules as today. `resume.py`: bump the checkpoint schema constant one step (3→4, preserving its exact existing type — int or str as found) and update `_OUTPUT_DIRS`/`output_sizes` dir list. `run_planning.scrub_outputs`: scrub the six new dirs; KEEP the existing ≤0.10.0 legacy lines; ADD `shutil.rmtree(out / "data", ignore_errors=True)` with a comment `# Legacy (0.10.1–0.10.3) grouped layout`.
- [ ] **Step 3: New scrub tests** in `tests/test_run_planning.py` (append to the scrub test class):

```python
    def test_scrub_removes_grouped_data_layout(self, tmp_path):
        out = tmp_path / "out"
        (out / "data" / "cleaned").mkdir(parents=True)
        (out / "data" / "cleaned" / "x.00001.cleaned.txt").write_text("stale")
        run_planning.scrub_outputs(str(out))
        assert not (out / "data").exists()

    def test_scrub_removes_numbered_dirs(self, tmp_path):
        out = tmp_path / "out"
        for d in ("01-cleaned", "02-broken", "03-report"):
            (out / d).mkdir(parents=True)
            (out / d / "stale.txt").write_text("stale")
        run_planning.scrub_outputs(str(out))
        for d in ("01-cleaned", "02-broken", "03-report"):
            assert not (out / d).exists()
```

- [ ] **Step 4: Mechanical test-fixture sweep.** Run the full suite; update every test that builds or asserts `data/...`, `cleaned/`, `verify/`, `dedup/` paths to the new constants (import them rather than hardcoding where the test file already imports constants — follow each file's current style). `tests/test_extract.py`'s `write_import_tree` uses `DEDUP_DIRNAME` already — it inherits the new value; its CLI tests keep working.
- [ ] **Step 5: Resume STALE test.** In `tests/test_resume.py`, add: build a valid checkpoint dict via the module's own helpers, set `"schema_version"` to the OLD value, assert `classify_checkpoint` (or the function that gates schema — read the file) yields the stale/reject outcome. Follow the file's existing test idiom for invalid checkpoints.
- [ ] **Step 6: Full verification** — `uv run pytest -q && uv run ruff check . && uv run ruff format --check .` all green.
- [ ] **Step 7: Commit** — `feat(output): flat numbered out-dir layout — 01-cleaned … 05-dedup, data/ retired`

---

### Task 2: Per-dir READMEs

**Files:**
- Modify: `src/lintle/output_artifacts.py` (rewrite `write_layout_readme` → root + 01/02/03 READMEs), `verify/report.py` (04 README at `SuspectSink.render`/finalize site), `dedup.py` (05 README beside summary.json write), `src/lintle/extract.py` (06 README written once per `run()` into the resolved dest ONLY when dest is the default `06-extract` inside the out-dir — an explicit `--dest` is the user's directory, don't decorate it; implement as a `write_readme: bool` parameter threaded from cli, see Task 3)
- Test: `tests/test_output_artifacts.py`, `tests/test_verify.py`, `tests/test_dedup.py`, `tests/test_extract.py` (append)

**Interfaces:**
- Consumes: Task 1's constants. Produces: `README.md` in root + each populated dir; helper `output_artifacts.write_step_readme(path: Path, text: str)` is NOT needed — call `fsutil.durable_write_text` directly (YAGNI).

- [ ] **Step 1: Write the README texts.** Exact content per dir — static, no counts/timestamps. Root README replaces the current one: lists the six dirs in order with one line each, the "regenerate, don't migrate" note, and the transient-state note. Per-dir texts (verbatim start, complete each in the same voice — 4-8 lines):
  - `01-cleaned/README.md`: "# 01-cleaned — validated output\n\nEvery record here passed the full validator after repair (Critical Rule #1).\nFiles are `<stem>.NNNNN.cleaned.txt` chunk sets: concatenate a stem's chunks\nin index order to reproduce the single-file form. Regenerate with\n`lintle clean`.\n"
  - `02-broken/README.md`: quarantined records + reasons; sidecar format pointer (`lintle explain <rule>` for any rule tag); regenerate with `lintle clean`.
  - `03-report/README.md`: `report.md` human summary; `report.json` machine envelope (`lintle report` renders it); `report.NNNNN.jsonl` per-record findings (input to `lintle diff`); `broken-noradids.ndjson` complete quarantined-ID list.
  - `04-verify/README.md`: suspects + summary; hard vs soft; regenerate `lintle verify`.
  - `05-dedup/README.md`: latest-re-issue import list + notes; regenerate `lintle dedup`.
  - `06-extract/README.md`: `<id>.txt` pure TLE history + `<id>.json` stats; `lintle extract <id>`.
- [ ] **Step 2: Failing tests** — per step module: README exists after the step runs; two consecutive runs produce byte-identical README; `resume.output_sizes` result contains no `README.md` basename (clean-path test).
- [ ] **Step 3: Implement** — each step writes its README(s) at its existing finalize site via `fsutil.durable_write_text`. `output_sizes`: skip `README.md` when walking (one `if` — check how it enumerates; chunk readers already ignore non-chunk names, the shard stat is exact-name, so only add the skip if the walk actually picks READMEs up — write the test first to find out).
- [ ] **Step 4: Full verification**; **Step 5: Commit** — `feat(output): every out-dir step ships its own README`

---

### Task 3: extract default dest + docs + changelog

**Files:**
- Modify: `src/lintle/cli.py` (extract `--dest` default `None`; dispatch resolves `args.dest or str(Path(args.out_dir) / EXTRACT_DIRNAME)` and passes `write_readme=args.dest is None`), `src/lintle/extract.py` (accept `write_readme: bool = False` keyword on `run()`), `ARCHITECTURE.md` (§ output-tree section + extract row's dest wording), `README.md` (layout block), `CLAUDE.md` (corpus section's output tree), `CHANGELOG.md` ([Unreleased] ### Changed breaking entry + ### Added README entry)
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: Tasks 1-2. Produces: `lintle extract N` with no `--dest` writes `<out-dir>/06-extract/N.txt` + README; explicit `--dest DIR` writes `DIR/N.txt`, no README.

- [ ] **Step 1: Failing tests** — replace `test_dest_defaults_to_cwd` with `test_dest_defaults_to_out_dir_extract` (no `--dest` → files under `<out>/06-extract/`, README.md present); add `test_explicit_dest_gets_no_readme`.
- [ ] **Step 2: Implement** the cli default resolution + `run(write_readme=...)`.
- [ ] **Step 3: Docs.** ARCHITECTURE output-tree section rewritten to the numbered layout (mirror the spec's tree); README/CLAUDE.md layout blocks updated; CHANGELOG under `[Unreleased]`:

```markdown
### Changed

- **BREAKING (output layout): flat, pipeline-ordered out-dir.** The 0.10.1
  `data/` grouping is retired: `01-cleaned/`, `02-broken/`, `03-report/`,
  `04-verify/`, `05-dedup/`, `06-extract/` now sit directly under the
  out-dir, numbered in order of operations. Every step dir ships its own
  `README.md` describing its files and the command that regenerates it.
  `lintle extract` now defaults `--dest` to `<out-dir>/06-extract/`.
  Outputs from ≤ 0.10.3 must be regenerated by re-running the steps; a fresh
  run scrubs both legacy layouts.
```

- [ ] **Step 4: Full verification** (suite + ruff + format); **Step 5: Commit** — `feat(extract): default dest 06-extract; docs for the numbered layout`

---

## Self-review

- Spec coverage: layout+constants → T1; scrub both legacies → T1; resume bump → T1; READMEs incl. determinism + output_sizes skip → T2; extract dest + no-decorating-user-dirs → T3; docs/changelog → T3; machine artifacts untouched (no task removes any) ✓.
- Placeholders: README texts for 02–06 are directive-plus-example rather than full verbatim — acceptable here because T2 Step 1 pins voice, length, and required facts per dir, and the reviewer gate checks them against the spec's README content rule (purpose + formats + regen command, no counts/timestamps).
- Type consistency: `EXTRACT_DIRNAME` defined T1, consumed T3; `run(write_readme=False)` default keeps T2's extract README behavior inert until T3 wires it.
