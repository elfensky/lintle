# Grouped Reject Exemplars — Design

- **Date:** 2026-05-24
- **Status:** Approved; ready for implementation planning
- **Topic:** Issue #21 — group `validate` reject exemplars by `RejectCategory` and
  emit the first N per category, so a single noisy defect class cannot hide rarer
  ones in the operator's summary view.

## 1. Problem statement

`format_reject_lines` (`src/lintle/report.py:204`) renders the per-file reject
listing for `validate` mode by slicing `stats.reject_exemplars[:100]` — the first
100 entries in **encounter order**. The exemplars themselves are accumulated in
`pipeline._record_reject` (`src/lintle/pipeline.py:262`) with an insertion-order
cap of `_EXEMPLAR_BOUND = 1000`.

On any file where one defect class dominates — and the corpus has examples that
fit this shape, e.g. millions of `checksum-mismatch` records concentrated in one
year of exports — the 1000-entry in-memory buffer fills with that one category
*before* rarer categories arrive in the stream, and the 100-entry slice the
operator sees is then 100 entries of the same defect. Rarer defect classes
(`non-ascii`, `wrong-length`, `bad-prefix`, `interior-char-missing`, etc.) become
invisible in the summary even though their counts are present in
`stats.reject_categories`.

The use case this hurts is **filing space-track defect reports**: the operator
wants ~5 concrete exemplars per rule violation as attachable evidence. Counts
alone are not sufficient evidence; raw exemplars in encounter order skew toward
the noisy class.

## 2. Goal & non-goals

**Goal:** every observed `RejectCategory` appears in the `validate` summary with
up to N (= 5) concrete exemplars, regardless of how lopsided the population is.
Total counts continue to come from `stats.reject_categories` (unchanged).

**Non-goals — explicitly excluded:**

| Excluded | Rationale |
|----------|-----------|
| Modifying the `.broken.txt` sidecar header | The 3-line ASCII preamble in `_render_header` stays as-is. The full byte-faithful catalog already lives in the sidecar body; adding a grouped summary on top is out of scope for this change. |
| Modifying `report.md` | The corpus rollup already shows the defect-category table with counts; per-file grouped exemplars belong in the per-file `validate` view, not the cross-file Markdown report. |
| A CLI flag for N | YAGNI. N = 5 is the issue's suggested value and it satisfies the defect-reporting use case. A module constant in `pipeline.py` is the single source of truth; a flag can be added in a later change if the need emerges. |
| Per-record category storage on `RejectEntry` | The category becomes the dict key on the new structure, so attaching it to the entry would be redundant. |

## 3. Constraints inherited from the authoritative spec

The corpus-cleaner design doc (`2026-05-21-tle-corpus-cleaner-design.md`) and
`CLAUDE.md`'s critical rules bind this change:

- **Constant memory (§1.3 critical rule).** The old ceiling was 1000 entries per
  file. The new ceiling is `|RejectCategory| × N = 9 × 5 = 45` entries per file
  — strictly tighter. Memory per worker decreases.
- **One validator definition (§1.4 critical rule).** Untouched; this is a
  reporting change, not a validation change.
- **Byte-faithful sidecar (spec §10).** Untouched; `BrokenFileWriter` still
  receives every reject entry and writes the full catalog. The in-memory
  sample is purely for the human-readable summary.
- **Run-summary layout (spec §9.3).** The existing
  `file.txt   N records   N clean   N quarantined   (...)` header line and the
  `fixes:` / `rejects:` count lines are unchanged. The grouped exemplar blocks
  slot in below `rejects:`.

## 4. Design

### 4.1 Data-structure change — `FileStats.reject_exemplars`

`reject_exemplars` changes from `list[RejectEntry]` to
`dict[RejectCategory, list[RejectEntry]]`. Each per-category list is capped at
N entries (see §4.2). The invariant becomes:

```
sum(len(v) for v in reject_exemplars.values()) ≤ |RejectCategory| × N
```

Total reject counts are still authoritatively held in `stats.reject_categories`
(unchanged shape: `dict[RejectCategory, int]`); the new dict only stores
sampled entries.

`RejectEntry` itself is unchanged — its three fields (`raw_lines`,
`source_lines`, `reason`) are all that the renderer needs. The category lives
on the outer dict key.

### 4.2 Pipeline change — `pipeline._record_reject`

Replace the existing constant and the insertion rule:

```python
# old
_EXEMPLAR_BOUND = 1000
…
if len(stats.reject_exemplars) < _EXEMPLAR_BOUND:
    stats.reject_exemplars.append(entry)

# new
_PER_CATEGORY_EXEMPLAR_BOUND = 5  # first-N-per-category sample (issue #21)
…
bucket = stats.reject_exemplars.setdefault(category, [])
if len(bucket) < _PER_CATEGORY_EXEMPLAR_BOUND:
    bucket.append(entry)
```

