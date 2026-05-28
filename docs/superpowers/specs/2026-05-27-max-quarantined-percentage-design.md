# `--max-quarantined` percentage threshold — Design

- **Date:** 2026-05-27
- **Status:** Designed; not yet implemented
- **Revision:** initial
- **Topic:** Let `--max-quarantined` accept a trailing `%` so the exit-code gate can be
  expressed as a *rate* (fraction of routed records quarantined), not only an absolute
  count. Mutually exclusive by construction; fully backward-compatible.

## 1. Problem

`--max-quarantined N` (issue #13, `cli.py:204`) is an absolute record count: the run
exits `1` when `quarantined_count > N`, else `0` (operational error `2` and Ctrl-C `130`
outrank it). A fixed count does not scale. Over a growing corpus — runs of 10⁸–10⁹
records are in scope — a budget like `1000` silently tightens in relative terms as the
input grows, and there is no scale-invariant way to say "alert me only if the quarantine
*rate* spikes." Quarantine is the designed, correct outcome for unrepairable input
(Critical Rule #2), so a large real corpus always quarantines some records; an absolute
budget must be hand-retuned as input volume changes — exactly the maintenance burden a
long-lived data pipeline wants to avoid.

The gate is not exercised in GitHub CI (`ci.yml` runs pytest/ruff on fixtures; the corpus
is git-ignored). Its real consumer is the production `lintle clean` / `validate` invocation
inside a data pipeline, where the exit code drives downstream automation — the setting most
in need of scale-invariance.

## 2. Decision summary

Overload the existing `--max-quarantined` *value* rather than add a second flag:

- `--max-quarantined 100` — absolute count. Unchanged: fail if `quarantined_count > 100`.
- `--max-quarantined 1%` — rate. Fail if more than 1% of routed records were quarantined.
- Default stays `0` → count `0` → "any quarantine fails" (the existing, tested contract).

The two modes are **mutually exclusive by construction** — a single value is either a count
or a rate, never both. This is the decisive reason to overload the value rather than add a
`--max-quarantined-pct` flag: a second flag would force a defined interaction between two
thresholds *and* a defined meaning when the count default (`0`) coexists with an explicit
rate — an entire class of edge cases a single overloaded value never creates.

Rejected alternatives:

- **Second flag** (`--max-quarantined-pct R` alongside the count) — adds the
  combination/default-interaction complexity above for no expressive gain; a single gate
  only ever needs one threshold.
- **Replace the count with a rate** — a percentage cannot express "fail on *any* quarantine"
  at scale (1 bad record in 10⁸ rounds to ~0%), and dropping the integer form would break the
  issue-#13 contract.
- **Per-rule `--fail-on RULE-ID=N`** — already rejected under issue #13 (promotes `RuleID`
  strings to a forever-stable CI contract); unchanged here.

## 3. Behavior contract (normative)

**Denominator.** "Routed records" = `clean_count + quarantined_count`. `report.py:146`
guarantees `paired_records + orphan_entries == clean_count + quarantined_count`, and orphan
lines flow into `quarantined_count`, so this denominator counts every entry the pipeline
routed to an outcome — unambiguous, no double-counting.

**Comparison (rate mode).** With corpus-wide totals `q = Σ quarantined_count` and
`r = Σ (clean_count + quarantined_count)`, and threshold `p` (a percentage, `0 ≤ p ≤ 100`),
the run fails when:

```
100 * q  >  p * r
```

The cross-multiplied form is used instead of computing `100*q/r` and comparing, for two
reasons: it needs no zero-guard (an empty corpus has `r = 0`, `q = 0`, giving `0 > 0` →
pass), and it avoids divide-then-compare float drift at the boundary (an exact 1% does not
spuriously trip a `1%` threshold). Comparison is **strictly greater**, matching count mode:
exactly `p%` passes.

**Count mode.** Unchanged: fail when `q > N`.

**Edge values:**

- `0%` ≡ `0` — both mean "any quarantine fails" (`100*q > 0` ⟺ `q > 0`).
- `100%` — effectively never fails (only `q > r` could trip it, which is impossible since
  `q ≤ r`).
- Empty corpus — passes in both modes.

**Scope.** Applies to both `validate` and `clean`, identically to count mode today. No change
to `pipeline.py`, `repair.py`, `tle.py`, the JSON/JSONL reports, or the human `report.md`.

## 4. Parsing & validation

A single value is parsed by a pure, module-level helper in `cli.py` (testable like the
existing `discover_paths`):

```
parse_quarantine_threshold(raw: str) -> tuple[str, int | float]
```

Grammar:

- Trailing `%` → rate mode. Strip `%`, parse the rest as `float`. Valid iff `0 ≤ p ≤ 100`.
- Otherwise → count mode. Parse as `int`. Valid iff `N ≥ 0`. A non-integer count (e.g. `1.5`
  with no `%`) is invalid — counts are whole records.

Returns `("count", int)` or `("pct", float)`. On any malformed or out-of-range input it
raises `ValueError(message)`.

Validation stays in `main()` returning exit code **2** with a stderr message — consistent
with the adjacent `--jobs` check (`cli.py:506`) and the existing `--max-quarantined`
negative-value check (`cli.py:510`), and required to keep `test_max_quarantined_rejects_negative_value`,
which asserts `main()` *returns* `2` (not that argparse raises `SystemExit`). The
negative-count message preserves the exact existing substring `--max-quarantined must be >= 0`.
New messages cover: non-numeric (`abc`, `1.2.3%`), bare `%`, non-integer count, and a
percentage outside `0–100`.

The argparse argument changes from `type=int, default=0` to a string value with
`default="0"` and `metavar="N[%]"`; `args.max_quarantined` is parsed once in `main()` into
`(mode, threshold)` immediately after the `--jobs` check, before any file processing.

## 5. Backward compatibility

Every existing invocation behaves identically. `--max-quarantined 100`, `--max-quarantined 0`,
and the unset default all parse to count mode with the same integers as today; the count-mode
exit branch is the current expression unchanged. Of the existing `TestMaxQuarantinedThreshold`
tests (`tests/test_cli.py:520`), the four count/default tests pass unmodified;
`test_max_quarantined_rejects_negative_value` still asserts `rc == 2` and the same message
substring.

## 6. Out of scope (deferred)

- **Two thresholds at once** (count AND rate simultaneously) — a single gate needs one number;
  deferred unless a concrete need appears.
- **Regression-vs-baseline gating** ("fail if worse than the last run") — that is `lintle diff`
  territory (per-rule deltas between two runs), a larger and different feature; not folded in.
- **Per-rule thresholds** (`--fail-on RULE-ID=N`) — rejected under issue #13; unchanged.
- **JSON report surface** — the chosen threshold/mode is not added to the report envelope; the
  exit code remains the sole gate signal.

## 7. Implementation

- `src/lintle/cli.py`
  - `parse_quarantine_threshold(raw)` — the pure helper above.
  - Argument def (`cli.py:204`): string value, `default="0"`, `metavar="N[%]"`, help text
    describing both forms.
  - `main()`: replace the `< 0` check (`cli.py:510-514`) with a parse-and-validate block that
    stores `(mode, threshold)` and returns `2` on `ValueError`.
  - Exit decision (`cli.py:758-759`): branch on mode — count → `q > threshold`; rate →
    `100*q > threshold*r` with `r = Σ(clean_count + quarantined_count)`.
  - Update the `Exit codes` epilog block (`cli.py:32-36`) and the `main()` docstring
    (`cli.py:471-475`) to describe the `%` form.
- `tests/test_cli.py`
  - Extend `TestMaxQuarantinedThreshold` with rate cases: under threshold passes, over fails,
    exactly-at-boundary passes, applies to `validate`, malformed / out-of-range returns `2`.
  - New `TestParseQuarantineThreshold` unit-tests the helper directly: count/rate parsing,
    boundaries (`0`, `0%`, `100%`), and each error case.
- `CHANGELOG.md` — `Unreleased` note: `--max-quarantined` now accepts a trailing `%` for a
  scale-invariant rate threshold; absolute-count behavior and the default unchanged.

## 8. Validation

`uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` — all must pass.
Manual smoke: a fixture with a known quarantine rate, asserting the exit code flips around its
boundary in both modes.
