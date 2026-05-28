# Issue #53 — `clean` progress UI redesign — Design

- **Date:** 2026-05-28
- **Status:** Designed; ready for implementation.
- **Revision:** rev 2 (2026-05-28) — **dropped the pre-scan.** The roster is now size-only
  (instant, from `os.stat`); exact record counts ride on the cleaning pass itself — shown
  live as each file is read and finalized at completion — instead of a second full read of
  the corpus. §4 reconciled with the now-canonical dependency policy (`rich` is a
  *candidate*, adopted in this feature's PR with evidence). · rev 1: pre-scan + roster.
- **Topic:** Replace the single-line, "stuck on one filename" live progress display in
  `lintle clean` with (a) an **instant size-only roster** of discovered files, (b) a
  **multi-file per-worker live block** showing each file's byte progress and running record
  count, and (c) a refined `--jobs` default that reserves one core and caps at the file
  count. `rich` is the candidate runtime dependency that drives the terminal rendering.

## 1. Problem

`lintle clean` is a long-running batch over ~30 GB of TLE input — wall-clock around an
hour on the production corpus. While it runs, the only user-visible signal is the single
live line emitted by `_ProgressDisplay` in `cli.py`. Three concrete complaints, all
filed as issue #53:

1. **The "active file" hint is dishonest.** The live line ends with
   `tle2004_6of8.txt +9 more`, where the leading filename is the *oldest* of the
   currently in-flight files (`cli.py:449–456`, design intent: surface the bottleneck).
   In steady state with N similar-sized workers, the oldest file changes only when one
   finishes — so the same name stays on screen for many minutes while peers complete
   silently behind it. Users reasonably read the unchanging name as "the program is
   stuck on this file."
2. **There is no pre-run roster.** Before workers dispatch, `clean` emits one line —
   `processing N file(s) with N worker(s)...` (`cli.py:606–608`). The user has no
   up-front visibility into what is about to be done: which files, how big, in what order.
3. **`--jobs` defaults to `os.cpu_count()` flat.** The default is already CPU-aware
   (`cli.py:186`) but oversubscribes the box: 16 workers on a 16-core machine leaves no
   headroom for interactive work during a one-hour run, and 16 workers on a 4-file
   directory is wasteful.

The current renderer is also a maintenance liability: ~150 lines of hand-rolled ANSI in
`cli.py` (`_SPINNER`, `_format_elapsed`, `_render`, manual `isatty()` branching, manual
`\r\x1b[K` cursor moves, no terminal-resize handling). A multi-file extension would push
that toward ~300 lines plus golden-frame ANSI tests — work that maps almost 1:1 onto a
mature ecosystem library (`rich.progress.Progress`).

## 2. Decision summary

Three coordinated changes to `lintle clean`'s startup and live UI. The record-count
information users want is delivered **without a second read of the corpus**: the cleaning
pass already reads every byte once, so exact counts are a free byproduct — surfaced live
and finalized as each file completes.

### 2.1 Instant size-only roster (init phase)

Before workers dispatch, print a one-shot roster to stderr from `os.stat` alone — **no
file contents are read**, so it appears instantly regardless of disk speed. Columns:
`#`, `file`, `size`, with a final total-size row. Sort order is `discover_paths(args.path)`
order (alphabetical), so a known filename is easy to find. Rendered as a
`rich.table.Table`; degrades to plain ASCII when stderr is not a TTY.

There is deliberately **no record-count column here** — an exact count would require
reading all 30 GB before cleaning even starts (a second full pass on a constant-memory
tool, 5–10 min on HDD), to learn early what the cleaning pass reveals anyway. Counts
appear live during the run (§2.2) and exact at completion (§2.3 of the behaviour contract).

### 2.2 Multi-file per-worker live block

Active only when stderr is a TTY (`Console(stderr=True).is_terminal`); when stderr is
piped or redirected, the block is suppressed and the existing one-line-per-completed-file
fallback (`cli.py:371–379`) is preserved.

Replace the single live line with a `rich.progress.Progress` instance carrying:

- One **overall** line: `done/total files`, total **records** processed so far, and
  records/sec (the running tally the current display already shows).
- One **per-active-file** row: `name`, a byte-progress bar + percentage, and that file's
  **running record count**. The byte bar's denominator is `os.stat().st_size` (captured
  when the file starts) — bytes are unambiguous even when a file is corrupt and its record
  count is still settling.

Illustration (the record numbers are live, ticking up as each file is read; exact when it
finishes):

