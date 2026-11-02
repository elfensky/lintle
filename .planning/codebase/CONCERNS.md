---
last_mapped_commit: bf834f78234eaa5b6f879c0bff96587181b4d723
last_mapped_at: 2026-11-02
---
# Codebase Concerns

**Analysis Date:** 2026-11-02

<!-- refreshed: 2026-11-02 -->

Scope: `src/lintle/` and `.github/workflows/`. Line numbers match the worktree on branch `night/gsd-standard`. Items are grouped by type. Within each group, the highest impact comes first.

## Tech Debt

**Output scrub hides failures:**
- Issue: `scrub_outputs` calls `shutil.rmtree(..., ignore_errors=True)` for every output tree. A failed delete leaves stale files, and the run continues with no signal.
- Files: `src/lintle/run_planning.py` (lines 168-205)
- Impact: A fresh run can publish new outputs next to old `cleaned/`, `verify/`, or `dedup/` files. This breaks the "no orphaned outputs" promise in the function docstring.
- Fix approach: Catch the `OSError`, collect the paths that remain, and refuse the run with exit 2 when any remain.

**Single-file checkpoint rewritten on every file:**
- Issue: After each completed file, `run_workers` rewrites the full checkpoint. The payload holds the `inputs` fingerprints and the `completed` summaries for every file.
- Files: `src/lintle/worker_pool.py` (line 128), `src/lintle/resume.py` (`write_checkpoint`, `build_checkpoint`)
- Impact: Write cost grows with the number of inputs. Each write also runs two durable syncs. For the 29-file corpus the cost is small. For a much larger input set the total cost grows quadratically.
- Fix approach: Batch checkpoint writes (for example every N files or every T seconds) and always write once on exit.

**Verify and dedup spill to the system temp directory:**
- Issue: `ExternalSorter` creates its spill directory with `tempfile.TemporaryDirectory(prefix=...)` and no `dir=`. `record_sorter()` sorts every cleaned record, so the spill can approach the full corpus size.
- Files: `src/lintle/verify/grouping.py` (line 78; `record_sorter` at line 110), used by `src/lintle/verify/__init__.py` (line 46), `src/lintle/verify/orbit.py` (line 345), and `src/lintle/dedup.py` (line 271)
- Impact: Spill data lands on the boot volume (`$TMPDIR`). The disk guard in `check_disk_space` checks only `--out-dir`, so a run can fill the boot volume with no warning.
- Fix approach: Put spill files next to the output tree (for example `<out-dir>/.spill/`), or check free space on the temp volume before the sort starts.

**Broad exception fallbacks:**
- Issue: The clean path catches `Exception` in several places and maps it to exit 2 or to a quarantine record.
- Files: `src/lintle/pipeline.py` (line 416, `INTERNAL_ERROR` quarantine), `src/lintle/worker_pool.py` (lines 111, 136), `src/lintle/cli.py` (lines 681, 1001)
- Impact: A programming bug inside the repair path appears as a quarantined record, not as a crash. Quarantine is the intended safe default, but the findings count changes without a failing test.
- Fix approach: Keep the quarantine. Print a summary warning when `INTERNAL_ERROR` appears, so a code bug is visible.

**Silent drain loop in progress display:**
- Issue: The live-table refresh thread catches all `Exception` and continues. The final drain runs under `contextlib.suppress(Exception)`.
- Files: `src/lintle/cli_progress.py` (around line 294)
- Impact: A persistent rendering bug hides progress with no message.
- Fix approach: Log the first failure once through `term.warning`, then continue.

**Config defaults detected by string match on argv:**
- Issue: Config defaults are applied by scanning `argv` for `--flag` or `--flag=`.
- Files: `src/lintle/cli.py` (`_flag_present` at line 615, `_apply_config_paths` at line 630)
- Impact: A value that equals a flag name is read as a flag. Presence is judged from text, not from the parser.
- Fix approach: Use argparse with a sentinel default, so presence comes from the parser.

