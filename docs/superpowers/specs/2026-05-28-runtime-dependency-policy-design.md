# Runtime dependency policy — Design

- **Date:** 2026-05-28
- **Status:** Draft
- **Revision:** initial.
- **Topic:** Replace `lintle`'s flat "pure standard library at runtime" prohibition with a
  calibrated, goal-led four-bar dependency policy; record the considered-and-deferred
  dependency decisions canonically so they stop being relitigated; and reconcile this with
  the issue-53 progress-UI spec, which is `rich`'s pending trigger. **No code change** —
  `dependencies = []` is unchanged by this work.

## 1. Problem

The runtime-purity rule is stated as an absolute in four places —
`CLAUDE.md` § Tech Stack (two phrasings), `CONTRIBUTING.md:28`, and the authoritative
spec at § 3 (lines 183–188) and § 4 (line 192) — and "zero runtime dependencies" has
since become part of the README pitch (the redundancy-paradox / value-prop sections).

We are now open to dependencies that genuinely save us from reinventing the wheel. The
absolute rule no longer matches that intent, but there is no *written bar* for when a
dependency is justified — so every candidate (`click`, `rich`, `polars`, …) gets
relitigated from first principles. The cost of that is the motivation here: a durable,
written policy plus a canonical record of what was already considered and why.

The issue-53 progress-UI spec (`2026-05-28-issue-53-progress-ui-design.md`) already
proposes adopting `rich` and drafts a four-bar test — but it *bundles* the policy edit
with the `rich` code (its § 4.2: "all five edits land in the same PR as the code"). This
design **decouples** the two: codify the policy now (docs only), and let `rich` land with
issue-53 when that feature is actually built.

## 2. Decision

### 2.1 The policy — a goal-led four-bar test

The aim is the headline, not the proxy. Line-count saved is the loudest *signal* that a
dependency serves the aim, but it is only a net win when the thing taken on is itself
small and trustworthy — which is what the other three bars confirm.

> **Runtime dependencies.** The aim is a stable, maintainable, easy-to-understand app.
> Add a third-party runtime dependency when it *advances* that aim — typically when it
> collapses a meaningful amount of custom code, or a gotcha-prone domain (terminal
> control, parsing, compression), into a small, well-trodden API. It should clear all
> four bars:
>
> 1. **Earns its weight** — it replaces real code we would otherwise write and maintain
>    (rule of thumb: ≥ ~100 lines, or a known gotcha-prone domain). A `left-pad`-style
>    one-liner never qualifies; an `axios`-class domain does.
> 2. **Mature & widely deployed** — used by major CLIs, active upstream.
> 3. **Small transitive surface** — ≤ ~3 well-known transitive dependencies. *A single
>    API call that drags a large tree fails here* — the line win does not pay for the
>    stability and audit cost.
> 4. **Justified in `CHANGELOG.md`** alongside the `pyproject.toml` edit, so future
>    contributors can see why it was accepted.
>
> The bars serve the aim; they do not replace it. If "fewer lines" would make the code
> *harder* to trust or audit, the aim wins and the dependency loses — and the four
> Critical Rules bind dependency and hand-rolled code alike. Dev-only dependencies (test
> oracles, tooling) are unconstrained beyond "prefer the ecosystem standard."

### 2.2 No layering rule (considered and rejected)

An earlier draft added a fifth rule: third-party runtime code permitted only in the
presentation/CLI surface, never in the validation/repair/streaming core. It is **rejected**
because it gates on *location* as a proxy for what is really a *value* judgment — and the
value judgment is already made, better, by the four-bar test. "Is this worth a dependency?"
does not change based on which file imports it.

The one legitimate concern the layering rule was reaching for — keeping the validator
auditable — is already covered, and more precisely, by the existing Critical Rules, which
bind dependency and hand-rolled code alike:

- **Critical Rule #3 (constant memory)** forbids anything in `pipeline.py` that loads a
  file whole — a dependency that did so is out on this rule, not on its location.
- **Critical Rule #4 (one validator definition)** forbids a second validation path in
  `tle.py` — which is *why* `sgp4` is a test oracle only. A parsing/validation library in
  the validator is out on this rule, not on its location.

So the policy is the four-bar test, applied everywhere, sitting on top of the four
unchanged Critical Rules. This subsection is recorded so the layering rule is not
reproposed.

### 2.3 `rich` is deferred, not adopted

`rich` clears all four bars — but only against the **issue-53 feature** (a multi-file
per-worker progress block + pre-run roster), where it replaces ~150 lines of hand-rolled
ANSI plus the new multi-line block we would otherwise write. Adopting it merely to reach
*parity* with today's single-line display would fail bar 1: a parity swap is lateral
churn, because the load-bearing parts of the current display (the `multiprocessing.Queue`
drain, the open-ended records/`rec/s` counter, the non-TTY log branch) are not things
`rich` does for you.

Therefore `rich` is **approved-in-principle, deferred**: `dependencies = []` holds, and
`rich` lands with issue-53. See § 3 for its row in the canonical table and § 5 for the
issue-53 reconciliation.

## 3. Considered & deferred dependencies (canonical table)

This table is the canonical record. It lands in the authoritative spec's § 3 (see § 4).
It merges the dependency survey with the progress-UI alternatives already weighed in the
issue-53 spec § 9.

### Runtime

