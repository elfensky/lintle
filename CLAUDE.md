# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

`lintle` validates and cleans a ~30 GB corpus of Two-Line Element (TLE)
satellite-tracking files exported from space-track.org.

## Design reference

[`ARCHITECTURE.md`](ARCHITECTURE.md) is the living design reference — read it before changing
validation, repair, pipeline, or output-format behaviour, and keep it current when the design
changes. The dated design specs, implementation plans, and corpus-run summaries are archived
under `docs/superpowers/archive/` (`specs/`, `plans/`, `runs/`) as historical rationale only;
they are point-in-time records and are no longer maintained — `ARCHITECTURE.md` and the code
are the current truth.

## Tech Stack

Python 3.14 · uv · lean runtime (**`rich`** + **`humanize`**) · `sgp4` (test oracle;
`lintle verify` physics engine) · `pytest` · `pytest-cov` · `pytest-xdist` · `hypothesis` · `ruff`

**Runtime dependencies** are governed by a *relaxed* policy (revised 2026-05-31): a popular,
actively-maintained library that genuinely reduces the code we'd otherwise own should be
adopted where it makes sense. The old four-MUST gate and its "aim is a veto" clause are
retired — those four (popular · maintained · reduces-our-burden · sensible shape) are now
*favourable signals*, not necessary conditions. The **only vetoes are the hard correctness
invariants**: one validator definition (Critical Rule #4), constant-memory streaming (#3),
`sgp4`-never-in-the-clean-path (the clean/validate/repair modules never import `sgp4` or
`lintle.verify` — enforced by an import-graph test; see below), byte-deterministic *unstyled*
structured/stdout output (#1/#2 —
`report.*`, NDJSON, sidecar, `--report json`, checkpoint, `cleaned/*`; every record/line output
stream is now a `<stem>.NNNNN.<suffix>` **chunk set** whose in-index-order concatenation is the
byte-deterministic artifact — see `chunking.py`), and the atomic-durable
commit + advisory-flock out-dir lock. The canonical rule and the considered/deferred table live in
[`ARCHITECTURE.md` §7](ARCHITECTURE.md#7-runtime-dependency-policy); the original rationale is
archived under `docs/superpowers/archive/specs/2026-05-28-runtime-dependency-policy-design.md`.
**Current runtime deps: `rich>=15,<16`** (terminal rendering for `clean`),
**`humanize>=4,<5`** (human-readable durations + sizes in the human display; confined to
`summary.py` and `cli_progress.py` — never structured output), and **`sgp4>=2.25,<3`**
(the physics engine for `lintle verify --orbit`; imported only by `verify/orbit.py`). A 2026-06-07 relaxed-bar
re-audit re-confirmed all other candidates as rejected or deferred. `pytest` is dev-only.
**`sgp4` is a physics engine, not a validity authority.** The clean/validate/repair path
(`pipeline`, `repair`, `tle`, and `cli`'s clean path) must never import `sgp4` or
`lintle.verify` — enforced by an import-graph test. Two rules require the wall, and size is not
one of them: (#4) "perfect" is defined once in `tle.py`, and `sgp4` is permissive enough to
become a divergent second definition of validity if the clean path could consult it; and `sgp4`
measures physical *position*, not record validity — an orthogonal concern kept in separate code.
`sgp4` is the sole province of `lintle verify`, which uses it only for consistency/residual
metrics; validity there always routes through `tle.validate_record`. It was promoted from a
dev-only test oracle to a **verify-scoped runtime dependency** with the `verify --orbit` physics
pass (Increment 2) — imported only by `verify/orbit.py`, lazily, and only when `--orbit` runs;
the wall that keeps it out of the clean path stays enforced.

## Critical Rules — principles that must not be violated

These are the reason the design exists; an implementation that breaks them is wrong.

1. **Validated transformation.** Never apply a fix and trust it. Apply a candidate fix,
   re-run *full* validation, and commit the result only if it now passes — otherwise
   quarantine.
2. **Correctness over recovery.** Never emit a wrong-but-valid-looking record; when in
   doubt, quarantine. No reconstruction of missing *data* characters. The one sanctioned
   reconstruction is a missing *checksum* digit, which is deterministically recomputable —
   and even that is a distinct, weaker repair tier with its own reporting.
3. **Constant memory.** Files stream; the pairing state machine holds at most two lines.
   A 3.2 GB file must never be loaded whole.
4. **One validator definition.** "Perfect" is defined once, in `tle.py`. Never add a
   second, divergent validation path.

**Report outcomes faithfully.** If tests fail, say so with the output. If a verification
step was skipped, say that rather than implying it ran. Never claim "all tests pass" when
output shows failures.

## The corpus (`data/`, git-ignored)

- `data/source/` — 29 raw `tle*.txt` files (~30 GB) plus `TLEs.zip` (~12 GB). Inputs.
- `data/output/` — where `clean` writes its numbered output tree (`01-cleaned/`, `02-broken/`,
  `03-report/`) plus a root `README.md`; `verify`/`dedup`/`extract` add `04-verify/`/
  `05-dedup/`/`06-extract/`. Outputs.
- The whole `data/` tree is git-ignored — ~42 GB — and must never be staged or committed.
- **Never read a corpus file whole** — the largest is 3.2 GB. Sample with `head`, `awk`,
  or `sed -n`.

## Code Style

- Python 3.14. Concise one-paragraph docstrings on every public module, function, and
  class — match that established style; do not expand to Args/Returns/Raises blocks.
- `ruff` for linting and formatting, configured in `pyproject.toml` (rule sets `E`, `F`,
  `I`, `UP`, `B`, `SIM`; 88-column lines).
- **Modern Python (3.14) idioms.** `ruff`'s `UP`/`SIM` sets auto-enforce most of these on
  every commit — f-strings, `X | None` unions, builtin generics (`list[bytes]`),
  `contextlib.suppress`, PEP 758 `except A, B:`. Three conventions `ruff` does *not*
  enforce, so apply them by hand for consistency:
  - **`match`** for 3-or-more-way type/shape dispatch — not an `isinstance`/`elif` chain
    (a single 2-way `isinstance` check stays a plain `if`).
  - **`@dataclasses.dataclass(slots=True)`** on every dataclass (add `frozen=True` when
    immutable). Slotted dataclasses pickle correctly across the worker pool — keep it that
    way (no `__dict__`-dependent tricks).
  - **`collections.Counter`** (`.update()`) for tally/accumulate loops — not
    `d[k] = d.get(k, 0) + 1`. Convert back with `dict()` at any byte-deterministic output
    boundary so first-seen key order — and thus the JSON bytes — is preserved.
- `src/` layout — all package code lives under `src/lintle/`.
- Run `uv run ruff check .` and `uv run ruff format --check .` before committing.

## Project Layout

```
src/lintle/
├── __main__.py    # python -m lintle entry point
├── __init__.py    # __version__, stem() filename helper
├── cli.py         # argparse, globbing, top-level clean orchestration, exit codes
├── cli_progress.py # live multi-file progress display, file roster, status spinner, post-run phase bar (rich+humanize)
├── run_planning.py # clean-run preflight: disk-space guard, output scrub, resume classification, RunPlan
├── worker_pool.py  # process-pool dispatch, progress collection, per-file failure + checkpoint
├── process_control.py # worker SIGINT setup, fast pool termination, cancel/exit-code helpers
├── thresholds.py   # --max-quarantined parsing + quality-gate exit policy (pure)
├── output_artifacts.py # end-of-clean-run report.md / report.json / broken-noradids.ndjson / report.jsonl
├── pipeline.py    # streams a file in binary, pairs 1/2 lines into records, routes them
├── repair.py      # speculative fixes, each confirmed by tle.py before commit
├── report.py      # FileStats + dataclasses, the run summaries, the run report
├── report_aggregation.py # pure corpus aggregation: run totals + per-NORAD rollups for report.py
├── report_writers.py # structured-file writers: .broken.txt sidecar, report.jsonl findings, broken-noradids.ndjson, shard concat
├── resume.py      # single-run checkpoint for `clean --resume` (#56); run-stamp + output-size helpers
├── fsutil.py      # durable_replace — the one atomic+fsync commit path (issue #58)
├── summary.py     # responsive aggregate-panel renderer + read-only `lintle report` (rich+humanize)
├── term.py        # stderr+stdout Consoles + error/warning/note/prompt + is_interactive/prompt_yes_no (rich)
├── diff.py        # read-only: per-rule delta between two runs' report.jsonl (lintle diff)
├── explain.py     # read-only: renders rule/fix documentation (lintle explain)
├── dedup.py       # `lintle dedup` — latest-re-issue-only import list + per-satellite manifest.jsonl from cleaned/ (reuses verify's sort)
├── extract.py     # `lintle extract` — one satellite's TLE history + stats sidecar from a dedup run (binary search, no index)
├── history.py     # pure history reducer (HistoryStats/Gap, analyze_epochs) shared by extract + dedup — no I/O, no sgp4
├── chunking.py    # ChunkedWriter/ChunkedReader — the <stem>.NNNNN.<suffix> chunk-set layer
├── config.py      # optional ./.lintle.json remembering source/output dirs (stdlib JSON)
├── wizard.py      # interactive rich menu shown when `lintle` runs with no subcommand
├── tle.py         # the validator — column layout, checksum, semantic ranges, pairing
├── diagnostics.py # stable RuleID registry + structured Diagnostic dataclass (pure data)
├── categories.py  # FixClass enum + FixSpec registry — the repair taxonomy (pure data)
├── explain_examples.py # validator-verified examples + citations backing explain (pure data)
└── verify/        # `lintle verify` — post-run auditor (sole importer of sgp4, via orbit.py)
    ├── __init__.py # run() orchestration
    ├── checks.py   # revalidation, source byte-diff, contradiction rules
    ├── epoch.py    # epoch parsing/keys
    ├── grouping.py # external merge-sort for (catalog, epoch) grouping
    ├── orbit.py    # opt-in --orbit sgp4 physics pass (lazy import)
    ├── records.py  # streams cleaned chunk sets back as records
    └── report.py   # SuspectSink, suspects.jsonl + summary.{json,md} writers
```

Module dependencies flow one way: `cli.py → pipeline.py → repair.py → tle.py`,
with the read-only `cli.py → diff.py` and `cli.py → explain.py → explain_examples.py`
consumers and the `cli.py → resume.py` single-run checkpoint alongside. The `clean`
orchestration is split into cli-helper leaves — `run_planning.py` (preflight:
disk-space guard, output scrub, resume classification), `worker_pool.py` (process-pool
dispatch), `process_control.py` (signals/shutdown), `thresholds.py` (quarantine exit
policy, pure), and `output_artifacts.py` (run finalization). These leaves import their
own collaborators directly rather than receiving them by injection: `worker_pool`
imports `concurrent.futures`/`multiprocessing`/`signal` plus `process_control`,
`pipeline`, `cli_progress`, `report`, `resume`, and `term`; `run_planning` imports
`report`, `resume`, and `term`. `process_control` is depended on by `cli` and
`worker_pool`. `report_aggregation.py` is a pure corpus-aggregation leaf depended on by
`report.py`. `diagnostics.py` and `categories.py` are pure-data leaves depended on by
`repair`, `pipeline`, `report`, and `explain`; `explain_examples.py` is also pure data,
composing those two leaves into documented examples. `report_writers.py` is the
structured-file writers leaf (the `.broken.txt` sidecar, the `report.jsonl` findings
shards, the corpus `broken-noradids.ndjson`, and the shard concat) depended on by
`pipeline` and `cli`; it imports the dataclasses and the shared `format_diagnostic`
renderer from `report.py` — one-way, never the reverse, so no cycle. `cli_progress.py`
is a rich+humanize presentation leaf (the live `ProgressDisplay`, the pre-run
`render_roster`, the `status` spinner, and the `phase_bar` used by the
single-process post-run phases) depended on by `cli`, `worker_pool`,
`output_artifacts`, `verify`, `verify.orbit`, and `dedup` — those last three
consume the clean path's presentation leaf, never the reverse, so the
`sgp4`/verify wall is untouched; it imports `pipeline`'s typed progress messages
(`FileStarted`/`FileEnded`/`FileProgress`) to drive the display and `humanize` for
human-readable roster sizes (`naturalsize(gnu=True)`), so the chain
`cli → worker_pool → cli_progress → pipeline` is one-way and acyclic. `fsutil.py` is a
stdlib-only I/O leaf (the durable-commit helper) depended on by `pipeline`, `report`,
`report_writers`, and `resume`. `summary.py` is a rich+humanize presentation leaf (the
responsive aggregate-panel renderer and the read-only `lintle report` entry that reads
`<out-dir>/report.json`) depended on by `cli`; it imports the two shared Consoles from
`term`, `humanize` for human-readable panel durations (`precisedelta`), and consumes the
`build_run_envelope` dict shape, so `cli → summary → term` is one-way and acyclic. `term.py` is a rich-only terminal-IO leaf (the two shared
Consoles — `stderr_console` for status/errors, `stdout_console` for the report view —
the `error`/`warning`/`note`/`prompt` emitters, and the `is_interactive` /
`prompt_yes_no` stdin helpers) depended on by `cli`, `cli_progress`, `diff`,
`process_control`, `run_planning`, `summary`, and `worker_pool` — so the styled prefixes
and the prompt live in one place without a `→ cli` cycle. `resume.py` (which also owns the run-start timestamp and
the per-file output-size capture for the checkpoint) imports only `__version__`,
`fsutil`, and `stem`. `tle.py` and the data modules carry no I/O, so cycles are
structurally impossible.

→ See [`README.md`](README.md) for the architecture, usage, and data flow.
→ See [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup, testing, and the git workflow.

## Commands

```bash
uv sync                            # Install, including dev deps (sgp4, pytest, ruff)
uv run pytest                      # Full test suite (runs in parallel via -n auto)
uv run pytest tests/test_tle.py::TestComputeChecksum   # A single test class
uv run pytest --cov=lintle --cov-report=term-missing --cov-branch  # Tests + coverage
uv run ruff check .                # Lint
uv run ruff format --check .       # Format check
uv run lintle clean             # Clean data/source/ -> data/output/
```

> **`--pdb` caveat:** the default suite runs in parallel (`-n auto`); `--pdb` is incompatible
> with `pytest-xdist`. Disable parallelism when debugging: `uv run pytest -n0 --pdb ...`.

## Working Style

- **Use agents** for codebase exploration and multi-step research tasks.
- **Always verify** after a change: run `uv run pytest`, `uv run ruff check .`, and
  `uv run ruff format --check .`, and report the actual output.
- Build order, if rebuilding from the spec (§12): `pyproject.toml` → `tle.py` (test-first,
  it is the correctness oracle) → `repair.py` → `pipeline.py` → `report.py` / `cli.py`.

## Worktree Workflow

Trunk is `develop`; `main` carries one merge commit per release and never
receives direct commits. Two paths into `develop`:

- **Direct commits** for chores and bugfixes (`chore:`, `fix:`, `docs:`,
  `test:`, `style:`) — commit on `develop`, push. No branch, no PR.
- **Branch + PR** for features and multi-file refactors (`feature/<desc>` or
  `refactor/<desc>`) — land via **rebase-and-merge** so `develop` stays linear
  (see `CONTRIBUTING.md` § Git Workflow).

**Worktrees are the parallel-development mechanism for branched work** — they
let multiple branches share one clone without contention, so you can keep a
long-running test run in one worktree while editing in another.

**When to use a worktree:** any `feature/<desc>` or `refactor/<desc>` branch.
Default for any non-trivial change you'd raise a PR for.

**When to skip the worktree:** chores and bugfixes — single-line fixes, doc
edits, dependency bumps, `ruff format` passes. Commit directly on `develop` in
the main checkout. No branch, no PR.

**Feature workflow (worktree):**

1. From the main checkout, create the worktree off `develop`:
   `git worktree add .worktrees/<branch-dir> -b <branch-name> develop`
2. `cd .worktrees/<branch-dir>`
3. Install dev deps in the worktree: `uv sync`
4. **Symlink the corpus into the worktree** (the ~30 GB `data/` tree lives only
   in the main checkout; the symlink keeps a single copy on disk and lets the
   CLI work transparently): `ln -s ../../data data`
5. Do the work in the worktree directory — small, logical commits as you go
   (tests first, then implementation), not one giant commit at the end
6. Verify in the worktree: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
7. Land via PR. Push the branch (`git push -u origin <branch-name>`), open a
   PR against `develop`, then use the GitHub UI's **"Rebase and merge"**
   button (or `gh pr merge --rebase --delete-branch`). Do not use "Create a
   merge commit" or "Squash and merge".
8. Clean up: `git worktree remove .worktrees/<branch-dir>` then
   `git branch -D <branch-name>` (use `-D`, not `-d`: rebase-and-merge
   rewrites the SHAs on `develop`, so the local branch won't look "merged"
   to git even though its content has landed).

**No per-merge version bumps.** Feature merges to `develop` do not touch
`pyproject.toml`'s version. Version bumps and the dated `CHANGELOG.md` section
land together on a `chore/release-X.Y.Z` branch — see `CONTRIBUTING.md`
§ Versioning § Release flow. Add CHANGELOG-worthy notes alongside the code in
your feature branch; they'll be collected under the next dated version when the
release is cut.

**Chore/bugfix workflow (direct on develop):** stay on `develop` in the main
checkout, edit, run the same verification chain, commit with the right
conventional-commit prefix (`chore:`, `fix:`, `docs:`, `test:`, `style:`),
push. No branch, no worktree, no PR.

**Worktree directory:** `.worktrees/` in project root (git-ignored). Directory
names mirror the branch with slashes replaced by hyphens —
`feature/repair-tier-2` → `.worktrees/feature-repair-tier-2`.

**Parallel worktrees:** multiple `.worktrees/*` directories can coexist. Each has
its own `.venv/` (created by `uv sync`); the symlinked `data/` is shared, so
don't write through it — `clean` writes to `data/output/` and concurrent
worktrees writing there will collide. Pass `--out-dir <worktree-local-dir>` to
`lintle clean` when iterating in parallel so each worktree writes to its own
output tree.

## Verification

After completing edits, run these before reporting success:

```bash
uv run pytest                      # Must pass
uv run ruff check .                # Must pass
uv run ruff format --check .       # Must pass
```

If any fail, report the actual output — do not suppress or simplify failures.

## File Guidelines

- Never read a corpus file whole — sample with `head`, `awk`, or `sed -n`.
- When renaming a function or variable, search for direct calls, string literals
  containing the name, re-exports, and test references.
- Prefer files with one clear responsibility; keep functions focused and readable.

## Conventions

- The living design reference is `ARCHITECTURE.md` (repo root) — keep it current when the
  design changes. Historical design docs, implementation plans, and run summaries are archived
  under `docs/superpowers/archive/{specs,plans,runs}/` (dated `YYYY-MM-DD-topic.md`, point-in-time
  records, not maintained).
- Tests are grouped into `Test*` classes, one per unit or behaviour under test.
- Git: `develop` is the trunk; `main` carries one merge commit per release and
  never receives direct commits. On `develop`: chores and bugfixes (`chore:`,
  `fix:`, `docs:`, `test:`, `style:`) commit directly; features and multi-file
  refactors go on a `feature/<desc>` or `refactor/<desc>` branch and land via
  **rebase-and-merge** so `develop` stays linear. Releases are hand-assembled
  merge commits on `main` (tree = develop's release-point tree; second parent =
  develop's release-point) — see CONTRIBUTING.md § Versioning for the
  `git commit-tree` recipe. Tagged on `main`. Use `git log --first-parent main`
  for the release-only view. Use conventional commits (`feat:`, `fix:`,
  `docs:`, `test:`, `refactor:`, `style:`, `chore:`).
- Versioning: `pyproject.toml`'s `[project] version` is the single source of truth;
  `src/lintle/__init__.py` resolves `__version__` from it at runtime via
  `importlib.metadata`. Bump it once, add a `CHANGELOG.md` entry — see CONTRIBUTING.md
  for the release flow.
