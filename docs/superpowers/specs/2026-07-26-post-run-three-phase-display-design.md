# The post-run commands join the three-phase display

**Date:** 2026-07-26
**Status:** implemented (2026-07-26)

## Problem

`clean` renders as discovery → progress → results (see the archived
`2026-07-26-three-phase-clean-display-design.md`). The post-run commands do not. A TUI
audit of every command found:

| command | discovery | progress | results | closing |
| --- | --- | --- | --- | --- |
| `clean` | roster table | live table + summary row | per-file table + total | aggregate panel |
| `verify` | — | one-line bar | — | one PASS/FAIL line |
| `dedup` | — | one-line bar ×2 | — | one PASS/FAIL line |
| `extract` | — | — | — | one `wrote <path>` per id |
| `report` | n/a | n/a | phase-3 table | aggregate panel |
| `diff` | n/a | n/a | indented plain text | summary line |

Three consequences, in descending order of how much they cost an operator:

1. **No discovery.** `verify` and `dedup` know their stems and sizes before they start
   and print nothing, so the first thing an operator learns about a mis-pointed
   `--out-dir` is a "no cleaned output" error minutes later — or worse, a run over the
   wrong tree that succeeds.
2. **No results.** `verify` collapses a whole `04-verify/summary.json` into
   `0 hard, 0 soft suspect(s) across 200000 records`. Which of the 29 files carried the
   suspects is on disk but never on screen. Same for `dedup` and `extract`.
3. **Two idioms for the same job.** A table row per unit in `clean`, a one-line bar in
   the post-run commands, with the record count in its own column in one and glued into
   the label in the other.

## Approach

The same three phases, the same chrome, keyed to each command's own unit of work:

| command | phase-1 unit | phase-2 unit | phase-3 row |
| --- | --- | --- | --- |
| `verify` | cleaned stem | cleaned stem | stem: records, hard, soft |
| `dedup` | cleaned stem | cleaned stem | stem: records read, excluded |
| `extract` | requested NORAD id | (none — see below) | id: records, span, gaps, status |

Phase 2 is already implemented for `verify`/`dedup` (`cli_progress.phase_bar`) and stays
a bar, not a table: those commands are single-process, so there is never more than one
unit in flight, and a table of one row is a table for its own sake.

`extract` gets no phase 2. Each id is a binary search over a fixed-width chunk set —
milliseconds — and it prompts y/n mid-extraction for a gappy history, which a live block
would fight. Its per-id `wrote`/`skipped` notes stay as the completion record, exactly as
`clean`'s per-file completion lines do.

## Design

### Shared chrome

`summary.results_table(*headers)` builds every phase-3 table: `box.SIMPLE`, no edge
padding, a dim right-justified `#` column, a left-justified name column, and every
remaining column right-justified. The convention is positional — `headers[0]` is the
index, `headers[1]` is the name, the rest are numbers — which is the shape all four
tables already have. `summary.render_files` is refactored onto it, so `clean`'s table and
the post-run tables cannot drift apart.

Width tiering continues through the one `summary.display_tier` (narrow < 80 ≤ medium <
100 ≤ wide). Each table names which columns each tier drops; columns disappear whole.

### Discovery

`cli_progress.render_roster` already takes an ordered `name -> size` map and prints
unconditionally, degrading to plain text off a TTY. `verify` and `dedup` get their map
from `verify.records.cleaned_fingerprint(out_dir)` — a stat-only structural fingerprint
they already compute for staleness checks, so discovery costs no extra I/O and cannot
disagree with what phase 2 then streams.

`extract`'s roster lists the requested ids, and only when more than one was asked for: a
one-row roster above a one-row result table is noise, not orientation.

### Results

**`verify`** — `#`, stem, size, records, hard, soft; total row. Per-stem record counts
come from the phase-2 loop, which already counts them. Per-stem suspect counts come from
two new `collections.Counter`s on `SuspectSink`, keyed by `Suspect.src_file` (the stem):
every suspect already carries its origin stem, including the contradiction and orbit
findings raised after the streaming pass, so no finding is unattributed and the column
sums equal the verdict line. Medium drops `size`; narrow also drops `records`.

**`dedup`** — `#`, stem, size, records, excluded; total row. `excluded` is the
hard-suspect exclusions from a prior `verify` run, currently only reported as a corpus
total. The verdict line keeps the group-level numbers (written, collapsed, dropped,
conflicts) because those are properties of the *sorted stream*, not of any one stem.

**`extract`** — `#`, id, records, span (years), gaps, status; total row over the written
ids. Every column already exists in the `<id>.json` sidecar this command writes, so the
table is a render of committed data, not a second computation.

**`diff`** — on a TTY, the rule deltas and the per-file deltas render as tables through
the same chrome. Off a TTY the existing pure `format_text` / `format_file_deltas` strings
are printed byte-for-byte unchanged: `diff` output is meant to be piped and grepped, and
those renderers have byte-exact tests worth keeping. This is the one command where the
two paths genuinely differ, and the plain path is the contract.

### Deliberately not unified

- **Byte bars, MB/s, ETA** stay `clean`-only. It streams source bytes and knows the
  total; the post-run commands stream a record count whose total is unknown until the
  stream ends. A fake denominator would make the ETA a lie.
- **`explain`** stays prose. It is documentation, not tabular data.
- **`diff`'s piped output** stays plain text, as above.

## Testing

- `results_table` chrome: index column dim and right-justified, name column left, the
  rest right.
- Discovery: `verify`/`dedup` print a roster listing every stem with its size, off a TTY
  too; `extract` prints one only for 2+ ids.
- Results: a per-stem row for each command at each tier; the total row; suspects
  attributed to the right stem, including a contradiction raised after the streaming
  pass; and the column sums matching the verdict line.
- `diff`: a TTY render contains the table chrome; the non-TTY render is byte-identical to
  today's `format_text` output (the existing tests stand unchanged).

## Scope

`summary.py` (+ the shared table), `verify/__init__.py`, `verify/report.py`,
`dedup.py`, `extract.py`, `diff.py`, and their tests. Feature-sized and multi-file:
`feature/postrun-three-phase` in a worktree, landing via rebase-and-merge.
