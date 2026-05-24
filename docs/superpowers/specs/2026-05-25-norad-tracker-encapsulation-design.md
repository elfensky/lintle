# NORAD Tracker Encapsulation — Design

- **Date:** 2026-05-25
- **Status:** Approved (post 3-agent review, Path C); ready for implementation
- **Revision:** **2026-05-25:** Initial draft; same-day revisions from a
  3-agent local adversarial review (Architect-Skeptic +
  Implementation-Realist + YAGNI-Contrarian). Headline changes:
  *(a)* dropped `merge` from the type (YAGNI's lock-out argument — see
  §10.1/§10.6); *(b)* keep field name `quarantined_norad_ids`,
  type-only change (preserves JSON-key contract — Impl-Realist +
  YAGNI converge, §10.7); *(c)* dropped dual-field migration in
  favor of a single atomic consumer-and-fixture commit (fixes
  Architect + Impl-Realist's triangulated sequencing bug — §4.6);
  *(d)* dropped §7.4/§7.5 tautological tests (Impl-Realist); *(e)*
  expose `.counts` as the read surface (§10.2). Review artefacts:
  `~/.claude-octopus/debates/default-session/002-norad-tracker-spec-review/`.
- **Topic:** Issue #47 — wrap `FileStats.quarantined_norad_ids` (a plain
  `dict[int, dict[RuleID, int]]`) in a typed `NoradTracker` so the
  single-writer convention enforced by `pipeline._record_reject` becomes a
  structural property of the type rather than an in-author's-head invariant.
  Sibling refactor to issue #19's `RejectSink` extraction (landed
  2026-05-24); shares the architectural argument but deliberately
  **simpler and smaller** — no cap, no file resource, no context-manager,
  no freeze boundary, no `merge` monoid. See §4.1 for the contract
  divergence table and §10 for the rationale on each diverging
  decision.

## 1. Problem statement

`FileStats.quarantined_norad_ids` (`src/lintle/report.py:134`) is a plain
`dict[int, dict[RuleID, int]]` accumulating per-satellite per-rule
quarantine counts for one file. Today exactly one production writer
populates it: `pipeline._record_reject` (`src/lintle/pipeline.py:308–311`),
via a `setdefault` + `get`/`+1` dance:

```python
norad_id = tle.extract_norad_id(raw_lines[0])
if norad_id is not None:
    per_rule = stats.quarantined_norad_ids.setdefault(norad_id, {})
    per_rule[primary.rule_id] = per_rule.get(primary.rule_id, 0) + 1
```

The shape is identical to the pre-#19 state of `reject_exemplars`:

1. **A plain `dict` field on `FileStats`.** No type wraps it; the dataclass
   is incapable of refusing a malformed write.
2. **A single production caller follows the discipline.** Any future writer
   — a stats merger, a `lintle stats merge` command, a crash-recovery
   loader — that forgets the `setdefault` + `.get(...)+1` pattern can
   trivially produce a malformed inner dict (wrong value type, missing
   initialisation, double-counting).
3. **The invariant lives only in the author's head.** "Outer keys are int
   NORAD IDs; values are `dict[RuleID, int]` accumulators; the only
   mutation is `+1` on `(norad_id, rule_id)` when a record is quarantined"
   is a sentence in `report.py:127–134`'s docstring, not a property the
   compiler or runtime enforces.

Three consumer paths read the field today, each with a slightly different
access pattern:

| Consumer | Access pattern | File:line |
|----------|---------------|-----------|
| `summary_dict` | `nid, cats in stats.quarantined_norad_ids.items()` — needs key+value iteration, shallow-clones each per-rule dict | `report.py:425–427` |
| `_aggregate_per_norad` | `nid, categories in stats.quarantined_norad_ids.items()` — needs key+value iteration, accumulates into a rollup dict | `report.py:528–538` |
| `aggregate_broken_norad_ids` | `ids |= set(stats.quarantined_norad_ids)` — needs key iteration only | `report.py:720` |

Plus ~26 test fixture sites in `tests/test_report.py` that construct
`FileStats(... quarantined_norad_ids={...})` directly, and 7 read sites
in `tests/test_pipeline.py` that assert against the dict shape
(per the Implementation-Realist's grep audit; spec earlier estimated
~30 and 6 respectively).

