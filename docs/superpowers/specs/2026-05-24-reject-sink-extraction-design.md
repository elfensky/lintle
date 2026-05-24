# Reject Sink Extraction — Design

- **Date:** 2026-05-24
- **Status:** Approved (post-review); ready for implementation
- **Revision:** **2026-05-24:** §4.5 / §4.7 / §7 / §10 — applied findings from
  a multi-AI adversarial review (Codex + Gemini + Sonnet). §4.5 pins
  post-finalize `add()` behavior (raises `RuntimeError`). §4.7 corrects the
  test-line-change estimate from "~30" to "~50–80" per Sonnet's grep. §7
  gains a post-finalize-add test. §10 carries the resolved status for each
  open question; `dropped_count` observability per Gemini becomes a follow-up
  issue.
- **Topic:** Issue #19 — encapsulate the bounded reject sample and the
  streaming `.broken.txt` writer behind a single `RejectSink` type so the
  5-per-rule cap is a structural property of the data, not a convention
  enforced by exactly one caller.

## 1. Problem statement

Issue #19 asked for `FileStats` to split into aggregate counters and a
"streaming reject sink." The constant-memory work that landed via PRs #42
and #45 substantively delivered the *memory* outcome — the unbounded
`rejects: list[RejectEntry]` is gone; what remains in `FileStats` is
`reject_exemplars`, a bounded sample. The byte-faithful streaming is in
`BrokenFileWriter` (`src/lintle/report.py:141`).

But the *encapsulation* outcome is incomplete:

1. **The 5-per-rule cap is convention-enforced.** `FileStats.reject_exemplars`
   (`report.py:65`) is a plain `dict[RuleID, list[RejectEntry]]`. The cap is
   applied in `pipeline._record_reject` (`pipeline.py:299–304`) by checking
   `len(bucket) < _PER_RULE_EXEMPLAR_BOUND` before appending. The dataclass
   itself is incapable of refusing an over-cap insert. The dataclass docstring
   (`report.py:53–54`) explicitly documents this as a convention: *"The cap
   is enforced by the pipeline, not by this dataclass, so tests can populate
   it freely."*
2. **14 test sites mutate the dict directly** (`tests/test_report.py:57,86,
   102,133,171,198,294,305,336,363,384,393,415,452` plus `tests/test_pipeline.py:
   292,313`), bypassing the cap by design. That is *load-bearing* — tests need
   to drive renderers without spinning up the pipeline — but it means the
   invariant has at least 15 mutation paths, only one of which checks the cap.
3. **`pipeline.process_file` juggles two collaborators** (`pipeline.py:175–176,
   261–264,268–269`) — the local `broken_writer` and `stats.reject_exemplars`
   — both keyed off the same `RejectEntry`, both manipulated in a `try/finally`
   block that has three exit paths (success, exception, validate-mode no-op).
   Future maintenance has to keep all three sites synchronized.

The constant-memory rule from `CLAUDE.md` §Critical Rules holds today only
because exactly one production caller exists. Any future writer — a stats
merger, a crash-recovery loader, an `lintle stats merge` command — that
forgets to consult `_PER_RULE_EXEMPLAR_BOUND` will silently violate it. There
is no test that would catch this; the existing tests *are* the violators.

## 2. Goal & non-goals

**Goal.** Move the bounded sample, its cap, and the `BrokenFileWriter`
lifecycle into one type — `RejectSink` — that:

- exposes a single mutation entry point that cannot blow the cap;
- owns the `.broken.txt` writer's open/finalize/cleanup lifecycle in `clean`
  mode and skips it in `validate` mode;
- hands the finalized sample to `FileStats` as an immutable value object
  (`FileSample`) so post-finalize consumers (`format_reject_lines`,
  `write_broken_file`) cannot accidentally mutate it.

The diff prize is collapsing the three `pipeline.process_file` juggling sites
into one `sink.add(entry)` call.

**Non-goals — explicitly excluded:**

| Excluded | Rationale |
|----------|-----------|
| Renaming `BrokenFileWriter` | It is an internal helper; `RejectSink` *owns* it but does not subsume its byte-format responsibility. Stays named. |
| Refactoring `FileStats.quarantined_norad_ids` | Same shape, same convention-enforced bound, but unrelated user-facing concern (per-NORAD breakdown vs per-rule sample). Out of scope; revisit as a follow-up if the same maintenance pain recurs (§9). |
| Any user-visible byte change | `.broken.txt` bytes, `--report json` payload, and `report.md` Markdown stay byte-identical. Existing format-lock tests must pass unchanged. |
| Public API changes | `lintle` exports only `__version__` and `stem()` (`__init__.py`); nothing under refactor is part of a programmatic contract. |
| Introducing a new third-party dependency | `hypothesis`-style property testing would suit the invariant well but the runtime is pure stdlib and `hypothesis` is not a dev dep. Use a deterministic seeded randomized test instead (§7). |

