# lintle

## What This Is

lintle is a command-line tool that validates and cleans Two-Line Element (TLE) satellite-tracking files
exported from space-track.org ("lint TLE"). It repairs the systematic export defects (trailing `\`
artifact, missing column-69 checksum digit), re-validates every repair through one validator, and
quarantines what it cannot safely repair into a sidecar detailed enough for a space-track defect report.
It is used by the descent.space pipeline (satellite re-entry prediction); its maintainer, Andrei, is
also its main user.

## Core Value

Never emit a wrong-but-valid-looking record: every record in `01-cleaned/` is valid by construction,
and on any doubt a record is quarantined, never guessed.

## Requirements

### Validated

<!-- Shipped in 0.1.0–0.14.1 and relied upon. -->

- ✓ `lintle clean` streams each source file at constant memory, pairs lines into records, applies candidate fixes, re-validates each fix through `tle.py`, and routes records to `01-cleaned/` or `02-broken/` — existing
- ✓ Five fix classes with repair tiers; missing-checksum reconstruction is opt-in (`--reconstruct-checksum`, since 0.6.0) — existing
- ✓ Run reports: `03-report/` (`report.md`, `report.json`, `report.NNNNN.jsonl` findings, `broken-noradids.ndjson`), `--report json` envelope, stable `RuleID` registry — existing
- ✓ Process-pool parallelism across files, atomic durable commits (`fsutil.durable_replace`), advisory-flock out-dir lock, resume by checkpoint, disk-space guard — existing
- ✓ `--max-quarantined` quality gate with documented exit codes — existing
- ✓ Byte-deterministic unstyled outputs; every record/line stream is a `<stem>.NNNNN.<suffix>` chunk set — existing
- ✓ `lintle verify`: revalidation, source byte-diff, same-epoch contradiction rules, opt-in sampled `sgp4` orbit pass (`--orbit`, `--sensitivity`) → `04-verify/` — existing
- ✓ `lintle dedup`: latest-re-issue-only import list plus per-satellite `manifest.jsonl` → `05-dedup/` — existing
- ✓ `lintle extract`: one satellite's TLE history plus stats sidecar → `06-extract/` — existing
- ✓ Read-only `lintle report`, `lintle explain`, `lintle diff`; interactive wizard; `.lintle.json` remembered paths — existing
- ✓ One definition of a record's moment in time (`epoch.py`, #199); Alpha-5 catalog id decoding (#203) — existing
- ✓ Off-TTY per-unit and phase progress lines for `verify`/`dedup` (#221, in `[Unreleased]`) — existing

### Active

<!-- From the open GitHub issues (2026-11-02). None is in a milestone. -->

- [ ] #228 — finish the `history` reduction: `span_days` computed once in `HistoryStats`, one shared `history.stats_row` for the 8 keys dedup and extract both emit, one home for `RECORD_BYTES` in `tle.py`; outputs byte-identical
- [ ] #227 — one owner for the checkpoint write side (`CheckpointSession` open/record/delete); checkpoint bytes identical
- [ ] #226 — extract the clean orchestration from `cli.py` into `clean_run.py` with `run(args) -> int` and a pure, table-tested `join_exit_code(...)`
- [ ] #223 — make the sidecar byte-determinism claim true: drop the `generated:` stamp from the `.broken.txt` header and inject the run stamp into `report.md` (resolution chosen and prototyped in the 2026-08-16 issue comment)
- [ ] #222 — parallelise the per-file cleaned scan shared by `verify` and `dedup` (`verify/scan.py`), within constant memory and with byte-identical emission order
- [ ] Doc drift: CONTRIBUTING.md says Python 3.11+ (pyproject.toml requires 3.14); README.md and CONTRIBUTING.md call `sgp4` dev-only (it is a verify-scoped runtime dependency)

### Out of Scope

- Reconstruction of missing data characters — Critical Rule #2; only the checksum digit is deterministically recomputable, and only opt-in
- A second validity definition, or `sgp4` deciding validity — Critical Rule #4; `sgp4` is a physics engine, not a validity authority
- `sgp4` or `lintle.verify` imported from the clean path — enforced by an import-graph test
- Loading a whole source file into memory — Critical Rule #3; the largest file is 3.2 GB
- Network access, a server, a database or a GUI — lintle is an offline CLI over local files
- New TLE catalog formats above 99999 — space-track publishes no TLE GP data for those objects; inputs stay 5-character catalog fields
- Committing the corpus — `data/` (~42 GB) is git-ignored

## Context

- Brownfield: 0.14.1 released 2026-08-08; `CHANGELOG.md` `[Unreleased]` holds #221 and #229 fixes not yet cut into a release.
- Codebase map: `.planning/codebase/` (2026-11-02). Living design reference: repo-root `ARCHITECTURE.md`.
- Measured on the 30.4 GB / 232,380,554-record corpus (#222): `clean --reconstruct-checksum` 21 m 23 s on 7 workers; `verify --source --orbit` 2 h 05 m 36 s on 1 core (64% of pipeline wall clock); `dedup` 48 m 50 s on 1 core.
- #226, #227 and #228 were each prototyped before filing: suite green, outputs byte-identical. #227 is described as prerequisite-shaped for #222. A #222 spike (2026-08-16 comment, 4-stem fixture) showed a stem-order merge of parallel per-stem scans is byte-identical to serial.
- Open Dependabot PRs: #238 (setup-uv 9.0.0 → 10.3.3), #239 (dev-dependency bumps).
- Concerns recorded in `.planning/codebase/CONCERNS.md` (not filed as issues): `scrub_outputs` deletes with `rmtree(ignore_errors=True)`; the verify spill directory under `$TMPDIR` is outside the disk guard; CI runs on Ubuntu only.

## Constraints

- **Tech stack**: Python 3.14, uv, runtime deps `rich`, `humanize`, `sgp4` (verify-scoped) — relaxed runtime-dependency policy, `ARCHITECTURE.md` §7
- **Correctness**: the four Critical Rules in `AGENTS.md` — the reason the design exists
- **Determinism**: byte-deterministic unstyled structured/stdout output and chunk sets — diffable, CI-friendly runs
- **Memory**: constant-memory streaming; parallel work must shrink per-worker spill budgets, not multiply them
- **Workflow**: `develop` trunk, one worktree per session, rebase-and-merge PRs; releases are merge commits on `main`; no per-merge version bumps
- **Verification**: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` pass before any PR

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| One validator (`tle.py`) defines "perfect" | A second definition drifts and lets wrong records through | ✓ Good |
| Checksum reconstruction is opt-in (0.6.0) | A dropped data character is indistinguishable from a dropped checksum | ✓ Good |
| `sgp4` is walled out of the clean path | It is permissive enough to become a second validity definition | ✓ Good |
| Relaxed runtime-dependency policy (2026-05-31) | Adopt popular, maintained libraries where they cut owned code; correctness invariants are the only vetoes | ✓ Good |
| Record/line outputs are chunk sets (`<stem>.NNNNN.<suffix>`) | No single output file is huge; `cat` reproduces the single file byte-for-byte | ✓ Good |
| `.planning/ROADMAP.md` is the one roadmap (2026-11-01, keijiban standard) | A repo owns its roadmap; keijiban publishes it to projects.static.lav.ren | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-11-02 after initialization*