| Tool | Disposition | Reason |
|---|---|---|
| `click` / `typer` | **Reject** | `argparse` covers the small CLI surface; swapping is churn for ~0 net lines. Fails bar 1. |
| `pydantic` | **Reject** | We own our formats (`report.jsonl`, resume checkpoint) end-to-end; dataclasses + stdlib `json` already cover them (fails bar 1), and `pydantic-core`'s compiled weight strains bar 3. Using it *as* TLE validation would be a second validator — out by Critical Rule #4. |
| `structlog` / `loguru` | **Reject** | `lintle` emits a report and a progress UI, not logs. Nothing to save. |
| `orjson` / `ujson` | **Reject** | Replaces ~zero custom code (`json.dumps` either way); JSON is not the bottleneck (pairing + validation is). Fails bar 1. |
| `polars` / `pandas` | **Reject** | The `diff` is per-rule counters; a `dict[str, int]` does it (fails bar 1), and the transitive tree fails bar 3. The "one call, large tree" trap. |
| `tqdm` | **Reject** | Cannot render a dynamic block of N concurrent bars whose set changes over time (`position=` assumes a fixed bar count); we would rebuild the block ourselves, losing the reason to take the dependency. |
| `textual` | **Reject** | A full TUI framework (event loop, screens, widgets); we want a progress block, not an app. |
| `blessed` / `prompt_toolkit` | **Reject** | Lower-level terminal control; still ~50 lines of layout glue for the multi-bar case. `rich` is the better fit. |
| `rich` | **Approved-in-principle, deferred** | Clears all four bars via the gotcha-prone-terminal-control clause: replaces ~150 lines of hand-rolled ANSI plus the multi-file block; mature (used by `pip`, `uv`, `pdm`, `typer`); two well-known transitive deps (`markdown-it-py`, `pygments`). **Not adopted by this work — `dependencies = []` holds.** Trigger: the issue-53 progress-UI feature. A parity-only swap would fail bar 1. |
| `zstandard` | **Defer (needs a ticket)** | Would pay off if we stream-compress quarantine sidecars or shard outputs (30 GB → much less). Stdlib `gzip` suffices until someone measures and complains. |

### Dev-only (unconstrained by the policy — may land any time)

| Tool | Disposition | Reason |
|---|---|---|
| `hypothesis` | **Adopt anytime** | Property-based tests for `tle.py` / `repair.py` edge cases. Dev-only, so no policy gate; the strongest candidate. |
| `pytest-xdist` | **Adopt anytime** | Parallel test runs. Dev-only, no policy implication. |

## 4. Where each piece lands (implementation surface — docs only)

| File | Edit |
|---|---|
| `CLAUDE.md` § Tech Stack | Replace the "standard library only at runtime" bullet and the "The runtime is **pure standard library**." sentence with the operative four-bar policy (§ 2.1, compact form — the rule is enforced here, so it states the bars, not just a pointer) + "currently zero runtime dependencies" + a pointer to this spec for the rationale and the considered/deferred table. Keep the `sgp4`/`pytest` dev-only note. **The four Critical Rules are untouched.** |
| `CONTRIBUTING.md` § Managing dependencies | Replace line 28 ("The **runtime has no third-party dependencies** — `lintle` is pure standard library.") with the § 2.1 policy (short form + pointer to this spec). Keep line 29 (`sgp4` dev-only). |
| `docs/superpowers/specs/2026-05-21-…-design.md` § 3 | Add a subsection — "Runtime dependency policy & considered dependencies" — carrying the § 2.1 policy and the § 3 canonical table. Soften the "no runtime library" / "runtime stays pure-stdlib" lines (183–188) and the § 4 "pure standard library at runtime" line (192) to point at the new subsection ("a lean runtime under the new policy subsection; currently zero dependencies"). Add a dated entry to the header revision log. |
| `pyproject.toml` | **Unchanged** — `dependencies = []`. Listed here to make "policy now, `rich` later" unambiguous. |
| `README.md` | **No change now.** Its "zero runtime dependencies" claim stays true while `dependencies = []`. Revisit when `rich` lands via issue-53. |
| `docs/superpowers/specs/2026-05-28-issue-53-…-design.md` § 4 | Reconcile — see § 5. |

All edits are `docs:` changes committed directly on `develop` (no code, no PR), per the
project's chore/bugfix workflow.

## 5. Reconciliation with the issue-53 spec

The issue-53 spec currently *introduces* the policy (its § 4.1) and *bundles* its edits
with the `rich` code (its § 4.2: "all five edits land in the same PR"). After this work,
the policy already exists in `CLAUDE.md` / `CONTRIBUTING.md` / the authoritative spec.
The issue-53 spec is updated to:

- **Reference** the codified policy in its § 4 rather than re-derive the four-bar test.
- Make its "Current runtime deps: `rich>=13`" line **conditional** — "this feature, when
  built, adds `rich>=13`, the first dependency to clear the policy."
- Shrink its § 4.2 PR scope from five edits to two: `pyproject.toml`
  (`[]` → `["rich>=13"]`) and the `CHANGELOG.md` entry that flips the policy's
  "current dependencies" line. The policy *text* is already in place.

Net: issue-53 stays the feature spec; it no longer owns the policy.

## 6. Out of scope

- **Any runtime code change.** No `cli.py` / `pipeline.py` edits — those belong to
  issue-53. `dependencies = []` is unchanged.
- **Adopting `rich` now.** It is deferred to issue-53 (§ 2.3).
- **Dev-dependency additions** (`hypothesis`, `pytest-xdist`). They are unconstrained by
  the policy and can land any time as separate `chore:`/`test:` changes.
- **README pitch rewording.** The zero-deps claim stays true today; it is revisited when
  `rich` lands.