Every other action inside `_record_reject` is preserved:
`stats.quarantined_count`, `stats.reject_categories`,
`broken_writer.write_entry`, and `stats.quarantined_norad_ids` continue to be
updated identically. The full, byte-faithful catalog still streams to the
sidecar.

### 4.3 Report change — `report.format_reject_lines`

Rewritten to emit one block per category. The `limit` kwarg is removed (no
caller passes a non-default, and the per-category cap is now the only meaningful
ceiling). Categories are walked in descending order of total count from
`stats.reject_categories`, with ties broken alphabetically by category name so
output is deterministic.

```python
def format_reject_lines(stats):
    """Render grouped reject exemplars for the ``validate`` summary.

    Walks categories in descending order of total occurrences from
    ``stats.reject_categories`` and emits up to N exemplars per category
    from ``stats.reject_exemplars``, followed by a trailing
    ``...and X more`` when the bucket is shorter than the category total.
    A single noisy category cannot hide rarer defects (issue #21).
    """
    blocks = []
    for category, total in sorted(
        stats.reject_categories.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        bucket = stats.reject_exemplars.get(category, [])
        lines = [f"  {category} ({total:,}):"]
        for entry in bucket:
            if len(entry.source_lines) == 2:
                location = f"{entry.source_lines[0]}-{entry.source_lines[1]}"
            else:
                location = str(entry.source_lines[0])
            lines.append(f"    line {location}: {entry.reason}")
        remaining = total - len(bucket)
        if remaining > 0:
            lines.append(f"    ...and {remaining:,} more")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
```

### 4.4 Report change — `report.write_broken_file`

The test-only helper that emits `.broken.txt` from `stats.reject_exemplars`
must flatten the new dict. Two-level loop:

```python
with BrokenFileWriter(path, src_name) as writer:
    for entries in stats.reject_exemplars.values():
        for entry in entries:
            writer.write_entry(entry)
    writer.finalize(stats.paired_records + stats.orphan_entries)
```

The production path (`pipeline._run` → `BrokenFileWriter.write_entry` per
reject) is unaffected because it streams entries direct to disk and never reads
from `stats.reject_exemplars`.

### 4.5 CLI — no change

`cli.py:529` reads:

```python
if args.command == "validate" and stats.reject_exemplars:
    print(report.format_reject_lines(stats))
```

`bool({}) is False` exactly as `bool([]) is False`, so the truthiness guard
continues to suppress the block on files with zero rejects. No other CLI site
references `reject_exemplars`.

## 5. Display format

Spec §9.3's run-summary layout is preserved; the grouped block slots in below
`rejects:`. Before:

```
tle2022.txt   8,412,066 records   8,412,064 clean   3 quarantined   (1 orphan, 16,824,135 lines)
  fixes:   trailing-backslash 8,412,064 | reconstructed-checksum 195,293
  rejects: checksum-mismatch 1,245,678 | non-ascii 42 | orphan-line 3
  line 10-11: line 2: checksum mismatch
  line 20-21: line 2: checksum mismatch
  ... (98 more entries, all checksum-mismatch)
  ...and 1,245,578 more
```

After:

```
tle2022.txt   8,412,066 records   8,412,064 clean   3 quarantined   (1 orphan, 16,824,135 lines)
  fixes:   trailing-backslash 8,412,064 | reconstructed-checksum 195,293
  rejects: checksum-mismatch 1,245,678 | non-ascii 42 | orphan-line 3
  checksum-mismatch (1,245,678):
    line 10-11: line 2: checksum mismatch
    line 20-21: line 2: checksum mismatch
    line 30-31: line 2: checksum mismatch
    line 40-41: line 2: checksum mismatch
    line 50-51: line 2: checksum mismatch
    ...and 1,245,673 more
  non-ascii (42):
    line 100-101: line 1: non-ASCII byte at column 35
    line 200-201: line 1: non-ASCII byte at column 12
    line 300-301: line 1: non-ASCII byte at column 8
    line 400-401: line 1: non-ASCII byte at column 51
    line 500-501: line 1: non-ASCII byte at column 19
    ...and 37 more
  orphan-line (3):
    line 5000: orphan line 1 at end of file
    line 6000: orphan line 1: followed by another line 1
    line 7000: orphan line 2: no preceding line 1
```

Indent contract: category headings are 2-space indented (one nest level, aligned
with `fixes:` / `rejects:`); exemplar lines are 4-space indented (one nest
deeper). Counts are thousand-separated to match the existing summary style.