## 3. Critical-rule compliance

- **Constant memory (CLAUDE.md §Critical Rules).** The cap moves from a
  convention to a structural property. Strictly stronger guarantee.
- **Correctness over recovery.** Untouched — no validation or repair behavior
  changes.
- **One validator definition.** Untouched — `tle.py` is not modified.
- **Validated transformation.** Untouched — no fix tier changes.

## 4. Architectural decisions

### 4.1 Ownership model — three candidates (**primary debate topic**)

The crux question: where does the bounded sample live *after* the file is
processed but before the CLI renders the summary?

| Variant | Sketch | Pros | Cons |
|---------|--------|------|------|
| **A — Sink writes, FileSample stores** | `RejectSink` is short-lived (per-file). It owns `BrokenFileWriter` and an internal sample dict. On `finalize`, the sink produces an immutable `FileSample` value object and attaches it to the file's `FileStats`. Sink dies at the file boundary; stats survive to the aggregation/report phase. | Clear separation: sink = write device, FileSample = data record. Tests of the renderer construct `FileSample.from_bounded(...)` directly with no pipeline involvement. Cap enforcement happens at one site (sink); immutability prevents post-finalize mutation. | Adds a second type (`FileSample`) on top of `RejectSink`. Two new names to learn. |
| **B — Sink replaces `reject_exemplars` entirely** | `pipeline.process_file` returns `(stats, sink)` per file; CLI keeps two lists. Sink stays alive past file boundary as the per-file sample record. | Only one new type. | Sink lifetime now spans file boundary, so its context-manager idiom no longer matches "write device" — it's now a data record that *happens* to have had a write phase. Mixes two concerns the issue specifically asked to separate. |
| **C — Sink subsumes FileStats** | `FileStats` becomes pure aggregate counters. `RejectSink` carries everything sample-related; the CLI keeps `list[RejectSink]` for rendering and the same list for sample aggregation. | Maximal split — exactly what the issue title literally said. | Restructures every call site that takes `FileStats` (`_aggregate`, `format_run_report`, `summary_dict`, the per-NORAD rollup). Largest blast radius; spills into JSON/Markdown emitters that don't need to change for any *functional* reason. |