The encapsulation prize: collapse the four-line `setdefault`/`.get`/`+1`
dance at `_record_reject` to a single `stats.quarantined_norad_ids.
record(norad_id, rule_id)` call, lock the write-side invariant into the
type, and stop future writers from re-inventing the dance differently.
**The prize does NOT include corpus-rollup encapsulation** — that
operation stays a free function (`_aggregate_per_norad`), reading the
tracker's `.counts` dict directly. See §10.1 for the rationale.

## 2. Goal & non-goals

**Goal.** Introduce `lintle.report.NoradTracker` as a typed wrapper around
the per-satellite per-rule accounting dict, with:

- a single mutation entry point (`record(norad_id, rule_id)`) — the only
  way to add a quarantine fact to the tracker;
- a public `counts` dict attribute serving the three existing consumer
  read patterns (no proxy methods; see §10.2 for why);
- no behavior change visible to the operator — `.broken.txt`, the JSON
  output, `report.md`'s per-NORAD breakdown table, and the NDJSON
  sidecar remain byte-identical.

**Explicitly dropped from the goal (vs initial spec draft):**

- **No `merge` method.** `_aggregate_per_norad` keeps its current
  accumulator loop, reading `tracker.counts` directly. Rationale:
  committing to `merge` as an associative/commutative monoid locks the
  data shape — future evolutions toward per-NORAD timestamps (issue
  #15's `lintle trace`) or per-NORAD provenance (issue #20's richer
  JSON metadata) want non-monoid fields. See §10.1 for the full
  argument from YAGNI-Contrarian's review.
- **No `seal()` / freeze boundary.** The tracker stays mutable through
  its life. The seal+frozen pattern made sense for `RejectSink → FileSample`
  because the sample handed off across a write/read phase boundary that
  had a natural `sink.finalize()` call site; this tracker has no
  similar gain absent `merge`. See §10.5.
- **No field rename.** Field stays named `quarantined_norad_ids`; only
  its type changes. Rationale: the JSON output key in `summary_dict`
  (`"quarantined_norad_ids"`) is locked by `tests/test_report.py:298,
  305–308`. Keeping the field name preserves both the JSON contract
  and the `git log -S` history. See §10.7.
- **No runtime `isinstance(int)` narrowing on `record()`.** Deferred as
  a one-line follow-up if/when the hypothetical second caller appears.
  See §10.9.

**Non-goals — explicitly excluded:**

| Excluded | Rationale |
|----------|-----------|
| Changing `broken-noradids.ndjson` byte format | Locked artifact; downstream consumers parse it. Additive evolution is fine in a future issue, structural change is not. |
| Changing `report.md`'s per-NORAD breakdown table | Same lock; deterministic Markdown is asserted by `tests/test_report.py` format-lock tests. |
| Renaming the JSON output key | `summary_dict` returns a dict keyed `"quarantined_norad_ids"`. That key is part of the operator-visible contract and MUST stay even though the implementation now reads via `tracker.counts`. |
| Capping the tracker | Unlike `RejectSink`'s 5-per-rule display cap, the per-NORAD bound is *natural* (catalog × \|RuleID\| — bounded by the satellite catalog at ~tens of thousands × ~15 rules) and already constant-memory. There is nothing to cap. |
| Owning a file resource | The NDJSON emission lives in `write_broken_noradids_ndjson` (a CLI-time corpus-wide concern), not on the per-file tracker. |
| Refactoring `FileStats.reject_sample` further | Already encapsulated by #19's `RejectSink`. |
| Introducing a third-party dependency | Runtime is stdlib-only. |
| Public API changes | `lintle` exports only `__version__` and `stem()`; nothing here is part of a programmatic contract. |

## 3. Critical-rule compliance

- **Constant memory (CLAUDE.md §Critical Rules).** Unchanged. The per-file
  tracker is already O(catalog × \|RuleID\|); no memory shape change.
- **Correctness over recovery.** Untouched — no validation or repair
  changes.
- **One validator definition.** Untouched — `tle.py` not modified.
- **Validated transformation.** Untouched — no fix tier changes.

## 4. Architectural decisions

