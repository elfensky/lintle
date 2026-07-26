# Three-phase `clean` display: discovery → progress → results

**Date:** 2026-07-26
**Status:** implemented (2026-07-26) — see `cli_progress.ProgressDisplay` (phase 2) and `summary.render_files` (phase 3)

## Problem

`lintle clean` renders its file roster and its progress display as two disconnected
things. The roster is a table of index, basename, and size; the progress block below
it is a set of `rich` progress bars labelled by basename only. Nothing ties a bar back
to its roster row, and per-file results never appear on screen at all — a completed
file gets one scrolling line, and the aggregate panel at the end reports corpus totals.
There is no moment at which an operator can see how each of 29 files fared.

## Approach

Three named phases, bookended by the same 29 rows:

| Phase | Renders | Lifetime |
| --- | --- | --- |
| 1. Discovery | All files, sizes, total | Printed once, before work starts |
| 2. Progress | In-flight files only, plus a pinned summary row | Live, redrawn in place |
| 3. Results | All files with per-file outcomes, plus a total row | Printed once, after work ends |

Phase 1 answers *did lintle find everything?*. Phase 3 answers *how did each file go?*.
Phase 2 stays bounded to the files actually being worked.

### Why phase 2 is bounded

The obvious design — one live table holding all 29 rows for the whole run — was
rejected on measured evidence (rich 15.0.0). A `rich.live.Live` region cannot scroll:
rich only ever emits `ESC[1A`, so it cannot rewind past the top of the viewport.

- `vertical_overflow="visible"` is unusable. Six file completions produced 467 stranded
  duplicate rows and 605 lines of polluted scrollback.
- `"ellipsis"` and `"crop"` bound the per-frame output but crop the *bottom* — the
  totals row is exactly what disappears.
- Terminal resize is the decisive failure. Shrinking 40→20 rows mid-run with a 33-row
  live table stranded 114 lines and left 8 duplicate headers. A bounded 8-row table
  left 1 header and 0 stranded lines.

A 29-row table needs 33 lines. That is fine on a tall terminal and broken at height 24
— the POSIX default, and any tmux split. Bounding phase 2 to in-flight rows makes
correctness independent of terminal height, and moving the full-roster view to a
static phase-3 print gets the complete picture with no `Live` involved, so neither
cropping nor resize can touch it.

Render cost was measured and is not a factor either way: 9.8 ms/frame for 29 rows at
width 120, versus 3.0 ms for 7 rows. At 10 Hz that is ~10% of one core, irrelevant
beside seven I/O-bound workers.

## Design

### Phase 1 — Discovery

`cli_progress.render_roster` is unchanged, including its unconditional print off a TTY.

### Phase 2 — Progress

`cli_progress.ProgressDisplay` replaces `rich.progress.Progress` with a
`rich.live.Live` wrapping a `rich.table.Table`. Bars render inside cells via
`rich.progress_bar.ProgressBar` (verified). Columns: `#`, file, size, progress, percent,
records, MB/s, ETA. Rows are the in-flight files plus a pinned summary row carrying
files-done/total, corpus size, overall percent, total records, aggregate rate, and
elapsed.

The `#` and `size` columns are the identity link back to phase 1. Both orderings are
sorted by basename — `cli.py:106` globs sorted, `cli.py:526` sorts `all_stats` by
`src_name` — so an index means the same file in every phase.

`_ForKind` is deleted. It exists only because a single `Progress` had to serve two row
shapes (a count-based overall row and byte-based file rows) and gate byte columns off
the count row. A `Table` builds each row explicitly, so the gating has nothing to do.

`vertical_overflow` stays at its default. Bounded height means it is never reached.

The per-file completion lines keep printing above the live block. They are the durable
scrollback record of completion order, they survive a Ctrl-C at 60%, and off a TTY —
where there is no live block — they are the only output during a 20-minute run.

**No worker-protocol change.** Every phase-2 column derives from the existing
`pipeline.FileProgress` (`bytes_delta`, `records_delta`) and the pre-run `sizes` map.
Clean and quarantined counts appear only in phase 3, which reads finished `FileStats`.
`pipeline.py` is untouched.

### Phase 3 — Results

New `summary.render_files(envelope, *, console)`, called from `cli.py:571` immediately
before `summary.render`, on `term.stderr_console`. Columns: `#`, file, size, records,
clean, quarantined, repaired, time.

It lives in `summary.py` rather than `cli_progress.py` because it is envelope-driven:
`report.build_run_envelope` already emits `"files": [summary_dict(s) for s in all_stats]`,
carrying every needed column (`bytes`, `paired_records`, `clean_count`,
`quarantined_count`, `fix_counts`, `elapsed_seconds`). `repaired` is `sum(fix_counts.values())`.
Consequently `lintle report`, which re-reads `report.json` at `summary.py:202`, gains the
same per-file table for free.

Printed statically — no `Live`, so no cropping and no resize hazard — and printed
unconditionally, matching phase 1. Structured output is unaffected: this is stderr, so
`--report json` and any piped stdout stay clean.

