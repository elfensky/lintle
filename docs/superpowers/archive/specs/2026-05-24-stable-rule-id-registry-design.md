# Stable Rule-ID Registry & Structured Diagnostics — Design

- **Date:** 2026-05-24
- **Status:** Approved; not yet implemented
- **Topic:** Replace free-form rejection prose with stable, citable rule identifiers
  (`TLE-COL-001`, `TLE-CHK-001`, …) carried on a structured `Diagnostic` dataclass.
  Implements [issue #8](https://github.com/elfensky/lintle/issues/8).
- **Supersedes for the rejection model:** §6 / §9 of
  [`2026-05-21-tle-corpus-cleaner-design.md`](2026-05-21-tle-corpus-cleaner-design.md)
  — the `RejectCategory` enum and free-form `reason: str` are replaced by a `RuleID` +
  `Diagnostic` model. All other parts of the master design (validator definition, repair
  tiers, streaming/constant-memory, pairing) remain authoritative.

## 1. Goal

Make the *identity* of each rejection class stable, citable, and machine-readable,
while leaving the prose around it free to evolve. Today the cleaner emits free-form
strings like `"wrong-length"` or `"line 1: checksum mismatch"`; tomorrow it emits
`TLE-COL-001` and `TLE-CHK-001` with structured evidence (column range, observed,
expected) attached.

This unlocks (separately) `lintle explain TLE-CHK-001`, `lintle diff run-a run-b`,
JSON output, and stable test assertions independent of message wording.

## 2. Non-goals

- **No new validation logic.** All "what is wrong with this line" decisions remain in
  `tle.py`. This change is purely about how rejections are *labelled, structured, and
  reported* — never about what counts as a rejection.
- **No CLI surface change in this PR.** `lintle explain` and `lintle diff` are separate
  follow-up issues.
- **No JSON output mode.** Designed for, not delivered by, this PR.
- **No CCSDS/Space-Track schema alignment.** lintle defines its own taxonomy.
- **No code-generated enum from a TOML registry.** The enum *is* the registry's
  Python surface; overkill at ~15 rules.

## 3. Critical-rule compliance

Read against the four Critical Rules in `CLAUDE.md`:

| Rule | Compliance |
|---|---|
| Validated transformation | Unchanged. Repair → re-validate → commit-or-quarantine flow is untouched. |
| Correctness over recovery | Unchanged. No new repairs, no new reconstruction. |
| Constant memory | Preserved by Decision §4.5 (tuples, no rich payload retention, ephemeral `Rejected`). |
| One validator definition | Honored. `diagnostics.py` is pure data (StrEnum + frozen dataclasses + a `RULES` dict); no predicates live there. `tle.py` remains the single source of "perfect". |

## 4. Architectural decisions

### 4.1 New module: `src/lintle/diagnostics.py` (pure data)

Sits in the dependency graph **beside** `tle.py`, depended on by `repair`, `pipeline`,
and `report`. Zero logic, zero runtime imports beyond `enum` and `dataclasses`.

```
cli → pipeline → repair → tle
              ↘     ↘       ↗
                diagnostics
                     ↑
                  report
```

### 4.2 `RuleID` — stable wire token

```python
class RuleID(enum.StrEnum):
    # TLE-COL-* — column / layout (physical line shape)
    LINE_LENGTH            = "TLE-COL-001"   # length ≠ 69 after normalization
    INTERIOR_CHAR_MISSING  = "TLE-COL-002"   # 68-char line where columns 1-68 fail layout
    NON_ASCII_BYTE         = "TLE-COL-003"   # non-ASCII byte in the line
    INVALID_COLUMN_LAYOUT  = "TLE-COL-004"   # other column / format failure

    # TLE-CHK-* — checksum (mod-10 digit at column 69)
    CHECKSUM_MISMATCH      = "TLE-CHK-001"

    # TLE-PAIR-* — line-1/line-2 pairing
    ORPHAN_LINE            = "TLE-PAIR-001"  # line 1 with no line 2 (or v.v.)
    BAD_PREFIX             = "TLE-PAIR-002"  # line does not start with "1 " or "2 "
    CATALOG_MISMATCH       = "TLE-PAIR-003"  # paired lines disagree on NORAD ID

    # TLE-SEM-* — semantic ranges (RESERVED; no rule emitted yet)
    # First semantic rule, when added, takes TLE-SEM-001.

    # TLE-INT-* — internal (cleaner failed on this record)
    INTERNAL_ERROR         = "TLE-INT-001"
```

**Naming discipline (irreversible — read carefully):**

- IDs are **never reused, never recycled**. A retired ID stays in the enum, annotated
  with `# DEPRECATED → TLE-COL-NNN` in source.
- Member NAME is a Pythonic internal alias (e.g. `LINE_LENGTH`); the **string value
  is the public contract**. Renaming the member is safe; changing the value is a
  breaking format change.
- Family prefixes are semantic (`COL`, `CHK`, `PAIR`, `SEM`, `INT`) — no generic
  `CHK` ambiguity. New families require a spec amendment.
- Numeric suffix is 3-digit zero-padded (`001`–`999`); a family overflowing 999 gets
  a sub-family (`TLE-COL2-001`) rather than wrapping. Unlikely.

### 4.3 `RuleSpec` — out-of-band metadata registry

Metadata about each rule lives in a separate dataclass keyed by `RuleID`. **Not on
the enum members.** Reason: deprecation/aliasing/docs evolution is awkward when
metadata is fused to enum membership, and downstream code shouldn't import an enum
just to ask policy questions.

```python
@dataclasses.dataclass(frozen=True, slots=True)
class RuleSpec:
    rule_id: RuleID
    family: str               # "COL", "CHK", "PAIR", "SEM", "INT"
    short_title: str          # one-line human title
    introduced: str           # lintle version that first emitted it
    deprecated_for: tuple[RuleID, ...] = ()  # rules that supersede this one

RULES: dict[RuleID, RuleSpec] = {
    RuleID.LINE_LENGTH: RuleSpec(
        RuleID.LINE_LENGTH, "COL",
        "line length after normalization is not 69 columns", "0.3.0"
    ),
    # ... one entry per RuleID member
}
```

`RULES` is initialised at import time; the `__post_init__` of the module asserts
`set(RULES) == set(RuleID)` so a missing or extra spec fails fast in tests.

### 4.4 `RepairTier` — reporting which tier was attempted

```python
class RepairTier(enum.StrEnum):
    NONE                 = "none"     # rejected without repair (orphan, bad-prefix, ...)
    NORMALIZATION        = "tier-1"   # CRLF / whitespace / trailing backslash
    CHECKSUM_RECONSTRUCT = "tier-2"   # missing-checksum reconstruction
```

A `Diagnostic` records *which tier was attempted before this diagnostic fired*.
This is what lets a reader distinguish a tier-2 checksum-reconstruct that still
failed (strong corruption signal) from a record we rejected at first read.

### 4.5 `Diagnostic` — the structured rejection unit

```python
@dataclasses.dataclass(frozen=True, slots=True)
class Diagnostic:
    rule_id: RuleID
    source_line_nos: tuple[int, ...]              # 1-indexed source lines
    tier_attempted: RepairTier = RepairTier.NONE
    column_range: tuple[int, int] | None = None   # 1-indexed inclusive, or None
    observed: str | None = None                   # ≤ 16 chars; truncated if longer
    expected: str | None = None                   # ≤ 16 chars; truncated if longer
    note: str = ""                                # ≤ 80 chars; human aside only
```

**Memory discipline (load-bearing):**

- `frozen=True` makes Diagnostics hashable → groupable, set-storable, dict-keyable.
- `slots=True` drops the per-instance `__dict__` (~40% memory cut). The codebase's
  other dataclasses (`Accepted`, `Rejected`) don't use slots; `Diagnostic` does
  because it's the high-cardinality one.
- `source_line_nos` and `column_range` are tuples (immutable, hashable).
- `observed` / `expected` / `note` are **bounded at construction** to the limits
  above. Construction helper (see §4.7) enforces the cap. Unbounded prose at scale
  was the single biggest memory risk flagged in the brainstorm.

### 4.6 `Rejected` — `primary + related`, not free-form list

```python
@dataclasses.dataclass
class Rejected:
    raw_lines: list
    source_lines: list
    primary: Diagnostic                       # the headline diagnosis
    related: tuple[Diagnostic, ...] = ()      # supporting/secondary diagnostics
```

**Why not `list[Diagnostic]`?** The brainstorm flagged this as the make-or-break
call. With a free list, every downstream consumer (`_record_reject` aggregation,
`.broken.txt` headline line, `report.md` counter) invents its own "first-finding
wins" rule. Forcing the architecture to designate a `primary` makes the aggregation
key unambiguous and matches rustc's primary-diagnostic + secondary-notes model.

A record with one defect → `primary=Diagnostic(...)`, `related=()`.
A record where line 1 has a checksum mismatch AND line 2 has wrong length →
`primary` is whichever was hit first in `repair.process_record`, `related` carries
the other. Aggregation always counts `primary.rule_id`.

### 4.7 Construction helper

```python
def diagnostic(
    rule_id: RuleID,
    *,
    source_line_nos: tuple[int, ...],
    tier_attempted: RepairTier = RepairTier.NONE,
    column_range: tuple[int, int] | None = None,
    observed: str | None = None,
    expected: str | None = None,
    note: str = "",
) -> Diagnostic:
    """Construct a Diagnostic with size-bounded strings.

    Truncates `observed`/`expected` to 16 chars and `note` to 80 chars at construction
    so memory and on-disk size stay bounded regardless of input corruption.
    """
```

Truncation is silent and one-way — the goal is bound, not loss-of-information
warning. Truncated values get a trailing `…` (U+2026) if cut. Tests assert the bound.

### 4.8 `FixClass` and `Accepted` are unchanged

The asymmetry — `Diagnostic` on rejected, `FixClass` on accepted — is intentional.
`FixClass` is the diagnostic-equivalent for repairs that *succeeded*; pairing
`Diagnostic` onto `Accepted` too would create two parallel taxonomies for the same
conceptual space. The Defect-Remedy duality is already expressed by the existing
types. Don't refactor `Accepted`.

## 5. Module-level changes

### 5.1 `src/lintle/diagnostics.py` — new

Holds `RuleID`, `RepairTier`, `RuleSpec`, `RULES`, `Diagnostic`, `diagnostic(...)`.
~120 lines, pure data + the construction helper.

### 5.2 `src/lintle/categories.py` — slimmed

- `FixClass` stays as-is.
- `RejectCategory` **removed** in this same PR. A `# Removed in 0.3.0 — use
  diagnostics.RuleID` comment in the docstring history is sufficient. No call sites
  remain after the PR.

The compressed migration (no transition release) is safe because `RejectCategory` is
internal-only — the only externalised surface is the `.broken.txt` prose, which is
being intentionally rewritten in this same PR (§6).

### 5.3 `src/lintle/repair.py` — restructured

- `repair_line(raw, lineno)` now returns
  `(clean_line, fixes, diagnostic_or_None)` — a single optional `Diagnostic`
  instead of `(error, category)`. The construction sites that previously built
  `(error_string, RejectCategory.X)` now call `diagnostic(RuleID.X, ...)` with the
  structured fields.
- `Rejected` gains `primary: Diagnostic, related: tuple[Diagnostic, ...] = ()`
  and loses `category` and `reason`.
- `process_record(...)` composes diagnostics from both lines: if both line-1 and
  line-2 fail, line-1's diagnostic is `primary` and line-2's is in `related`.

### 5.4 `src/lintle/pipeline.py` — aggregation key change

- `Orphan` dataclass gains `diagnostic: Diagnostic` (replaces `category` + `reason`).
- `_record_reject(stats, broken_writer, primary, related, raw_lines, source_lines)`
  — signature changes. `stats.reject_categories` is **renamed** to
  `stats.reject_counts: dict[str, int]` keyed by `primary.rule_id` (the
  StrEnum value, so the dict key is `"TLE-CHK-001"`).
- Existing internal-error path: `RuleID.INTERNAL_ERROR` with the exception repr
  truncated to 80 chars in `note`.

### 5.5 `src/lintle/report.py` — output rewrite

- `RejectEntry` gains `primary: Diagnostic, related: tuple[Diagnostic, ...]`.
- `FileStats.reject_categories` → `FileStats.reject_counts` (same dict; rename
  honours the new vocabulary). The exemplar buffer continues to hold full
  `RejectEntry` objects, bounded by `_EXEMPLAR_BOUND = 1000`.
- `_render_entry` rewrites the `.broken.txt` body line — see §6.
- `format_summary` / `format_run_report` / `summary_dict` all cite rule IDs.

## 6. `.broken.txt` format change (published byte format)

**This is the only externally-observable contract change.** Acknowledged and shipped
under a minor version bump (`0.2.0 → 0.3.0`) with a CHANGELOG entry.

**Old line format:**
```
[N] source lines X-Y - reason: <free-form prose>
```

**New line format:**
```
[N] source lines X-Y - rule: TLE-CHK-001 (tier-1) - col 69 observed='7' expected='3'
[N] source lines X-Y - rule: TLE-PAIR-001 - <note text>
```

Format rules:
- `rule: <id>` always present.
- `(<tier>)` only when `tier_attempted ≠ NONE`.
- `col N` for `column_range=(N, N)`, `cols N-M` for `(N, M)`, omitted if `None`.
- `observed=...` / `expected=...` quoted with single quotes, ASCII-escaped, only
  when set.
- `<note>` (no key) appended at end if non-empty.
- `related` diagnostics, if any, render on indented continuation lines:
  ```
  [N] source lines X-Y - rule: TLE-CHK-001 - col 69 observed='7' expected='3'
      and: rule: TLE-COL-001 - line length 68 expected 69
  ```

Header is unchanged (filename / timestamp / version / count); the version field
already pins the byte format to a lintle release.

## 7. `report.md` changes

- The per-file `rejects:` line and the corpus totals shift from category names
  (`wrong-length 412`) to rule IDs (`TLE-COL-001 412`).
- A new section `## Rule reference` lists every rule ID that fired in this run with
  its `RuleSpec.short_title` — auto-generated from `RULES`, no hand-maintained doc.

## 8. Test strategy

Add `tests/test_diagnostics.py`:
- Construction helper bounds `observed`/`expected`/`note` and appends `…` when cut.
- `Diagnostic` is hashable + slots-bearing.
- `RULES` is complete (`set(RULES) == set(RuleID)`).
- `RULES` has no two entries with the same `rule_id`.
- Family prefix consistency: `RULES[X].family` matches the middle token of
  `X.value`.

Update existing tests:
- `tests/test_repair.py` — every assertion on `Rejected.category` /
  `Rejected.reason` becomes an assertion on `Rejected.primary.rule_id` (and where
  meaningful, `primary.column_range` / `primary.observed` / `primary.expected`).
- `tests/test_pipeline.py` — same; aggregation tests assert `stats.reject_counts`
  by rule-ID string.
- `tests/test_report.py` — `.broken.txt` golden tests are rewritten against the
  new format. Add tests that `related` renders on indented continuation lines.

The test count increases by ~12, no existing test is deleted.

## 9. Build sequence (test-first per CLAUDE.md §Working Style)

Sequential commits inside the feature branch:

1. `feat(diagnostics): introduce RuleID, RepairTier, RuleSpec, Diagnostic` —
   new file `src/lintle/diagnostics.py` + `tests/test_diagnostics.py`. No other
   module touched. Tests pass.
2. `refactor(repair): emit Diagnostic from repair_line, replace category+reason on
   Rejected` — `repair.py` + `tests/test_repair.py`. Pipeline still compiles
   because `Rejected.primary.rule_id` is castable to the old string in a thin
   compatibility shim (deleted in step 4).
3. `refactor(pipeline): aggregate by primary.rule_id, rename reject_categories →
   reject_counts` — `pipeline.py` + `tests/test_pipeline.py`.
4. `feat(report): emit rule IDs in .broken.txt and report.md` — `report.py` +
   updated golden test files. Remove the shim from step 2.
5. `chore: drop RejectCategory from categories.py` — final cleanup; no behaviour
   change beyond the previous step.
6. `docs(changelog): note .broken.txt format change in v0.3.0` — CHANGELOG entry.

Each commit runs `uv run pytest && uv run ruff check . && uv run ruff format --check .`.

## 10. Open questions (none blocking)

- **`lintle explain` integration**: this design lets a future `lintle explain TLE-CHK-001`
  introspect `RULES[RuleID.CHECKSUM_MISMATCH]` directly. No dedicated docs file needed
  until prose grows past `short_title` — at which point a `docs/rules/TLE-CHK-001.md`
  pattern is the natural extension.
- **JSON output**: when a future `--json` flag is added, the natural shape is one
  record per Rejected with `{"primary": {...}, "related": [...]}` — no further
  schema work required.

## 11. Revision log

- **2026-05-24** — initial design.
