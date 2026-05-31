# Runtime dependency policy — Design

- **Date:** 2026-05-28
- **Status:** Partially superseded — the **four-MUST gate was relaxed on 2026-05-31** (rev 3).
  The current, authoritative rule lives in spec §3.1; the body below is retained as the
  historical record of the rev-2 decision and its debate.
- **Revision:** rev 3 (2026-05-31) — the policy is **relaxed**: the four MUST bars become
  *favourable signals* (popular · maintained · reduces-our-burden · sensible shape) and the
  "aim is a veto, never a waiver" clause is retired. The **only** remaining vetoes are the hard
  correctness invariants (one validator #4, constant memory #3, `sgp4`-never-at-runtime,
  byte-deterministic unstyled structured/stdout output #1/#2, atomic durability + host-aware
  lock, validated transformation). §2.1's quoted MUST policy below is **superseded** by spec
  §3.1; §2.2/§2.3/§5–§7 remain accurate history. A relaxed-bar audit re-evaluated every library
  (adding `filelock`, atomic-write, `tabulate`, file-hashing rows to the canonical table) and
  adopted none — so the runtime stays `rich`-only. · rev 2 (2026-05-28) — revised per a blinded
  four-model debate (Gemini, Codex, Sonnet, Opus): bars are now MUST conditions, the "aim" is a
  veto-only clause, a fifth **operational-fit** bar + a version-pinning clause were added, the
  policy now has a single canonical source, `rich` is downgraded to a candidate, and the
  considered/deferred table gains the TLE-library, config, and caching rows. · rev 1: initial.
- **Topic:** Replace `lintle`'s flat "pure standard library at runtime" prohibition with a
  calibrated, goal-led dependency policy; record the considered-and-deferred decisions
  canonically so they stop being relitigated; and reconcile with the issue-53 progress-UI
  spec, which is `rich`'s pending trigger. **No code change** — `dependencies = []` is
  unchanged by this work.

## 1. Problem

The runtime-purity rule is stated as an absolute in four places —
`CLAUDE.md` § Tech Stack (two phrasings), `CONTRIBUTING.md:28`, and the authoritative
spec at § 3 (lines 183–188) and § 4 (line 192) — and "zero runtime dependencies" has
since become part of the README pitch.

We are now open to dependencies that genuinely save us from reinventing the wheel. The
absolute rule no longer matches that intent, but there is no *written, enforceable bar*
for when a dependency is justified — so every candidate gets relitigated from first
principles. This spec supplies that bar and a canonical record of what was already weighed.

Revision 1 was stress-tested by a blinded four-model debate; § 7 records which findings
were folded in. The headline finding: rev 1's bars were advisory, not enforceable. Rev 2
fixes that.

## 2. Decision

### 2.1 The policy — a goal-led test with four MUST bars

> **⚠️ SUPERSEDED (2026-05-31, rev 3).** The four-MUST framing quoted in this subsection was
> relaxed: the four bars are now *favourable signals*, not necessary conditions, and the
> "aim is a veto, never a waiver" clause is retired. The only vetoes are now the hard
> correctness invariants. **The current authoritative rule is spec §3.1.** The text below is
> kept verbatim as the historical rev-2 record.

The aim is the headline, not the proxy. Line-count saved is the loudest *signal* that a
dependency serves the aim, but it is only a net win when the thing taken on is itself
small, trustworthy, and well-behaved — which is what the other bars confirm.

