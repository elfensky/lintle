---
gsd_state_version: '1.0'
status: planning
progress:
  total_phases: 4
  completed_phases: 0
  total_plans: 0
  completed_plans: 0
  percent: 0
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-11-02)

**Core value:** Never emit a wrong-but-valid-looking record: every record in `01-cleaned/` is valid by construction, and on any doubt a record is quarantined, never guessed.
**Current focus:** Phase 1 - Leaf cleanups and determinism claim (#228, #223, doc drift)

## Current Position

Phase: 1 of 4 (Leaf cleanups and determinism claim)
Plan: 0 of TBD in current phase
Status: Ready to plan
Last activity: 2026-11-02 - Roadmap created; 20/20 v1 requirements mapped

Progress: [░░░░░░░░░░] 0%

## Performance Metrics

**Velocity:**
- Total plans completed: 0
- Average duration: -
- Total execution time: 0.0 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| - | - | - | - |

**Recent Trend:**
- Last 5 plans: -
- Trend: -

*Updated after each plan completion*

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- [Roadmap]: Phase order is #228 + #223 + doc drift, then #226, then #227, then #222. Phases 2-4 edit the same files, so they stay sequential.
- [Roadmap]: #223 is resolved. Drop the `generated:` stamp from the `.broken.txt` header and inject the run stamp into `report.md`. Do not thread the stamp through `CleanConfig`.
- [Roadmap]: `resolve_jobs` belongs in `worker_pool.py`, never in `clean_run.py` (Phase 2 plans, used by Phase 4).
- [Roadmap]: Default `--jobs` for `verify`/`dedup` is decided at Phase 4 plan time. Recommended: match `clean` in the CLI, keep single-stem fixtures on the serial path of the same code.

### Pending Todos

None yet.

### Blockers/Concerns

- [Phase 1]: Dependabot PRs #238 (setup-uv) and #239 (dev dependencies) are open. Merge or close them first, so each refactor diff has one cause.
- [Phase 3]: Confirm with a test that an older checkpoint cannot be resumed after the sidecar-header change (`validate_run_identity` checks `lintle_version`).
- [Phase 4]: Needs research before planning. Open questions: final-merge shape (parent merge vs worker pre-merge), `SuspectSink` split with per-stem spill, progress channel for `UnitTable`, default `--jobs` and `--orbit` memory, spill-dir location vs the disk guard.
- [Phase 4]: `tests/test_freeze.py` uses one stem with four records. A multi-stem, forced-spill, reverse-completion fixture must be the first commit of the phase.
- [Phase 4]: Unmeasured: share of scan time in `verify --source --orbit` and `dedup`; Linux soft fd limit (1,024 assumed); spill budget floor (about 25,000 records) and fan-in cap (about 256).
- [Phase 4]: CI runs on Ubuntu only. Add a macOS job for the parallel tests, or document manual coverage.

## Deferred Items

Items acknowledged and deferred at milestone close, most recent first:

| Category | Item | Status | Deferred At | Milestone |
|----------|------|--------|-------------|-----------|
| *(none)* | | | | |

## Session Continuity

Last session: 2026-11-02
Stopped at: Roadmap created; ready for `/gsd-plan-phase 1`
Resume file: None