**Large single module:**
- Issue: `src/lintle/cli.py` has about 1000 lines. It holds argument parsing, clean orchestration, post-run dispatch, and lock handling.
- Files: `src/lintle/cli.py`
- Impact: Changes to one subcommand touch shared helpers. Review cost is high.
- Fix approach: Move the verify, dedup, and extract dispatch into leaf modules, following the split already used for `run_planning`, `worker_pool`, and `output_artifacts`.

## Known Bugs and Correctness Risks

**Resume skips files whose interior changed:**
- Symptoms: A resumed run reuses a stored result for an input whose middle bytes changed, when size, mtime, ctime, inode, and the first and last 64 KB are unchanged.
- Files: `src/lintle/resume.py` (`input_fingerprint`, line 45; residual noted in the docstring at lines 47-54)
- Trigger: An in-place edit that keeps size and timestamps. This is rare. The docstring documents it.
- Workaround: Run without `--resume`, or delete `.clean-state.json`, after any manual edit to a source file.

**Crash between paired `extract` commits leaves an orphan:**
- Symptoms: The `.txt` commits, then the process is killed before the `.json` sidecar commits. The rollback in the `except` block does not run on a kill.
- Files: `src/lintle/extract.py` (`_extract_one`, lines 360-380)
- Trigger: `SIGKILL`, power loss, or an OOM kill in that window.
- Workaround: Re-run `lintle extract`. The next run overwrites the `.txt`. Check that each `.txt` has a matching `.json` before use.

**Ownership marker written before the run:**
- Symptoms: The `.lintle-output` marker is written before scrub and before the run. A run that fails early leaves the marker. A later run then treats the directory as lintle-owned, even when it holds user files.
- Files: `src/lintle/run_planning.py` (lines 285-293; gate at `_is_safe_to_scrub`, line 133)
- Trigger: `--out-dir` points at a folder that holds an earlier lintle output and user files.
- Workaround: Use a new `--out-dir` for each dataset.

**Lock message suggests manual lock removal:**
- Symptoms: The "held by" message tells the operator to remove the lock file when no run is active. The module docstring says the file must never be unlinked while a run might hold it.
- Files: `src/lintle/fsutil.py` (line 223, message in `out_dir_lock`; rationale at lines 200-206)
- Trigger: A lock message while a run is still active. Removing the file then lets a second run in.
- Workaround: Check with `ps` that no `lintle` process runs before removing the file.

## Security Considerations

**Unpinned third-party GitHub Actions:**
- Risk: Workflows pin `actions/checkout@v7` and `astral-sh/setup-uv@v9.0.0` by tag. A moved tag runs new code in the publish job, which holds `id-token: write`.
- Files: `.github/workflows/publish.yml` (lines 37, 40; `permissions: id-token: write`), `.github/workflows/ci.yml` (lines 24, 27, 48, 51), `.github/workflows/version.yml` (line 16)
- Current mitigation: Dependabot updates the `github-actions` ecosystem weekly. Trusted Publishing uses OIDC, so no API token is stored.
- Recommendations: Pin each action to a full commit SHA. Keep the Dependabot entry so SHA pins update.

**Production publish approval is set outside the repo:**
- Risk: PyPI publish runs on `workflow_dispatch` with `target=pypi`. Protection depends on the GitHub `pypi` environment settings, which the repo does not hold.
- Files: `.github/workflows/publish.yml` (environment `pypi`, lines 1-30)
- Current mitigation: A push to `main` publishes only to TestPyPI.
- Recommendations: Confirm in GitHub that the `pypi` environment requires a reviewer. Record that setting in the repo docs.

