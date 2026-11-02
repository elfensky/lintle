# Requirements: lintle

**Defined:** 2026-11-02
**Core Value:** Never emit a wrong-but-valid-looking record: every record in `01-cleaned/` is valid by construction, and on any doubt a record is quarantined, never guessed.

Source of scope: the five open GitHub issues (#222, #223, #226, #227, #228, read 2026-11-02, with Andrei's
2026-08-16 comments on #222 and #223), the doc drift found by the codebase map, and the research in
`.planning/research/`. Every requirement keeps the four Critical Rules in `AGENTS.md`.

## v1 Requirements

### Determinism claim (#223)

- [ ] **DET-01**: Two `clean` runs over identical inputs produce byte-identical `02-broken/` trees: the `.broken.txt` header is `# source: <name> | lintle <version>`, with no `generated:` stamp
- [ ] **DET-02**: `report.md` carries the same run stamp as `report.json`'s `run.timestamp`, injected from the envelope (`format_run_report`/`write_run_report` take `generated_at`), not read from the clock inside the renderer
- [ ] **DET-03**: The freeze test no longer canonicalises timestamps under `02-broken/`, so a reintroduced clock in the sidecar fails the test
- [ ] **DET-04**: Every statement of the byte-determinism invariant (`AGENTS.md`, `ARCHITECTURE.md` incl. the example header at line ~722, `CONTRIBUTING.md`) says exactly which artifacts carry run metadata (`report.json` `run.timestamp` and timing floats, `report.md` stamp)

### History leaf (#228)

- [ ] **HIST-01**: `span_days` is computed once, with the empty-epochs guard, in `history.analyze_epochs` / `HistoryStats`; `dedup` and `extract` read it from there
- [ ] **HIST-02**: One `history.stats_row(catalog, hs)` builds the 8 keys both the dedup manifest row and the extract sidecar emit; `04-verify/`, `05-dedup/` and `06-extract/` stay byte-identical
- [ ] **HIST-03**: `RECORD_BYTES` has one home, `tle.py` (`2 * (LINE_LENGTH + 1)`, equal to 140); `verify/scan.py` and `extract.py` import it

### Clean orchestration (#226)

- [ ] **CLEAN-01**: The clean run lives in `src/lintle/clean_run.py` behind `run(args) -> int`; `cli.py`'s clean arm calls it; import direction stays `cli → clean_run`
- [ ] **CLEAN-02**: A pure `join_exit_code(...)` maps the five sources (plan verdict, operational error, interrupt, failed file, quarantine gate) to an exit code, with a table test of the documented precedence
- [ ] **CLEAN-03**: The clean-path import-graph test seeds include `clean_run`, and the suite passes with zero golden diff

### Checkpoint session (#227)

- [ ] **CKPT-01**: A `CheckpointSession` (open / record / delete) owns the checkpoint write side; `worker_pool` records a finished file with one `session.record(path, stats)` call and no longer imports `resume` or `report`
- [ ] **CKPT-02**: Checkpoint bytes are identical to the four-call path, pinned by a test against one fully populated stats object
- [ ] **CKPT-03**: `resume.py` still never imports `report` (the summary builder is injected at `open()`)

### Parallel cleaned scan (#222)

- [ ] **PAR-01**: Per-phase timing for `verify` and `dedup` (scan, merge/conflicts, orbit, writes) is measured on the corpus before the parallel scan is built, and the result decides whether `dedup` is parallelised too
- [ ] **PAR-02**: `verify` (and `dedup`, if PAR-01 justifies it) accepts `--jobs N` with the same meaning as `clean`; `--jobs 1` runs serially
- [ ] **PAR-03**: Output under `04-verify/` (and `05-dedup/`) is byte-identical for every `--jobs` value and to the serial output: per-stem results merge in stem order, never completion order, proven by a multi-stem fixture with cross-stem ties, forced spills and forced reverse completion
- [ ] **PAR-04**: Memory stays constant: workers return spilled run paths and small tallies, never record lists; the per-worker spill budget shrinks with the worker count; the final merge's open-file count is bounded
- [ ] **PAR-05**: Exit codes, SIGINT behaviour and per-stem progress rows work as in the serial scan; the `sgp4` wall is untouched (`--orbit` stays a separate, serial pass)

### Documentation drift

- [ ] **DOC-01**: `CONTRIBUTING.md` states Python 3.14 (as `pyproject.toml` requires)
- [ ] **DOC-02**: `README.md` and `CONTRIBUTING.md` list the actual runtime dependencies (`rich`, `humanize`, and `sgp4` as verify-scoped), not "`sgp4` is a dev-only test oracle"

## v2 Requirements

Deferred. Tracked, not in the current roadmap.

### Pipeline throughput

- **THRU-01**: Parallel sort/merge and parallel `--orbit` pass (Amdahl limit after #222)
- **THRU-02**: Reuse of a prior sorted order via `cleaned_fingerprint`

### Robustness (from `.planning/codebase/CONCERNS.md`, not filed as issues)

- **ROB-01**: `scrub_outputs` reports a failed delete instead of `rmtree(ignore_errors=True)`
- **ROB-02**: The disk-space guard covers the `verify`/`dedup` spill directory under `$TMPDIR`
- **ROB-03**: A macOS CI job (CI runs on `ubuntu-latest` only)

## Out of Scope

| Feature | Reason |
|---------|--------|
| Reconstruction of missing data characters | Critical Rule #2; only the checksum digit is recomputable, and only opt-in |
| A second validity definition or `sgp4` deciding validity | Critical Rule #4; `sgp4` is a physics engine, not a validity authority |
| `sgp4` or `lintle.verify` in the clean path | Enforced by the import-graph test |
| Completion-order (unordered) merge, unbounded prefetch, thread pools | Break byte-determinism or constant memory; the GIL keeps threads on one core |
| A `--deterministic` toggle, `SOURCE_DATE_EPOCH`-style override, checksum manifests | Anti-features: determinism is the default, not an option |
| Behaviour changes inside the #226/#227/#228 refactors | They are pure refactors with zero golden diff |
| Network access, a server, a database, a GUI | lintle is an offline CLI over local files |
| TLE catalog ids above 99999 | space-track publishes no TLE GP data for those objects |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|

**Coverage:**
- v1 requirements: 22 total
- Mapped to phases: 0
- Unmapped: 22 ⚠️

---
*Requirements defined: 2026-11-02*
*Last updated: 2026-11-02 after initial definition*
