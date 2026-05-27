# Grouped Reject Exemplars — Design

- **Date:** 2026-05-24
- **Status:** Approved; ready for implementation planning
- **Revision:** **2026-05-24:** §4.2 / §4.4 / §7 — applied findings from a multi-AI
  adversarial review (Codex + Gemini + Perplexity). §4.2 swaps `setdefault` for a
  get-or-create pattern (avoids per-reject empty-list allocation in the hot path).
  §4.4 sorts the flattened entries by `source_lines[0]` so the test helper matches
  production encounter order. §7.1 reshapes the test list to preserve the existing
  `TestStreamingRejects` invariant (memory caps + complete on-disk catalog).
  §7.2 adds multi-category `write_broken_file` coverage and corrects the test-update
  pattern. §7.3 adds a CLI shape-lock test.
  **2026-05-24 (post-diagnostics):** API surface updated to match the stable
  rule-ID registry that landed in 0.3.0 ([`2026-05-24-stable-rule-id-registry-design.md`](2026-05-24-stable-rule-id-registry-design.md)).
  `RuleID` → `lintle.diagnostics.RuleID`; `stats.reject_counts` →
  `stats.reject_counts`; example labels switch from free-form tags
  (`"checksum-mismatch"`, etc.) to stable wire tokens (`"TLE-CHK-001"`, etc.).
  The design is otherwise unchanged — per-rule bucketing composes cleanly with
  `RuleID` since it is also a `StrEnum`. `RejectEntry` now carries
  `primary: Diagnostic` + `related: tuple[Diagnostic, ...]` instead of
  `reason: str`; the bucket key is `primary.rule_id` rather than a separate
  `category` parameter.
- **Topic:** Issue #21 — group `validate` reject exemplars by `RuleID` and
  emit the first N per rule, so a single noisy rule cannot hide rarer ones in
  the operator's summary view.

## 1. Problem statement

`format_reject_lines` (`src/lintle/report.py:204`) renders the per-file reject
listing for `validate` mode by slicing `stats.reject_exemplars[:100]` — the first
100 entries in **encounter order**. The exemplars themselves are accumulated in
`pipeline._record_reject` (`src/lintle/pipeline.py:262`) with an insertion-order
cap of `_EXEMPLAR_BOUND = 1000`.

On any file where one defect class dominates — and the corpus has examples that
fit this shape, e.g. millions of `TLE-CHK-001` (checksum mismatch) records
concentrated in one year of exports — the 1000-entry in-memory buffer fills
with that one rule *before* rarer rules arrive in the stream, and the
100-entry slice the operator sees is then 100 entries of the same defect.
Rarer rules (`TLE-COL-003` non-ASCII byte, `TLE-COL-001` line-length,
`TLE-PAIR-002` bad prefix, `TLE-COL-002` interior char missing, etc.) become
invisible in the summary even though their counts are present in
`stats.reject_counts`.

The use case this hurts is **filing space-track defect reports**: the operator
wants ~5 concrete exemplars per rule violation as attachable evidence. Counts
alone are not sufficient evidence; raw exemplars in encounter order skew toward
the noisy class.

## 2. Goal & non-goals

**Goal:** every observed `RuleID` appears in the `validate` summary with
up to N (= 5) concrete exemplars, regardless of how lopsided the population is.
Total counts continue to come from `stats.reject_counts` (unchanged).

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
  file. The new ceiling is `|RuleID| × N = 9 × 5 = 45` entries per file
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
`dict[RuleID, list[RejectEntry]]`. Each per-category list is capped at
N entries (see §4.2). The invariant becomes:

```
sum(len(v) for v in reject_exemplars.values()) ≤ |RuleID| × N
```

Total reject counts are still authoritatively held in `stats.reject_counts`
(unchanged shape: `dict[RuleID, int]`); the new dict only stores
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
_PER_RULE_EXEMPLAR_BOUND = 5  # first-N-per-rule sample (issue #21)
…
rule_id = primary.rule_id
bucket = stats.reject_exemplars.get(rule_id)
if bucket is None:
    bucket = []
    stats.reject_exemplars[rule_id] = bucket
if len(bucket) < _PER_RULE_EXEMPLAR_BOUND:
    bucket.append(entry)