**Recommendation: A.** Justification: separates the write-time invariant
(cap enforcement, sink's job) from the read-time data record (immutable
sample, `FileSample`'s job). Tests of renderers don't depend on the sink at
all. The `_aggregate` rollup doesn't change shape. The pipeline's three
juggling sites collapse to one `sink.add(entry)` — the actual diff prize.

### 4.2 Module location

`RejectSink` and `FileSample` live in `src/lintle/report.py`, alongside
`BrokenFileWriter` and `FileStats`. Adding a fourth module for ~120 lines of
class+dataclass would violate the "no premature abstraction" guidance in
`CLAUDE.md` §Doing tasks. If `report.py` grows past comfort later, extraction
is a follow-up.

### 4.3 Lifecycle — context manager

```python
with RejectSink(broken_path=..., src_name=..., cap=5) as sink:
    for record in records:
        if reject:
            sink.add(entry)
    sample = sink.finalize(entries=stats.paired_records + stats.orphan_entries)

stats.reject_sample = sample
```

`__exit__` delegates to `BrokenFileWriter.__exit__` (already correct re:
partial-cleanup on abnormal exit). In `validate` mode `broken_path=None`
and `__enter__`/`finalize` skip the writer setup entirely — the sink
becomes an in-memory bucket plus a no-op finalize.

### 4.4 `FileSample` value object

```python
@dataclasses.dataclass(frozen=True)
class FileSample:
    """Immutable, per-file bounded sample of quarantined records.

    Produced by ``RejectSink.finalize``; consumed by renderers
    (``format_reject_lines``, ``write_broken_file``). Frozen so post-finalize
    consumers cannot accidentally mutate the sample — the cap invariant is
    locked in at construction time.
    """
    buckets: Mapping[RuleID, tuple[RejectEntry, ...]]  # tuple → immutable
    cap: int  # the cap that was applied; metadata so renderers can show truncation

    @classmethod
    def from_bounded(
        cls, cap: int, entries_by_rule: Mapping[RuleID, Sequence[RejectEntry]]
    ) -> "FileSample":
        """Build a FileSample, asserting every bucket honours ``cap``.

        Test-friendly constructor. Clones each bucket into a ``tuple`` and
        raises ``ValueError`` if any bucket exceeds ``cap`` — strict by
        design so silent over-cap inputs surface in tests.
        """
        for rule_id, entries in entries_by_rule.items():
            if len(entries) > cap:
                raise ValueError(
                    f"bucket {rule_id} has {len(entries)} entries; cap is {cap}"
                )
        return cls(
            buckets={rid: tuple(entries) for rid, entries in entries_by_rule.items()},
            cap=cap,
        )

    @classmethod
    def empty(cls, cap: int) -> "FileSample":
        """Sentinel for files with no rejects — keeps consumers from None-checking."""
        return cls(buckets={}, cap=cap)
```

### 4.5 `RejectSink` class shape

```python
class RejectSink:
    """File-scoped reject sink: bounded sample + optional streaming sidecar.

    Single mutation entry point ``add(entry)`` enforces the cap structurally.
    On ``finalize`` produces an immutable :class:`FileSample` and (in clean
    mode) stitches the byte-faithful ``.broken.txt`` sidecar via the owned
    :class:`BrokenFileWriter`.
    """
    def __init__(
        self,
        *,
        cap: int = _PER_RULE_EXEMPLAR_BOUND,
        broken_path: str | None = None,
        src_name: str | None = None,
    ): ...

    def add(self, entry: RejectEntry) -> None:
        """Append ``entry`` to its rule's bucket if cap permits, then stream.

        Silently drops past-cap entries — matches today's
        ``pipeline._record_reject`` behavior, and the full count is still
        retained in ``stats.reject_counts`` so no information is lost.
        Raises ``RuntimeError`` if called after ``finalize`` — the sink is
        single-use and post-finalize mutation has no defined semantics.
        """

    def finalize(self, *, entries: int) -> FileSample:
        """Stitch the sidecar (if any) and return the immutable sample.

        Marks the sink as finalized — any subsequent ``add`` raises
        ``RuntimeError``. The returned ``FileSample`` is built via the
        sink's own bookkeeping; it is NOT re-validated by
        ``FileSample.from_bounded`` because the sink IS the invariant
        boundary. ``from_bounded`` stays strict for test fixtures and any
        future external construction path.
        """

    # context-manager methods delegate to the writer when broken_path is set
```

`_PER_RULE_EXEMPLAR_BOUND = 5` moves from `pipeline.py:16` to `report.py`
since the cap is now an attribute of the sink, not a pipeline-level policy.

### 4.6 `FileStats` shape change

Before:
```python
reject_exemplars: dict = dataclasses.field(default_factory=dict)
```

After:
```python
reject_sample: FileSample = dataclasses.field(
    default_factory=lambda: FileSample.empty(_PER_RULE_EXEMPLAR_BOUND)
)
```

The empty default eliminates `if stats.reject_sample is not None` boilerplate
in every consumer. The cost is a tiny per-file allocation that always happens
even for zero-reject files — measured in nanoseconds, irrelevant against
30 GB of TLE I/O.

### 4.7 Test-rewrite policy

- **`tests/test_pipeline.py`** — exercises `_record_reject` end-to-end. Migrates
  to driving `sink.add(entry)` through the existing `process_file` test fixtures
  (the pipeline still wires it up; the test asserts post-finalize sample shape).
- **`tests/test_report.py`** — exercises renderers (`format_reject_lines`,
  `write_broken_file`) in isolation. Migrates to constructing `FileSample.
  from_bounded(cap=5, {RuleID.X: [entries]})` directly, bypassing the sink.
  This is *intentional*: the renderers don't care about the sink; they care
  about the value object.

Net test diff: ~50–80 line changes across two files (revised upward from
the initial ~30 estimate after a grep audit: `test_report.py` has 14
mutation sites, several inside nested loops over multiple rules — those
require collapsing into a pre-built bucket dict for `from_bounded`, not a
1-for-1 line swap). The `from_bounded` constructor still collapses many
setup blocks, but the net diff is dominated by the loop-collapsing
rewrites.

## 5. Module-level changes

| File | Change |
|------|--------|
| `src/lintle/report.py` | Add `FileSample` (frozen dataclass + `from_bounded` + `empty` classmethods). Add `RejectSink` class. Move `_PER_RULE_EXEMPLAR_BOUND` constant here from `pipeline.py`. Change `FileStats.reject_exemplars` → `FileStats.reject_sample: FileSample`. Update `format_reject_lines` and `write_broken_file` to read from `FileSample.buckets` and `FileSample.cap`. |
| `src/lintle/pipeline.py` | Drop `_PER_RULE_EXEMPLAR_BOUND` constant (re-imports from `report` if still needed locally — likely not). Replace `broken_writer = report.BrokenFileWriter(...)` plus the dict-write in `_record_reject` with `sink = report.RejectSink(broken_path=..., src_name=...)` and a single `sink.add(entry)` call. Collapse the `try/finally` cleanup since the sink's `__exit__` now handles both the writer and the sample. On success, call `sample = sink.finalize(...)` and assign to `stats.reject_sample`. |
| `tests/test_pipeline.py` | Update existing exemplar-shape assertions to read from `stats.reject_sample.buckets` instead of `stats.reject_exemplars`. Add a new test asserting that the cap holds under adversarial input order (1000 entries of one rule arriving before a single entry of another rule). |
| `tests/test_report.py` | All 14 sites migrate from direct dict mutation to `FileSample.from_bounded(cap=5, {RuleID.X: [entries]})`. |
| `tests/test_report.py` (new test class) | `TestFileSample` — `from_bounded` cap-violation raises `ValueError`; frozen-ness verified by `pytest.raises(FrozenInstanceError)`; `empty(cap)` produces a zero-bucket sample with the requested cap. |
| `tests/test_report.py` (new test class) | `TestRejectSink` — `add` past cap drops silently (matches today's pipeline behavior); `finalize` returns a `FileSample` whose every bucket honours the cap; context-manager exit without `finalize` cleans up writer partials; validate-mode (no `broken_path`) skips writer setup. |
| `CHANGELOG.md` | Append to `[Unreleased]` under `### Changed`: "Internal: extract `RejectSink` and `FileSample` from `FileStats` so the 5-per-rule exemplar cap is enforced by construction. Closes #19." |

## 6. Critical-rule compliance (restated)

- Constant memory: ✓ — cap structurally enforced (stronger).
- Byte-faithful sidecar: ✓ — `BrokenFileWriter` byte format unchanged.
- One validator definition: ✓ — `tle.py` untouched.
- Validated transformation: ✓ — repair pipeline untouched.

## 7. Test strategy

1. **All existing tests pass with mechanical rewrite.** No behavior change is
   intended; the `.broken.txt`, JSON, and Markdown emitters produce
   byte-identical output. Format-lock tests in `test_report.py` and
   `test_cli.py` must pass unchanged after the renderer-input migration.
2. **New structural-invariant test.** `TestRejectSink::test_cap_holds_under_skew`
   — feed 1000 `RuleID.CHECKSUM_MISMATCH` entries then 1 `RuleID.BAD_PREFIX`
   entry to a single sink; assert the finalized `FileSample` has exactly 5
   checksum entries and 1 bad-prefix entry. Mirrors the issue #21 invariant
   from a different angle.
3. **New negative test.** `TestFileSample::test_from_bounded_rejects_over_cap`
   — `FileSample.from_bounded(cap=5, {RuleID.CHECKSUM_MISMATCH: [6 entries]})`
   raises `ValueError`. Locks in the strict-validation choice from §4.4.
4. **Deterministic randomized test.** `TestRejectSink::
   test_cap_holds_under_random_input` — seed `random.Random(42)`, generate a
   1000-element sequence of `(rule_id, entry)` pairs from `RuleID`'s enum,
   feed to a sink, assert every bucket in the finalized sample is ≤ cap.
   Catches off-by-ones the targeted tests miss. No new dep.
5. **Context-manager cleanup test.** `TestRejectSink::
   test_exit_without_finalize_cleans_partials` — open a sink with a real
   `broken_path`, add an entry, exit the context without calling `finalize`,
   assert no `*.partial` files remain. Mirrors `TestStreamingRejects`'s
   existing partial-cleanup assertion at the sink level.
6. **Post-finalize-add behavior test.** `TestRejectSink::
   test_add_after_finalize_raises` — finalize the sink, then call `add()`,
   assert `RuntimeError`. Locks the §4.5 single-use contract so future
   contributors can't accidentally turn the sink into a reusable container.

Coverage target: `RejectSink` and `FileSample` at 100 % branch coverage. The
classes are tiny; this is achievable.

## 8. Build order

Per `CLAUDE.md` §Working Style — test-first.

1. `FileSample` value object — write `TestFileSample` first (`from_bounded`
   validation, `empty` shape, frozen-ness). Implement until green.
2. `RejectSink` class — write `TestRejectSink` first (add cap, finalize,
   context-manager cleanup, validate-mode no-writer path). Implement until
   green.
3. Wire `RejectSink` into `pipeline.process_file`. Adapt `_record_reject`
   call sites. Existing pipeline tests should pass with shape assertions
   updated to read `stats.reject_sample.buckets[...]`.
4. Migrate renderer tests in `test_report.py` to `FileSample.from_bounded`.
   Renderers (`format_reject_lines`, `write_broken_file`) updated to read
   from the `FileSample`. Existing renderer behavior is unchanged.
5. Remove `FileStats.reject_exemplars` field. Replace with `reject_sample`.
   Cascade any lingering references.
6. Add the adversarial-skew and deterministic-random invariant tests (§7.2,
   §7.4). These would have failed before the structural enforcement — they
   pass now and become regression locks.
7. Verification chain (`uv run pytest && uv run ruff check . && uv run ruff
   format --check .`) clean. Then CHANGELOG.

## 9. Out-of-scope follow-ups

- **`quarantined_norad_ids`.** Same convention-enforced shape on `FileStats`
  (`report.py:73`). Same risk if a future writer forgets the cap discipline.
  Bound is different (catalog size × |RuleID|, not 5 × |RuleID|) so the
  encapsulation question is less acute, but the architectural argument is
  identical. File a follow-up issue if this refactor lands cleanly.
- **`hypothesis` property-based testing for the invariant.** Would express
  the cap rule more naturally than the seeded random test. Out of scope: the
  runtime is stdlib-only and `hypothesis` is not currently a dev dep; adopt
  it project-wide as its own decision, not as a side effect of this refactor.

## 10. Open questions (resolved by multi-AI debate, 2026-05-24)

All five open questions were settled by an adversarial four-way review
(Claude Opus 4.7 + Sonnet 4.6 + Codex + Gemini). Three of four voices
agreed on every question; Gemini was the consistent dissenter (4 of 5
questions) but surfaced one strong novel point captured as a follow-up.

1. **Ownership model A/B/C (§4.1).** ✅ A. `FileSample`'s frozen-ness
   structurally prevents a renderer from being handed an unfinalized sink
   — that's the load-bearing benefit, not aesthetics. Dissent (Gemini)
   pushed B but did not engage with the immutability argument.
2. **`FileSample` default — `None` or `empty()`?** (§4.6.) ✅ `empty()`.
   Production access surface is 3 sites; None-guards add ceremony with no
   payoff. Dissent (Gemini) called it a "zombie pattern" but the
   alternative would require ceremony at every consumer.
3. **`from_bounded` strictness (§4.4).** ✅ Strict. Raises on over-cap
   input so test mistakes surface immediately. The sink's `finalize`
   builds the `FileSample` directly without re-validating (sink IS the
   invariant boundary, see §4.5 finalize docstring).
4. **Should `quarantined_norad_ids` ride along?** (§9.) ✅ Defer.
   Different contract (per-satellite accounting, not display sample),
   different lifetime (alive at `_aggregate_per_norad` time), different
   cap semantics (catalog-bounded). Dissent (Gemini) called the deferral
   "hypocritical" but didn't engage with the contract difference. Will
   file as a follow-up issue.
5. **Where does `cap` come from?** ✅ Module default with override
   permitted (sketch in §4.5 retained).

### Carry-forward as follow-up issues

- **`FileSample.dropped_count: Mapping[RuleID, int]`** — operator
  observability so reports can distinguish "5 of 5 hits" from "5 of
  50,000 hits." Gemini's strongest novel point; not part of the
  encapsulation refactor but a clean mechanical follow-up. Field shape:
  `Mapping[RuleID, int]`, incremented in `RejectSink.add` when the cap
  blocks an append, surfaced in `FileSample` and in
  `format_reject_lines` / `report.md`'s rule reference.
- **`quarantined_norad_ids` encapsulation** — same architectural argument
  as this refactor; different abstraction (`NoradTracker` shape, not
  `RejectSink`). File once this refactor lands cleanly.

## 11. Revision log

- **2026-05-24:** Initial draft.
- **2026-05-24:** Spec revisions from multi-AI adversarial review.
  §4.5 pins post-finalize `add()` behavior (raises `RuntimeError`) and
  documents that `finalize` builds the `FileSample` without re-validating.
  §4.7 corrects the test-line-change estimate "~30" → "~50–80" per a
  grep audit. §7 gains test 6 (post-finalize add raises). §10 carries
  resolved status for each open question and captures `dropped_count`
  observability + `quarantined_norad_ids` encapsulation as
  follow-up-issue work. Status changed from Draft to Approved.