**Project-local config can redirect destructive paths:**
- Risk: `./.lintle.json` sets the `source` and `output` directories. A checked-in file in a cloned repo changes where `clean` writes and where the scrub runs.
- Files: `src/lintle/config.py` (`load`, `save`), `src/lintle/cli.py` (`_apply_config_paths`, line 630)
- Current mitigation: The scrub ownership gate (`_is_safe_to_scrub`) refuses non-lintle directories. Explicit CLI arguments override the file.
- Recommendations: Print the resolved `out-dir` before a destructive run, or require `--out-dir` when the config file was not created by the user.

**Checksum reconstruction writes recomputed digits:**
- Risk: `--reconstruct-checksum` writes a recomputed checksum digit into output records. The records then look valid downstream.
- Files: `src/lintle/repair.py`, `src/lintle/pipeline.py` (`reconstruct_checksum` flag)
- Current mitigation: The repair tier is recorded per record in `report.jsonl` and the sidecar. This follows Critical Rule #2.
- Recommendations: Show the tier count in the summary panel, not only in per-record output.

## Performance Bottlenecks

**Full-corpus external sort for verify and dedup:**
- Problem: Verify and dedup sort every cleaned record by `(catalog, epoch)` on disk.
- Files: `src/lintle/verify/grouping.py` (`ExternalSorter`, `record_sorter`), `src/lintle/verify/__init__.py` (line 46)
- Cause: The constant-memory rule (Critical Rule #3). Each run writes the full record set to temp files, then reads it back.
- Improvement path: Reuse the sorted order when `01-cleaned/` is unchanged. `cleaned_fingerprint` in `src/lintle/verify/records.py` already detects that change. Cache the sorted runs and skip the spill.

**Group-level memory in streaming passes:**
- Problem: `grouped` materializes one full catalog group in memory.
- Files: `src/lintle/verify/grouping.py` (`grouped`, line 27)
- Cause: `list(group)` per catalog key. Memory is bounded by the largest single satellite history, not by a fixed constant.
- Improvement path: Stream each group and keep only the window the orbit and dedup checks need. Document the bound until then.

**Quarantine NORAD list loaded whole:**
- Problem: `_quarantined_ids` reads all of `broken-noradids.ndjson` into a set.
- Files: `src/lintle/extract.py` (`_quarantined_ids`, lines 66-84)
- Cause: One set of NORAD IDs held in memory. Size equals the number of distinct broken catalogs.
- Improvement path: Acceptable at current scale. Stream the file if the broken set grows.

## Fragile Areas

**Atomic commit and fsync path:**
- Files: `src/lintle/fsutil.py` (`durable_replace`, `_fsync`, `durable_write_text`), `src/lintle/chunking.py`, `src/lintle/report_writers.py`
- Why fragile: The `F_FULLFSYNC` path runs only on macOS. The fallback latches a process-wide flag. Tests monkeypatch `fcntl.fcntl` (see `tests/test_fsutil.py` around lines 231-280), so the real syscall is not exercised. CI runs on `ubuntu-latest` only (`.github/workflows/ci.yml`).
- Safe modification: Run the full suite on macOS after any change to `fsutil.py`. Keep the fallback order.
- Test coverage: Simulated only.

**Out-dir lock is POSIX only:**
- Files: `src/lintle/fsutil.py` (`out_dir_lock`, line 189)
- Why fragile: `fcntl.flock` binds to the local host. ARCHITECTURE.md (around line 403) states that Windows is out of scope. A shared out-dir over NFS is not a tested configuration.
- Safe modification: Give each host its own `--out-dir`.
- Test coverage: No multi-host test.

**Epoch normalization is shared by every pass:**
- Files: `src/lintle/epoch.py` (`_normalize`, lines 26-45), `src/lintle/tle.py` (semantic check, around line 230)
- Why fragile: Day-of-year rolls at year boundaries depend on the year pivot (57). A change to `_normalize` changes sort keys, dedup output, and verify bins at once.
- Safe modification: Change the pivot or the roll rules only with a fixture that covers day `0.x` and `366.x` in leap and non-leap years.
- Test coverage: `tests/test_epoch.py` exists. Confirm the leap-year boundary cases before any change.

**Output scrub covers many legacy layouts:**
- Files: `src/lintle/run_planning.py` (`scrub_outputs`, lines 168-205)
- Why fragile: The function removes current and legacy directories (`data/`, `report/`, root `cleaned/`, `verify/`, `dedup/`). A future directory that reuses a legacy name is removed with no warning.
- Safe modification: Add each new output tree to the tuple at lines 175-181, and to a test that lists the expected trees.

## Scaling Limits

**Two-digit year pivot:**
- Current capacity: Years 1957 to 2056 are representable (`yy` 57-99 and 00-56).
- Limit: Records with an epoch year of 2057 or later are read as 1957 or earlier. The validator does not reject them.
- Scaling path: Replace the pivot with a window relative to the run year, or with a four-digit field from the source.

**Chunk size is counted in records, not bytes:**
- Current capacity: `ExternalSorter` spills after 200,000 items (`src/lintle/verify/grouping.py`, line 111).
- Limit: A chunk of long suspect lines uses much more memory than a chunk of TLE rows.
- Scaling path: Spill by estimated byte count.

## Dependencies at Risk

**`humanize` and `rich` pin upper bounds:**
- Risk: `humanize>=4,<5` and `rich>=15,<16` (`pyproject.toml`). A major release needs a manual bump.
- Impact: Terminal output format changes in the progress table and the summary panel. Structured output is not affected (ARCHITECTURE.md §6, stdout discipline).
- Migration plan: Dependabot groups minor and patch updates. Review majors by hand.

**`sgp4` is a verify-only dependency:**
- Risk: `sgp4>=2.25,<3`. Its parser strictness changed between releases (ARCHITECTURE.md, `verify` section, notes 2.26 and 2.27).
- Impact: `lintle verify --orbit` results can change when the installed `sgp4` changes. The clean path is not affected.
- Migration plan: `uv.lock` pins the exact version. Run `tests/test_orbit.py` before each bump.

## Missing Critical Features

**No pre-run estimate of temp space:**
- Problem: The run does not report the expected spill size for `verify`, `dedup`, or `extract`.
- Blocks: Operators on a small boot volume cannot plan a full-corpus run.

## Test Coverage Gaps

**Real macOS durability path:**
- What's not tested: `F_FULLFSYNC` on a real volume, and the `ENOTSUP` fallback on SMB, NFS, or exFAT.
- Files: `src/lintle/fsutil.py` (`_fsync`, line 46)
- Risk: A macOS regression goes unnoticed. CI does not run on macOS.
- Priority: High

**Hard kill between paired commits:**
- What's not tested: A `SIGKILL` between the `.txt` and `.json` commit in `extract`, and between the checkpoint and output commits in `clean`.
- Files: `src/lintle/extract.py` (lines 360-380), `src/lintle/worker_pool.py` (lines 111-150)
- Risk: Orphan files, or a checkpoint that does not match the outputs.
- Priority: High

**Scrub failure paths:**
- What's not tested: `shutil.rmtree` failures inside `scrub_outputs`, which are ignored by design.
- Files: `src/lintle/run_planning.py` (lines 168-205)
- Risk: Stale outputs survive a fresh run with no message.
- Priority: Medium

**Spill location and free space:**
- What's not tested: Behaviour when `$TMPDIR` is small or read-only during `verify` and `dedup`.
- Files: `src/lintle/verify/grouping.py` (line 78)
- Risk: A run fails late, after most of the work is done.
- Priority: Medium

**Interior-edit resume case:**
- What's not tested: An in-place edit that keeps size, mtime, ctime, and inode.
- Files: `src/lintle/resume.py` (`input_fingerprint`, line 45)
- Risk: Stale reuse of a result, as described above.
- Priority: Low (documented residual)

---

*Concerns audit: 2026-11-02*