```

`primary` is the `Diagnostic` argument introduced by the diagnostics refactor
(0.3.0); `primary.rule_id` is the stable `RuleID` enum member and serves as
the bucket key. The get-or-create form is deliberate: `setdefault(rule_id, [])`
would evaluate `[]` on every call (CPython evaluates positional arguments
before checking key membership), allocating an empty list per reject that is
discarded once the second-and-later call sees the existing key. On a
pathologically reject-heavy file that allocation thrash adds up; the
get-or-create form allocates only on the first occurrence of a rule.

Every other action inside `_record_reject` is preserved:
`stats.quarantined_count`, `stats.reject_counts`,
`broken_writer.write_entry`, and `stats.quarantined_norad_ids` continue to be
updated identically. The full, byte-faithful catalog still streams to the
sidecar.

### 4.3 Report change — `report.format_reject_lines`

Rewritten to emit one block per category. The `limit` kwarg is removed (no
caller passes a non-default, and the per-category cap is now the only meaningful
ceiling). Categories are walked in descending order of total count from
`stats.reject_counts`, with ties broken alphabetically by category name so
output is deterministic.

```python
def format_reject_lines(stats):
    """Render grouped reject exemplars for the ``validate`` summary.

    Walks categories in descending order of total occurrences from
    ``stats.reject_counts`` and emits up to N exemplars per category
    from ``stats.reject_exemplars``, followed by a trailing
    ``...and X more`` when the bucket is shorter than the category total.
    A single noisy category cannot hide rarer defects (issue #21).
    """
    blocks = []
    for rule_id, total in sorted(
        stats.reject_counts.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        bucket = stats.reject_exemplars.get(rule_id, [])
        lines = [f"  {rule_id} ({total:,}):"]
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
must flatten the new dict. Two-level flatten, sorted by `source_lines[0]`
so the helper's output mirrors true encounter order — matching what
production's streaming `BrokenFileWriter` produces and keeping multi-category
test fixtures deterministic across dict-insertion-order changes:

```python
with BrokenFileWriter(path, src_name) as writer:
    flattened = [
        entry
        for bucket in stats.reject_exemplars.values()
        for entry in bucket
    ]
    flattened.sort(key=lambda e: e.source_lines[0])
    for entry in flattened:
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
  rejects: TLE-CHK-001 1,245,678 | TLE-COL-003 42 | TLE-PAIR-001 3
  line 10-11: rule: TLE-CHK-001 col 69 observed='3' expected='7'
  line 20-21: rule: TLE-CHK-001 col 69 observed='1' expected='5'
  ... (98 more entries, all TLE-CHK-001)
  ...and 1,245,578 more
```

After:

```
tle2022.txt   8,412,066 records   8,412,064 clean   3 quarantined   (1 orphan, 16,824,135 lines)
  fixes:   trailing-backslash 8,412,064 | reconstructed-checksum 195,293
  rejects: TLE-CHK-001 1,245,678 | TLE-COL-003 42 | TLE-PAIR-001 3
  TLE-CHK-001 (1,245,678):
    line 10-11: rule: TLE-CHK-001 col 69 observed='3' expected='7'
    line 20-21: rule: TLE-CHK-001 col 69 observed='1' expected='5'
    line 30-31: rule: TLE-CHK-001 col 69 observed='9' expected='2'
    line 40-41: rule: TLE-CHK-001 col 69 observed='4' expected='8'
    line 50-51: rule: TLE-CHK-001 col 69 observed='0' expected='6'
    ...and 1,245,673 more
  TLE-COL-003 (42):
    line 100-101: rule: TLE-COL-003 col 35 - non-ASCII byte
    line 200-201: rule: TLE-COL-003 col 12 - non-ASCII byte
    line 300-301: rule: TLE-COL-003 col 8 - non-ASCII byte
    line 400-401: rule: TLE-COL-003 col 51 - non-ASCII byte
    line 500-501: rule: TLE-COL-003 col 19 - non-ASCII byte
    ...and 37 more
  TLE-PAIR-001 (3):
    line 5000: rule: TLE-PAIR-001 - orphan line 1 at end of file
    line 6000: rule: TLE-PAIR-001 - orphan line 1: followed by another line 1
    line 7000: rule: TLE-PAIR-001 - orphan line 2: no preceding line 1
```

Indent contract: rule headings are 2-space indented (one nest level, aligned
with `fixes:` / `rejects:`); exemplar lines are 4-space indented (one nest
deeper). Counts are thousand-separated to match the existing summary style.
Exemplar bodies render the structured `Diagnostic` (`rule:` + optional
`col`/`cols` + optional `observed=`/`expected=` + optional free-text note),
matching the `.broken.txt` headline format from spec §9.2.

## 6. Error handling

| Edge case | Behavior |
|-----------|----------|
| No rejects in this file | `stats.reject_exemplars == {}`. `format_reject_lines` returns `""`. The cli.py truthiness guard skips printing the block — same as today's empty-list behavior. |
| Category with count ≤ N | Bucket holds all entries; no trailing `...and X more` line emitted. |
| Category with count > N | Bucket holds N entries; trailing `...and (total − N) more`. |
| `RuleID.INTERNAL_ERROR` rejects (the catch-all in `pipeline.py:208`) | Land in their own bucket, capped at N like every other category. They appear in the grouped output with the same shape, surfacing programmer bugs alongside data defects. |
| Rule present in `reject_counts` but absent from `reject_exemplars` | Impossible by construction — every `_record_reject` call writes to both — but `bucket = stats.reject_exemplars.get(primary.rule_id, [])` handles it gracefully (renders the heading and a `...and N more` line). |
| Workers writing concurrently | Per-file stats are owned by a single worker; `FileStats` is not shared across the process pool. No synchronization needed. |

## 7. Testing

Test-driven: the new behavior is encoded in tests first, then made to pass.

### 7.1 Pipeline (`tests/test_pipeline.py`)

Update `TestStreamingRejects` (lines 269–309). The class's load-bearing
invariant — *in-memory exemplars stay bounded while the on-disk `.broken.txt`
catalog is complete* — is preserved exactly. Tests are re-encoded for the
per-category bucket and gain category-specific cases:

- `test_exemplars_bucketed_per_category_with_complete_broken_catalog` —
  the evolved form of the existing
  `test_exemplars_bounded_but_broken_catalog_is_complete`. Push
  N >> `_PER_RULE_EXEMPLAR_BOUND` rejects of one category through
  `process_file` in `"clean"` mode; assert (a) the per-category bucket holds
  exactly `_PER_RULE_EXEMPLAR_BOUND` entries, (b) `stats.quarantined_count == N`
  and `stats.reject_counts[cat] == N`, (c) the sidecar header reads
  `f"# {N} quarantined of {N} entries"`, (d) the bytes of the Nth (last)
  reject are present in the sidecar. **This test is the regression guard for
  the "memory caps but disk catalog stays complete" invariant — losing it
  would let an implementation correctly bucket exemplars while silently
  regressing the streaming-to-disk semantics.**
- `test_validate_mode_bucket_caps_per_category` — the evolved form of
  `test_validate_mode_bounds_memory_too`. In `"validate"` mode, feed
  N > `_PER_RULE_EXEMPLAR_BOUND` rejects of one category; assert the
  bucket caps at `_PER_RULE_EXEMPLAR_BOUND` and `stats.quarantined_count == N`.
- `test_rare_categories_preserved_under_skew` — feed 1000 rejects of category
  A followed by 5 of category B; assert *both* keys exist in
  `stats.reject_exemplars` and B's bucket has all 5 entries. (The old
  design's failure mode written as a regression test.)
- `test_internal_error_category_bucketed_like_data_defects` —
  programmer-error rejects (catch-all in `pipeline._run`) appear in their own
  `RuleID.INTERNAL_ERROR` bucket capped at N like every data defect.

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

Add to the `write_broken_file` test class (around lines 47–102) — the current
single-entry single-category case is degenerate under the new dict, so the
flattening logic in §4.4 is otherwise unexercised:

- `test_write_broken_file_flattens_multiple_categories` — populate three
  categories with two entries each (interleaved source lines, e.g. catA at
  10 and 40, catB at 20 and 50, catC at 30 and 60); assert all six entries
  appear in the rendered sidecar bytes with `[index]` prefixes 1–6. Guards
  against flattening bugs (skipped buckets, dict-key iteration, nested-list
  mishandling).
- `test_write_broken_file_orders_by_source_line` — locks the §4.4 sort.
  Given the above interleaved fixture, assert the sidecar's six entries
  appear in source-line order 10, 20, 30, 40, 50, 60 — not dict-insertion
  order, not category order.

Existing tests in `test_report.py` that populate `stats.reject_exemplars`
directly (the setup helpers at lines 47–102 and the format tests at lines
139–162) are updated to append via the pattern
`stats.reject_exemplars.setdefault(category, []).append(entry)`, which
preserves multi-entry semantics within a category and matches how the
production pipeline writes the dict.

### 7.3 CLI (`tests/test_cli.py`)

The existing `test_main_validate_lists_reject_locations` (lines 387–399)
asserts `"checksum" in stdout` — a substring that survives every plausible
format change (both old and new format contain it). It stays as a smoke test
but does not lock the new shape. To lock the shape:

- `test_main_validate_renders_grouped_exemplars` — drive a file with at least
  two distinct rules through `cli.main(["validate", ...])`; assert the
  captured stdout contains (a) the rule heading literal
  `"  TLE-CHK-001 ("` (two-space indent + rule ID + space + open paren),
  (b) a 4-space-indented exemplar line `"    line "`. Locks the grouped
  display contract at the CLI boundary so a shape regression cannot slip
  past unit tests.

## 8. Build order

1. Update `tests/test_pipeline.py::TestStreamingRejects` and
   `tests/test_report.py::TestFormatRejectLines` first (red); add the new
   `TestWriteBrokenFile` cases and the CLI shape-lock test described in §7.
2. Change `FileStats.reject_exemplars` default to a dict and update its
   docstring.
3. Change `pipeline.py`: rename constant to `_PER_RULE_EXEMPLAR_BOUND`,
   rewrite the insertion rule in `_record_reject` with the get-or-create
   pattern from §4.2.
4. Rewrite `report.format_reject_lines`; drop the `limit` kwarg.
5. Update `report.write_broken_file` to flatten + sort by `source_lines[0]`
   per §4.4.
6. Update the remaining tests in `test_report.py` that populate
   `reject_exemplars` directly to use the
   `setdefault(category, []).append(entry)` pattern.
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