```
⠹ cleaning · 0:42 · 6/29 files · 18.2 M records · 431k rec/s
   tle2005.txt  ███████░░░  63%   6.9 M records
   tle2009.txt  ████░░░░░░  38%   4.1 M records
✓ tle2004.txt   10,431,072 records · 9,998,210 clean · 432,862 quarantined
```

The renderer's job is to drain the queue, translate each message into a
`Progress.update(...)` call, and otherwise stay out of `rich`'s way. Frame rate, cursor
positioning, terminal-resize handling, non-TTY detection, `NO_COLOR` / `FORCE_COLOR`
honouring, ANSI generation, and the clear-on-exit contract are all delegated to `rich`.

### 2.3 Refined `--jobs` default

Change `cli.py:186`'s default from `os.cpu_count() or 1` to a value resolved after
`discover_paths` returns:

```python
default_jobs = max(1, min((os.cpu_count() or 1) - 1, len(files)))
```

Two effects: reserve one logical core for the OS / interactive work during the long
clean run, and avoid spawning more workers than there are files. The explicit
`--jobs N` override is *not* capped at the file count — a user who passes `--jobs 16`
on a 4-file directory has chosen oversubscription deliberately. `--help` text becomes
`default: CPU count − 1, capped at file count`.

### 2.4 `rich` is a candidate dependency

`rich` drives the roster table and the live block. Per the now-canonical dependency
policy (authoritative spec §3.1), it is a **candidate, not pre-approved** — this feature's
PR is where it clears the four MUST bars *with evidence*. See §4.

## 3. Behaviour contract (normative)

### 3.1 Startup sequence

`lintle clean <path>` executes the following stages in order:

1. Argument parse, `discover_paths(args.path)` → `files`.
2. Resolve `default_jobs` per §2.3 from `len(files)` and `os.cpu_count()`.
3. `os.stat` each file for its size (instant; no contents read). Print the size-only
   roster (`rich.table.Table`) once on stderr.
4. Print the existing one-line summary `processing N file(s) with N worker(s)...`
   (preserved verbatim — downstream log parsers may rely on it).
5. Dispatch the cleaning workers as today.

`lintle clean --resume`:

- The roster lists only `files_to_process` (the not-yet-completed subset).
- The existing `resuming: N file(s) already complete, processing M of N with K worker(s)...`
  line (`cli.py:599–604`) is preserved verbatim and replaces stage 4.

### 3.2 Live progress block

Active only on a TTY (`Console.is_terminal == True`). When active, stderr carries:

- One overall line: completed files / total files, total records so far, records/sec.
  Advances on each `("end", name)` and on every progress message.
- Zero or more per-file rows: one row per file currently in `_active`. A row appears on
  `("start", name)`, advances on each `("progress", name, bytes_delta, records_delta)`,
  and is removed on `("end", name)`. Each row shows: filename (middle-ellipsis if it
  exceeds the column width), byte-progress bar + percentage, and that file's running
  record count.

When stderr is not a TTY, the live block is suppressed. The existing one-line summary
emitted per completed file by `_ProgressDisplay.file_done` (`cli.py:371–379`) — carrying
that file's **exact** clean/quarantined counts — is preserved verbatim, modulo being
routed through the same `rich.console.Console` (which renders plain text in non-TTY mode).

### 3.3 Record-count exactness

Per-file record counts shown *during* a file are live partial sums; the count is **exact**
the moment the file completes — emitted in the per-file completion line, the end-of-run
summary, and `report.md`. No number shown is ever a guess; the up-front roster simply omits
the count rather than estimate it.

### 3.4 Worker-count contract

| Invocation | Resolved `jobs` |
|---|---|
| No flag, `cpu_count = 16`, `len(files) = 29` | `15` |
| No flag, `cpu_count = 16`, `len(files) = 4`  | `4`  |
| No flag, `cpu_count = 2`,  `len(files) = 30` | `1`  |
| No flag, `cpu_count = 1`,  `len(files) = 30` | `1`  |
| `--jobs 16`, `len(files) = 4`                | `16` (user override, not capped) |
| `--jobs 0`                                   | argparse-rejected (existing `type=int`; validator rejects `< 1`) |

### 3.5 Error and interrupt contract