### 4.1 Contract differences vs `RejectSink` (**read this first**)

The most likely reviewer mistake will be conflating `NoradTracker` with
`RejectSink`. They share an *architectural argument* (encapsulate a `dict`
field on `FileStats` behind a typed mutation entry point) but **the
contract is fundamentally simpler**:

| Property | `RejectSink` + `FileSample` (#19, landed) | `NoradTracker` (#47, this spec) |
|----------|------------------------------------------|--------------------------------|
| **Bound** | Hard cap: 5 exemplars per rule, structurally enforced. Drops counted in `FileSample.dropped_count`. | Soft bound: satellite catalog × \|RuleID\|. Never "full"; no drops; no cap parameter. |
| **Lifetime** | Sink dies at file boundary. `FileSample` (frozen) lives on `FileStats` for the read phase. | Tracker lives on `FileStats` for both write *and* read phases. |
| **Mutability after the file ends** | Frozen at `sink.finalize()`. Renderers cannot mutate. | **Stays mutable** — no freeze boundary (see §10.5). Convention-defended: post-file consumers happen not to mutate, but the type doesn't prevent it. |
| **Owns a file resource?** | Yes — `BrokenFileWriter` for `.broken.txt`. | No. The NDJSON sidecar is corpus-wide and written by `write_broken_noradids_ndjson` from a list of `FileStats`. |
| **Context-manager lifecycle?** | Yes — `with sink:` ensures partial cleanup on abnormal exit. | No. Nothing to clean up; no resource to release. |
| **Single mutation entry point?** | Yes — `add(entry)` | Yes — `record(norad_id, rule_id)`. **Same architectural prize.** |
| **Frozen value-object companion?** | Yes — `FileSample`. | No — the read surface is `tracker.counts` (a plain dict the type owns but exposes). Half-encapsulation by deliberate choice (§10.2). |

**Reviewer instruction:** if you find yourself reasoning by analogy to
`RejectSink` and wanting to add a `seal()` method or a frozen value
object, read §10.5. That symmetry has a cost (locks data shape, adds
ceremony) and no payoff absent `merge`.

### 4.2 Type sketch

```python
@dataclasses.dataclass
class NoradTracker:
    """Per-file per-satellite per-rule quarantine accounting (issue #47).

    Wraps the previously-raw ``dict[int, dict[RuleID, int]]`` field on
    :class:`FileStats` so all mutations route through :meth:`record` —
    the encapsulation prize is a single named entry point for future
    writers to find by grep instead of reinventing the setdefault
    dance.

    The read surface is the public ``counts`` dict, by deliberate
    choice: the three production consumers each want different access
    shapes (``.items()``, key iteration, value-clone), and a proxy-
    method API would add surface tax with no encapsulation gain
    (see §10.2 of the design doc). Half-encapsulation: write-side
    discipline enforced; read-side accessible to anyone who reaches
    through ``.counts``.

    Bounded by the satellite catalog and the ``RuleID`` enum — never
    "full", no drops, no cap. Mutable through its life; no freeze
    boundary (§10.5). No ``merge`` method; corpus rollup stays a free
    function in :func:`_aggregate_per_norad` (§10.1).
    """

    counts: dict = dataclasses.field(default_factory=dict)

    def record(self, norad_id, rule_id):
        """Tally one quarantine for ``norad_id`` against ``rule_id``.

        The only sanctioned mutation entry point. Outer key is the
        catalog-decoded integer NORAD ID; inner key is the
        :class:`RuleID` member, value is a running count. Repeated
        calls accrue (creates the bucket on first call, increments
        thereafter).
        """
        per_rule = self.counts.setdefault(norad_id, {})
        per_rule[rule_id] = per_rule.get(rule_id, 0) + 1
```

That's the entire type. ~12 lines including docstring; ~5 lines of
actual code.

### 4.3 Module location

`NoradTracker` lives in `src/lintle/report.py` next to `FileStats`,
`FileSample`, `RejectSink`, `BrokenFileWriter`. Same justification as
#19's spec §4.2: no premature module split for ~12 lines of dataclass +
one method.

### 4.4 `FileStats` shape change

Before:
```python
quarantined_norad_ids: dict = dataclasses.field(default_factory=dict)
```

After:
```python
quarantined_norad_ids: NoradTracker = dataclasses.field(
    default_factory=NoradTracker
)
```

**Field name unchanged.** Only the type changes. Rationale:

- The JSON output key in `summary_dict` (`"quarantined_norad_ids"`) is
  contract-locked — `tests/test_report.py:298, 305–308` assert against
  `data["quarantined_norad_ids"][...]`. Operator-visible contract per §2
  non-goals.
- `git log -S "quarantined_norad_ids"` history stays intact. No
  rename-storm in the diff.
- Reader cognitive cost of "field name doesn't match type name" is
  trivial; one line of docstring resolves it.

### 4.5 Consumer migration

Three consumers change their access pattern; **none** of them changes its
output shape. All swaps are one substitution per site:

| Site | Before | After |
|------|--------|-------|
| `pipeline._record_reject:308–311` | `per_rule = stats.quarantined_norad_ids.setdefault(norad_id, {}); per_rule[rule_id] = per_rule.get(rule_id, 0) + 1` (4 lines) | `stats.quarantined_norad_ids.record(norad_id, rule_id)` (1 line) |
| `report.summary_dict:425–427` | `nid, cats in stats.quarantined_norad_ids.items()` | `nid, cats in stats.quarantined_norad_ids.counts.items()` |
| `report._aggregate_per_norad:528–538` | `nid, categories in stats.quarantined_norad_ids.items()` | `nid, categories in stats.quarantined_norad_ids.counts.items()` |
| `report.aggregate_broken_norad_ids:716–721` | `ids \|= set(stats.quarantined_norad_ids)` | `ids \|= set(stats.quarantined_norad_ids.counts)` |

**Pinned invariant:** The JSON output key in `summary_dict` MUST remain
`"quarantined_norad_ids"`. The implementation now reads via
`.counts.items()` but the emitted key stays as-is. `tests/test_report.
py:298, 305–308` will fail loudly if an implementer forgets and renames
the key — those tests are the format-lock and stay unchanged.

### 4.6 Migration shape — single atomic commit, not dual-field

**Revised from initial spec draft.** The original draft proposed a
dual-field bisectability strategy (keep `quarantined_norad_ids` dict
alive alongside a new `norad_tracker` field; migrate consumers one at a
time). The 3-agent review identified a hard sequencing bug in that
approach:

- Test fixture sites construct `FileStats(quarantined_norad_ids={...})`
  directly. They never flow through `_record_reject`. During the dual-
  field window, fixtures populate the old field but leave the new
  tracker empty.
- Consumers migrated to read the tracker (e.g. `summary_dict` in the
  draft's step 4) would see empty data on those fixtures and tests
  would pass for the wrong reason — the format-lock tests for
  `report.md`'s per-NORAD section would go green because both
  observed and expected are empty.
- The fix proposed in review (reorder so fixture migration precedes
  consumer migration, OR add a `__post_init__` bridge) adds complexity
  to defend against a problem that vanishes if we drop dual-field
  altogether.

**Revised approach: one atomic refactor commit.**

The diff is small enough that a single atomic commit is the simplest
correct strategy:

1. Add the `NoradTracker` class to `report.py`.
2. Change `FileStats.quarantined_norad_ids` field type from `dict` to
   `NoradTracker`.
3. Update all three consumers (`summary_dict`, `_aggregate_per_norad`,
   `aggregate_broken_norad_ids`) to read via `.counts`.
4. Update `_record_reject` to call `.record(norad_id, rule_id)`.
5. Migrate all ~26 fixture sites in `test_report.py` from
   `quarantined_norad_ids={...}` to `quarantined_norad_ids=NoradTracker(counts={...})`.
6. Migrate all 7 read sites in `test_pipeline.py` from
   `stats.quarantined_norad_ids == {...}` to
   `stats.quarantined_norad_ids.counts == {...}`.

Bisectability cost: revertable as a single commit, not per-consumer.
Diff cost: one regex-clean review (the fixture migration is a
mechanical wrap of every dict literal). This is the same shape as
several existing refactors in the project's history.

Optionally, split the commit into:
- **Commit A:** Add the `NoradTracker` class with `TestNoradTracker` —
  no `FileStats` changes (the type exists but isn't used).
- **Commit B:** The atomic refactor described above.
- **Commit C:** CHANGELOG entry.

Commit A is independently revertable (it adds an unused type) and gives
test-first signal on the type itself. Commit B is necessarily atomic
because the field type and its consumers must change together.

### 4.7 `_aggregate_per_norad` reshape (minimal)

Almost no reshape required. The function keeps its existing
accumulator loop; the only change is the access pattern:

```python
# Before:
for nid, categories in stats.quarantined_norad_ids.items():

# After:
for nid, categories in stats.quarantined_norad_ids.counts.items():
```

The `files`-set bookkeeping, the per-NORAD total derivation, the
sort-by-count rendering all stay exactly as they are. Net diff: one
line per consumer.

### 4.8 Test shape — `TestNoradTracker`

New test class. Four tests cover the type's full surface:

1. **`test_record_creates_new_satellite_bucket`** — fresh tracker;
   `record(25544, RuleID.CHECKSUM_MISMATCH)`; assert
   `counts[25544][RuleID.CHECKSUM_MISMATCH] == 1`.
2. **`test_record_increments_existing_pair`** — call
   `record(25544, X)` three times; assert `counts[25544][X] == 3`.
3. **`test_record_distinguishes_rules_for_same_satellite`** — record
   two different rules for one NORAD; assert both keys present, each
   at 1.
4. **`test_record_distinguishes_satellites_for_same_rule`** — record
   same rule for two different NORADs; assert both outer keys present,
   each inner dict has the rule at 1.

The empty-tracker case is implicit in tests 1 and 4 (each starts fresh).
No `merge` tests (no merge method). No iteration-surface tests (the
read surface is just `tracker.counts.<dict-method>` — covered by
Python's own dict tests).

Coverage target: `NoradTracker` at 100 % branch coverage. Trivial.

## 5. Module-level changes

| File | Change |
|------|--------|
| `src/lintle/report.py` | Add `NoradTracker` dataclass (`counts` field + `record` method per §4.2). Change `FileStats.quarantined_norad_ids` type from `dict` to `NoradTracker` (same default-factory pattern). Update three consumer sites (`summary_dict`, `_aggregate_per_norad`, `aggregate_broken_norad_ids`) to read via `.counts` per §4.5. |
| `src/lintle/pipeline.py` | Replace the `setdefault`/`get`/`+1` dance in `_record_reject` (lines 308–311) with `stats.quarantined_norad_ids.record(norad_id, primary.rule_id)`. |
| `tests/test_pipeline.py` | Update 7 assertion sites (lines 397, 406, 417, 428, 439, 454, 472) from `assert stats.quarantined_norad_ids == {...}` to `assert stats.quarantined_norad_ids.counts == {...}`. |
| `tests/test_report.py` | Migrate ~26 fixture sites from `quarantined_norad_ids={...}` constructor arg to `quarantined_norad_ids=NoradTracker(counts={...})`. Update the direct-read at line 302 from `42 in stats.quarantined_norad_ids` to `42 in stats.quarantined_norad_ids.counts`. Add `TestNoradTracker` class with the 4 tests from §4.8. |
| `CHANGELOG.md` | Append to `[Unreleased]` under `### Changed`: "Internal: encapsulate `FileStats.quarantined_norad_ids` behind a `NoradTracker` type with a single `record(norad_id, rule_id)` mutation entry point. JSON output key and field name unchanged. Mirrors the #19 `RejectSink` extraction; this refactor is deliberately simpler (no cap, no merge, no freeze boundary — see design doc §4.1). Closes #47." |

## 6. Critical-rule compliance (restated)

- Constant memory: ✓ — same bound, no change to memory shape.
- Byte-faithful sidecar (`.broken.txt`): ✓ — untouched.
- One validator definition: ✓ — `tle.py` not modified.
- Validated transformation: ✓ — no repair changes.

NDJSON byte format (`broken-noradids.ndjson`) and `report.md`'s per-NORAD
breakdown table and `summary_dict`'s JSON key vocabulary: ✓ — locked by
existing format-lock tests in `test_report.py`. Migration is mechanical;
any byte drift will surface immediately.

## 7. Test strategy

1. **All existing tests pass with the mechanical rewrite.** The ~26
   fixture sites in `test_report.py` and the 7 assertion sites in
   `test_pipeline.py` swap access patterns; the format-lock tests
   for `report.md` Markdown, `summary_dict` JSON, and
   `broken-noradids.ndjson` bytes pass unchanged. Any byte drift means
   the migration is wrong. **The existing tests ARE the regression
   rock** — no separate "round-trip" or "invariance" test is needed
   (the draft's §7.4 and §7.5 were dropped on the 3-agent review's
   tautology argument; the existing `test_summary_dict_surfaces_
   quarantined_norad_ids` at `test_report.py:282` and the
   `TestPerNoradBreakdown` class at `test_report.py:782` already lock
   the contracts at the byte boundary).
2. **New `TestNoradTracker` class** — the 4 tests enumerated in §4.8.
3. **Shallow-copy isolation contract** — `tests/test_report.py:301–302`
   already asserts that `summary_dict` shallow-copies per-NORAD inner
   dicts so caller mutations don't leak back. After the migration,
   the assertion becomes `42 in stats.quarantined_norad_ids.counts`;
   the contract itself stays intact. **The implementer must keep
   `dict(cats)` in `summary_dict` line 426 — replacing it with a
   direct reference would break this test silently until line 302
   runs.**

Coverage target: `NoradTracker` at 100 % branch coverage.

## 8. Build order

Per `CLAUDE.md` §Working Style — test-first. With Path C's simplified
scope, the order is short:

1. Write `TestNoradTracker` (§4.8). Watch every test fail
   (`ImportError` initially, then `AttributeError`).
2. Implement `NoradTracker` (`counts` field + `record` method per §4.2).
   Iterate until `TestNoradTracker` green. **Commit A** (optional split
   — see §4.6).
3. Atomically:
   - Change `FileStats.quarantined_norad_ids` field type from `dict` to
     `NoradTracker`.
   - Update `_record_reject` to call `.record(...)`.
   - Update three consumer access sites (`summary_dict`,
     `_aggregate_per_norad`, `aggregate_broken_norad_ids`) to read via
     `.counts`.
   - Migrate ~26 fixture sites in `test_report.py` to wrap dict
     literals in `NoradTracker(counts=...)`.
   - Migrate 7 read sites in `test_pipeline.py` to read via `.counts`.
   - Update direct read at `test_report.py:302` to use `.counts`.
4. Verification chain (`uv run pytest && uv run ruff check . &&
   uv run ruff format --check .`) green. **Commit B.**
5. CHANGELOG entry. **Commit C.**

Each commit is independently revertable. Commit B is necessarily
atomic; commits A and C are independent.

## 9. Out-of-scope follow-ups

- **Runtime `isinstance(norad_id, int)` narrowing on `record()`.** The
  3-agent review split: Impl-Realist said "add it, the encapsulation
  prize is don't trust future writers," YAGNI said "non-problem, no
  current caller can pass a non-int." Deferred to a one-line follow-
  up if/when the hypothetical second caller appears. Cost of being
  wrong either way is one assert and one test.
- **`merge` / corpus-rollup encapsulation.** Deferred. If
  `_aggregate_per_norad` ever needs to handle non-counter fields
  (timestamps, provenance) and the current free-function pattern
  becomes ugly, revisit a `merge`-on-`FrozenNoradTracker` design then —
  with the actual evolution in hand, not as a speculative monoid
  commitment now.
- **Property-based tests via `hypothesis`.** Runtime stays pure stdlib;
  `hypothesis` is not a dev dep. Adopt as its own decision, not as a
  side effect of this refactor.

## 10. Open questions — resolved by 3-agent local review

All 9 open questions from the initial draft were resolved by the
2026-05-25 3-agent local adversarial review (Architect-Skeptic +
Implementation-Realist + YAGNI-Contrarian, all Claude Opus 4.7
sub-agents). Five questions dissolved when Path C dropped `merge`; four
were settled directly. Review artefacts at
`~/.claude-octopus/debates/default-session/002-norad-tracker-spec-review/`.

| # | Question | Resolution | Source |
|---|----------|-----------|--------|
| §10.1 | `merge` method or free function? | **Dropped — no `merge`.** YAGNI's lock-out argument: committing to `merge` as an associative/commutative monoid locks the data shape against plausible future per-NORAD field evolutions (timestamps for #15, provenance for #20). The dict trivially absorbs additive shape changes; `merge()` forces API evolution. `_aggregate_per_norad` keeps its current loop. | YAGNI; Impl-Realist agrees on the lean-toward-in-place if kept. |
| §10.2 | Expose `.counts` directly or hide it? | **Expose.** Three production consumers each want different access shapes; a proxy-method API would add surface tax with no encapsulation gain. Acknowledged as half-encapsulation: write-side disciplined via `record`, read-side accessible. Honest framing per YAGNI's review. | YAGNI explicit; Impl-Realist explicit; Architect dissents (wanted hidden, but conditional on §10.5 freeze which we dropped). |
| §10.3 | Same PR or follow-up? | **Same PR** (single atomic refactor — §4.6). The scope is small enough that splitting buys no review-burden win. | Convergent across reviewers. |
| §10.4 | Naming — `NoradTracker` / `NoradQuarantine` / `PerSatelliteRejects` / `QuarantineCatalog`? | **`NoradTracker`.** Bikeshed per YAGNI — names will not outlive a 2026 release. Default to issue #47's proposed name. | YAGNI explicit; others didn't argue. |
| §10.5 | Freeze boundary like `FileSample`? | **No freeze boundary.** Architect argued the technical opportunity exists at `pipeline.py:264` (correct), but YAGNI's "RejectSink envy" countered it: the seal+frozen pattern earned its keep for #19 because `FileSample` was the read-time value object that the renderer consumed; absent `merge`, this tracker's read surface is just `.counts`, and a `seal()` ceremony adds no payoff. | YAGNI explicit; Architect dissents (conditional on keeping `merge`). |
| §10.6 | Functional vs in-place `merge`? | **Dropped — no `merge`.** Question evaporates. (Had it stayed, Impl-Realist + YAGNI both leaned in-place to match `_aggregate`'s existing idiom.) | Dissolved. |
| §10.7 | Field name — rename or preserve? | **Preserve `quarantined_norad_ids`.** Impl-Realist's load-bearing catch: the JSON output key is contract-locked by `tests/test_report.py:298, 305–308`. YAGNI agrees on the rename-for-rename's-sake argument. Field name = JSON key — keep both intact. | Convergent across Impl-Realist + YAGNI. |
| §10.8 | One commit vs per-test-class for fixture migration? | **One commit.** Mechanical change; one regex-clean review beats N small ones. Bisectability not improved by splitting (no dual-field anymore). | Convergent across Impl-Realist + YAGNI. |
| §10.9 | Runtime `isinstance(int)` narrowing? | **Defer as a one-line follow-up.** Impl-Realist + YAGNI directly contradicted; cost of either choice is low. Resolve when a second caller actually exists. | Conflict deferred. |

### Carry-forward as follow-up issues (if Path C ships cleanly)

- **`record()` runtime type narrowing** — see §10.9. One-line follow-up.
- **`merge` / `FrozenNoradTracker` encapsulation of corpus rollup** —
  see §9. Revisit if/when `_aggregate_per_norad` needs to handle
  non-counter fields.

## 11. Revision log

- **2026-05-25:** Initial draft.
- **2026-05-25:** Path C revisions from a 3-agent local adversarial
  review (Architect-Skeptic + Implementation-Realist + YAGNI-Contrarian,
  all Claude Opus 4.7 sub-agents). Headline changes: dropped `merge`
  from the type (§4.2, §10.1); preserved field name
  `quarantined_norad_ids` (§4.4, §10.7) to keep the JSON-key contract
  intact; switched from dual-field migration to a single atomic
  refactor commit (§4.6) to dissolve the sequencing bug the review
  triangulated; dropped §7.4/§7.5 tautological tests (§7); exposed
  `.counts` as the read surface (§4.2, §10.2). Reduced test count
  from 12 to 4. Status changed from Draft to Approved (Path C).
  Review artefacts: `~/.claude-octopus/debates/default-session/002-norad-tracker-spec-review/`.