## 6. Error handling

| Edge case | Behavior |
|-----------|----------|
| No rejects in this file | `stats.reject_exemplars == {}`. `format_reject_lines` returns `""`. The cli.py truthiness guard skips printing the block — same as today's empty-list behavior. |
| Category with count ≤ N | Bucket holds all entries; no trailing `...and X more` line emitted. |
| Category with count > N | Bucket holds N entries; trailing `...and (total − N) more`. |
| `RejectCategory.INTERNAL_ERROR` rejects (the catch-all in `pipeline.py:208`) | Land in their own bucket, capped at N like every other category. They appear in the grouped output with the same shape, surfacing programmer bugs alongside data defects. |
| Category present in `reject_categories` but absent from `reject_exemplars` | Impossible by construction — every `_record_reject` call writes to both — but `bucket = stats.reject_exemplars.get(category, [])` handles it gracefully (renders the heading and a `...and N more` line). |
| Workers writing concurrently | Per-file stats are owned by a single worker; `FileStats` is not shared across the process pool. No synchronization needed. |

## 7. Testing

Test-driven: the new behavior is encoded in tests first, then made to pass.

### 7.1 Pipeline (`tests/test_pipeline.py`)

Replace the existing `TestExemplarBound` class (lines 270–309). New tests under
a renamed `TestPerCategoryExemplarBound` class:

- `test_per_category_bound_caps_each_category` — feed 100 rejects of one
  category through the pipeline path; assert that category's bucket holds
  exactly `_PER_CATEGORY_EXEMPLAR_BOUND` entries.
- `test_rare_categories_preserved_under_skew` — feed 1000 rejects of category A
  then 5 of category B; assert *both* keys exist in `stats.reject_exemplars`
  and B's bucket has all 5 entries. (This is the old design's failure mode
  written as a regression test.)
- `test_full_counts_unchanged_by_sampling` — verify `stats.quarantined_count`
  and `stats.reject_categories[cat]` reflect the full reject population, not
  the sampled subset.
- `test_internal_error_category_bucketed_like_data_defects` — programmer-error
  rejects (catch-all in `pipeline._run`) appear in their own bucket capped at N.

### 7.2 Report (`tests/test_report.py`)

Reshape `TestFormatRejectLines` (lines 139–162):

- `test_format_reject_lines_groups_by_category` — populate the dict with
  entries across 3 categories; assert output contains 3 category-heading
  blocks with correct `(count)` annotations.
- `test_format_reject_lines_sorts_by_descending_count` — categories with counts
  100 / 10 / 5 render in that order.
- `test_format_reject_lines_ties_break_alphabetically` — two categories with
  equal count render in alphabetical order, so output is deterministic.
- `test_format_reject_lines_emits_per_category_remainder` — bucket of 5 with
  category total 1000 → trailing `...and 995 more`; bucket of 3 with category
  total 3 → no trailing line.
- `test_format_reject_lines_empty_when_no_rejects` — empty dict → empty string.
- `test_format_reject_lines_indentation_contract` — category heading 2-space
  indented, exemplar lines 4-space indented, locking the display format.

Existing tests elsewhere in `test_report.py` that append directly to
`stats.reject_exemplars` (lines 51, 75, 91) are updated to set
`stats.reject_exemplars[category] = [entry]` instead of `.append(entry)`.

### 7.3 CLI (`tests/test_cli.py`)

No CLI changes; existing tests continue to pass unmodified. Add no new tests.

## 8. Build order

1. Update `tests/test_pipeline.py::TestPerCategoryExemplarBound` and
   `tests/test_report.py::TestFormatRejectLines` first (red).
2. Change `FileStats.reject_exemplars` default to a dict and update its
   docstring.
3. Change `pipeline.py`: rename constant to `_PER_CATEGORY_EXEMPLAR_BOUND`,
   rewrite the insertion rule in `_record_reject`.
4. Rewrite `report.format_reject_lines`; drop the `limit` kwarg.
5. Update `report.write_broken_file` to flatten the dict.
6. Update the other tests that touch `reject_exemplars` directly (the
   list-append paths in `test_report.py` and any incidental references).
7. Run the full verification chain: `uv run pytest`,
   `uv run ruff check .`, `uv run ruff format --check .`.

## 9. Out-of-scope follow-ups

Recorded here so they are not forgotten and not silently smuggled into this
change:

- Adding a `.broken.txt` grouped-summary preamble (would require expanding
  `_render_header` and the writer's header pipeline).
- Adding a `report.md` "Exemplars by category" section.
- A CLI `--exemplars-per-category` flag.
- Surfacing per-category sample size in `summary_dict` for the JSON report.