| Event | Behaviour |
|---|---|
| `os.stat` fails on one file (permission, race) | Show `—` for that file's size in the roster and proceed; the clean stage's existing per-file error handling applies. Other files unaffected. |
| `KeyboardInterrupt` during cleaning | Unchanged from today (`cli.py:678–680`): terminate workers, exit `130`, surviving outputs are durable per the existing fsutil/resume contracts. The live block is cleared on exit (delegated to `rich`). |
| Terminal resize mid-run | Handled by `rich`. The block redraws at the new width on its next frame. |
| `NO_COLOR=1` env var | `rich.console.Console` honours it; bars and headers render without ANSI colour. Layout unchanged. |

## 4. Policy & `rich` adoption (reconciled)

The runtime-dependency policy is codified canonically in the **authoritative spec §3.1**
(rationale + the four-model debate: [`2026-05-28-runtime-dependency-policy-design.md`](2026-05-28-runtime-dependency-policy-design.md)).
This feature does not introduce the policy; it *exercises* it.

`rich` is a **candidate** in the §3.1 table. This feature's PR is the evidence-driven
adoption point, where it must demonstrably clear the four MUST bars and then:

- flip `pyproject.toml` `dependencies` from `[]` to `["rich>=13,<14"]` (major capped per
  the policy's maintenance clause);
- add the `CHANGELOG.md` `[Unreleased]` § Added entry;
- move `rich`'s authoritative-spec §3.1 table row from **candidate** → **adopted (vN)**;
- update the "Current runtime dependencies" line in §3.1 (and the CLAUDE.md / CONTRIBUTING
  pointers' status) from "none" to `rich`.

Bar evidence to record in the PR: **earns its weight** (removes ~150 lines of hand-rolled
ANSI in `cli.py` and avoids the multi-line block we would otherwise write); **mature**
(`pip`, `uv`, `pdm`, `typer`); **small surface** (pure-Python; transitive `markdown-it-py`
+ `pygments`); **operational fit** (pure-Python wheels, no native toolchain; terminal-only,
no streaming-path or memory impact; honours `NO_COLOR`).

## 5. Module touchpoints

Module dependency direction (from CLAUDE.md) is unchanged; `rich` is imported only by
`cli.py`. Touched modules:

- `src/lintle/cli.py` — adds `_render_roster` (size-only, from `os.stat`); rewrites
  `_ProgressDisplay` as a `rich.progress.Progress` driver; resolves `default_jobs` per
  §2.3.
- `src/lintle/pipeline.py` — `process_file` emits per-file progress deltas (bytes +
  records) on the existing `progress_queue`, coalesced (§6). No `rich` import; this is
  pure data on the queue, so the constant-memory streaming path (Critical Rule #3) is
  unaffected.

Untouched: `tle.py`, `repair.py`, `report.py`, `fsutil.py`, `resume.py`, `diff.py`,
`explain.py`, `diagnostics.py`, `categories.py`, `explain_examples.py`, `__init__.py`,
`__main__.py`. The validator, repair, report, resume, and durable-commit contracts are all
unaffected. This is a UI change.

## 6. Wire protocol — progress queue

The existing queue (drained at `cli.py:410–433`) carries:

- `int` — record-count delta (per-worker, coalesced) — **replaced** (see below).
- `(str, str)` — lifecycle event: `("start"|"end", name)`. **Unchanged.**

Rev 2 replaces the bare-`int` record delta with a unified per-file progress message:

- `("progress", name, bytes_delta, records_delta)` — coalesced worker-side: a worker
  accumulates both deltas and emits one message every ~100 ms or every 50 k records,
  whichever first.

The renderer derives everything from these:

- **per-file byte bar** — `completed += bytes_delta`, denominator `os.stat(path).st_size`
  captured at `("start", name)`;
- **per-file record count** — `+= records_delta`;
- **overall records + rec/s** — summed across files;
- **overall files bar** — advances on `("end", name)`.

Progress is ephemeral (never checkpointed), and `--resume` re-runs the current binary's
workers on the remaining files, so there is no cross-version wire concern and no `--resume`
checkpoint-format change.

## 7. Edge cases

| Case | Behaviour |
|---|---|
| File completes faster than one drain tick (~100 ms) | `("start","x")` and `("end","x")` arrive together; the row is created and removed in one frame, never displayed. The overall bar advances; the file's exact counts still print on completion. |
| Zero-byte file (no `("progress", …)` ever) | Row shows 0 % / 0 records until `("end", …)`; then removed. Overall bar advances. |
| Terminal narrower than ~60 cols | `rich.progress.Progress`'s column layout collapses (drops percentage, then shortens the bar); filenames middle-ellipsis automatically. |
| `--report json` | Unaffected. The roster and live block are stderr ephemera; the JSON envelope is stdout (existing path). |
| Multiple `clean` runs in parallel worktrees | Each writes its own stderr; `rich` does not coordinate across processes. Parallel runs already require `--out-dir <worktree-local>` per CLAUDE.md; nothing new here. |

## 8. Testing

New / changed tests, in `tests/test_cli.py` unless noted:

- `test_roster_uses_stat_not_file_contents` — the roster's sizes come from `os.stat`; assert
  no file *contents* are read to build it (e.g. patch the record reader / `open` for read
  and assert it is not called during roster rendering). Locks in "no pre-scan."
- `test_roster_table_snapshot` — golden snapshot of the size-only `_render_roster` output
  for a small fixture via `rich.console.Console.capture()`.
- `test_default_jobs_caps_at_file_count` — patched `os.cpu_count() = 16`, 4 files → `4`.
- `test_default_jobs_reserves_one_core` — patched `os.cpu_count() = 8`, 100 files → `7`.
- `test_default_jobs_floor_is_one` — patched `os.cpu_count() = 1` → `1`.
- `test_explicit_jobs_not_capped_at_file_count` — `--jobs 16` on 4 files → `16`.
- `test_progress_drives_rich_tasks` — feed
  `[("start","a"), ("progress","a",100,5), ("end","a")]` to the drain loop; assert the
  per-file byte/record state and the overall tally reflect them.
- `test_overall_record_tally_sums_files` — progress messages across two files sum into the
  overall record count.
- `test_non_tty_emits_no_ansi_and_keeps_per_file_line` — `Console(stderr=True,
  force_terminal=False)` captures plain text; the per-completed-file line with exact
  clean/quarantined counts is preserved.

Golden-ANSI-frame tests are not written: with `rich` owning escape generation, they would
test the library, not us.

## 9. Considered alternatives

**Pre-scan for an up-front exact record count (rev 1's approach).** Reading every file to
count lines before cleaning starts. **Rejected:** it is a second full read of the 30 GB
corpus on a constant-memory tool (5–10 min on HDD) to learn early what the unavoidable
cleaning read reveals anyway — and `n_lines // 2` is itself only an estimate (blank/orphan
lines skew it). Exact counts ride on the cleaning pass instead, shown live and finalized at
completion; the byte-progress bar needs only `os.stat().st_size`, which is free.

**Size-plus-estimated-count roster.** A count column guessed from file size. Rejected:
shows an inexact number up front for little gain when the exact count arrives live moments
later; size-only is simpler and never misleads.

**Keep stdlib, hand-roll the multi-line block.** Buildable (cursor-up, clear-to-EOL,
shrink-on-completion, resize on each frame) at ~150–250 lines of `cli.py` plus
golden-ANSI tests. Rejected: terminal control is gotcha-prone (operational-fit territory),
the lines saved are real, and those tests would check our ANSI generation, not behaviour.

**`tqdm` instead of `rich`.** Mature and small, but does not support a *block* of N
concurrent bars whose set changes over time (`position=` assumes a fixed count); we would
re-implement the block layout on top of it, losing the reason to take the dep.

**`textual` instead of `rich`.** A full TUI framework (event loop, screens, widgets); we
want a progress block, not an app.

**`blessed` / `prompt_toolkit`.** Lower-level terminal control; better than raw ANSI for
portability but still ~50 lines of layout glue for the live multi-bar case.

**Rotating single-line display (cycle active filenames).** Cheap and on-aesthetic, but
gives no per-file progress — the user still can't tell how far through a file a worker is.

**Physical cores (not logical) for `--jobs`.** Cleaner on hyperthreaded machines, but
detecting physical vs logical cores reliably across Linux/macOS/Windows/containers is a
small library's worth of code. `cpu_count() - 1` is the simple, predictable heuristic;
revisit if `os.process_cpu_count()` comes into reach.

## 10. Out of scope

- `lintle validate` progress display (read-only command; different code path).
- `--report json` envelope or `report.jsonl` schema (post-run artifacts; the roster and
  live block are stderr ephemera).
- `--resume` checkpoint format.
- Colour / theming beyond `rich` defaults.
- Cross-process progress aggregation (parallel worktrees stay independent).
- Adopting `rich` for anything other than the live block and roster table — error
  messages, the "processing N file(s)" line, and downstream report rendering keep their
  current plain-text behaviour.