This knowingly revisits the decision recorded at `cli.py:567` ("replacing the old
per-file stdout dump; per-file detail lives in report.md"). That decision concerned a
raw dump on *stdout*, which polluted pipes. A stderr table alongside the existing
aggregate panel does not, and `report.md` remains the durable copy.

### Responsive tiers

Both tables select columns by console width, mirroring the existing
`summary._pick_tier(is_terminal, width, unicode_ok)`:

| Tier | Width | Phase 2 | Phase 3 |
| --- | --- | --- | --- |
| wide | ≥ 100 | all columns | all columns |
| medium | 80–99 | drop MB/s, ETA | drop repaired, time |
| narrow | < 80 | also drop size | also drop size |

The tiers are cumulative: narrow drops `size` *in addition to* everything medium drops.
The boundaries partition every width — there is no gap between medium and narrow.
Columns disappear whole; values are never truncated. Column widths are pinned from
bounds known before dispatch (max index digits, max basename length, max formatted
size). Pinning is required, not cosmetic: with auto-width, measured column reflow was
observed mid-run (`#` widening 1→2 characters at row 10, count columns 2→6→8), which
re-lays out the whole table and visibly jumps the row being read.

### Edge cases

- **`--resume`.** Files carried over from a previous run appear in phase 3 from
  `plan.completed`, dim-styled. Their `time` is the earlier run's duration.
- **Failures.** A failed file gets a row with `—` in every result column, styled red.
  The existing failures table (`report.py:178`) continues to carry the error text.
- **Total time.** The phase-3 total row is the run's wall-clock elapsed, never the sum
  of the `time` column. Under parallel workers those diverge, and CLAUDE.md is explicit
  that per-file worker durations must not be summed into a corpus total.
- **Off a TTY.** Phase 1 and phase 3 print as plain text via `rich`'s own degradation.
  Phase 2's live block is suppressed as today; the per-file completion lines remain.

## Testing

- Phase 2: extend `TestProgressDisplayRendering` for table row content and the
  bounded-height invariant (rows never exceed in-flight count plus the summary row).
  Delete `TestProgressColumns`, which tests the removed `_ForKind`.
- Phase 3: new tests driving `render_files` from a fixture envelope at each tier width,
  asserting column presence/absence and no truncation, plus a resumed-file row, a
  failed-file row, and the wall-clock total.
- `render_roster` tests are untouched.

## Scope

Roughly 90 lines changed in `cli_progress.py`, ~50 new in `summary.py`, ~17 deleted,
zero in `pipeline.py`. Feature-sized and multi-file: `feature/three-phase-clean-display`
in a worktree, landing via rebase-and-merge per CONTRIBUTING.md.

## Consistency across commands

`clean` is not the only long-running command, and the three phases are meant to be the
*house vocabulary*, not a `clean` private detail. `verify`, `verify --orbit`, and `dedup`
gained their first progress display separately (`cli_progress.phase_bar`, a single-task
bar per streaming phase, plus `status` spinners over the sort and write passes). That
landed first because those commands had *no* display at all and answered Ctrl-C with a
traceback; it is deliberately phase 2 only.

The invariants both displays already share, and which any new one must keep:

- Every live region is bounded and disabled off a TTY. Only static prints survive a pipe.
- Nothing live nests: `Live` cannot nest, so a phase owns the terminal or yields it.
- Progress labels name the unit being streamed (a source file for `clean`, a cleaned
  stem for `verify`/`dedup`), so a label always maps to one row of the discovery view.
- Counters refresh sparsely. `clean` is driven by the worker progress protocol; the
  post-run phases self-throttle (every 100k records read, 10k groups written), because
  one `update` per record costs more than the checks it is reporting on.

Two gaps are left open for whoever implements this spec, to be closed in the same idiom
rather than reinvented:

- **Discovery for the post-run commands.** `verify`/`dedup` know their stems and sizes
  before they start and print nothing. They should get the same roster treatment
  (`render_roster` already takes names + sizes), printed unconditionally like phase 1.
- **Results for the post-run commands.** `verify` ends on one verdict line; its natural
  phase 3 is a per-stem table (records checked, suspects found), driven from
  `04-verify/summary.json` the way `render_files` is driven from the run envelope — so
  the read-only re-render gets it for free, exactly as `lintle report` does.

Deliberately *not* unified: byte-based bars with MB/s and ETA. `clean` streams source
bytes and can measure both; the post-run commands stream a record count whose total is
unknown until the stream ends. Forcing a fake denominator to make the two look alike
would make the ETA a lie.

## Rejected alternatives

- **All 29 rows live for the whole run.** Fails at terminal height 24 and shreds the
  transcript on resize, as measured above.
- **Rows accumulate as files finish (active + done).** Becomes the all-rows design by
  end of run — hitting the same overflow cliff precisely when the run is longest — and
  thrashes table height on every start and finish.
- **Index and size folded into the existing bar labels, no table.** About five lines,
  and it does fix the identity link, but it shows no per-file results, which is the
  substance of the request.
- **Phase 3 behind a `--per-file` flag, or TTY-only.** A flag nobody sets is a feature
  that does not exist; TTY-only would make phase 1 and phase 3 disagree about whether
  redirected output gets the roster.
