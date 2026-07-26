# Modernization: validator-oracle test hardening + humanize the human display

- **Date:** 2026-06-07
- **Status:** Approved in brainstorm — pending implementation plan
- **Scope:** dev/test hardening + a small human-display modernization. Folds into the
  pending **v0.5.0** release (already accumulating the `report.json` / `lintle report`
  changes).

## 1. Motivation & audit outcome

The goal was to modernize/optimize by replacing hand-rolled implementations with tested,
maintained third-party libraries — the way `rich` replaced the custom ANSI/TUI code.

A fresh, adversarial audit across the codebase (five focused auditors + a synthesis pass),
re-checked against [`ARCHITECTURE.md` §7](../../../ARCHITECTURE.md) and the hard correctness
invariants, found the **runtime-dependency frontier essentially exhausted** — confirming the
existing §7 "Considered & deferred" table:

- **Hard-blocked** (would break a core promise): `orjson` / `msgspec` (byte-deterministic
  JSON), `filelock` (the host-aware lock), atomic-write libs (`F_FULLFSYNC` + dir-fsync
  ordering), `joblib` / `tenacity` (exactly-once cancel + checkpoint), runtime
  `sgp4` / `pydantic` (a second validation path).
- **Not worth it** (safe but ~0 benefit): `click` / `typer` (≈0 LOC saved; would change the
  `--help`/error text the e2e tests assert), `tqdm` / `textual` / `blessed` (lose rich's
  responsive two-level rendering — `textual` would *replace* rich, not augment it),
  `structlog` (the tool doesn't log), `polars` / `pandas` / `platformdirs` / `tomli` /
  `diskcache` (no applicable use case).
- The one tempting *new* candidate, `humanize`, was first refuted (its default strings —
  `"512 Bytes"`, `"2 minutes"` — don't match the byte-pinned test assertions) and then
  **revived**: those formatters feed only the human display, so the "byte-pinned" objection
  was test-pickiness, not a hard invariant. Under a relaxed, pre-v1 appetite where the
  *human-readable* output may change, `humanize` becomes a genuine (if small) win.

The decisive distinction: the blocked items are blocked on **semantics, correctness, or
no-benefit — not on output-format pickiness**. So relaxing the human-readable format unlocks
**only** `humanize`, and only on the human display channels. The machine/data outputs stay
byte-stable for *functional* reasons (resume reads back its own checkpoint; `diff` compares
two runs; `report.json` is byte-identical to `--report json`; `cleaned/*.txt` is the product).

The real value therefore splits into (1) **dev/test hardening of the validator oracle** and
(2) a **small human-display modernization** via `humanize`.

## 2. Goals / non-goals

**Goals**
- Close verified coverage gaps in `tle.py` (the correctness *oracle* the whole tool trusts).
- Add property-based fuzzing (`hypothesis`) of the validator + repair invariants.
- Speed the suite (`pytest-xdist`).
- Replace hand-rolled time/size formatting on the human display with `humanize`, fixing a
  latent unit-label bug.

**Non-goals**
- No change to any byte-deterministic / machine output (`report.md`, `report.json`,
  `report.jsonl`, `broken-noradids.ndjson`, the `.broken.txt` sidecar, the `--report json`
  envelope, the `.clean-state.json` checkpoint, `cleaned/*.txt`) — they keep raw numbers and
  exact bytes.
- No runtime `sgp4` / orbital / validation library (one validator definition; `sgp4` stays a
  dev-only oracle).
- No change to streaming / constant-memory, the host-aware lock, or `durable_replace`.

## 3. Part 1 — Harden the validator oracle (dev/test only)

`tle.py` is the single definition of "perfect"; everything downstream trusts it, so its tests
are the highest-value target. Nothing here imports at runtime.

### 3.1 `tle.py` semantic-range boundary coverage
`_check_semantics` has verified-uncovered branches; explicit boundary tests exist today only
for inclination and mean motion. Add explicit boundary-value tests for **every** range it
enforces. The intended bounds (confirm the exact inclusive/exclusive edges against `tle.py`
during implementation):
- inclination — extend existing
- mean motion (> 0) — extend existing
- eccentricity — lower edge accepted, upper edge rejected, just-inside accepted
- RAAN, argument-of-perigee, mean-anomaly — `0` accepted, `360` rejected, just-under accepted
- epoch day-of-year — `0` rejected, just-above accepted, upper edge rejected, just-under accepted
- the numeric-parse-failure path (a non-numeric field) → emits a diagnostic, does not crash

Each test documents the inclusive/exclusive intent so it serves as a regression anchor that
the §3.2 property tests then fuzz around.

### 3.2 `hypothesis` (dev-dep)
Property-based tests; never imported at runtime. Pinned per §7's
`>=current_major,<next_major` rule. Properties:
- **Checksum (mod-10):** for any 68-char body, `compute_checksum` is a single digit `0–9`;
  appending it makes the checksum check pass; corrupting exactly one digit fails it.
- **Semantic ranges:** generate field values inside/outside each range; `_check_semantics`
  accepts iff all fields are in range — the generator concentrates on the boundaries §3.1
  documents.
- **Repair contract (validated transformation):** for generated dirty lines/records,
  `repair`'s outcome is *either* a committed record that `tle.py` now validates *or* a
  quarantine — **never a committed invalid record** — and the reported tier is the max of the
  per-line repair attempts.
- Recorded seeds for reproducibility; slow property tests may be marked so the default suite
  stays fast.
- A `sgp4` cross-validation property (hypothesis-generated valid lines accepted by `tle.py`
  must `twoline2rv` cleanly in the dev oracle) is **deferred** — `sgp4`'s permissiveness limits
  what divergence it can catch; revisit only on an observed divergence.

### 3.3 `repair.py` multi-line combo tests
`repair.py` is already line-covered, but the multi-line orchestration is only implicitly
exercised. Add explicit cases: both lines fail with different rule IDs; a catalog mismatch
that surfaces only after repair; an orphan (line 1 with no line 2); one line repairs while the
other fails; tier / primary / related selection across the pair.

### 3.4 `pytest-xdist` (dev-dep)
Run with `-n auto` (configurable). The suite already uses isolated `tmp_path` fixtures, so the
tests need no changes — but verify isolation holds (no shared global state, fixed ports, or
fixed paths) before enabling by default. Pin per §7 policy. Expected ~13s → ~5s on typical
hardware; a convenience, not load-bearing.

## 4. Part 2 — Humanize the human display (runtime-dep; v0.5 display change)

Add `humanize` (pure-Python, **zero transitive deps**; pin per §7 policy) as a second runtime
dependency, **confined to the human display leaves** (`summary.py`, `cli_progress.py`). Format
by audience:

| Channel | Field | Today | New |
|---|---|---|---|
| Summary panel (human, has room) | elapsed | `_humanize_duration` → `2m 04s` | `humanize.precisedelta(…, minimum_unit="seconds")` → `2 minutes and 4 seconds` |
| Pre-run roster (human, compact) | file / total size | `_format_size` → `3.0 GB` (binary math, decimal label — a bug) | `humanize.naturalsize(…, gnu=True)` → `3.0G` (correct units) |
| Live progress (human, compact, ticking) | elapsed | `_format_elapsed` → `2:04` | **keep custom** — `humanize` has no compact `M:SS` clock, and its verbose form is too long for a rapidly-repainting line |
| Machine (`report.*`) | `elapsed_seconds`, `bytes`, … | raw numbers | **unchanged** — raw numbers, never humanized |

- Delete `_humanize_duration` (summary) and `_format_size` (roster); keep `_format_elapsed`
  (live clock).
- Update the display-assertion tests (`test_summary.py`, the `cli_progress` tests) to the new
  strings — this is the deliberate **v0.5 breaking display change**.
- Add a guard test asserting the structured outputs (`report.md` / `report.json`) are
  byte-unchanged by this work (they carry raw numbers and never import `humanize`).

## 5. Invariant safety

- **Part 1** is test-only; nothing new imports at runtime; `sgp4` stays a dev oracle (one
  validator definition).
- **Part 2** touches only stderr/stdout human rendering. Verified call sites:
  `_humanize_duration` → summary panel (`summary.py`); `_format_size` → roster
  (`cli_progress.py`); `_format_elapsed` → live progress (`cli_progress.py`). **None** feeds
  `report.*`, the sidecar, the checkpoint, or `cleaned/*`; the machine envelope stores raw
  numbers. `humanize` is confined to the same stderr/stdout surface as `rich`.
- No change to streaming / constant-memory, the host-aware out-dir lock, or `durable_replace`.

## 6. Docs / policy updates (during implementation)

- **`ARCHITECTURE.md` §7:** move `humanize` to **Adopted** (human display only; pure-Python,
  zero transitive deps); note it is confined to the stderr/stdout panel and never structured
  output; record that the audit re-confirmed every other candidate as rejected/deferred for
  the reasons already tabled.
- **`CLAUDE.md` Tech Stack / runtime-deps line:** two runtime deps now — `rich` + `humanize`.
- **Module-dependency notes** (ARCHITECTURE.md / CLAUDE.md): `summary.py` and `cli_progress.py`
  import `humanize`.
- **`CHANGELOG.md` `[Unreleased]`:** Added (`hypothesis` / `pytest-xdist` dev infra; new
  `humanize` runtime dep); Changed (human display durations/sizes now via `humanize` — a
  breaking display format).

## 7. Versioning

Folds into the **pending v0.5.0** (already accumulating the `report.json` / `lintle report` /
aggregate-panel changes and the `validate`-subcommand removal). The humanize display reformat
is a "Changed"/breaking-display entry, consistent with the 0.5.0 minor bump.

## 8. Risks / tradeoffs

- **Second runtime dependency.** lintle's "one runtime dep (`rich`)" identity becomes two.
  Accepted by the maintainer: `humanize` is popular, pure-Python, has zero transitive deps,
  deletes ~19 LOC of gotcha-prone formatting, and fixes the `GB`-vs-`GiB` label bug — and the
  relaxed §7 policy permits it.
- **Display churn.** Existing display-assertion tests change; acceptable as a pre-v1 breaking
  display change.
- **Verbose durations.** `"2 minutes and 4 seconds"` is longer than `"2m 04s"`; accepted for
  the summary panel; the compact live clock stays custom.
- **hypothesis flakiness** is mitigated by recorded seeds and by marking slow property tests.

## 9. Out of scope / deferred (audit verdicts)

- `zstandard` — trigger-gated on a *measured* output-size / transfer bottleneck.
- `mypy` / pyright, dataclass `slots`, `match`-statement rewrites, `towncrier` / `uv version` /
  release script / pre-commit / CI matrix — not worth it at this scale.
- Runtime `sgp4` / orbital libs, `orjson` / `msgspec`, `filelock` / atomic-write libs,
  `joblib` / `tenacity`, `polars` / `pandas` — hard-blocked by the correctness invariants.