> **Runtime dependencies.** The aim is a stable, maintainable, easy-to-understand app.
> A third-party runtime dependency may be added only when it *advances* that aim and
> **clears all four bars below. Each bar is a necessary condition (MUST).** The aim is a
> **veto, never a waiver**: it can reject a dependency that clears every bar (if taking it
> on would still hurt clarity or auditability), but it can **never admit one that fails a
> bar**. A genuine exception requires an explicit row in the considered/deferred table
> (§ 3 / authoritative spec § 3) naming which bar fails and why it is accepted.
>
> 1. **Earns its weight.** It replaces real code we would otherwise write and maintain —
>    rule of thumb ≥ ~100 lines, *or* a known gotcha-prone domain (terminal control,
>    parsing, compression). A `left-pad`-style one-liner never qualifies; an `axios`-class
>    domain does. (Lines saved is inherently approximate; this bar stays a judgment call.)
> 2. **Mature & widely deployed.** Used by major CLIs, active upstream, healthy release
>    history.
> 3. **Small transitive surface.** Count its *direct* runtime dependencies: ≤ 3, each
>    itself well-known. A single API call that drags a large tree fails here — the line win
>    does not pay for the surface.
> 4. **Operational fit.** Its packaging and runtime behavior must not threaten our
>    invariants: ships as pure-Python or widely-prebuilt wheels (no surprise native
>    toolchain at install); bounded, streaming-friendly memory (Critical Rule #3);
>    deterministic, locale-independent output (Critical Rules #1/#2); no heavy import-time
>    side effects; an acceptable license; a clean `pip-audit` / CVE history.
>
> **Recording requirement (not a bar).** Adoption lands with a `CHANGELOG.md` entry beside
> the `pyproject.toml` edit, so future contributors see why it was accepted.
>
> **Maintenance.** Pin to exclude the next major (e.g. `rich>=13,<14`); `uv.lock` is the
> lockfile of record; **re-run all four bars on any major-version bump** — the bars gate
> the relationship over time, not just the moment of adoption.
>
> **The Critical Rules are hard gates on a dependency's *behavior*, not just our own code.**
> A dependency that would create a second validation path (#4), load a file whole (#3), or
> make output nondeterministic (#1/#2) is out regardless of which module imports it.
>
> Dev-only dependencies (test oracles, tooling) are exempt from the bars, but a nontrivial
> dev dependency still records its purpose, scope, and any reproducibility impact.

### 2.2 No layering rule (considered and rejected — confirmed by the debate)

An earlier draft added a fifth rule: third-party runtime code permitted only in the
presentation/CLI surface, never in the validation/repair/streaming core. It is **rejected**
because it gates on *location* as a proxy for what is really a *value* judgment — and that
judgment is already made, better, by the bars. "Is this worth a dependency?" does not
change based on which file imports it.

The debate pushed back (3 of 4 models), arguing the core is exposed to risks the Critical
Rules don't enumerate. That concern is real but is **not** answered by a location rule; it
is answered by **bar 4 (operational fit)**, which makes the threatening *behaviors*
(native code, unbounded memory, nondeterminism, locale sensitivity) explicit and applies
everywhere. No separate core rule is needed, for two reasons:

- The core is simple, auditable stdlib code, so **bar 1 (earns its weight) rejects most
  core dependencies on its own** — there is little there worth an audit-surface tax.
- The Critical Rules already gate the catastrophic cases: a parser in `tle.py` is a second
  validator (#4); a whole-file lib in `pipeline.py` breaks constant memory (#3).

We gate on a dependency's value and behavior, never its location. (See § 7.)

### 2.3 `rich` is a candidate, not approved

`rich` plausibly clears the bars — but **only against the issue-53 feature** (a multi-file
per-worker progress block + pre-run roster), where it would replace ~150 lines of
hand-rolled ANSI plus the multi-line block we would otherwise write. Adopting it merely to
reach *parity* with today's single-line display fails bar 1: the load-bearing parts of the
current display (the `multiprocessing.Queue` drain, the records/`rec/s` counter, the
non-TTY fallback) are not things `rich` does for you.

To keep the first exception **evidence-driven rather than outcome-driven**, `rich` is a
**candidate, pending issue-53 implementation evidence**. Approval happens *in the PR that
removes the hand-rolled code and demonstrates the behavior* — not here. `dependencies = []`
holds until then. See § 3 for its row and § 5 for the issue-53 reconciliation.

## 3. Considered & deferred dependencies (canonical table)

This table is the canonical record (it lands in authoritative spec § 3 — see § 4). It
merges the dependency survey, the progress-UI alternatives from issue-53 § 9, and the
additions the debate surfaced.

### Runtime

| Tool | Disposition | Reason |
|---|---|---|
| **TLE / orbital libs** (`sgp4`, `skyfield`, `tletools`, `astropy`) | **Reject (runtime)** | The most on-point "wheel" for this domain — and a hard no at runtime: using one as a parser/validator is a second validation path (**Critical Rule #4**). This is *why* `sgp4` is a test-oracle dev dep. Dev-only test-oracle use is fine. |
| `click` / `typer` | **Reject** | `argparse` covers the small CLI surface; swapping is churn for ~0 net lines. Fails bar 1. |
| `pydantic` | **Reject** | We own our formats (`report.jsonl`, resume checkpoint); dataclasses + stdlib `json` cover them (fails bar 1); `pydantic-core` is a native ext (fails bar 4) and strains bar 3. As TLE validation it would be a second validator (Rule #4). |
| `structlog` / `loguru` | **Reject** | `lintle` emits a report and a progress UI, not logs. Nothing to save. |
| `orjson` / `ujson` | **Reject** | Replaces ~zero custom code (`json.dumps` either way); JSON is not the bottleneck; native ext (bar 4). Fails bar 1. |
| `polars` / `pandas` | **Reject** | The `diff` is per-rule counters; a `dict[str, int]` does it (fails bar 1); huge native tree (fails bars 3 + 4). The "one call, large tree" trap. |
| config parsing (`tomli`, etc.) | **Reject** | `tomllib` is stdlib on 3.11+, `configparser`/`json`/`argparse` cover the rest. Stdlib already wins. |
| caching (`diskcache`, `cachetools`) | **Reject** | No caching need in a streaming one-pass tool; a `dict` suffices where bounded state is required. Fails bar 1. |
| `tqdm` | **Reject** | Cannot render a dynamic block of N concurrent bars whose set changes over time (`position=` assumes a fixed count); we'd rebuild the block ourselves, losing the reason to take the dep. |
| `textual` | **Reject** | A full TUI framework (event loop, screens, widgets); we want a progress block, not an app. |
| `blessed` / `prompt_toolkit` | **Reject** | Lower-level terminal control; still ~50 lines of layout glue for the multi-bar case. `rich` is the better fit. |
| `rich` | **Candidate (pending issue-53 evidence)** | Plausibly clears all four bars for the issue-53 feature (replaces ~150 lines of gotcha-prone ANSI; mature — `pip`/`uv`/`pdm`/`typer`; pure-Python; transitive deps `markdown-it-py` + `pygments`). **Not approved here; `dependencies = []` holds.** Approval is evidence-driven, in the adopting PR. A parity-only swap would fail bar 1. |
| `zstandard` | **Defer (trigger-gated)** | Would pay off only if output size or transfer time becomes a measured bottleneck (stream-compressing quarantine sidecars or shards). **Trigger:** file a ticket *with the measurement* (sidecar/shard bytes or transfer time dominating a run); until then stdlib `gzip`. Note: native ext — would have to clear bar 4. |

### Dev-only (exempt from the bars — record purpose/scope; may land any time)

| Tool | Disposition | Reason |
|---|---|---|
| `hypothesis` | **Adopt anytime** | Property-based tests for `tle.py` / `repair.py` edge cases. The strongest candidate. |
| `pytest-xdist` | **Adopt anytime** | Parallel test runs. |

## 4. Where each piece lands (implementation surface — docs only)

The policy has **one canonical home** to avoid the "same fact in four places" drift this
spec itself diagnosed. On any policy change, edit the canonical source first.

| File | Edit |
|---|---|
| `docs/…/2026-05-21-…-design.md` § 3 | **Canonical source.** Add a subsection carrying the full § 2.1 policy + the § 3 table. Soften the "no runtime library" / "pure-stdlib" lines (183–188) and § 4 (192) to point here ("a lean runtime under this subsection's policy; currently zero dependencies"). Add a dated revision-log entry. |
| `CLAUDE.md` § Tech Stack | Replace the two "pure standard library" phrasings with: a one-line pointer to the canonical source, the four bar *names* (Earns its weight · Mature · Small surface · Operational fit), and current status ("runtime deps: none"). **Not the prose.** Keep the `sgp4`/`pytest` dev-only note. **The four Critical Rules are untouched.** |
| `CONTRIBUTING.md` § Managing dependencies | Replace line 28 with a one-line pointer to the canonical source + current status. **Not the prose.** Keep line 29 (`sgp4` dev-only). |
| `pyproject.toml` | **Unchanged** — `dependencies = []`. Listed to make "policy now, `rich` later" unambiguous. |
| `README.md` | **No change now.** Its "zero runtime dependencies" claim stays true while `dependencies = []`. Revisit when `rich` lands via issue-53. |
| `docs/…/2026-05-28-issue-53-…-design.md` § 4 | Reconcile — see § 5. |

**Atomicity.** All edits land in **one commit** (incl. the issue-53 reconciliation), or the
repo briefly holds contradictory guidance. The rev-1 design doc is already committed
(`bda1af0`); this rev-2 revision and the implementation are the remaining commits.

**Workflow.** These are `docs:` edits. By convention `docs:` commits go direct to `develop`;
a foundational *policy* change is weightier than a typo, so optionally raise it as a PR for
the CI run and a reviewable artifact. (Solo repo → low reviewer value; author's discretion.)

## 5. Reconciliation with the issue-53 spec

The issue-53 spec *introduces* the policy (its § 4.1) and *bundles* its edits with the
`rich` code (§ 4.2). After this work the policy exists canonically. Update issue-53 § 4 to:

- **Reference** the canonical policy rather than re-derive it.
- Reflect that `rich` is a **candidate**: its adopting PR is where `rich` clears the bars
  *with evidence* (code removed, behavior shown), flips `dependencies` to `["rich>=13,<14"]`,
  and moves the table row from "candidate" to "adopted (vN)".
- Shrink § 4.2's PR scope to: `pyproject.toml`, the `CHANGELOG.md` entry, and the
  canonical-source status line. The policy *text* is already in place.

Net: issue-53 stays the feature spec; it no longer owns the policy.

## 6. Out of scope

- **Any runtime code change.** No `cli.py` / `pipeline.py` edits — those belong to
  issue-53. `dependencies = []` is unchanged.
- **Approving `rich`.** It is a candidate; approval is in the issue-53 adopting PR.
- **Dev-dependency additions** (`hypothesis`, `pytest-xdist`) — exempt from the bars; land
  any time as separate `chore:`/`test:` changes.
- **README pitch rewording.** The zero-deps claim stays true today; revisited when `rich`
  lands.

## 7. Debate findings incorporated (rev 1 → rev 2)

A blinded four-model debate (synthesis at
`~/.claude-octopus/debates/local/001-runtime-dep-policy-problems/`) reviewed rev 1.

**Adopted:**
- Bars are now **MUST**; the "aim" is **veto-only**, with a documented exception path
  (unanimous #1 finding: rev 1's bars were advisory).
- New **operational-fit bar** + version-pinning/`uv.lock`/re-run-on-major clause (unanimous:
  no supply-chain/versioning/operational axis).
- **Single canonical source**; CLAUDE.md/CONTRIBUTING become pointers (unanimous: rev 1
  reproduced the drift it diagnosed).
- `rich` → **candidate pending evidence** (3 models: pre-approval looked rubber-stamped).
- **Table additions**: TLE/orbital libs (the key domain omission), config, caching;
  `zstandard` given a concrete trigger.
- **Atomic implementation commit**; dev-only wording tightened.

**Decided against the debate (with reason):**
- **Layering rule stays dropped** (debate leaned toward reinstating). The concern — core
  exposure — is met by bar 4 + the Critical-Rules-gate-behavior clause, not by location
  gating, which conflates a dependency's *quality* (location-independent) with its *blast
  radius* (handled by the Critical Rules). See § 2.2.
- **"Adopt `rich` now for parity to prove the policy"** (Gemini) — rejected: we have
  evidence parity fails bar 1, so adopting for parity would itself violate the policy.
- **"Require a PR for the docs change"** (Gemini/Codex) — noted as optional in § 4 rather
  than mandated, since it conflicts with the project's deliberate `docs:`-direct convention
  in a solo repo.
